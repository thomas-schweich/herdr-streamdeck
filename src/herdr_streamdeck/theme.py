"""Colour schemes for the key faces.

Two, because the deck is read in two very different rooms: a dim one, where a
dark field with bright marks is comfortable, and a bright office, where the
same field reads as a black hole and the marks disappear.

The non-obvious part is **which way dimming goes**. Brightness carries agent
status, and on a dark field "quieter" means multiplying toward black. Doing the
same on a white field is wrong twice over: the key turns muddy grey, and the
dark mark is crushed toward the background it is meant to contrast with. So a
theme declares the colour it dims *toward* -- black on dark, white on light --
and quiet keys fade into their own background either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

RGB = tuple[int, int, int]


class ThemeName(StrEnum):
    DARK = "dark"
    LIGHT = "light"


@dataclass(frozen=True, slots=True)
class Theme:
    """How a key is coloured."""

    background: RGB
    empty_background: RGB
    badge_fill: RGB
    badge_text: RGB
    dim_target: int
    """Channel value dimming approaches: 0 on a dark theme, 255 on a light one."""

    mark_peak: int
    """Brightest channel a mark is scaled to.

    Agent hues are tuned for a dark field, where near-white reads well. On
    white they would vanish, so each is scaled down to this peak -- preserving
    hue, which is the part that identifies the agent, while guaranteeing
    contrast against the field.
    """

    def mark_color(self, color: RGB) -> RGB:
        """Adapt an agent's accent colour to this theme."""
        brightest = max(color)
        if brightest <= self.mark_peak:
            return color
        scale = self.mark_peak / brightest
        return (
            round(color[0] * scale),
            round(color[1] * scale),
            round(color[2] * scale),
        )


DARK = Theme(
    background=(38, 38, 42),
    empty_background=(20, 20, 23),
    badge_fill=(72, 72, 80),
    badge_text=(236, 236, 241),
    dim_target=0,
    mark_peak=255,  # hues are already tuned for this field
)

LIGHT = Theme(
    background=(244, 244, 247),
    # Barely distinguishable from the field: an unoccupied key should be the
    # least eye-catching thing on a white deck, not the most.
    empty_background=(226, 226, 231),
    badge_fill=(64, 64, 72),
    badge_text=(246, 246, 249),
    dim_target=255,
    mark_peak=118,
)

THEMES: dict[ThemeName, Theme] = {ThemeName.DARK: DARK, ThemeName.LIGHT: LIGHT}


def theme_for(name: str) -> Theme:
    return THEMES[ThemeName(name)]
