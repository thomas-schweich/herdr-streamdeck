#!/usr/bin/env bash
# Prepare a macOS host to talk to a Stream Deck over raw HID.
#
# Far simpler than WSL: the device is native, so there is no passthrough, no
# udev, and no scheduled task. Two things actually matter -- the native hidapi
# library, and making sure Elgato's own software is not holding the device.
#
# Idempotent: safe to re-run.

set -euo pipefail

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m fail\033[0m %s\n' "$*" >&2; exit 1; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }

[[ "$(uname -s)" == "Darwin" ]] || fail "this script is for macOS; use setup-wsl.sh on Linux/WSL"

# ------------------------------------------------------------------- packages
install_hidapi() {
    info "Installing hidapi"
    command -v brew >/dev/null || fail "Homebrew not found -- see https://brew.sh"

    if brew list --formula hidapi >/dev/null 2>&1; then
        ok "hidapi already installed"
    else
        brew install hidapi
        ok "hidapi installed"
    fi

    # Apple Silicon puts brew under /opt/homebrew, Intel under /usr/local. The
    # Python ctypes loader searches neither by default in some environments.
    local prefix; prefix="$(brew --prefix)"
    if [[ ! -f "$prefix/lib/libhidapi.dylib" ]]; then
        warn "libhidapi.dylib not found under $prefix/lib -- check 'brew --prefix hidapi'"
    else
        ok "libhidapi.dylib at $prefix/lib"
    fi
}

# ------------------------------------------------------------- device conflict
# The single most common macOS failure: Elgato's app is running and holds an
# exclusive claim on the HID device, so python-elgato-streamdeck sees it but
# cannot open it. This does not exist as a problem on Linux (no Linux build).
check_elgato_software() {
    info "Checking for conflicting Elgato software"

    if pgrep -qi "Stream Deck" 2>/dev/null; then
        warn "Elgato Stream Deck software is RUNNING and will hold the device."
        warn "Quit it (and remove it from System Settings > General > Login Items)."
        warn "  osascript -e 'quit app \"Stream Deck\"'"
        return 0
    fi

    if [[ -d "/Applications/Stream Deck.app" ]]; then
        warn "Elgato software is installed but not running -- make sure it stays"
        warn "out of Login Items, or it will grab the device after a reboot."
        return 0
    fi

    ok "no conflicting Elgato software"
}

# --------------------------------------------------------------------- verify
verify() {
    info "Looking for an attached Stream Deck"

    # ioreg is the macOS equivalent of lsusb for this purpose.
    if ioreg -p IOUSB -l 2>/dev/null | grep -qi 'elgato'; then
        ioreg -p IOUSB -l 2>/dev/null \
            | grep -i '"USB Product Name"' \
            | grep -i 'stream' \
            | sed 's/^ */    /' || true
        ok "Elgato device present on the USB bus"
    else
        warn "no Elgato device found -- plug the Stream Deck in and re-run"
    fi
}

main() {
    if [[ "${1:-}" == "--verify-only" ]]; then
        verify
        check_elgato_software
        exit 0
    fi
    install_hidapi
    check_elgato_software
    verify
    echo
    ok "macOS side ready."
}

main "$@"
