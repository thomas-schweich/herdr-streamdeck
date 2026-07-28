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
from typing import TYPE_CHECKING, Protocol, TypeAlias, runtime_checkable

if TYPE_CHECKING:
    from PIL import ImageFont

    # PIL returns different font classes depending on which loader worked.
    ImageFontLike: TypeAlias = ImageFont.FreeTypeFont | ImageFont.ImageFont

logger = logging.getLogger(__name__)

RGB = tuple[int, int, int]

PressHandler = Callable[[int, bool], None]
"""Called with (key_index, pressed). ``pressed`` is False on release."""


BACKGROUND: RGB = (38, 38, 42)
"""Neutral grey field. The mark and the status strip carry the colour."""

BADGE_FILL: RGB = (72, 72, 80)
BADGE_TEXT: RGB = (236, 236, 241)


@dataclass(frozen=True, slots=True)
class ButtonFace:
    """What a single key should show.

    A neutral field with the agent's mark centred, an optional status strip
    along the top edge, and the pane's name in a badge near the lower-right --
    offset from the corner rather than flush to it, echoing how Claude Code
    shows a session name inside its prompt box.
    """

    mark: str = ""
    """Agent glyph, e.g. the asterisk for Claude or the pi for pi."""

    mark_color: RGB = (228, 228, 231)
    mark_scale: float = 1.0
    badge: str = ""
    status_color: RGB | None = None
    """Thin strip along the top edge. None draws no strip."""

    background: RGB = BACKGROUND


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

    @property
    def key_count(self) -> int: ...

    @property
    def key_layout(self) -> tuple[int, int]: ...

    """(rows, columns). An MK.2 reports (3, 5)."""

    def open(self) -> None: ...

    def close(self) -> None: ...

    def set_face(self, index: int, face: ButtonFace) -> None: ...

    def set_press_handler(self, handler: PressHandler | None) -> None: ...


@dataclass
class NullSurface:
    """An in-memory surface. Records faces; presses can be injected.

    Used by the tests and by ``--no-device`` so the whole daemon can run
    without hardware.
    """

    key_count_: int = 15
    key_layout_: tuple[int, int] = (3, 5)
    faces: dict[int, ButtonFace] = field(default_factory=dict)
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

    def set_face(self, index: int, face: ButtonFace) -> None:
        if not 0 <= index < self.key_count:
            raise IndexError(f"key {index} out of range (0..{self.key_count - 1})")
        self.faces[index] = face

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

    def __init__(self, *, serial: str | None = None, brightness: int = 60) -> None:
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
        image = self._render(deck, face)
        with self._lock:
            deck.set_key_image(index, image)

    @staticmethod
    def _font(size: int) -> ImageFontLike:
        from PIL import ImageFont

        for name in ("DejaVuSans.ttf", "Arial Unicode.ttf", "Helvetica.ttc"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        # Bundled bitmap fallback: ugly, fixed-size, but never missing. Every
        # glyph in icons.MARKS is verified present in DejaVu, so this path is
        # only reached on a system with no usable TrueType font at all.
        return ImageFont.load_default()

    def _render(self, deck: DeckDevice, face: ButtonFace) -> bytes:
        from PIL import ImageDraw
        from StreamDeck.ImageHelpers import PILHelper

        source = PILHelper.create_key_image(deck, background=face.background)
        draw = ImageDraw.Draw(source)
        width, height = source.size

        # Status along the top edge, so the neutral field stays neutral while
        # agent state is still readable across the whole deck at a glance.
        if face.status_color is not None:
            draw.rectangle((0, 0, width, max(3, height // 18)), fill=face.status_color)

        if face.mark:
            mark_font = self._font(max(9, int(height * 0.36 * face.mark_scale)))
            draw.text(
                (width / 2, height / 2 - height * 0.06),
                face.mark,
                font=mark_font,
                anchor="mm",
                fill=face.mark_color,
            )

        if face.badge:
            self._draw_badge(draw, face.badge, width, height)

        # to_native_format is deprecated since 0.9.5; this already returns bytes.
        native: bytes = PILHelper.to_native_key_format(deck, source)
        return native

    def _draw_badge(self, draw: object, text: str, width: int, height: int) -> None:
        """Name badge inset from the lower-right corner."""
        from PIL import ImageDraw

        assert isinstance(draw, ImageDraw.ImageDraw)
        font = self._font(max(8, int(height * 0.15)))

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

        draw.rounded_rectangle((left, top, right, bottom), radius=3, fill=BADGE_FILL)
        draw.text(
            ((left + right) / 2, (top + bottom) / 2),
            label,
            font=font,
            anchor="mm",
            fill=BADGE_TEXT,
        )


def open_surface(*, use_device: bool = True, serial: str | None = None) -> ButtonSurface:
    """Return a real deck when asked for one, otherwise an in-memory surface."""
    if not use_device:
        return NullSurface()
    return StreamDeckSurface(serial=serial)
