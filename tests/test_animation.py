"""Animation tests.

The load-bearing property is **synchronisation**: phase must derive from the
shared clock alone, never from when a pane entered its state. Several of these
exist to fail if someone reintroduces per-key phase.
"""

from __future__ import annotations

import math
from itertools import pairwise

import pytest

from herdr_streamdeck.animation import (
    GAMMA,
    LEGIBLE_FLOOR,
    LEVELS,
    Animation,
    Waveform,
    animation_for,
    frame_index,
    quantise,
    scale_factor,
)

WORKING = animation_for("working")
BLOCKED = animation_for("blocked")


# ------------------------------------------------------------- status mapping


def test_each_status_gets_the_requested_behaviour() -> None:
    idle = animation_for("idle")
    assert idle.waveform is Waveform.STEADY
    assert idle.high < 1.0, "idle should be dimmer than done"

    assert animation_for("working").waveform is Waveform.PULSE

    done = animation_for("done")
    assert done.waveform is Waveform.STEADY
    assert done.high == 1.0, "done should be full brightness"

    blocked = animation_for("blocked")
    assert blocked.waveform is Waveform.BLINK
    assert blocked.high == 1.0
    assert blocked.low < blocked.high, "blocked should swing, not sit"


def test_everything_meant_to_be_read_stays_legible() -> None:
    """Below the measured floor a mark cannot be read, so nothing readable
    may sit there. LEGIBLE_FLOOR came from a ramp shown on the device."""
    for status in ("idle", "working", "done", "blocked"):
        animation = animation_for(status)
        assert animation.low >= LEGIBLE_FLOOR, f"{status} dips below legibility"
        assert animation.high >= LEGIBLE_FLOOR


def test_working_never_looks_dimmer_than_idle() -> None:
    """Otherwise the bottom of each breath inverts the meaning."""
    assert animation_for("working").low >= animation_for("idle").high


def test_blocked_swings_wider_than_working_pulses() -> None:
    """Blocked needs to grab attention that a working pulse does not."""
    working = animation_for("working")
    blocked = animation_for("blocked")
    assert (blocked.high - blocked.low) > (working.high - working.low)


def test_unknown_status_is_steady_but_still_legible() -> None:
    """A pane with no detected agent is a terminal, and switching to a shell
    is a normal thing to want -- so it stays readable rather than being
    dimmed into an absence."""
    unknown = animation_for("nonsense")
    assert unknown.waveform is Waveform.STEADY
    assert unknown.high >= LEGIBLE_FLOOR
    assert unknown.high < 1.0, "still quieter than an agent that finished"


def test_working_and_blocked_use_different_periods() -> None:
    """Same rate would be hard to tell apart in peripheral vision."""
    assert WORKING.period != BLOCKED.period


# ----------------------------------------------------------------- waveforms


def test_steady_ignores_time() -> None:
    steady = animation_for("done")
    assert not steady.animated
    assert {steady.level_at(t) for t in (0.0, 1.7, 900.0)} == {1.0}


def test_pulse_starts_bright_and_reaches_both_extremes() -> None:
    """Starting at the trough would make a pane fade in when it starts work."""
    assert WORKING.level_at(0.0) == pytest.approx(WORKING.high)
    assert WORKING.level_at(WORKING.period / 2) == pytest.approx(WORKING.low)
    assert WORKING.level_at(WORKING.period) == pytest.approx(WORKING.high)


def test_pulse_stays_within_bounds() -> None:
    samples = [WORKING.level_at(t / 100) for t in range(1000)]
    assert min(samples) >= WORKING.low - 1e-9
    assert max(samples) <= WORKING.high + 1e-9


def test_pulse_is_smooth() -> None:
    """No step should be large enough to read as a jump at 20 fps."""
    step = 1 / 20
    levels = [WORKING.level_at(i * step) for i in range(int(WORKING.period / step) + 1)]
    deltas = [abs(b - a) for a, b in pairwise(levels)]
    assert max(deltas) < 0.12


