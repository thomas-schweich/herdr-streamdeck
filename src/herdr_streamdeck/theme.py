"""Colour schemes for the key faces.

Two, because the deck is read in two very different rooms: a dim one, where a
dark field with bright marks is comfortable, and a bright office, where the
same field reads as a black hole and the marks disappear.

**Only the field carries brightness.** The mark, the name badge and the status
strip are drawn at full strength in every frame; status is the shade of the
field *behind* them. Dimming the foreground was answering the wrong question --
the thing that was actually hard to see was whether a key was occupied at all,
and that is now unconditional: a key with an agent always has a fully lit mark
on it, whatever the agent is doing.

Holding the foreground still also frees the field to use its whole range.
``field_quiet`` can sit as far from ``background`` as looks right, rather than
stopping where a dimmed glyph would have stopped being readable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .animation import blend_channel

RGB = tuple[int, int, int]


class ThemeName(StrEnum):
    DARK = "dark"
    LIGHT = "light"


@dataclass(frozen=True, slots=True)
class Theme:
    """How a key is coloured."""

    background: RGB
    """The field at full brightness -- what a `done` pane sits on."""

    field_quiet: RGB
    """The field at the bottom of the range -- what an `idle` pane sits on."""

    empty_background: RGB
    """A key with no pane. Not animated, so it never leaves this colour."""

    badge_fill: RGB
    badge_text: RGB

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

    def field_at(self, background: RGB, level: float) -> RGB:
        """The field colour for a perceptual level in 0..1.

        Interpolates from :attr:`field_quiet` up to ``background``, which is
        passed in rather than read from the theme so an empty key can hold its
        own colour. Note the direction is per-theme by construction: on the
        light theme ``field_quiet`` is a grey *below* a near-white background,
        so quieter means greyer, not darker-toward-black.
        """
        clamped = max(0.0, min(1.0, level))
        return (
            blend_channel(self.field_quiet[0], background[0], clamped),
            blend_channel(self.field_quiet[1], background[1], clamped),
            blend_channel(self.field_quiet[2], background[2], clamped),
        )


DARK = Theme(
    background=(58, 58, 64),
    field_quiet=(14, 14, 17),
    # At or below the quiet field: an empty key must never look busier than an
    # idle one. What tells them apart is the mark, not the shade.
    empty_background=(10, 10, 12),
    badge_fill=(72, 72, 80),
    badge_text=(236, 236, 241),
    mark_peak=255,  # hues are already tuned for this field
)

LIGHT = Theme(
    background=(250, 250, 252),
    # Grey rather than dark: on a white deck, "quiet" means less contrast under
    # the mark, and going black would make idle panes the loudest thing there.
    field_quiet=(176, 176, 188),
    empty_background=(238, 238, 243),
    badge_fill=(64, 64, 72),
    badge_text=(246, 246, 249),
    mark_peak=118,
)

THEMES: dict[ThemeName, Theme] = {ThemeName.DARK: DARK, ThemeName.LIGHT: LIGHT}


def theme_for(name: str) -> Theme:
    return THEMES[ThemeName(name)]
