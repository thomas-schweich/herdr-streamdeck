"""Glyph marks identifying each agent on a key.

Deliberately typographic rather than pictorial. Three reasons:

* At 72x72 JPEG -- the MK.2's actual key format -- a downscaled vendor logo
  reads as mush, while a one or two character mark stays legible.
* Redistributing vendor trademarks in an MIT-licensed repository is a
  licensing question this project should not have to own.
* Every glyph here is verified present in DejaVu Sans, the font the renderer
  falls back to, so nothing degrades into tofu on a machine without extra
  fonts installed.

Users who want real logos can drop PNGs into the icon override directory
instead; see ``resolve_override``.

Marks are chosen to be collision-free at a glance: no two share a rendered
form. Codex takes the bare ``C`` and Copilot spells its name out, because a
single letter cannot serve both.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

RGB = tuple[int, int, int]

NEUTRAL: RGB = (228, 228, 231)


@dataclass(frozen=True, slots=True)
class AgentMark:
    """How one agent is drawn."""

    glyph: str
    color: RGB = NEUTRAL
    scale: float = 1.0
    """Font-size multiplier. Longer marks shrink to stay inside the key."""


# Keys are herdr's agent identifiers. Its detection enum at protocol 17 is:
#   pi omp claude codex copilot devin droid kimi opencode kilo hermes
#   qodercli cursor mastracode
# `qwencode` is absent from that enum, so herdr never reports it -- but
# `display_agent` accepts arbitrary strings, so a wrapper can self-report and
# still land on a mark here. See Pane.mark_key.
# Codex is `C` rather than a `>_`-style mark for the same reason TERMINAL is
# `$_`: the two must stay visually distinct.
MARKS: dict[str, AgentMark] = {
    # U+2733 eight-spoked asterisk, echoing Anthropic's starburst.
    "claude": AgentMark("✳", (217, 119, 87), 1.5),
    "pi": AgentMark("π", (150, 180, 255), 1.5),
    # "pi prime" -- U+2032 PRIME, not an ASCII apostrophe, which sits too low
    # and reads as a typo at this size.
    "omp": AgentMark("π′", (196, 160, 255), 1.35),
    "codex": AgentMark("C", (236, 236, 241), 1.4),
    # Spelled out: Codex has the single C, and two agents cannot share it.
    "copilot": AgentMark("copilot", (139, 148, 158), 0.62),
    "opencode": AgentMark("OC", (120, 200, 160), 1.0),
    "cursor": AgentMark("Cu", (200, 200, 210), 1.0),
    "devin": AgentMark("D", (110, 170, 240), 1.4),
    "droid": AgentMark("Dr", (140, 200, 120), 1.0),
    "kimi": AgentMark("K", (240, 160, 100), 1.4),
    "kilo": AgentMark("Ki", (200, 180, 120), 1.0),
    "hermes": AgentMark("H", (180, 140, 240), 1.4),
    "qodercli": AgentMark("Q", (120, 190, 210), 1.4),
    "qwencode": AgentMark("Qw", (150, 130, 230), 1.0),
    "mastracode": AgentMark("M", (230, 140, 170), 1.4),
}

TERMINAL = AgentMark("$_", (150, 152, 160), 1.05)
"""A pane with no detected agent: a plain shell.

Not an absence to be greyed out -- switching to a terminal is a normal thing
to want from the deck, so it gets a real mark and stays legible.

``$_`` rather than ``>_`` deliberately: Codex's own logo is a cloud containing
``>_``, and a terminal key must not read as a Codex key.

Replaces an earlier middle dot, which at 41px rendered as a small featureless
square -- present in the font, but meaningless on the key."""


def normalise(agent: str | None) -> str:
    """Fold an agent identifier to a lookup key.

    herdr uses bare lowercase ids, but ``display_agent`` is free-form, so
    tolerate the spellings a wrapper might plausibly report.
    """
    if not agent:
        return ""
    return agent.strip().lower().replace("-", "").replace("_", "").replace(" ", "")


def mark_for(agent: str | None) -> AgentMark:
    """The mark for an agent, falling back sensibly for unknown ones.

    An unrecognised but non-empty agent gets its capitalised initial rather
    than the terminal mark -- it is still more informative than nothing, and
    makes a newly supported agent visible before it has a hand-tuned mark. An
    *empty* agent is a real terminal, which is a different thing entirely.
    """
    key = normalise(agent)
    if not key:
        return TERMINAL
    known = MARKS.get(key)
    if known is not None:
        return known
    return AgentMark(key[0].upper(), NEUTRAL, 1.4)


def override_dir() -> Path | None:
    """Directory of user-supplied key images, if configured.

    herdr provides a per-plugin config directory; a PNG named after the agent
    (``claude.png``) replaces its glyph. Nothing ships here -- this exists so
    users can use real logos without the project redistributing them.
    """
    from_env = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not from_env:
        return None
    directory = Path(from_env) / "icons"
    return directory if directory.is_dir() else None


def resolve_override(agent: str | None) -> Path | None:
    """Path to a user-supplied image for this agent, if one exists."""
    directory = override_dir()
    if directory is None:
        return None
    key = normalise(agent)
    if not key:
        return None
    for suffix in (".png", ".jpg", ".jpeg"):
        candidate = directory / f"{key}{suffix}"
        if candidate.is_file():
            return candidate
    return None
