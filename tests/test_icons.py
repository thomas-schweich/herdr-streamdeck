"""Agent mark tests.

Marks must be collision-free and renderable; a duplicate or a missing glyph is
invisible in code review but obvious -- and useless -- on the device.
"""

from __future__ import annotations

import pytest

from herdr_streamdeck.icons import MARKS, TERMINAL, mark_for, normalise


def test_requested_marks() -> None:
    assert mark_for("omp").glyph == "π′"  # pi prime
    assert mark_for("pi").glyph == "π"
    assert mark_for("opencode").glyph == "OC"
    assert mark_for("codex").glyph == "C"
    assert mark_for("copilot").glyph == "copilot"
    assert mark_for("claude").glyph == "✳"


def test_codex_owns_the_bare_c() -> None:
    """Copilot spells its name out precisely so Codex can keep 'C'."""
    assert mark_for("codex").glyph == "C"
    assert mark_for("copilot").glyph != "C"


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


def test_marks_render_in_dejavu() -> None:
    """A glyph the fallback font lacks would draw as tofu.

    Covers TERMINAL as well as MARKS: an earlier version checked only MARKS,
    and the terminal mark -- which lives outside it -- went unverified.
    """
    fonttools = pytest.importorskip("fontTools.ttLib")
    from pathlib import Path

    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if not font_path.exists():
        pytest.skip("DejaVu not installed on this platform")

    cmap = fonttools.TTFont(str(font_path)).getBestCmap()
    everything = {**MARKS, "<terminal>": TERMINAL}
    for name, mark in everything.items():
        missing = [c for c in mark.glyph if ord(c) not in cmap]
        assert not missing, f"{name}: {missing!r} absent from DejaVu"


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
    """Codex's real logo is a cloud containing >_, hence $_ here."""
    assert TERMINAL.glyph not in {m.glyph for m in MARKS.values()}
    assert TERMINAL.glyph != mark_for("codex").glyph


def test_terminals_stay_legible() -> None:
    """They are actionable targets, so they must not be dimmed into absence."""
    from herdr_streamdeck.animation import LEGIBLE_FLOOR, animation_for

    assert animation_for("unknown").high >= LEGIBLE_FLOOR


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


def test_the_resolved_font_covers_every_mark() -> None:
    """Check the font this platform actually resolves, not just DejaVu.

    PIL performs no font fallback: a glyph missing from the chosen face
    renders as tofu, silently. DejaVu does not exist on macOS, so the loader
    falls through to whatever that platform has -- and the marks include
    symbols (U+2733, U+2032) that common text faces omit. This is the check
    that would catch it, and it only means anything when run on that
    platform, which is what the macOS CI leg is for.
    """
    fonttools = pytest.importorskip("fontTools.ttLib")
    from pathlib import Path

    from herdr_streamdeck.deck import load_font

    font = load_font(30)
    path = getattr(font, "path", None)
    assert path, (
        "no TrueType font resolved; PIL fell back to its bitmap default, "
        "which renders every mark as unreadable fixed-size text"
    )

    font_path = Path(str(path))
    try:
        loaded = fonttools.TTFont(str(font_path), fontNumber=0)
        cmap = loaded.getBestCmap()
    except Exception as exc:  # pragma: no cover - platform dependent
        pytest.skip(f"cannot inspect {font_path}: {exc}")

    everything = {**MARKS, "<terminal>": TERMINAL}
    missing = {
        name: [c for c in mark.glyph if ord(c) not in cmap] for name, mark in everything.items()
    }
    missing = {name: chars for name, chars in missing.items() if chars}
    assert not missing, f"{font_path.name} lacks glyphs: {missing}"
