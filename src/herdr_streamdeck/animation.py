"""Brightness animation, evaluated against a clock shared by every key.

Each agent status maps to a waveform:

===========  ===========================================================
idle         dim and steady
working      slow pulse
done         full brightness
blocked      blink between half and full
===========  ===========================================================

**Phase comes from absolute elapsed time, never from when a pane entered its
state.** That is what makes pulsing synchronised: two panes that start working
a second apart still pulse together, because both evaluate the same clock. An
animation that started its own phase on entry would leave the deck shimmering
out of step, which reads as noise rather than status.

Levels are *perceptual*. Human brightness perception is roughly a power law, so
scaling pixels linearly makes the bottom of a pulse fall away far faster than
the top -- the pulse looks lopsided. :func:`scale_factor` converts a perceptual
level into the multiplier that produces it, so a sine in perceptual space
actually looks like a sine.
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

LEGIBLE_FLOOR = 0.66
"""Dimmest level at which a mark stays readable.

Measured, not guessed: a 15-key ramp from 0.20 to 1.00 was shown on an MK.2 at
60% device brightness in a dimly lit room, and key 9 (0.657) was the dimmest
still comfortably legible. Everything meant to be *read* sits at or above this;
below it a key is effectively off. Re-measure if the device brightness setting
changes materially, since the floor moves with it."""


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
STATUS_ANIMATIONS: dict[str, Animation] = {
    # Dim, but at the measured floor: idle panes still have to be readable.
    "idle": Animation(Waveform.STEADY, low=LEGIBLE_FLOOR, high=LEGIBLE_FLOOR),
    # Troughs above idle, so a working pane never looks *dimmer* than an idle
    # one -- which would invert the meaning at the bottom of each breath.
    "working": Animation(Waveform.PULSE, low=0.74, high=1.0, period=2.4),
    "done": Animation(Waveform.STEADY, low=1.0, high=1.0),
    # The original brief said "blink between 50% and 100%", but a perceptual
    # 0.5 measured as effectively black on this panel. Swinging the full
    # legible band keeps the intent -- a pronounced half/full blink -- while
    # both states stay readable.
    "blocked": Animation(Waveform.BLINK, low=LEGIBLE_FLOOR, high=1.0, period=1.0),
}

# A pane with no detected agent shows only a dot, so it can sit below the
# legibility floor: there is nothing there worth reading.
UNKNOWN_ANIMATION = Animation(Waveform.STEADY, low=0.45, high=0.45)
EMPTY_ANIMATION = Animation(Waveform.STEADY, high=1.0)
"""Empty keys are already dark by their face; do not dim them further."""


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


def quantise(level: float, levels: int = LEVELS) -> int:
    """Snap a perceptual level to a prebuffered frame index."""
    if levels < 1:
        raise ValueError("levels must be positive")
    clamped = max(0.0, min(1.0, level))
    return min(levels - 1, round(clamped * (levels - 1)))


def frame_index(animation: Animation, elapsed: float, levels: int = LEVELS) -> int:
    """The prebuffered frame to show for an animation at a given time."""
    return quantise(animation.level_at(elapsed), levels)
