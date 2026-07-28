# herdr-streamdeck

[![CI](https://github.com/thomas-schweich/herdr-streamdeck/actions/workflows/ci.yml/badge.svg)](https://github.com/thomas-schweich/herdr-streamdeck/actions/workflows/ci.yml)

Drive an [Elgato Stream Deck](https://www.elgato.com/stream-deck) from
[herdr](https://github.com/herdr/herdr)'s socket API. One key per agent pane,
coloured by agent status; press a key to focus that pane.

Elgato's own software is **not** required — this speaks raw HID directly. On
macOS it must not be running, since it claims the device exclusively.

```
┌──────────┬──────────┬──────────┐
│ claude   │ codex    │ claude   │
│ working  │ idle     │ blocked  │   amber = busy   grey = idle
└──────────┴──────────┴──────────┘   green = done   red  = needs input
```

## Status

Working end to end on a Stream Deck MK.2 (15 keys): panes render with
status colours, and pressing a key focuses that pane in herdr. Verified against
real hardware and a live herdr server.

## Install

As a herdr plugin, once published:

```bash
herdr plugin install <owner>/herdr-streamdeck
```

For local development:

```bash
git clone <this repo> && cd herdr-streamdeck
uv sync
herdr plugin link .
```

`[[startup]]` commands run **only when a herdr server starts** — not on
`plugin link`, `plugin enable`, or `server reload-config` (all verified). So
during development, run the daemon by hand:

```bash
uv run herdr-streamdeck --no-device --all-panes -v   # no hardware required
uv run herdr-streamdeck                              # with a device
```

## Host setup

macOS needs `hidapi` and no Elgato software. WSL2 additionally needs USB
passthrough via `usbipd`, plus a udev rule.

```bash
./scripts/setup-macos.sh      # macOS
./scripts/setup-wsl.sh        # WSL2 -- one command, does both sides
```

Under WSL the script prepares the Linux side and then launches the Windows
half itself, elevated: a UAC prompt appears, you approve it, and it waits for
the device to attach. `setup-windows.ps1` can still be run directly from an
Administrator PowerShell if you prefer.

Full walkthrough in [docs/setup.md](docs/setup.md). Windows is not a target for
the daemon itself — under Windows, herdr runs in WSL and the device is passed
through to it.

## Development

```bash
uv run pytest        # 53 tests; herdr-dependent ones skip when no socket
uv run mypy          # strict; no suppressions in our own code
uv run ruff check .
```

Tests run without hardware or herdr. The 6 tests in `tests/test_integration.py`
skip themselves unless a live server is reachable at `HERDR_SOCKET_PATH` (or the
XDG default); the other 47 run anywhere, and pin the protocol quirks documented
below.

CI additionally covers what this machine cannot: macOS (the primary target),
`shellcheck` on the setup scripts, `PSScriptAnalyzer` on the PowerShell, and
structural validation of `herdr-plugin.toml`.

## Docs

- [docs/setup.md](docs/setup.md) — host setup for macOS and WSL2, and the
  usbipd logon task
- [docs/protocol.md](docs/protocol.md) — the socket API as observed, including
  the one-request-per-connection rule that shapes the client
- [docs/plugin-system.md](docs/plugin-system.md) — the `herdr-plugin.toml`
  manifest format, reverse-engineered

Both protocol documents were established empirically against herdr 0.7.5 /
protocol 17, since the generated schema does not cover connection semantics and
is incomplete on event naming.

## License

MIT
