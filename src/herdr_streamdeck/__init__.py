"""Drive an Elgato Stream Deck from herdr's socket API."""

from .client import (
    ConnectionClosed,
    HerdrClient,
    HerdrSession,
    SingleUseViolation,
    default_socket_path,
    request_once,
)
from .deck import ButtonFace, ButtonSurface, NullSurface, StreamDeckSurface, open_surface
from .protocol import Event, HerdrError, ProtocolError, Response, subscription

__version__ = "0.1.0"

__all__ = [
    "ButtonFace",
    "ButtonSurface",
    "ConnectionClosed",
    "Event",
    "HerdrClient",
    "HerdrError",
    "HerdrSession",
    "NullSurface",
    "ProtocolError",
    "Response",
    "SingleUseViolation",
    "StreamDeckSurface",
    "__version__",
    "default_socket_path",
    "open_surface",
    "request_once",
    "subscription",
]
