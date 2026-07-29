"""Pixel-level tests for the frame ladder.

These exercise ``key_frames`` -- the real pipeline, minus the device -- because
the property that matters cannot be checked any other way: **the foreground is
identical in every frame, and only the field moves**.

That is what makes a quiet key still tell you it is occupied, and it is easy to
undo by accident. The previous implementation dimmed the whole composed image
through a lookup table; anyone reaching for that again would pass every other
test in the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from herdr_streamdeck.animation import LEVELS
from herdr_streamdeck.deck import ButtonFace, compose_foreground, key_frames
from herdr_streamdeck.theme import DARK, LIGHT, Theme

SIZE = (72, 72)
WIDTH, HEIGHT = SIZE
THEMES = (DARK, LIGHT)

RGB = tuple[int, int, int]
Point = tuple[int, int]


def face_for(theme: Theme) -> ButtonFace:
    return ButtonFace(
        mark="✳",
        mark_color=theme.mark_color((217, 119, 87)),
        badge="ENG-4521",
        status_color=(217, 132, 24),
        background=theme.background,
    )


def ladder(theme: Theme, face: ButtonFace | None = None) -> list[bytes]:
    """Every frame as raw RGB bytes, which are cheap to compare exhaustively."""
    subject = face if face is not None else face_for(theme)
    return [image.tobytes() for image in key_frames(SIZE, subject, theme, LEVELS)]


def pixel(data: bytes, point: Point) -> RGB:
    offset = (point[1] * WIDTH + point[0]) * 3
    return (data[offset], data[offset + 1], data[offset + 2])


def opaque_points(theme: Theme, face: ButtonFace | None = None) -> list[Point]:
    """Coordinates the foreground covers completely.

    Antialiased edges are excluded deliberately: they blend with the field by
    design, so only fully opaque pixels can be expected to be level-invariant.
    """
    subject = face if face is not None else face_for(theme)
    alpha = compose_foreground(SIZE, subject, theme).getchannel("A").tobytes()
    return [
        (index % WIDTH, index // WIDTH) for index, value in enumerate(alpha) if value == 255
    ]


FIELD_POINT: Point = (2, HEIGHT - 2)
"""A patch of bare field: below the status strip, left of the badge."""

MARK_POINT: Point = (WIDTH // 2, HEIGHT // 2 - 4)


@pytest.mark.parametrize("theme", THEMES, ids=("dark", "light"))
def test_the_foreground_is_identical_at_every_level(theme: Theme) -> None:
    """The core promise: a mark is exactly as legible idle as it is done."""
    points = opaque_points(theme)
    assert len(points) > 200, "expected the mark, strip and badge to cover real area"

    frames = ladder(theme)
    reference = frames[-1]
    for level, frame in enumerate(frames):
        for point in points:
            assert pixel(frame, point) == pixel(reference, point), (
                f"foreground pixel {point} changed at level {level}"
            )


@pytest.mark.parametrize("theme", THEMES, ids=("dark", "light"))
def test_the_field_actually_changes_between_levels(theme: Theme) -> None:
    """The other half: if nothing moved, status would be invisible."""
    shades = {pixel(frame, FIELD_POINT) for frame in ladder(theme)}
    assert len(shades) > 20, f"only {len(shades)} distinct field shades across the ladder"


@pytest.mark.parametrize("theme", THEMES, ids=("dark", "light"))
def test_the_ladder_ends_on_the_declared_field_colours(theme: Theme) -> None:
    frames = ladder(theme)
    assert pixel(frames[0], FIELD_POINT) == theme.field_quiet
    assert pixel(frames[-1], FIELD_POINT) == theme.background


@pytest.mark.parametrize("theme", THEMES, ids=("dark", "light"))
def test_every_level_is_produced(theme: Theme) -> None:
    assert len(ladder(theme)) == LEVELS


@pytest.mark.parametrize("theme", THEMES, ids=("dark", "light"))
def test_an_empty_key_holds_its_own_colour(theme: Theme) -> None:
    """Empty keys are pinned to full level, and must land on exactly the colour
    the theme declared -- not a shade the field arrived at."""
    frames = ladder(theme, ButtonFace(background=theme.empty_background))
    assert pixel(frames[-1], MARK_POINT) == theme.empty_background


@pytest.mark.parametrize("theme", THEMES, ids=("dark", "light"))
def test_a_quiet_key_still_shows_its_mark(theme: Theme) -> None:
    """The failure that prompted all of this: not being able to tell whether a
    square was occupied. Even the quietest frame must differ sharply from an
    empty key at the centre of the mark."""
    quietest = ladder(theme)[0]
    empty = ladder(theme, ButtonFace(background=theme.empty_background))[-1]
    difference = max(
        abs(a - b)
        for a, b in zip(pixel(quietest, MARK_POINT), pixel(empty, MARK_POINT), strict=True)
    )
    assert difference > 60, "a quiet occupied key looks like an empty one"


@pytest.mark.parametrize("theme", THEMES, ids=("dark", "light"))
def test_a_quiet_key_still_shows_its_badge(theme: Theme) -> None:
    """Badges were the point of the change -- they must not fade either."""
    face = face_for(theme)
    badge_points = [p for p in opaque_points(theme, face) if p[1] > HEIGHT * 0.7]
    assert badge_points, "the badge drew nothing"

    frames = ladder(theme, face)
    for point in badge_points:
        assert pixel(frames[0], point) == pixel(frames[-1], point)


def test_a_broken_icon_falls_back_to_the_glyph(tmp_path: Path) -> None:
    """A PNG that cannot be decoded must not blank the key."""
    broken = tmp_path / "claude.png"
    broken.write_bytes(b"not a png")

    face = ButtonFace(mark="✳", icon=broken, background=DARK.background)
    alpha = compose_foreground(SIZE, face, DARK).getchannel("A").tobytes()
    assert max(alpha) == 255, "the glyph was not drawn"


def test_a_real_icon_replaces_the_glyph(tmp_path: Path) -> None:
    icon_path = tmp_path / "claude.png"
    Image.new("RGBA", (64, 64), (10, 200, 90, 255)).save(icon_path)

    face = ButtonFace(mark="✳", icon=icon_path, background=DARK.background)
    overlay = compose_foreground(SIZE, face, DARK)
    assert overlay.getpixel(MARK_POINT) == (10, 200, 90, 255)


def test_an_overlong_badge_is_truncated_rather_than_overflowing() -> None:
    face = ButtonFace(badge="x" * 40, background=DARK.background)
    alpha = compose_foreground(SIZE, face, DARK).getchannel("A").tobytes()
    # The badge is inset from the lower-right; nothing may reach the left edge.
    assert all(alpha[y * WIDTH] == 0 for y in range(HEIGHT))


# ------------------------------------------------------------------ mark fitting


def test_every_mark_fits_inside_its_key() -> None:
    """The per-agent scales are hand-set intent, not measurements, so the
    renderer has to guarantee this rather than trust them. `copilot` at its
    nominal size overruns a 72px key by a wide margin."""
    from PIL import Image, ImageDraw

    from herdr_streamdeck.deck import MIN_MARK_SIZE, fit_font
    from herdr_streamdeck.icons import MARKS, TERMINAL

    draw = ImageDraw.Draw(Image.new("RGB", SIZE))
    budget = WIDTH * 0.84
    for name, mark in {**MARKS, "<terminal>": TERMINAL}.items():
        nominal = max(MIN_MARK_SIZE, int(HEIGHT * 0.36 * mark.scale))
        font = fit_font(draw, mark.glyph, nominal, budget)
        assert draw.textlength(mark.glyph, font=font) <= budget, f"{name} overruns"


def point_size(font: object) -> float:
    """The size a font was loaded at.

    Only the TrueType face records one; the bitmap fallback has no size to
    report, and reaching it here would mean the bundled font failed to load.
    """
    from PIL import ImageFont

    assert isinstance(font, ImageFont.FreeTypeFont), "bundled font did not load"
    return font.size


def test_fitting_leaves_marks_that_already_fit_alone() -> None:
    """Shrinking a single glyph that fits would waste the key."""
    from PIL import Image, ImageDraw

    from herdr_streamdeck.deck import fit_font

    draw = ImageDraw.Draw(Image.new("RGB", SIZE))
    assert point_size(fit_font(draw, "C", 36, WIDTH * 0.84)) == 36


def test_fitting_stops_before_a_mark_becomes_unreadable() -> None:
    """An absurd budget must not shrink text to nothing -- overflowing is the
    better failure, since at least something is visible."""
    from PIL import Image, ImageDraw

    from herdr_streamdeck.deck import MIN_MARK_SIZE, fit_font

    draw = ImageDraw.Draw(Image.new("RGB", SIZE))
    assert point_size(fit_font(draw, "copilot", 36, 1.0)) == MIN_MARK_SIZE


def test_the_mark_stays_within_the_key_bounds() -> None:
    """End to end: nothing drawn may touch the left or right edge."""
    face = ButtonFace(mark="copilot", mark_scale=0.62, background=DARK.background)
    alpha = compose_foreground(SIZE, face, DARK).getchannel("A").tobytes()
    for y in range(HEIGHT):
        assert alpha[y * WIDTH] == 0 and alpha[y * WIDTH + WIDTH - 1] == 0


def test_single_glyph_marks_all_look_the_same_size() -> None:
    """Optical size is ink height, not font size and not advance width.

    Codex was `>_`, two characters and half again as wide as anything else.
    Scaling it to match on width made it 14px tall against Claude's 23 -- it
    matched the dimension being measured and lost the one being seen. This
    measures what the eye actually reads.

    Height rather than ink mass: a solid letter carries twice the ink of
    Claude's thin-spoked asterisk at the same apparent size, so mass compares
    stroke weight between glyph classes, not size.
    """
    from PIL import Image, ImageDraw

    from herdr_streamdeck.deck import MIN_MARK_SIZE, load_font
    from herdr_streamdeck.icons import MARKS

    def ink_height(glyph: str, size: int) -> int:
        canvas = Image.new("L", (WIDTH * 2, HEIGHT * 2), 0)
        ImageDraw.Draw(canvas).text(
            (WIDTH, HEIGHT), glyph, font=load_font(size), anchor="mm", fill=255
        )
        box = canvas.getbbox()
        assert box is not None, f"{glyph!r} drew nothing"
        return box[3] - box[1]

    def height_of(glyph: str, scale: float) -> int:
        return ink_height(glyph, max(MIN_MARK_SIZE, int(HEIGHT * 0.36 * scale)))

    reference = height_of(MARKS["claude"].glyph, MARKS["claude"].scale)
    for name, mark in MARKS.items():
        if len(mark.glyph) > 1:
            continue  # a word or a pair is a different problem; see below
        ratio = height_of(mark.glyph, mark.scale) / reference
        assert 0.85 <= ratio <= 1.40, f"{name} reads at {ratio:.2f}x Claude's size"
