"""Button surface abstraction.

Every import of the ``StreamDeck`` package lives in this module. It ships no
type stubs, so confining it here keeps the untyped surface to one file and lets
the rest of the codebase be checked strictly. It also means the daemon can run
against :class:`NullSurface` with no hardware attached, which is how the tests
and most of the development loop work.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeAlias, runtime_checkable

from .animation import LEVELS
from .theme import DARK, Theme

if TYPE_CHECKING:
    from collections.abc import Iterator

    from PIL import Image, ImageDraw, ImageFont

    # PIL returns different font classes depending on which loader worked.
    ImageFontLike: TypeAlias = ImageFont.FreeTypeFont | ImageFont.ImageFont
    ImageLike: TypeAlias = Image.Image
    ImageDrawLike: TypeAlias = ImageDraw.ImageDraw

logger = logging.getLogger(__name__)

RGB = tuple[int, int, int]


FONT_PATH = Path(__file__).parent / "fonts" / "DejaVuSansMono.ttf"
"""The one font, shipped with the package.

Monospace suits the subject -- these are terminal panes -- and it sidesteps a
practical problem with proportional faces at this size: kerning pairs that look
fine in running text read as uneven when the string is eight characters on a
72px key.

Bundling rather than searching the system removes a whole class of problem. A
search chain renders differently on every machine, degrades silently to a font
missing the glyphs it needs, and cannot be tested on a platform without going
and installing fonts there first. DejaVu Sans Mono specifically because it is
the only redistributable monospace font checked that carries U+2733, the
eight-spoked asterisk standing in for Claude's starburst -- JetBrains Mono,
Noto Sans Mono, Ubuntu Mono and Liberation Mono all lack it.

