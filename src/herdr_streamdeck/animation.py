"""Brightness animation, evaluated against a clock shared by every key.

Each agent status maps to a waveform:

===========  ===========================================================
idle         dim and steady
working      slow pulse
done         full brightness
blocked      blink between half and full
===========  ===========================================================

A level here drives the **field only** -- see :mod:`.theme`. Marks and badges
are drawn at full strength in every frame, so nothing that has to be read rides
on these numbers and they are free to use the whole range. An earlier version
dimmed the entire key, which forced a measured floor (0.66) below which a glyph
became unreadable, and squeezed all four states into the top third of the
scale. Holding the foreground still removed that constraint, so `idle` can now
go all the way down and `blocked` can blink at the half-and-full the brief
originally asked for.

**Phase comes from absolute elapsed time, never from when a pane entered its
state.** That is what makes pulsing synchronised: two panes that start working
a second apart still pulse together, because both evaluate the same clock. An
animation that started its own phase on entry would leave the deck shimmering
out of step, which reads as noise rather than status.

Levels are *perceptual*. Human brightness perception is roughly a power law, so
scaling pixels linearly makes the bottom of a pulse fall away far faster than
the top -- the pulse looks lopsided. :func:`blend_channel` interpolates in
perceptual space, so a cosine in level space actually looks like one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

LEVELS = 48
"""Quantisation steps. Fine enough that a slow pulse has no visible banding,
small enough that the prebuffer stays cheap: 48 x ~730-byte JPEGs per key."""

GAMMA = 2.2
"""Display gamma. Perceptual level p is produced by multiplying by p**GAMMA.

Validated on hardware: a 15-key ramp evenly spaced in perceptual space read as
evenly spaced to the eye, so this exponent matches the panel. If a ramp ever
looks bunched at one end, this is the number to change -- not the levels."""


class Waveform(StrEnum):
    STEADY = "steady"
    PULSE = "pulse"
    BLINK = "blink"


@dataclass(frozen=True, slots=True)
class Animation:
    """A brightness envelope in perceptual space.

    ``low`` and ``high`` are perceptual levels in 0..1; ``period`` is seconds
    for a full cycle and is ignored when steady.
    """

    waveform: Waveform = Waveform.STEADY
    low: float = 1.0
    high: float = 1.0
    period: float = 1.0

    @property
    def animated(self) -> bool:
        """Whether this needs redrawing over time."""
        return self.waveform is not Waveform.STEADY and self.low != self.high

    def level_at(self, elapsed: float) -> float:
        """Perceptual level at ``elapsed`` seconds on the shared clock."""
        if not self.animated:
            return self.high

        phase = (elapsed % self.period) / self.period
        if self.waveform is Waveform.PULSE:
            # Cosine rather than sine so the cycle starts at `high`: a pane
            # that begins working lights up immediately instead of fading in
            # from the trough.
            eased = (math.cos(2 * math.pi * phase) + 1) / 2
        else:  # BLINK
            eased = 1.0 if phase < 0.5 else 0.0
        return self.low + (self.high - self.low) * eased


# Deliberately distinct periods: a working pulse and a blocked blink running at
# the same rate would be hard to tell apart in peripheral vision, which is
# where a deck is actually read.
QUIET = 0.0
"""The bottom of the field range: a pane that wants no attention at all.

Bottoming out is safe now that only the field moves -- the mark stays fully lit
on top of it, so the key still reads as occupied.
"""

STATUS_ANIMATIONS: dict[str, Animation] = {
    "idle": Animation(Waveform.STEADY, low=QUIET, high=QUIET),
    # The trough sits above idle, so a working pane never looks *quieter* than
    # an idle one -- which would invert the meaning at the bottom of each breath.
    "working": Animation(Waveform.PULSE, low=0.30, high=1.0, period=2.4),
    "done": Animation(Waveform.STEADY, low=1.0, high=1.0),
    # Half and full, as originally asked for. A previous version could not use
    # 0.5 because a mark dimmed that far was unreadable; the field has no such
    # problem. What makes this grab attention is the hard edge and the rate,
    # not the size of the swing -- it is the only square wave on the deck.
    "blocked": Animation(Waveform.BLINK, low=0.5, high=1.0, period=1.0),
}

# A pane with no detected agent is a terminal. It is idle by nature -- there is
# no agent to be busy -- so it gets the idle field, and is told apart by its
# `$_` mark and the absence of a status strip rather than by brightness.
UNKNOWN_ANIMATION = Animation(Waveform.STEADY, low=QUIET, high=QUIET)
EMPTY_ANIMATION = Animation(Waveform.STEADY, high=1.0)
"""Empty keys hold their own face colour; the field must not move under them."""


def animation_for(status: str) -> Animation:
    return STATUS_ANIMATIONS.get(status, UNKNOWN_ANIMATION)


def scale_factor(level: float) -> float:
    """Pixel multiplier producing a given perceptual level.

    Perceived brightness of a linear scale by ``k`` goes roughly as
    ``k ** (1 / GAMMA)``, so to land on perceptual ``p`` the multiplier must be
    ``p ** GAMMA``.
    """
    clamped = max(0.0, min(1.0, level))
    return float(clamped**GAMMA)


def blend_channel(quiet: int, full: int, level: float) -> int:
    """Interpolate two channel values evenly in *perceived* brightness.

    A plain linear blend crowds the visible change at one end, because
    perception goes roughly as ``(v / 255) ** (1 / GAMMA)``. Converting both
    ends into perceptual space, interpolating there, and converting back makes
    equal level steps look equally spaced.

    Reduces exactly to ``full * scale_factor(level)`` when ``quiet`` is black,
    which is the case the dark theme is very nearly in.
    """
    clamped = max(0.0, min(1.0, level))
    low = float((quiet / 255) ** (1 / GAMMA))
    high = float((full / 255) ** (1 / GAMMA))
    return round(255 * float((low + (high - low) * clamped) ** GAMMA))


def quantise(level: float, levels: int = LEVELS) -> int:
    """Snap a perceptual level to a prebuffered frame index."""
    if levels < 1:
        raise ValueError("levels must be positive")
    clamped = max(0.0, min(1.0, level))
    return min(levels - 1, round(clamped * (levels - 1)))


def frame_index(animation: Animation, elapsed: float, levels: int = LEVELS) -> int:
    """The prebuffered frame to show for an animation at a given time."""
    return quantise(animation.level_at(elapsed), levels)