def test_blink_is_square_between_its_bounds() -> None:
    assert BLOCKED.level_at(0.0) == pytest.approx(BLOCKED.high)
    assert BLOCKED.level_at(BLOCKED.period * 0.75) == pytest.approx(BLOCKED.low)
    assert set(BLOCKED.level_at(t / 97) for t in range(400)) <= {BLOCKED.low, BLOCKED.high}


# ----------------------------------------------------------- synchronisation


def test_phase_depends_only_on_the_shared_clock() -> None:
    """Two panes that started working seconds apart must pulse together.

    Both evaluate the same elapsed time, so they cannot diverge -- this fails
    if phase is ever made relative to a per-key start.
    """
    for elapsed in (0.0, 0.37, 1.9, 12.5, 611.25):
        assert WORKING.level_at(elapsed) == WORKING.level_at(elapsed)


def test_identical_animations_agree_at_every_instant() -> None:
    a = animation_for("working")
    b = Animation(Waveform.PULSE, low=a.low, high=a.high, period=a.period)
    for step in range(200):
        elapsed = step * 0.05
        assert a.level_at(elapsed) == pytest.approx(b.level_at(elapsed))


def test_pulse_repeats_exactly_one_period_later() -> None:
    for elapsed in (0.0, 0.4, 1.1, 2.0):
        assert WORKING.level_at(elapsed) == pytest.approx(
            WORKING.level_at(elapsed + WORKING.period)
        )


# -------------------------------------------------------------- quantisation


def test_quantise_spans_the_frame_range() -> None:
    assert quantise(0.0) == 0
    assert quantise(1.0) == LEVELS - 1
    assert 0 < quantise(0.5) < LEVELS - 1


def test_quantise_clamps_out_of_range_input() -> None:
    assert quantise(-5.0) == 0
    assert quantise(9.0) == LEVELS - 1


def test_quantise_rejects_a_meaningless_level_count() -> None:
    with pytest.raises(ValueError, match="positive"):
        quantise(0.5, levels=0)


def test_granularity_is_fine_enough_for_a_slow_pulse() -> None:
    """Banding would be visible if a pulse used only a handful of frames."""
    step = 1 / 20
    frames = {frame_index(WORKING, i * step) for i in range(int(WORKING.period / step) + 1)}
    assert len(frames) >= 12, f"only {len(frames)} distinct frames across a pulse"


def test_frame_index_is_stable_for_a_steady_animation() -> None:
    steady = animation_for("idle")
    assert len({frame_index(steady, t / 10) for t in range(100)}) == 1


# ----------------------------------------------------------------- perceptual


def test_scale_factor_is_gamma_corrected() -> None:
    """Half perceptual brightness is far less than half the pixel value."""
    assert scale_factor(1.0) == pytest.approx(1.0)
    assert scale_factor(0.0) == pytest.approx(0.0)
    assert scale_factor(0.5) == pytest.approx(0.5**GAMMA)
    assert scale_factor(0.5) < 0.3, "a linear 0.5 would look far too bright"


def test_scale_factor_is_monotonic_and_clamped() -> None:
    values = [scale_factor(i / 100) for i in range(101)]
    assert values == sorted(values)
    assert scale_factor(-1.0) == 0.0
    assert scale_factor(2.0) == 1.0


def test_perceptual_steps_are_even() -> None:
    """The point of gamma correction: equal level steps look equally spaced."""
    perceived = [scale_factor(i / (LEVELS - 1)) ** (1 / GAMMA) for i in range(LEVELS)]
    deltas = [b - a for a, b in pairwise(perceived)]
    assert max(deltas) - min(deltas) < 1e-6


def test_pulse_matches_a_cosine_in_perceptual_space() -> None:
    for step in range(24):
        elapsed = step * WORKING.period / 24
        phase = (elapsed % WORKING.period) / WORKING.period
        expected = WORKING.low + (WORKING.high - WORKING.low) * (
            (math.cos(2 * math.pi * phase) + 1) / 2
        )
        assert WORKING.level_at(elapsed) == pytest.approx(expected)