Licensing is in fonts/LICENSE-DejaVu.txt: Bitstream Vera, which permits
redistribution provided that notice ships alongside.
"""


@lru_cache(maxsize=64)
def load_font(size: int) -> ImageFontLike:
    """The bundled font at a given size, cached.

    Font loading dominated rendering before this: a full key render measured
    1.26 ms, against 0.03 ms for the JPEG encode alone.
    """
    from PIL import ImageFont

    try:
        return ImageFont.truetype(str(FONT_PATH), size)
    except OSError:
        # Only reachable if the package was installed without its data files.
        # A bitmap fallback keeps the deck lit, but it is fixed-size and lacks
        # most of the marks, so say so loudly rather than shipping tofu quietly.
        logger.warning("bundled font missing at %s; keys will render poorly", FONT_PATH)
        return ImageFont.load_default()


def fit_font(draw: ImageDrawLike, text: str, size: int, budget: float) -> ImageFontLike:
    """The largest font at or below ``size`` whose ``text`` fits in ``budget``.

    Marks carry a per-agent scale multiplier, but those are an intent -- "the
    single-letter ones want to be bigger" -- not a measurement, and a fixed
    multiplier cannot know the metrics of whichever font a machine resolved.
    Measuring instead means a long mark like ``copilot`` shrinks to fit rather
    than running off the key, on any font, without sixteen tuned constants.
    """
    while size > MIN_MARK_SIZE and draw.textlength(text, font=load_font(size)) > budget:
        size -= 1
    return load_font(size)


MIN_MARK_SIZE = 8
"""Below this a mark is unreadable, so overflow beats shrinking further."""


@lru_cache(maxsize=32)
def load_icon(path: Path, size: int) -> ImageLike | None:
    """A user-supplied icon, decoded once and cached square at ``size``."""
    from PIL import Image

    try:
        with Image.open(path) as handle:
            icon = handle.convert("RGBA")
    except (OSError, ValueError):
        logger.warning("could not read icon %s", path, exc_info=True)
        return None
    icon.thumbnail((size, size), Image.Resampling.LANCZOS)
    return icon


PressHandler = Callable[[int, bool], None]
"""Called with (key_index, pressed). ``pressed`` is False on release."""


@dataclass(frozen=True, slots=True)
class ButtonFace:
    """What a single key should show.

    A neutral field with the agent's mark centred, an optional status strip
    along the top edge, and the pane's name in a badge near the lower-right --
    offset from the corner rather than flush to it, echoing how Claude Code
    shows a session name inside its prompt box.

    Everything here except ``background`` is drawn identically at every
    luminance level. Only the field moves.
    """

    mark: str = ""
    """Agent glyph, e.g. the asterisk for Claude or the pi for pi."""

    mark_color: RGB = (228, 228, 231)
    mark_scale: float = 1.0

    icon: Path | None = None
    """User-supplied image replacing the glyph. See icons.resolve_override."""

    badge: str = ""
    status_color: RGB | None = None
    """Thin strip along the top edge. None draws no strip."""

    background: RGB = DARK.background
    """The field at full brightness. Quieter levels interpolate down from it;
    see Theme.field_at."""


@dataclass(frozen=True, slots=True)
class KeyFrames:
    """One key's face, pre-encoded at every luminance level.

    Animation writes bytes that already exist rather than re-rendering: a full
    render costs ~1.26 ms, so animating 15 keys at 20 fps un-cached would burn
    ~38% of a core. With this it is a dict lookup and a USB write.
    """

    face: ButtonFace
    frames: tuple[bytes, ...]

    def __len__(self) -> int:
        return len(self.frames)

    def at(self, level_index: int) -> bytes:
        """Bytes for a level, clamped to what was actually buffered."""
        if not self.frames:
            raise ValueError("no frames buffered")
        return self.frames[max(0, min(len(self.frames) - 1, level_index))]


class DeckDevice(Protocol):
    """The device methods this package uses.

    The StreamDeck package is annotated but ships no ``py.typed`` marker, so
    mypy sees it as untyped. Restating the surface we touch keeps our own code
    strictly checked instead of degrading to Any at the boundary. Signatures
    mirror ``StreamDeck.Devices.StreamDeck.StreamDeck``.
    """

    def open(self) -> None: ...

    def close(self) -> None: ...

    def reset(self) -> None: ...

    def is_open(self) -> bool: ...

    def key_count(self) -> int: ...

    def deck_type(self) -> str: ...

    def get_serial_number(self) -> str: ...

    def set_brightness(self, percent: int | float) -> None: ...

    def set_key_callback(
        self, callback: Callable[[DeckDevice, int, bool], None] | None
    ) -> None: ...

    def key_layout(self) -> tuple[int, int]: ...

    def set_key_image(self, key: int, image: bytes) -> None: ...


@runtime_checkable
class ButtonSurface(Protocol):
    """A grid of labelled, pressable keys."""

    theme: Theme
    """Field colours at both ends of the brightness range. See theme.Theme."""

    @property
    def key_count(self) -> int: ...

    @property
    def key_layout(self) -> tuple[int, int]: ...

    """(rows, columns). An MK.2 reports (3, 5)."""

    def open(self) -> None: ...

    def close(self) -> None: ...

    def set_face(self, index: int, face: ButtonFace) -> None: ...

    def render(self, face: ButtonFace) -> KeyFrames: ...

    def write(self, index: int, frames: KeyFrames, level_index: int) -> None: ...

    @property
    def levels(self) -> int: ...

    def set_press_handler(self, handler: PressHandler | None) -> None: ...


@dataclass
class NullSurface:
    """An in-memory surface. Records faces; presses can be injected.

    Used by the tests and by ``--no-device`` so the whole daemon can run
    without hardware.
    """

    key_count_: int = 15
    key_layout_: tuple[int, int] = (3, 5)
    theme: Theme = DARK
    faces: dict[int, ButtonFace] = field(default_factory=dict)
    shown: dict[int, int] = field(default_factory=dict)
    """Key index -> last level index written. Lets tests assert animation."""

    writes: int = 0
    opened: bool = False
    _handler: PressHandler | None = None

    @property
    def key_count(self) -> int:
        return self.key_count_

    @property
    def key_layout(self) -> tuple[int, int]:
        return self.key_layout_

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.opened = False

    @property
    def levels(self) -> int:
        return LEVELS

    def set_face(self, index: int, face: ButtonFace) -> None:
        self.write(index, self.render(face), LEVELS - 1)

    def render(self, face: ButtonFace) -> KeyFrames:
        # Content is what tests care about; the bytes only need to be distinct
        # per level so a missing or repeated write is detectable.
        return KeyFrames(
            face=face, frames=tuple(f"{id(face)}:{i}".encode() for i in range(LEVELS))
        )

    def write(self, index: int, frames: KeyFrames, level_index: int) -> None:
        if not 0 <= index < self.key_count:
            raise IndexError(f"key {index} out of range (0..{self.key_count - 1})")
        self.faces[index] = frames.face
        self.shown[index] = max(0, min(len(frames) - 1, level_index))
        self.writes += 1

    def set_press_handler(self, handler: PressHandler | None) -> None:
        self._handler = handler

    def press(self, index: int, *, pressed: bool = True) -> None:
        """Simulate a key press (test helper)."""
        if self._handler is not None:
            self._handler(index, pressed)


class StreamDeckSurface:
    """A physical Elgato Stream Deck, driven through hidapi.

    The vendor software is not required on any platform, and on macOS it must
    not be running -- it takes an exclusive claim on the HID device.
    """

    def __init__(
        self,
        *,
        serial: str | None = None,
        brightness: int = 60,
        theme: Theme = DARK,
    ) -> None:
        self.theme = theme
        self._serial = serial
        self._brightness = brightness
        self._deck: DeckDevice | None = None
        self._handler: PressHandler | None = None
        self._lock = threading.Lock()
        self._key_count = 0
        self._key_layout = (0, 0)

    @property
    def key_count(self) -> int:
        return self._key_count

    @property
    def key_layout(self) -> tuple[int, int]:
        return self._key_layout

    def open(self) -> None:
        from StreamDeck.DeviceManager import DeviceManager

        decks: list[DeckDevice] = DeviceManager().enumerate()
        if not decks:
            raise RuntimeError(
                "no Stream Deck found. On Linux check the udev rule and that the "
                "device is attached (under WSL, that means usbipd); on macOS quit "
                "the Elgato Stream Deck app, which holds the device exclusively."
            )

        chosen: DeckDevice | None = None
        if self._serial is None:
            chosen = decks[0]
        else:
            for candidate in decks:
                candidate.open()
                try:
                    if candidate.get_serial_number() == self._serial:
                        chosen = candidate
                        break
                finally:
                    if candidate is not chosen:
                        candidate.close()
            if chosen is None:
                raise RuntimeError(f"no Stream Deck with serial {self._serial!r}")

        if chosen.is_open() is False:
            chosen.open()
        chosen.reset()
        chosen.set_brightness(self._brightness)
        chosen.set_key_callback(self._on_key)

        self._deck = chosen
        self._key_count = int(chosen.key_count())
        rows, columns = chosen.key_layout()
        self._key_layout = (int(rows), int(columns))
        logger.info("opened Stream Deck: %d keys, %dx%d", self._key_count, rows, columns)

    def close(self) -> None:
        deck = self._deck
        if deck is None:
            return
        with self._lock:
            try:
                deck.reset()
                deck.close()
            except Exception:
                logger.warning("error while closing the deck", exc_info=True)
            finally:
                self._deck = None

    def set_press_handler(self, handler: PressHandler | None) -> None:
        self._handler = handler

    def _on_key(self, _deck: DeckDevice, key: int, pressed: bool) -> None:
        # Called on the library's own reader thread.
        handler = self._handler
        if handler is not None:
            handler(int(key), bool(pressed))

    def set_face(self, index: int, face: ButtonFace) -> None:
        deck = self._deck
        if deck is None:
            raise RuntimeError("deck is not open")
        self.write(index, self.render(face), self.levels - 1)

    @property
    def levels(self) -> int:
        return LEVELS

    def write(self, index: int, frames: KeyFrames, level_index: int) -> None:
        deck = self._deck
        if deck is None:
            raise RuntimeError("deck is not open")
        with self._lock:
            deck.set_key_image(index, frames.at(level_index))

    def render(self, face: ButtonFace) -> KeyFrames:
        """Encode the face at every luminance level.

        The expensive half -- fonts, text, the icon -- runs once inside
        ``key_frames``; per level all that remains is a fill, an alpha paste
        and a JPEG encode (measured 0.03 ms), so the whole ladder costs about
        one extra render.
        """
        from StreamDeck.ImageHelpers import PILHelper

        deck = self._deck
        if deck is None:
            raise RuntimeError("deck is not open")

        size = PILHelper.create_key_image(deck).size
        return KeyFrames(
            face=face,
            frames=tuple(
                PILHelper.to_native_key_format(deck, image)
                for image in key_frames(size, face, self.theme, self.levels)
            ),
        )


def key_frames(
    size: tuple[int, int], face: ButtonFace, theme: Theme, levels: int
) -> Iterator[ImageLike]:
    """Every luminance frame for a face: one foreground over many fields.

    This is the whole rendering pipeline, kept free of the device so it can be
    exercised without hardware. The foreground is composed once and reused
    unchanged, which is not just an optimisation -- it is the guarantee that
    marks and badges are equally legible at every level.
    """
    from PIL import Image

    overlay = compose_foreground(size, face, theme)
    for index in range(levels):
        field = theme.field_at(face.background, index / max(1, levels - 1))
        image = Image.new("RGB", size, field)
        image.paste(overlay, (0, 0), overlay)
        yield image


def compose_foreground(size: tuple[int, int], face: ButtonFace, theme: Theme) -> ImageLike:
    """Everything drawn *over* the field, on a transparent layer.

    RGBA rather than a flattened image so antialiased glyph edges blend against
    whichever field they end up on -- a foreground baked against one background
    would show a halo on the others.
    """
    from PIL import Image, ImageDraw

    layer: ImageLike = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    width, height = size

    # Status along the top edge, so the field itself stays neutral while agent
    # state is still readable across the whole deck at a glance.
    if face.status_color is not None:
        draw.rectangle((0, 0, width, max(3, height // 18)), fill=face.status_color)

    # A user-supplied icon replaces the glyph entirely; falling back to the
    # glyph when it cannot be read keeps a broken PNG from blanking a key.
    drew_icon = False
    if face.icon is not None:
        icon = load_icon(face.icon, int(height * 0.52))
        if icon is not None:
            origin = (
                (width - icon.width) // 2,
                int((height - icon.height) // 2 - height * 0.06),
            )
            layer.paste(icon, origin, icon)
            drew_icon = True

    if face.mark and not drew_icon:
        nominal = max(MIN_MARK_SIZE, int(height * 0.36 * face.mark_scale))
        mark_font = fit_font(draw, face.mark, nominal, width * 0.84)
        draw.text(
            (width / 2, height / 2 - height * 0.06),
            face.mark,
            font=mark_font,
            anchor="mm",
            fill=face.mark_color,
        )

    if face.badge:
        _draw_badge(draw, face.badge, width, height, theme)

    return layer


def _draw_badge(draw: ImageDrawLike, text: str, width: int, height: int, theme: Theme) -> None:
    """Name badge inset from the lower-right corner."""
    font = load_font(max(8, int(height * 0.15)))

    # Trim to what actually fits rather than letting it run off the key.
    margin = int(width * 0.07)
    usable = width - 2 * margin - 6
    label = text
    while label and draw.textlength(label, font=font) > usable:
        label = label[:-1]
    if not label:
        return
    if label != text and len(label) > 1:
        label = label[:-1] + "…"

    text_width = draw.textlength(label, font=font)
    pad_x, pad_y = 4, 2
    right = width - margin
    bottom = height - margin
    left = right - text_width - 2 * pad_x
    top = bottom - int(height * 0.15) - 2 * pad_y

    draw.rounded_rectangle((left, top, right, bottom), radius=3, fill=theme.badge_fill)
    draw.text(
        ((left + right) / 2, (top + bottom) / 2),
        label,
        font=font,
        anchor="mm",
        fill=theme.badge_text,
    )


def open_surface(
    *,
    use_device: bool = True,
    serial: str | None = None,
    theme: Theme = DARK,
) -> ButtonSurface:
    """Return a real deck when asked for one, otherwise an in-memory surface."""
    if not use_device:
        return NullSurface(theme=theme)
    return StreamDeckSurface(serial=serial, theme=theme)
