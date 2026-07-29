"""Theme tests.

The subtle requirement is the *direction* of dimming. Brightness carries agent
status, and dimming toward black is correct only on a dark field. On white it
turns quiet keys muddy grey and crushes the dark mark into the background it
exists to contrast with.
"""

from __future__ import annotations

import pytest

from herdr_streamdeck.animation import LEVELS
from herdr_streamdeck.deck import _brightness_lut
from herdr_streamdeck.theme import DARK, LIGHT, ThemeName, theme_for


def test_themes_resolve_by_name() -> None:
    assert theme_for("dark") is DARK
    assert theme_for("light") is LIGHT
    with pytest.raises(ValueError):
        theme_for("chartreuse")


def test_backgrounds_are_actually_opposite() -> None:
    assert sum(DARK.background) < 200, "dark field should be dark"
    assert sum(LIGHT.background) > 600, "light field should be near-white"


def test_each_theme_dims_toward_its_own_field() -> None:
    """The whole point: quiet keys fade into the background, not across it."""
    assert DARK.dim_target == 0
    assert LIGHT.dim_target == 255


def test_dimming_moves_toward_the_target_not_always_down() -> None:
    mid = 128
    quiet = LEVELS // 3

    darker = _brightness_lut(quiet, LEVELS, DARK.dim_target)[mid]
    lighter = _brightness_lut(quiet, LEVELS, LIGHT.dim_target)[mid]

    assert darker < mid, "dark theme dims toward black"
    assert lighter > mid, "light theme dims toward white"


def test_full_brightness_is_identity_in_both_themes() -> None:
    top = LEVELS - 1
    for target in (DARK.dim_target, LIGHT.dim_target):
        lut = _brightness_lut(top, LEVELS, target)
        assert lut[0] == 0 and lut[255] == 255 and lut[128] == 128


def test_dimming_stays_in_range() -> None:
    for target in (DARK.dim_target, LIGHT.dim_target):
        for level in range(LEVELS):
            lut = _brightness_lut(level, LEVELS, target)
            assert min(lut) >= 0 and max(lut) <= 255


def test_a_quiet_light_key_does_not_go_dark() -> None:
    """The failure this replaced: white backgrounds turning to grey mud."""
    quiet = _brightness_lut(LEVELS // 3, LEVELS, LIGHT.dim_target)
    assert quiet[LIGHT.background[0]] > 200, "light field must stay light when quiet"


def test_light_theme_darkens_marks_for_contrast() -> None:
    """Accents are tuned for a dark field; on white they would vanish."""
    near_white = (236, 236, 241)
    adapted = LIGHT.mark_color(near_white)
    assert max(adapted) <= LIGHT.mark_peak
    assert max(adapted) < max(near_white)


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


def test_empty_keys_are_least_conspicuous_in_both_themes() -> None:
    """An unoccupied key should recede, which means dark on dark and light
    on light -- not a black hole punched in a white deck."""
    assert sum(DARK.empty_background) < sum(DARK.background)
    assert sum(LIGHT.empty_background) < sum(LIGHT.background)
    assert sum(LIGHT.empty_background) > 500, "still light, just quieter"


def test_badge_contrasts_with_its_theme() -> None:
    for theme in (DARK, LIGHT):
        assert abs(sum(theme.badge_fill) - sum(theme.badge_text)) > 300


def test_theme_names_match_the_cli_choices() -> None:
    assert {t.value for t in ThemeName} == {"dark", "light"}
