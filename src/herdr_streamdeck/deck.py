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
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

RGB = tuple[int, int, int]

PressHandler = Callable[[int, bool], None]
"""Called with (key_index, pressed). ``pressed`` is False on release."""


@dataclass(frozen=True, slots=True)
class ButtonFace:
    """What a single key should show."""

    label: str = ""
    sublabel: str = ""
    color: RGB = (24, 24, 27)
    text_color: RGB = (250, 250, 250)


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

    def set_key_image(self, key: int, image: bytes) -> None: ...


@runtime_checkable
class ButtonSurface(Protocol):
    """A grid of labelled, pressable keys."""

    @property
    def key_count(self) -> int: ...

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
    faces: dict[int, ButtonFace] = field(default_factory=dict)
    opened: bool = False
    _handler: PressHandler | None = None

    @property
    def key_count(self) -> int:
        return self.key_count_

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

    @property
    def key_count(self) -> int:
        return self._key_count

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
        logger.info("opened Stream Deck with %d keys", self._key_count)

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

    def _render(self, deck: DeckDevice, face: ButtonFace) -> bytes:
        from PIL import ImageDraw, ImageFont
        from StreamDeck.ImageHelpers import PILHelper

        source = PILHelper.create_key_image(deck, background=face.color)
        draw = ImageDraw.Draw(source)

        font: ImageFont.FreeTypeFont | ImageFont.ImageFont
        small: ImageFont.FreeTypeFont | ImageFont.ImageFont
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 14)
            small = ImageFont.truetype("DejaVuSans.ttf", 11)
        except OSError:
            # Bundled bitmap fallback; ugly but never missing.
            font = ImageFont.load_default()
            small = font

        width, height = source.size
        if face.label:
            draw.text(
                (width / 2, height / 2 - (7 if face.sublabel else 0)),
                face.label,
                font=font,
                anchor="mm",
                fill=face.text_color,
            )
        if face.sublabel:
            draw.text(
                (width / 2, height / 2 + 10),
                face.sublabel,
                font=small,
                anchor="mm",
                fill=face.text_color,
            )

        # to_native_format is deprecated since 0.9.5 and already returns bytes.
        native: bytes = PILHelper.to_native_key_format(deck, source)
        return native


def open_surface(*, use_device: bool = True, serial: str | None = None) -> ButtonSurface:
    """Return a real deck when asked for one, otherwise an in-memory surface."""
    if not use_device:
        return NullSurface()
    return StreamDeckSurface(serial=serial)
