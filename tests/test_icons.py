"""Agent mark tests.

Marks must be collision-free and renderable; a duplicate or a missing glyph is
invisible in code review but obvious -- and useless -- on the device.
"""

from __future__ import annotations

import string
from pathlib import Path

import pytest

from herdr_streamdeck.icons import MARKS, TERMINAL, mark_for, normalise


def test_requested_marks() -> None:
    assert mark_for("omp").glyph == "π′"  # pi prime
    assert mark_for("pi").glyph == "π"
    assert mark_for("opencode").glyph == "OC"
    assert mark_for("codex").glyph == ">_"
    assert mark_for("copilot").glyph == "copilot"
    assert mark_for("claude").glyph == "✳"


def test_codex_and_the_terminal_stay_apart() -> None:
    """The only two prompt-shaped marks. They share a trailing underscore, so
    the sigil and the colour are all that separate them -- and both must."""
    codex = mark_for("codex")
    assert codex.glyph == ">_"
    assert TERMINAL.glyph == "$_"
    assert codex.glyph[0] != TERMINAL.glyph[0]
    assert codex.color != TERMINAL.color

    difference = sum(abs(a - b) for a, b in zip(codex.color, TERMINAL.color, strict=True))
    assert difference > 120, "too close in colour to tell apart at a glance"


def test_no_other_mark_takes_a_prompt_shape() -> None:
    """A third `X_` mark would break the pairing above."""
    prompts = {name for name, mark in MARKS.items() if mark.glyph.endswith("_")}
    assert prompts == {"codex"}


def test_no_two_agents_share_a_mark() -> None:
    glyphs = [m.glyph for m in MARKS.values()]
    assert len(glyphs) == len(set(glyphs)), "duplicate glyph would be unreadable"


def test_every_herdr_agent_has_a_mark() -> None:
    """herdr's detection enum at protocol 17, plus qwencode via display_agent."""
    enum = {
        "pi",
        "omp",
        "claude",
        "codex",
        "copilot",
        "devin",
        "droid",
        "kimi",
        "opencode",
        "kilo",
        "hermes",
        "qodercli",
        "cursor",
        "mastracode",
    }
    assert enum <= set(MARKS), f"unmarked agents: {enum - set(MARKS)}"
    assert "qwencode" in MARKS


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("OpenCode", "opencode"),
        ("open-code", "opencode"),
        ("open_code", "opencode"),
        ("  Claude ", "claude"),
        (None, ""),
        ("", ""),
    ],
)
def test_normalise_tolerates_display_agent_spellings(given: str | None, expected: str) -> None:
    assert normalise(given) == expected


def test_unknown_agent_falls_back_to_an_initial() -> None:
    """Better than nothing, and makes a new agent visible before it is styled."""
    assert mark_for("newthing").glyph == "N"


def test_a_pane_with_no_agent_is_marked_as_a_terminal() -> None:
    """A shell is a normal thing to switch to, not an absence."""
    assert mark_for(None) is TERMINAL
    assert mark_for("") is TERMINAL
    assert TERMINAL.glyph == "$_"


def test_terminal_does_not_collide_with_any_agent() -> None:
    assert TERMINAL.glyph not in {m.glyph for m in MARKS.values()}


def test_terminals_are_steady_targets() -> None:
    """They are actionable, so they must not flicker or animate -- their mark
    is drawn at full strength regardless, so a quiet field is no loss."""
    from herdr_streamdeck.animation import animation_for

    assert not animation_for("unknown").animated


def test_long_marks_are_scaled_down() -> None:
    assert MARKS["copilot"].scale < MARKS["codex"].scale


# ------------------------------------------------------------------ overrides


def test_no_override_dir_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from herdr_streamdeck.icons import override_dir, resolve_override

    monkeypatch.delenv("HERDR_PLUGIN_CONFIG_DIR", raising=False)
    assert override_dir() is None
    assert resolve_override("claude") is None


def test_override_found_for_a_matching_png(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    from pathlib import Path

    from herdr_streamdeck.icons import resolve_override

    root = Path(str(tmp_path))
    icons = root / "icons"
    icons.mkdir()
    (icons / "claude.png").write_bytes(b"not really a png")
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(root))

    assert resolve_override("claude") == icons / "claude.png"
    # Normalisation applies, so display_agent spellings still match.
    assert resolve_override("Claude") == icons / "claude.png"
    assert resolve_override("codex") is None


def test_override_missing_dir_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    from herdr_streamdeck.icons import resolve_override

    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path))  # no icons/ inside
    assert resolve_override("claude") is None


def test_the_bundled_font_covers_every_mark() -> None:
    """PIL performs no font fallback: a glyph missing from the chosen face
    renders as tofu, silently.

    Bundling one font makes this checkable once, here, instead of hoping the
    macOS CI leg happens to resolve a face with U+2733 in it. If a mark is ever
    added that DejaVu Sans Mono lacks, this fails on every platform at once.
    """
    fonttools = pytest.importorskip("fontTools.ttLib")

    from herdr_streamdeck.deck import FONT_PATH

    assert FONT_PATH.is_file(), f"the bundled font is missing from {FONT_PATH}"

    cmap = fonttools.TTFont(str(FONT_PATH)).getBestCmap()
    everything = {**MARKS, "<terminal>": TERMINAL}
    missing = {
        name: [c for c in mark.glyph if ord(c) not in cmap] for name, mark in everything.items()
    }
    missing = {name: chars for name, chars in missing.items() if chars}
    assert not missing, f"{FONT_PATH.name} lacks glyphs: {missing}"


def test_the_bundled_font_covers_badge_text() -> None:
    """Badges show arbitrary pane names, so the alphabet matters too -- plus
    the ellipsis the badge appends when it has to truncate."""
    fonttools = pytest.importorskip("fontTools.ttLib")

    from herdr_streamdeck.deck import FONT_PATH

    cmap = fonttools.TTFont(str(FONT_PATH)).getBestCmap()
    alphabet = string.ascii_letters + string.digits + " -_.:/#@()[]" + "\u2026"
    missing = [c for c in alphabet if ord(c) not in cmap]
    assert not missing, f"{FONT_PATH.name} lacks {missing!r}"


def test_the_loader_actually_uses_the_bundled_font() -> None:
    """A regression guard: the loader used to search the system, and a machine
    with a different DejaVu on its path would render differently."""
    from herdr_streamdeck.deck import FONT_PATH, load_font

    font = load_font(30)
    assert Path(str(getattr(font, "path", ""))) == FONT_PATH


def test_the_font_licence_ships_beside_it() -> None:
    """Bitstream Vera permits redistribution only if the notice travels with
    the font, so shipping the .ttf without this file would be a violation."""
    from herdr_streamdeck.deck import FONT_PATH

    licence = FONT_PATH.parent / "LICENSE-DejaVu.txt"
    assert licence.is_file(), "bundled font has no licence notice beside it"
    assert "Bitstream Vera" in licence.read_text()
