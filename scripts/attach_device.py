#!/usr/bin/env python3
"""Launch the Windows setup script elevated from WSL, then wait for the device.

Invoked by setup-wsl.sh; also usable directly.

Why Python rather than more bash: the call crosses three quoting layers --
bash, ``powershell.exe``, and ``Start-Process -ArgumentList`` feeding a second
elevated shell. Building the PowerShell snippet here and passing it as
``-EncodedCommand`` (UTF-16LE base64) reduces all of that to one opaque argv
token, so no path with a space or quote can break the invocation.

Deliberate design points:

* ``Start-Process -Verb RunAs`` raises the UAC prompt. It returns as soon as
  the elevated process starts, so this detaches by design -- we poll rather
  than wait on it.
* ``-Verb RunAs`` cannot be combined with ``-RedirectStandardOutput``; PowerShell
  rejects the combination. The elevated window's output would therefore be
  invisible, so setup-windows.ps1 is given ``-LogPath`` and transcribes to a
  file this script reads back.
* The script lives on a ``\\\\wsl.localhost\\`` UNC path, which Windows treats as
  an untrusted zone, so ``-ExecutionPolicy Bypass`` is required.

Standard library only, so it runs before any project dependencies exist.
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import time
from pathlib import Path

ELGATO_VID = "0fd9"

SYS_USB = Path("/sys/bus/usb/devices")
SYS_HIDRAW = Path("/sys/class/hidraw")

# Exit codes, so setup-wsl.sh can distinguish outcomes.
EXIT_OK = 0
EXIT_NOT_WSL = 2
EXIT_LAUNCH_FAILED = 3
EXIT_TIMEOUT = 4
EXIT_ELEVATION_DECLINED = 5


class Style:
    STEP = "\033[1;34m==>\033[0m"
    OK = "\033[1;32m  ok\033[0m"
    WARN = "\033[1;33m warn\033[0m"
    FAIL = "\033[1;31m fail\033[0m"


def step(message: str) -> None:
    print(f"{Style.STEP} {message}", flush=True)


def ok(message: str) -> None:
    print(f"{Style.OK} {message}", flush=True)


def warn(message: str) -> None:
    print(f"{Style.WARN} {message}", file=sys.stderr, flush=True)


def fail(message: str) -> None:
    print(f"{Style.FAIL} {message}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ environment


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def to_windows_path(path: Path) -> str:
    """Translate a WSL path to its Windows form via wslpath.

    Resolved first: wslpath passes a relative path through unchanged, which
    would silently produce a path meaningless to the elevated process, whose
    working directory is not ours.
    """
    result = subprocess.run(
        ["wslpath", "-w", str(path.resolve())], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


# -------------------------------------------------------------- device presence


def usb_present() -> bool:
    """True when an Elgato device is on the USB bus.

    Reads sysfs rather than shelling out to lsusb, which is a separate package
    (usbutils) and may not be installed yet when this runs.
    """
    for id_vendor in SYS_USB.glob("*/idVendor"):
        try:
            if id_vendor.read_text().strip().lower() == ELGATO_VID:
                return True
        except OSError:
            continue
    return False


def elgato_hidraw_nodes() -> list[Path]:
    """Device nodes belonging to Elgato, via each hidraw's HID_ID uevent."""
    nodes: list[Path] = []
    for entry in sorted(SYS_HIDRAW.glob("hidraw*")):
        uevent = entry / "device" / "uevent"
        try:
            content = uevent.read_text().upper()
        except OSError:
            continue
        # HID_ID=0003:00000FD9:00000080  -> bus:vendor:product
        for line in content.splitlines():
            if line.startswith("HID_ID=") and f":0000{ELGATO_VID.upper()}:" in line:
                nodes.append(Path("/dev") / entry.name)
                break
    return nodes


def describe_node(node: Path) -> str:
    """Owner, group and mode of a device node, for the permission check."""
    import grp
    import pwd

    info = node.stat()
    try:
        user = pwd.getpwuid(info.st_uid).pw_name
    except KeyError:
        user = str(info.st_uid)
    try:
        group = grp.getgrgid(info.st_gid).gr_name
    except KeyError:
        group = str(info.st_gid)
    return f"{user}:{group} {info.st_mode & 0o777:o}"


# ------------------------------------------------------------------- the launch


