# herdr-streamdeck

[![CI](https://github.com/thomas-schweich/herdr-streamdeck/actions/workflows/ci.yml/badge.svg)](https://github.com/thomas-schweich/herdr-streamdeck/actions/workflows/ci.yml)

Drive an [Elgato Stream Deck](https://www.elgato.com/stream-deck) from
[herdr](https://github.com/herdr/herdr)'s socket API. One key per agent pane,
coloured by agent status; press a key to focus that pane.

Elgato's own software is **not** required — this speaks raw HID directly. On
macOS it must not be running, since it claims the device exclusively.

Columns are workspaces in sidebar order, rows are that workspace's panes.
There is no stored layout: herdr's current arrangement is the source of truth,
so the deck always matches what the sidebar shows.

```
  diggy      codex      diggy    herdr-sd
┌─────────┬─────────┬─────────┬─────────┬─────────┐
│  ▔▔▔▔▔  │  ▔▔▔▔▔  │  ▔▔▔▔▔  │  ▔▔▔▔▔  │         │  ← status strip
│    ✳    │    C    │    ✳    │    ✳    │         │  ← agent mark
│    ⌐review│       │         │   ⌐api  │         │  ← name badge
├─────────┼─────────┼─────────┼─────────┼─────────┤
│    ✳    │         │         │    π′   │         │
└─────────┴─────────┴─────────┴─────────┴─────────┘
```

Status is brightness, not colour: **idle** dim · **working** slow pulse ·
**done** full · **blocked** blinking between half and full. All pulsing runs off
one shared clock, so every working pane breathes in step rather than each
starting its own phase.

Only the *field* dims. Marks and badges are drawn at full strength in every
frame, so a key tells you it is occupied whatever the agent is doing, and
brightness is free to use its whole range instead of stopping where a dimmed
glyph would stop being readable.

Two themes, for two rooms: `--theme dark` (default) and `--theme light` for a
bright office. Light is not an inversion — quiet keys go *grey*, not black,
since on a white deck a black key is the loudest thing on it. Agent accents are
darkened to keep contrast without losing their hue.

Badge text comes from herdr's `terminal_title_stripped` — the pane title with
the agent's own status glyph removed, since that duplicates the mark already
drawn and would cost two of the eight characters.

Badges show up to eight characters. A name that fits is shown whole; longer
ticket-style names drop the project prefix, since it is identical on every pane
in the project and so useless for telling them apart:

| pane name | badge | |
| --- | --- | --- |
| `ENG-4521` | `ENG-4521` | fits whole |
| `ENG-45211` | `45211` | too long: the number identifies it |
| `ENG-4521-refactor` | `refactor` | a description beats a number |
| `reviewer` | `reviewer` | not a ticket, and it fits |

Panes with no detected agent are plain terminals, marked `$_` and fully
switchable — pressing the key focuses the shell like any other pane.

Agent marks are typographic by default. Drop a PNG named after the agent into
`$HERDR_PLUGIN_CONFIG_DIR/icons/` (e.g. `claude.png`) to use your own artwork
instead.

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
uv run herdr-streamdeck --no-device -v               # no hardware required
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
uv run pytest        # 209 tests; herdr-dependent ones skip when no socket
uv run mypy          # strict; no suppressions in our own code
uv run ruff check .
```

Tests run without hardware or herdr. The 6 tests in `tests/test_integration.py`
skip themselves unless a live server is reachable at `HERDR_SOCKET_PATH` (or the
XDG default); the other 203 run anywhere, and pin the protocol quirks documented
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
