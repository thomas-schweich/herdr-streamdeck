"""Client tests against an in-process fake herdr server.

The fake speaks the same NDJSON framing as the real server so these run
anywhere, with no herdr installed.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from herdr_streamdeck.client import (
    ConnectionClosed,
    HerdrClient,
    HerdrSession,
    SingleUseViolation,
    default_socket_path,
)
from herdr_streamdeck.protocol import HerdrError, JSONObject

Handler = Callable[[JSONObject], Awaitable[list[JSONObject]]]


class FakeServer:
    """Minimal NDJSON Unix-socket server."""

    def __init__(self, path: Path, handler: Handler) -> None:
        self.path = path
        self.handler = handler
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._serve, str(self.path))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                for message in await self.handler(json.loads(line)):
                    writer.write(json.dumps(message).encode() + b"\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def server() -> AsyncIterator[Callable[[Handler], Awaitable[Path]]]:
    started: list[FakeServer] = []
    # NOT pytest's tmp_path: its paths run ~90 chars on CI runners, and macOS
    # caps AF_UNIX sun_path at 104 bytes. A short dir keeps this portable.
    root = Path(tempfile.mkdtemp(prefix="hsd"))

    async def start(handler: Handler) -> Path:
        path = root / "h.sock"
        fake = FakeServer(path, handler)
        await fake.start()
        started.append(fake)
        return path

    yield start

    for fake in started:
        await fake.stop()
    shutil.rmtree(root, ignore_errors=True)


async def test_request_returns_result(
    server: Callable[[Handler], Awaitable[Path]],
) -> None:
    async def handler(request: JSONObject) -> list[JSONObject]:
        return [{"id": request["id"], "result": {"type": "pong", "protocol": 17}}]

    path = await server(handler)
    async with HerdrClient(path) as client:
        assert (await client.ping())["protocol"] == 17


async def test_request_raises_herdr_error(
    server: Callable[[Handler], Awaitable[Path]],
) -> None:
    async def handler(request: JSONObject) -> list[JSONObject]:
        return [
            {
                "id": request["id"],
                "error": {"code": "pane_not_found", "message": "no such pane"},
            }
        ]

    path = await server(handler)
    async with HerdrClient(path) as client:
        with pytest.raises(HerdrError) as excinfo:
            await client.request("pane.focus", {"pane_id": "nope"})
        assert excinfo.value.code == "pane_not_found"


async def test_second_request_on_a_connection_is_refused(
    server: Callable[[Handler], Awaitable[Path]],
) -> None:
    """herdr closes a connection after one request; fail loudly, not with ECONNRESET."""

    async def handler(request: JSONObject) -> list[JSONObject]:
        return [{"id": request["id"], "result": {}}]

    path = await server(handler)
    async with HerdrClient(path) as client:
        await client.ping()
        with pytest.raises(SingleUseViolation, match="HerdrSession"):
            await client.ping()


async def test_session_opens_a_connection_per_request(
    server: Callable[[Handler], Awaitable[Path]],
) -> None:
    """Sequential requests must work, each on its own connection."""
    connections = 0

    async def handler(request: JSONObject) -> list[JSONObject]:
        return [{"id": request["id"], "result": {"method": request["method"]}}]

    path = await server(handler)

    class CountingSession(HerdrSession):
        async def request(
            self, method: str, params: JSONObject | None = None, *, timeout: float = 10.0
        ) -> JSONObject:
            nonlocal connections
            connections += 1
            return await super().request(method, params, timeout=timeout)

    session = CountingSession(path)
    assert (await session.request("ping"))["method"] == "ping"
    assert (await session.request("session.snapshot"))["method"] == "session.snapshot"
    assert connections == 2


async def test_events_are_demultiplexed_from_responses(
    server: Callable[[Handler], Awaitable[Path]],
) -> None:
    async def handler(request: JSONObject) -> list[JSONObject]:
        return [
            {"id": request["id"], "result": {"type": "subscription_started"}},
            {"event": "pane_created", "data": {"pane": {"pane_id": "w1:p1"}}},
            {"event": "pane_closed", "data": {"pane_id": "w1:p1"}},
        ]

    path = await server(handler)
    async with HerdrClient(path) as client:
        await client.subscribe([{"type": "pane.created"}])
        received = []
        async for event in client.events():
            received.append(event.kind)
            if len(received) == 2:
                break
    assert received == ["pane.created", "pane.closed"]


async def test_request_timeout_does_not_leak_pending(
    server: Callable[[Handler], Awaitable[Path]],
) -> None:
    async def handler(request: JSONObject) -> list[JSONObject]:
        return []  # never answers

    path = await server(handler)
    async with HerdrClient(path) as client:
        with pytest.raises(asyncio.TimeoutError):
            await client.request("ping", timeout=0.05)
        assert client._pending == {}


async def test_missing_socket_is_reported_clearly(tmp_path: Path) -> None:
    client = HerdrClient(tmp_path / "absent.sock")
    with pytest.raises(FileNotFoundError, match="is the server running"):
        await client.connect()


async def test_pending_requests_fail_when_closed(
    server: Callable[[Handler], Awaitable[Path]],
) -> None:
    async def handler(request: JSONObject) -> list[JSONObject]:
        return []

    path = await server(handler)
    client = HerdrClient(path)
    await client.connect()
    pending = asyncio.create_task(client.request("ping", timeout=5))
    await asyncio.sleep(0.01)
    await client.close()
    with pytest.raises(ConnectionClosed):
        await pending


def test_socket_path_prefers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/custom/herdr.sock")
    assert default_socket_path() == Path("/custom/herdr.sock")


def test_socket_path_falls_back_to_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    # A daemon started by launchd or systemd inherits no pane environment.
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
    assert default_socket_path() == Path("/xdg/herdr/herdr.sock")


async def test_overlong_socket_path_is_reported_clearly() -> None:
    """macOS caps AF_UNIX at 104 bytes; the kernel error names no path."""
    root = Path(tempfile.mkdtemp(prefix="hsd"))
    try:
        deep = root / ("d" * 90) / ("e" * 90) / "herdr.sock"
        deep.parent.mkdir(parents=True, exist_ok=True)
        deep.touch()
        with pytest.raises(OSError, match="AF_UNIX limit"):
            await HerdrClient(deep).connect()
    finally:
        shutil.rmtree(root, ignore_errors=True)