def powershell_quote(value: str) -> str:
    """Quote for a PowerShell single-quoted string literal."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def build_launch_command(script: Path, log: Path, extra: list[str]) -> str:
    """The PowerShell snippet that raises UAC and starts the setup script."""
    inner = [
        "-NoProfile",
        # The script sits on a \\wsl.localhost\ UNC path, which Windows treats
        # as an untrusted zone; without this the elevated shell refuses it.
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        to_windows_path(script),
        "-LogPath",
        to_windows_path(log),
        *extra,
    ]
    arg_list = ", ".join(powershell_quote(item) for item in inner)
    # A declined UAC prompt raises a terminating error here; report it as a
    # distinct exit code rather than a generic failure.
    return (
        "try { "
        f"Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList {arg_list}; "
        "exit 0 "
        "} catch { "
        "Write-Error $_.Exception.Message; "
        "exit 5 "
        "}"
    )


def launch_elevated(script: Path, log: Path, extra: list[str]) -> int:
    command = build_launch_command(script, log, extra)
    # UTF-16LE base64 is what -EncodedCommand expects, and it removes every
    # remaining quoting concern between here and the elevated process.
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")

    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
    )
    if result.returncode == EXIT_ELEVATION_DECLINED:
        fail("elevation was declined at the UAC prompt")
        return EXIT_ELEVATION_DECLINED
    if result.returncode != 0:
        fail(f"could not start the Windows script (exit {result.returncode})")
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        return EXIT_LAUNCH_FAILED
    return EXIT_OK


# ---------------------------------------------------------------------- polling


def read_log(log: Path) -> str:
    """Read the transcript, tolerating PowerShell's encoding choices."""
    try:
        raw = log.read_bytes()
    except OSError:
        return ""
    # Windows PowerShell 5.1 and PowerShell 7 disagree about transcript
    # encoding, and both may emit a BOM. utf-8-sig strips a UTF-8 BOM that
    # would otherwise show up as a stray character on the first line.
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def log_failed(log: Path) -> bool:
    """True once the Windows script has reported a failure.

    Without this a Windows-side failure would still burn the whole timeout,
    since the elevated window closes and takes its output with it.
    """
    return any(line.startswith("fail ") for line in read_log(log).splitlines())


def wait_for_device(timeout: float, log: Path, interval: float = 1.0) -> bool:
    """Poll sysfs until the device appears, the script fails, or time runs out."""
    deadline = time.monotonic() + timeout
    seen_usb = False

    while time.monotonic() < deadline:
        if not seen_usb and usb_present():
            seen_usb = True
            ok("device appeared on the USB bus")

        if seen_usb:
            nodes = elgato_hidraw_nodes()
            if nodes:
                for node in nodes:
                    ok(f"{node} -> {describe_node(node)}")
                return True

        if log_failed(log):
            fail("the Windows script reported a failure; not waiting further")
            return False

        time.sleep(interval)

    if seen_usb:
        warn("device is attached but no hidraw node appeared")
        warn("check the udev rule: sudo udevadm control --reload-rules && sudo udevadm trigger")
    return False


def show_log(log: Path) -> None:
    """Echo the elevated script's transcript, which is otherwise invisible."""
    content = read_log(log).strip()
    if not content:
        return
    print()
    step(f"Windows script output ({log})")
    for line in content.splitlines():
        print(f"    {line}")


# ------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="attach_device.py",
        description="Run the Windows usbipd setup elevated, then wait for the device.",
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="seconds to wait (default: 120)"
    )
    parser.add_argument(
        "--skip-launch",
        action="store_true",
        help="do not start the Windows script; only poll for the device",
    )
    parser.add_argument(
        "windows_args",
        nargs="*",
        help="extra arguments forwarded to setup-windows.ps1, e.g. -BusId 1-4",
    )
    args = parser.parse_args(argv)

    if not is_wsl():
        fail("not running under WSL; nothing to attach")
        return EXIT_NOT_WSL

    if usb_present() and elgato_hidraw_nodes():
        ok("device is already attached")
        return EXIT_OK

    script = Path(__file__).resolve().parent / "setup-windows.ps1"
    if not script.exists():
        fail(f"cannot find {script}")
        return EXIT_LAUNCH_FAILED

    # Written by the elevated process, read back here. Living under the WSL
    # filesystem means Windows reaches it over \\wsl.localhost and we can read
    # it without translating a Windows temp path back again.
    log = Path(__file__).resolve().parent.parent / ".windows-setup.log"
    # Stale content from a previous run would trip the failure detector.
    log.unlink(missing_ok=True)

    if not args.skip_launch:
        step("Starting the Windows setup script (a UAC prompt will appear)")
        print("    Approve it to continue; this window keeps waiting.")
        code = launch_elevated(script, log, list(args.windows_args))
        if code != EXIT_OK:
            return code
        ok("launched; waiting for the device")

    step(f"Polling for the Stream Deck (up to {args.timeout:.0f}s)")
    attached = wait_for_device(args.timeout, log)
    show_log(log)

    if not attached:
        fail("device did not appear")
        print()
        print("  Things to check:")
        print("    * Was the UAC prompt approved?")
        print("    * Is the Stream Deck plugged into the Windows host?")
        print("    * Run 'usbipd list' on Windows to confirm it is bound.")
        return EXIT_TIMEOUT

    print()
    ok("device attached and visible to WSL")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
