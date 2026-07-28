# Host setup

Getting an Elgato Stream Deck talking to a local process. Two supported
development hosts: **macOS** (native, trivial) and **WSL2 on Windows** (needs
USB passthrough).

The Elgato first-party software is never required and is actively unhelpful —
on macOS it holds an exclusive claim on the HID device, and on Linux it does not
exist. The `streamdeck` Python package speaks raw HID directly.

---

## macOS

```bash
./scripts/setup-macos.sh
```

Installs `hidapi` via Homebrew, warns if Elgato's software is running or sits in
Login Items, and confirms the device is on the USB bus.

There is nothing to keep running, nothing to schedule, and no reboot step.

---

## WSL2

One command, run inside WSL:

```bash
./scripts/setup-wsl.sh
```

It prepares the Linux side, then triggers the Windows half itself — a UAC
prompt appears, you approve it, and the script waits for the device to show up.
Nothing needs to be run by hand on the Windows side.

**Order matters and the script enforces it:** the udev rule must exist before
the device first appears, or the node lands as `root:root` and stays that way
until udev is re-triggered.

What the Linux phase does:

- Verifies the kernel has `CONFIG_HIDRAW`, `CONFIG_USB_HID`, `CONFIG_USBIP_VHCI_HCD`
- Installs `libhidapi-libusb0` and `usbutils`
- Ensures you are in `plugdev`
- Writes `/etc/udev/rules.d/70-streamdeck.rules` and reloads udev

> The rule uses `GROUP="plugdev"`, not the `TAG+="uaccess"` form most Stream Deck
> guides show. `uaccess` depends on logind seat assignment, which WSL does not
> perform — the rule parses cleanly and silently grants nothing.

Requires `systemd=true` under `[boot]` in `/etc/wsl.conf`. Without systemd, udev
never runs and the rule is inert. The script warns if PID 1 is not systemd.

The Windows phase (`setup-windows.ps1`, launched elevated) then:

- Installs `usbipd-win` via winget if absent
- Auto-detects the Elgato device (`VID_0FD9`) and runs `usbipd bind`
- Registers the logon task that keeps it attached
- Starts the task immediately, so no logout is needed

Admin is required **only** for `usbipd bind`, which persists across reboots.
The scheduled task itself runs unelevated.

### Options

```bash
./scripts/setup-wsl.sh --no-windows      # prepare Linux only
./scripts/setup-wsl.sh --attach-only     # skip prep; just attach and poll
./scripts/setup-wsl.sh --verify-only     # report current state
./scripts/setup-wsl.sh --timeout 300     # wait longer for the device
./scripts/setup-wsl.sh -- -BusId 1-4     # forward args to setup-windows.ps1
```

Use `-BusId` if you have several Elgato devices; the Windows script refuses to
guess between them.

### How the handoff works

`scripts/attach_device.py` does the crossing, in Python rather than bash for
reasons that are worth knowing before changing it:

- **Quoting.** The call passes through bash, `powershell.exe`, and
  `Start-Process -ArgumentList` feeding a second elevated shell. `subprocess`
  with an argument list only removes the first layer. The script instead builds
  the PowerShell snippet and passes it as `-EncodedCommand` (UTF-16LE base64),
  reducing all of it to one opaque argv token.
- **Elevation.** `Start-Process -Verb RunAs` raises the UAC prompt and returns
  as soon as the elevated process starts, so it detaches on its own. The WSL
  side then polls rather than waiting on a handle it does not own.
- **Invisible output.** `-Verb RunAs` cannot be combined with
  `-RedirectStandardOutput` — PowerShell rejects the pair — and the elevated
  window closes when done. So `setup-windows.ps1` takes `-LogPath` and
  transcribes to a file the WSL side reads back and prints.
- **Execution policy.** The script lives on a `\\wsl.localhost\` UNC path,
  which Windows treats as an untrusted zone, so the elevated shell is launched
  with `-ExecutionPolicy Bypass`.
- **Polling.** Reads `/sys/bus/usb/devices/*/idVendor` and the `HID_ID` line of
  each `/sys/class/hidraw/*/device/uevent` rather than shelling out to `lsusb`
  or `udevadm`, so it works before `usbutils` is installed. It also watches the
  transcript for a failure line, so a Windows-side error surfaces immediately
  instead of burning the whole timeout.

Exit codes: `2` not WSL, `3` launch failed, `4` device never appeared,
`5` UAC declined.

### Verify

```bash
./scripts/setup-wsl.sh --verify-only
```

You want a `/dev/hidraw*` node owned by group `plugdev`. If it reports
`root:root`, the udev rule did not fire — re-run and re-attach.

---

## About the scheduled task

`usbipd attach --wsl --busid <id> --auto-attach` does not exit. It blocks,
watching for the device so it can re-attach after a replug or a `wsl --shutdown`.
It is a foreground console program, not a service — so it needs somewhere to
live.

The task is configured:

| Setting | Value | Why |
| --- | --- | --- |
| Trigger | At logon, +20s delay | See below |
| Run level | Limited (unelevated) | `attach` needs no admin |
| Execution time limit | Unlimited | Default is 3 days, which would kill it |
| Multiple instances | Ignore new | Prevents duplicate attach loops |
| Restart | 3 attempts, 1 min apart | Survives transient USB errors |
| Window | Hidden via `conhost --headless` | Otherwise a console sits open all session |

**Why logon and not startup.** `usbipd attach --wsl` targets *your user's* WSL 2
VM, which does not exist until you log in. A task triggered at boot runs as
SYSTEM in session 0 and cannot reach your distro — it fails silently every time.
Logon is the earliest trigger that can actually work. The 20-second delay gives
WSL and the USB stack time to settle.

**If the task runs but the device never attaches**, re-register with a visible
window to see the error:

```powershell
.\scripts\setup-windows.ps1 -VisibleConsole
```

`conhost --headless` is lightly documented, so this is the escape hatch.

**Undo everything** — unregisters the task, detaches and unbinds:

```powershell
.\scripts\setup-windows.ps1 -Remove
```

---

## Running the daemon

> **Run the daemon, not a script.** Keys only become live when
> `DeckController.run()` installs the press handler. Driving `prime()`/`tick()`
> directly from a script gives a correct-looking display with dead keys, which
> is easy to mistake for a hardware fault. The daemon logs
> `press handler installed; keys are live` at startup — if that line is absent,
> presses will do nothing.

No hardware needed to develop against a live herdr server:

```bash
uv sync
uv run herdr-streamdeck --no-device --all-panes -v
```

With a device attached:

```bash
uv run herdr-streamdeck --probe      # list attached decks, then exit
uv run herdr-streamdeck              # run for real
```

`--probe` is the first thing to try when the deck goes dark — it distinguishes
"no device visible to this machine" from "daemon problem", and is also exposed
as the `probe-device` plugin action.

## Talking to herdr

See [protocol.md](protocol.md) for the socket API as actually observed —
notably that **herdr serves one request per connection**, which is not in the
generated schema and shapes the whole client. [plugin-system.md](plugin-system.md)
covers the `herdr-plugin.toml` manifest.

Regenerate the raw schema with:

```bash
herdr api schema --output schema.json   # 89 methods at protocol 17
herdr api snapshot                      # live runtime state
```
