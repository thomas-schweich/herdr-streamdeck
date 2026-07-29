"""Theme tests.

The invariant these exist to protect is that **only the field carries
brightness**. Marks and badges are drawn at full strength at every level, so a
theme's job is to say what the field looks like when a pane is loud and when it
is quiet -- and, on the light theme, that "quiet" means grey rather than black.
The corresponding pixel-level check lives in test_render.py.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from herdr_streamdeck.animation import GAMMA, LEVELS, blend_channel, scale_factor
from herdr_streamdeck.theme import DARK, LIGHT, ThemeName, theme_for


def test_themes_resolve_by_name() -> None:
    assert theme_for("dark") is DARK
    assert theme_for("light") is LIGHT
    with pytest.raises(ValueError):
        theme_for("chartreuse")


def test_backgrounds_are_actually_opposite() -> None:
    assert sum(DARK.background) < 250, "dark field should be dark"
    assert sum(LIGHT.background) > 600, "light field should be near-white"


def test_quiet_stays_on_its_own_side_of_the_room() -> None:
    """The whole point of two themes: quiet must not mean the same pixels.

    On white, dimming toward black would make an idle pane the highest-contrast
    key on the deck -- the exact opposite of what quiet is meant to convey.
    """
    assert sum(DARK.field_quiet) < sum(DARK.background), "dark quiet goes down"
    assert sum(LIGHT.field_quiet) < sum(LIGHT.background), "light quiet goes down too"
    assert min(LIGHT.field_quiet) > 140, "but nowhere near black"


def test_the_field_spans_a_visible_range_in_both_themes() -> None:
    """If quiet and loud look alike, brightness has stopped meaning anything."""
    for theme in (DARK, LIGHT):
        loud = theme.field_at(theme.background, 1.0)
        quiet = theme.field_at(theme.background, 0.0)
        assert max(abs(a - b) for a, b in zip(loud, quiet, strict=True)) > 30


def test_field_ends_land_exactly_on_the_declared_colours() -> None:
    for theme in (DARK, LIGHT):
        assert theme.field_at(theme.background, 1.0) == theme.background
        assert theme.field_at(theme.background, 0.0) == theme.field_quiet


def test_field_is_monotonic_and_in_range() -> None:
    for theme in (DARK, LIGHT):
        levels = [theme.field_at(theme.background, i / (LEVELS - 1)) for i in range(LEVELS)]
        for channel in range(3):
            values = [level[channel] for level in levels]
            assert values == sorted(values)
            assert min(values) >= 0 and max(values) <= 255


def test_field_clamps_levels_outside_the_range() -> None:
    for theme in (DARK, LIGHT):
        assert theme.field_at(theme.background, -3.0) == theme.field_quiet
        assert theme.field_at(theme.background, 4.0) == theme.background


def test_an_empty_key_is_not_dragged_toward_the_quiet_field() -> None:
    """Empty keys are pinned at full level, so their own colour must survive.

    They are not animated, so the field must not reinterpret them -- otherwise
    an unoccupied key would drift with whatever level happened to be current.
    """
    for theme in (DARK, LIGHT):
        assert theme.field_at(theme.empty_background, 1.0) == theme.empty_background


def test_empty_keys_recede_in_both_themes() -> None:
    """An unoccupied key should be the least eye-catching thing on the deck:
    dark on dark, light on light -- not a black hole punched in a white deck."""
    assert sum(DARK.empty_background) <= sum(DARK.field_quiet)
    assert sum(LIGHT.empty_background) > sum(LIGHT.field_quiet)
    assert sum(LIGHT.empty_background) > 600, "still light, just quieter"


# ---------------------------------------------------------- perceptual blending


def test_blending_reduces_to_plain_scaling_when_quiet_is_black() -> None:
    """The dark theme is very nearly this case, so the two must agree."""
    for level in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert blend_channel(0, 200, level) == round(200 * scale_factor(level))


def perceived(value: float) -> float:
    return float((value / 255) ** (1 / GAMMA))


def straight_line(quiet: int, full: int, level: float) -> float:
    """Where an evenly-spaced blend should land, in perceptual space."""
    low, high = perceived(quiet), perceived(full)
    return low + (high - low) * level


@pytest.mark.parametrize("ends", [(14, 58), (176, 250)], ids=("dark", "light"))
def test_blend_steps_are_perceptually_even(ends: tuple[int, int]) -> None:
    """Equal level steps must look equally spaced in both themes' ranges.

    The tolerance is one integer channel step: the curve is exact, but the
    result is rounded to a byte, and over a 44-value span that rounding is the
    dominant error.
    """
    quiet, full = ends
    for index in range(LEVELS):
        level = index / (LEVELS - 1)
        landed = perceived(blend_channel(quiet, full, level))
        assert abs(landed - straight_line(quiet, full, level)) < 0.005


def test_a_linear_blend_would_not_be_even() -> None:
    """Non-vacuity check for the test above: on the dark theme's range, simply
    interpolating the channel values drifts far outside that tolerance."""
    quiet, full = 14, 58
    worst = max(
        abs(
            perceived(quiet + (full - quiet) * (i / (LEVELS - 1)))
            - straight_line(quiet, full, i / (LEVELS - 1))
        )
        for i in range(LEVELS)
    )
    assert worst > 0.015


def test_blend_is_monotonic_across_the_ladder() -> None:
    values = [blend_channel(14, 58, i / (LEVELS - 1)) for i in range(LEVELS)]
    assert values == sorted(values)
    assert [b - a for a, b in pairwise(values)].count(0) < LEVELS // 2


def test_blend_hits_both_endpoints() -> None:
    assert blend_channel(176, 250, 0.0) == 176
    assert blend_channel(176, 250, 1.0) == 250


# -------------------------------------------------------------------- accents


def test_light_theme_darkens_marks_for_contrast() -> None:
    """Accents are tuned for a dark field; on white they would vanish."""
    near_white = (236, 236, 241)
    adapted = LIGHT.mark_color(near_white)
    assert max(adapted) <= LIGHT.mark_peak
    assert max(adapted) < max(near_white)


def test_adapted_marks_still_contrast_with_the_quietest_field() -> None:
    """Marks are never dimmed, so this contrast holds at every level -- but it
    has to be true against the quiet field, which is the closest they get."""
    adapted = LIGHT.mark_color((236, 236, 241))
    assert min(LIGHT.field_quiet) - max(adapted) > 40


def test_adapting_preserves_hue_order() -> None:
    """Hue identifies the agent, so the channel ordering must survive."""
    warm = (217, 119, 87)
    adapted = LIGHT.mark_color(warm)
    assert adapted[0] > adapted[1] > adapted[2]


def test_dark_theme_leaves_accents_alone() -> None:
    for color in ((217, 119, 87), (236, 236, 241), (120, 200, 160)):
        assert DARK.mark_color(color) == color


def test_already_dark_marks_are_not_brightened() -> None:
    dim = (40, 30, 20)
    assert LIGHT.mark_color(dim) == dim


def test_badge_contrasts_with_its_theme() -> None:
    for theme in (DARK, LIGHT):
        assert abs(sum(theme.badge_fill) - sum(theme.badge_text)) > 300


def test_theme_names_match_the_cli_choices() -> None:
    assert {t.value for t in ThemeName} == {"dark", "light"}
