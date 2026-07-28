"""Tests against a real herdr server.

Skipped automatically when no socket is present, so CI stays green without
herdr installed. Run locally inside a herdr pane to exercise the real wire
format -- that is where protocol drift will surface first.
"""

from __future__ import annotations

import pytest

from herdr_streamdeck.client import HerdrClient, default_socket_path
from herdr_streamdeck.daemon import _iter_panes
from herdr_streamdeck.protocol import PROTOCOL_VERSION, subscription

pytestmark = pytest.mark.skipif(
    not default_socket_path().exists(),
    reason="no herdr socket; start the server to run integration tests",
)


async def test_ping_reports_a_known_protocol() -> None:
    async with HerdrClient() as client:
        pong = await client.ping()

    assert pong["type"] == "pong"
    actual = pong["protocol"]
    assert isinstance(actual, int)
    if actual != PROTOCOL_VERSION:
        pytest.fail(
            f"herdr speaks protocol {actual}, this client was verified against "
            f"{PROTOCOL_VERSION}. Re-check the event names and subscription "
            f"shapes in docs/plugin-system.md before bumping."
        )


async def test_snapshot_yields_parseable_panes() -> None:
    async with HerdrClient() as client:
        snapshot = await client.snapshot()

    panes = _iter_panes(snapshot)
    assert panes, "expected at least the pane running this test"
    for record in panes:
        assert isinstance(record["pane_id"], str)


async def test_subscribe_is_accepted_for_every_global_event() -> None:
    """Guards against a global event silently becoming pane-scoped."""
    from herdr_streamdeck.daemon import SUBSCRIPTIONS

    async with HerdrClient() as client:
        await client.subscribe([subscription(kind) for kind in SUBSCRIPTIONS])


async def test_pane_scoped_subscription_requires_pane_id() -> None:
    """The server rejects the batch; confirms why the client validates early."""
    from herdr_streamdeck.protocol import HerdrError

    async with HerdrClient() as client:
        with pytest.raises(HerdrError) as excinfo:
            # Deliberately bypasses subscription() to hit the server's own check.
            await client.subscribe([{"type": "pane.agent_status_changed"}])
    assert "pane_id" in excinfo.value.message


async def test_server_serves_only_one_request_per_connection() -> None:
    """Pins the rule that shapes the whole client.

    Raw sockets on purpose: going through HerdrClient would hit our own
    SingleUseViolation guard and prove nothing about the server. If this ever
    fails, herdr learned to keep connections alive and HerdrSession could stop
    reconnecting per request.
    """
    import asyncio
    import json

    reader, writer = await asyncio.open_unix_connection(str(default_socket_path()))
    try:
        for index in range(2):
            payload = {"id": f"r{index}", "method": "ping", "params": {}}
            writer.write(json.dumps(payload).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=4)
            if index == 0:
                assert json.loads(line)["result"]["type"] == "pong"
            else:  # pragma: no cover -- only reached if the server changes
                pytest.fail("server answered a second request on the same connection")
    except (ConnectionResetError, BrokenPipeError):
        pass  # expected: the second write lands on a closed socket
    finally:
        writer.close()


async def test_session_separates_requests_from_events() -> None:
    """The supported arrangement: requests and subscriptions on separate sockets."""
    from herdr_streamdeck.client import HerdrSession

    async with HerdrSession() as session:
        await session.subscribe([subscription("pane.updated")])
        # Would reset the connection if it shared the subscription socket.
        assert (await session.ping())["type"] == "pong"
        assert (await session.snapshot())["type"]
