#!/usr/bin/env bash
# Prepare a WSL2 (or native Linux) host to talk to a Stream Deck over raw HID.
#
# Prepares the Linux side first -- the udev rule must exist before the device
# appears, or the node lands root-only -- then, under WSL, triggers the Windows
# setup elevated and waits for the device to show up.
#
# Usage:
#   ./setup-wsl.sh                     prepare, then attach from Windows
#   ./setup-wsl.sh --no-windows        prepare only; skip the Windows handoff
#   ./setup-wsl.sh --attach-only       skip preparation; only attach + poll
#   ./setup-wsl.sh --verify-only       report current state and exit
#   ./setup-wsl.sh -- -BusId 1-4       forward arguments to setup-windows.ps1
#
# Idempotent: safe to re-run.

set -euo pipefail

ELGATO_VID="0fd9"
UDEV_RULE="/etc/udev/rules.d/70-streamdeck.rules"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATTACH_TIMEOUT="${ATTACH_TIMEOUT:-120}"
WINDOWS_ARGS=()

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m fail\033[0m %s\n' "$*" >&2; exit 1; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }

is_wsl() { grep -qi microsoft /proc/version 2>/dev/null; }

# ---------------------------------------------------------------- kernel check
# Only meaningful on WSL: a kernel without usbip/hidraw can never see the
# device no matter what usbipd does on the Windows side.
check_kernel() {
    is_wsl || return 0
    info "Checking kernel support ($(uname -r))"

    local cfg
    if [[ -r /proc/config.gz ]]; then
        cfg=$(zcat /proc/config.gz)
    elif [[ -r "/boot/config-$(uname -r)" ]]; then
        cfg=$(cat "/boot/config-$(uname -r)")
    else
        warn "no kernel config found; skipping check"
        return 0
    fi

    grep -q '^CONFIG_HIDRAW=y' <<<"$cfg" \
        || fail "kernel lacks CONFIG_HIDRAW -- a custom WSL kernel is required"
    grep -qE '^CONFIG_USB_HID=[ym]' <<<"$cfg" \
        || fail "kernel lacks CONFIG_USB_HID -- a custom WSL kernel is required"
    grep -qE '^CONFIG_USBIP_VHCI_HCD=[ym]' <<<"$cfg" \
        || fail "kernel lacks CONFIG_USBIP_VHCI_HCD -- USB passthrough unavailable"

    ok "hidraw, usb-hid and usbip vhci all supported"
}

# ------------------------------------------------------------------- packages
# python-elgato-streamdeck ships only Python bindings; the native hidapi
# library is a separate system package. libusb backend is preferred -- it does
# not depend on hidraw, so it still works if usbip presents the device oddly.
install_packages() {
    info "Installing hidapi and usb tooling"
    if ! command -v apt-get >/dev/null; then
        warn "non-apt distro; install hidapi (libusb backend) + usbutils yourself"
        return 0
    fi
    sudo apt-get update -qq
    sudo apt-get install -y libhidapi-libusb0 usbutils
    ok "libhidapi-libusb0, usbutils installed"
}

# ----------------------------------------------------------------------- udev
# NOTE: most Stream Deck guides use TAG+="uaccess". That relies on logind seat
# assignment, which WSL does not perform -- the rule parses fine and silently
# grants nothing. GROUP/MODE works everywhere.
install_udev_rule() {
    info "Installing udev rule for Elgato ($ELGATO_VID)"

    local content
    content=$(cat <<EOF
# Elgato Stream Deck -- readable/writable by the plugdev group.
SUBSYSTEM=="usb", ATTRS{idVendor}=="$ELGATO_VID", MODE="0660", GROUP="plugdev"
KERNEL=="hidraw*", ATTRS{idVendor}=="$ELGATO_VID", MODE="0660", GROUP="plugdev"
EOF
)

    if [[ -f "$UDEV_RULE" ]] && [[ "$(cat "$UDEV_RULE")" == "$content" ]]; then
        ok "rule already current"
    else
        printf '%s\n' "$content" | sudo tee "$UDEV_RULE" >/dev/null
        ok "wrote $UDEV_RULE"
    fi

    # udev only runs under systemd; without it the rule is inert and the device
    # node keeps whatever ownership the kernel gave it (root:root).
    if [[ "$(ps -p 1 -o comm=)" != "systemd" ]]; then
        warn "PID 1 is not systemd -- udev will not run and this rule stays inert."
        warn "Add 'systemd=true' under [boot] in /etc/wsl.conf, then 'wsl --shutdown'."
        return 0
    fi

    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=usb --subsystem-match=hidraw
    ok "udev rules reloaded"
}

# ---------------------------------------------------------------------- group
ensure_plugdev() {
    info "Checking plugdev membership"
    if id -nG | tr ' ' '\n' | grep -qx plugdev; then
        ok "$USER is already in plugdev"
        return 0
    fi
    getent group plugdev >/dev/null || sudo groupadd plugdev
    sudo usermod -aG plugdev "$USER"
    warn "added $USER to plugdev -- run 'wsl --shutdown' from Windows for it to take effect"
}

# --------------------------------------------------------------------- verify
# ------------------------------------------------------------ windows handoff
# Hands off to attach_device.py, which raises the UAC prompt, detaches, and
# polls. Python rather than more bash because the call crosses three quoting
# layers -- see that script's docstring.
attach_from_windows() {
    is_wsl || return 0

    local helper="$SCRIPT_DIR/attach_device.py"
    if [[ ! -f "$helper" ]]; then
        warn "missing $helper; run the Windows script by hand"
        return 1
    fi
    if ! command -v python3 >/dev/null; then
        warn "python3 not found; run scripts/setup-windows.ps1 on Windows by hand"
        return 1
    fi

    python3 "$helper" --timeout "$ATTACH_TIMEOUT" "${WINDOWS_ARGS[@]}"
}

verify() {
    info "Looking for an attached Stream Deck"

    if ! lsusb 2>/dev/null | grep -qi "$ELGATO_VID:"; then
        echo
        echo "  No Elgato device visible yet."
        echo
        echo "  Next: run setup-windows.ps1 from an Administrator PowerShell,"
        echo "  or re-run this script without --verify-only to trigger it."
        return 0
    fi

    lsusb | grep -i "$ELGATO_VID:" | sed 's/^/    /'

    # Match on HID_ID from each hidraw's uevent, NOT udevadm's ID_VENDOR_ID --
    # hidraw nodes do not carry ID_VENDOR_ID at all, so that check silently
    # matched nothing. HID_ID looks like 0003:00000FD9:000000A5.
    local found=0
    local vid_upper; vid_upper=$(printf '%s' "$ELGATO_VID" | tr '[:lower:]' '[:upper:]')
    for sysdev in /sys/class/hidraw/hidraw*; do
        [[ -e "$sysdev/device/uevent" ]] || continue
        grep -qi "^HID_ID=.*:0000${vid_upper}:" "$sysdev/device/uevent" || continue

        found=1
        local node="/dev/$(basename "$sysdev")"
        local perms; perms=$(stat -c '%U:%G %a' "$node" 2>/dev/null || echo "missing")
        if [[ "$perms" == *plugdev* ]]; then
            ok "$node -> $perms"
        else
            warn "$node -> $perms (expected group plugdev; rule did not fire)"
        fi
    done

    # The libusb hidapi backend opens the USB node directly and never touches
    # hidraw, so this one governs whether the daemon can talk to the device.
    for id_vendor in /sys/bus/usb/devices/*/idVendor; do
        [[ -r "$id_vendor" ]] || continue
        [[ "$(cat "$id_vendor")" == "$ELGATO_VID" ]] || continue

        local dir; dir=$(dirname "$id_vendor")
        local busnum devnum usbnode
        busnum=$(cat "$dir/busnum" 2>/dev/null) || continue
        devnum=$(cat "$dir/devnum" 2>/dev/null) || continue
        usbnode=$(printf '/dev/bus/usb/%03d/%03d' "$busnum" "$devnum")
        [[ -e "$usbnode" ]] || continue

        found=1
        local uperms; uperms=$(stat -c '%U:%G %a' "$usbnode")
        if [[ "$uperms" == *plugdev* ]]; then
            ok "$usbnode -> $uperms (libusb backend)"
        else
            warn "$usbnode -> $uperms (expected group plugdev)"
        fi
    done

    [[ $found -eq 1 ]] || warn "device present but no accessible node found"
}

main() {
    local do_prepare=1 do_windows=1 verify_only=0

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --verify-only) verify_only=1 ;;
            --no-windows)  do_windows=0 ;;
            --attach-only) do_prepare=0 ;;
            --timeout)     shift; ATTACH_TIMEOUT="${1:?--timeout needs a value}" ;;
            --)            shift; WINDOWS_ARGS=("$@"); break ;;
            -h|--help)     sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
            *)             fail "unknown option: $1 (try --help)" ;;
        esac
        shift
    done

    if [[ $verify_only -eq 1 ]]; then
        verify
        exit 0
    fi

    if [[ $do_prepare -eq 1 ]]; then
        check_kernel
        install_packages
        ensure_plugdev
        install_udev_rule
    fi

    if [[ $do_windows -eq 1 ]] && is_wsl; then
        echo
        # attach_device.py prints its own progress and exits non-zero on
        # timeout or a declined UAC prompt; either way we still report the
        # Linux side as prepared, since that part did succeed.
        if ! attach_from_windows; then
            echo
            warn "the Linux side is ready, but the device is not attached yet"
            warn "re-run once resolved:  $0 --attach-only"
            exit 1
        fi
    else
        verify
    fi

    echo
    ok "WSL side ready."
}

main "$@"
