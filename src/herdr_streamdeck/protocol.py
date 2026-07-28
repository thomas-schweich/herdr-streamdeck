"""Wire protocol for herdr's Unix-socket API.

The transport is newline-delimited JSON. Two kinds of message travel back from
the server on the same connection:

    response  {"id": "7", "result": {...}}   or   {"id": "7", "error": {...}}
    event     {"event": "pane_closed", "data": {...}}

Responses carry the caller-chosen ``id`` of the request they answer; events
never carry an id. That difference is the whole demultiplexing rule, which is
what lets one connection serve both request/response and a live event stream.

Verified against herdr 0.7.5, protocol 17.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, TypeAlias

JSONValue: TypeAlias = (
    bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"] | None
)
JSONObject: TypeAlias = dict[str, JSONValue]

PROTOCOL_VERSION: Final = 17

# Subscriptions that are scoped to a single pane. Unlike every other
# subscription these require a ``pane_id`` in the subscription object, and the
# server rejects the whole events.subscribe call if it is missing. They are
# also the only events delivered under their dotted name -- see
# canonical_event_kind below.
PANE_SCOPED_SUBSCRIPTIONS: Final[frozenset[str]] = frozenset(
    {
        "pane.output_matched",
        "pane.agent_status_changed",
        "pane.scroll_changed",
    }
)


class HerdrError(Exception):
    """An ``error`` response from the server."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ProtocolError(Exception):
    """A message that does not conform to the wire format."""


@dataclass(frozen=True, slots=True)
class Response:
    """A reply to a request, correlated by ``id``."""

    id: str
    result: JSONObject | None
    error: HerdrError | None

    def unwrap(self) -> JSONObject:
        """Return the result, raising if the server reported an error."""
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise ProtocolError(f"response {self.id!r} has neither result nor error")
        return self.result


@dataclass(frozen=True, slots=True)
class Event:
    """An unsolicited subscription event."""

    kind: str
    """Canonical dotted form, e.g. ``pane.agent_status_changed``."""

    raw_kind: str
    """Exactly as it appeared on the wire, dotted or underscored."""

    data: JSONObject


def canonical_event_kind(wire_kind: str) -> str:
    """Normalise an event name to the dotted form used when subscribing.

    herdr is asymmetric here, and it trips people up: you subscribe with
    ``pane.closed`` but the event arrives as ``pane_closed``. The pane-scoped
    events are the exception -- they arrive already dotted.

    Replacing only the *first* underscore handles both, since the namespace is
    always a single leading segment::

        pane_closed                 -> pane.closed
        workspace_metadata_updated  -> workspace.metadata_updated
        pane.agent_status_changed   -> pane.agent_status_changed  (unchanged)
    """
    if "." in wire_kind:
        return wire_kind
    return wire_kind.replace("_", ".", 1)


def encode_request(request_id: str, method: str, params: JSONObject | None = None) -> bytes:
    """Serialise one request, including the trailing newline."""
    payload: JSONObject = {"id": request_id, "method": method, "params": params or {}}
    # separators avoids incidental whitespace; the server reads line-at-a-time
    # and has a maximum request line length.
    return json.dumps(payload, separators=(",", ":")).encode() + b"\n"


def decode_message(line: bytes) -> Response | Event:
    """Parse one line from the server into a response or an event."""
    try:
        parsed: object = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"malformed JSON: {line!r}") from exc

    if not isinstance(parsed, dict):
        raise ProtocolError(f"expected a JSON object, got {type(parsed).__name__}")

    message: JSONObject = parsed

    if "event" in message:
        wire_kind = message.get("event")
        if not isinstance(wire_kind, str):
            raise ProtocolError(f"event kind is not a string: {wire_kind!r}")
        data = message.get("data")
        if not isinstance(data, dict):
            raise ProtocolError(f"event {wire_kind} has no data object")
        return Event(kind=canonical_event_kind(wire_kind), raw_kind=wire_kind, data=data)

    if "id" not in message:
        raise ProtocolError(f"message is neither a response nor an event: {message!r}")

    request_id = message.get("id")
    if not isinstance(request_id, str):
        raise ProtocolError(f"response id is not a string: {request_id!r}")

    error = message.get("error")
    if error is not None:
        if not isinstance(error, dict):
            raise ProtocolError(f"error field is not an object: {error!r}")
        code = error.get("code")
        detail = error.get("message")
        return Response(
            id=request_id,
            result=None,
            error=HerdrError(
                code if isinstance(code, str) else "unknown",
                detail if isinstance(detail, str) else repr(error),
            ),
        )

    result = message.get("result")
    if result is not None and not isinstance(result, dict):
        raise ProtocolError(f"result is not an object: {result!r}")

    return Response(id=request_id, result=result, error=None)


def subscription(kind: str, pane_id: str | None = None, **extra: JSONValue) -> JSONObject:
    """Build one subscription object, validating pane scoping up front.

    Catching a missing ``pane_id`` here beats letting the server reject the
    entire events.subscribe call -- one bad entry fails every subscription in
    the batch, which is confusing to debug.
    """
    if kind in PANE_SCOPED_SUBSCRIPTIONS and pane_id is None:
        raise ValueError(f"{kind} is pane-scoped and requires a pane_id")
    if kind not in PANE_SCOPED_SUBSCRIPTIONS and pane_id is not None:
        raise ValueError(f"{kind} is global and does not accept a pane_id")

    entry: JSONObject = {"type": kind}
    if pane_id is not None:
        entry["pane_id"] = pane_id
    entry.update(extra)
    return entry
