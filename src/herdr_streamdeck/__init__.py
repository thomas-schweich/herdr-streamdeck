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
from .icons import AgentMark, mark_for
from .layout import Grid, Group, GroupingMode, GroupKey, Pane, build_columns
from .protocol import Event, HerdrError, ProtocolError, Response, subscription

__version__ = "0.1.0"

__all__ = [
    "AgentMark",
    "ButtonFace",
    "ButtonSurface",
    "ConnectionClosed",
    "Event",
    "Grid",
    "Group",
    "GroupKey",
    "GroupingMode",
    "HerdrClient",
    "HerdrError",
    "HerdrSession",
    "NullSurface",
    "Pane",
    "ProtocolError",
    "Response",
    "SingleUseViolation",
    "StreamDeckSurface",
    "__version__",
    "build_columns",
    "default_socket_path",
    "mark_for",
    "open_surface",
    "request_once",
    "subscription",
]
