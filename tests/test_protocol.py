"""Wire-format tests, pinned to behaviour observed against herdr 0.7.5."""

from __future__ import annotations

import json

import pytest

from herdr_streamdeck.protocol import (
    Event,
    HerdrError,
    ProtocolError,
    Response,
    canonical_event_kind,
    decode_message,
    encode_request,
    subscription,
)


def test_encode_request_is_newline_terminated() -> None:
    raw = encode_request("7", "ping", {})
    assert raw.endswith(b"\n")
    assert json.loads(raw) == {"id": "7", "method": "ping", "params": {}}


def test_encode_request_defaults_params_to_object() -> None:
    # The server rejects a missing params field, so None must become {}.
    assert json.loads(encode_request("1", "ping"))["params"] == {}


def test_decode_success_response() -> None:
    message = decode_message(b'{"id":"1","result":{"type":"pong","protocol":17}}')
    assert isinstance(message, Response)
    assert message.id == "1"
    assert message.unwrap()["type"] == "pong"


def test_decode_error_response_raises_on_unwrap() -> None:
    raw = b'{"id":"1","error":{"code":"invalid_request","message":"missing field"}}'
    message = decode_message(raw)
    assert isinstance(message, Response)
    assert message.error is not None
    with pytest.raises(HerdrError) as excinfo:
        message.unwrap()
    assert excinfo.value.code == "invalid_request"


def test_decode_event_has_no_id() -> None:
    raw = b'{"data":{"pane_id":"w6:p3","type":"pane_closed"},"event":"pane_closed"}'
    message = decode_message(raw)
    assert isinstance(message, Event)
    assert message.raw_kind == "pane_closed"
    assert message.kind == "pane.closed"
    assert message.data["pane_id"] == "w6:p3"


def test_decode_rejects_malformed_json() -> None:
    with pytest.raises(ProtocolError):
        decode_message(b"{not json")


def test_decode_rejects_message_that_is_neither() -> None:
    with pytest.raises(ProtocolError):
        decode_message(b'{"hello":"world"}')


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        # Global events arrive underscored...
        ("pane_closed", "pane.closed"),
        ("pane_agent_detected", "pane.agent_detected"),
        ("workspace_metadata_updated", "workspace.metadata_updated"),
        ("layout_updated", "layout.updated"),
        # ...while pane-scoped events arrive already dotted.
        ("pane.agent_status_changed", "pane.agent_status_changed"),
        ("pane.output_matched", "pane.output_matched"),
    ],
)
def test_canonical_event_kind(wire: str, expected: str) -> None:
    assert canonical_event_kind(wire) == expected


def test_subscription_global() -> None:
    assert subscription("pane.created") == {"type": "pane.created"}


def test_subscription_pane_scoped_requires_pane_id() -> None:
    # The server rejects the whole batch if one entry omits pane_id, so this is
    # caught client-side where the error is attributable.
    with pytest.raises(ValueError, match="pane-scoped"):
        subscription("pane.agent_status_changed")


def test_subscription_global_rejects_pane_id() -> None:
    with pytest.raises(ValueError, match="global"):
        subscription("pane.created", pane_id="w6:p1")


def test_subscription_pane_scoped_accepts_extras() -> None:
    entry = subscription("pane.output_matched", pane_id="w6:p1", source="recent", match="done")
    assert entry == {
        "type": "pane.output_matched",
        "pane_id": "w6:p1",
        "source": "recent",
        "match": "done",
    }
