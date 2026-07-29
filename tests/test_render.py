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
