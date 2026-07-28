"""Async client for the herdr socket API.

The shape here is dictated by one non-obvious server rule: **herdr serves
exactly one request per connection**, then closes it. ``events.subscribe`` is
the exception -- it turns the connection into a persistent, event-only stream.

So :class:`HerdrClient` models a single connection used one way or the other,
and :class:`HerdrSession` -- what callers normally want -- opens a fresh
connection per request while holding one open for events.

Within a connection, a reader task fans messages out: responses resolve the
pending future by id, events go onto a queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from types import TracebackType

from .protocol import (
    Event,
    HerdrError,
    JSONObject,
    ProtocolError,
    Response,
    decode_message,
    encode_request,
)

DEFAULT_EVENT_QUEUE_SIZE = 1024


def default_socket_path() -> Path:
    """Locate the herdr API socket.

    ``HERDR_SOCKET_PATH`` is injected into every managed pane and into plugin
    action/startup environments, so it is correct almost everywhere. The XDG
    fallback matters for a daemon started by launchd or systemd, which inherits
    neither.
    """
    from_env = os.environ.get("HERDR_SOCKET_PATH")
    if from_env:
        return Path(from_env)

    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "herdr" / "herdr.sock"


class ConnectionClosed(Exception):
    """The server closed the connection."""


class SingleUseViolation(RuntimeError):
    """A second request was attempted on a connection herdr has already used."""


class HerdrClient:
    """One connection to the herdr API socket.

    **A connection serves exactly one request.** herdr answers it and then
    closes the socket; a second request on the same connection is met with
    ECONNRESET. Verified against 0.7.5: two consecutive pings fail, as does
    ping-then-snapshot, in either order.

    ``events.subscribe`` is the sole exception. It converts the connection into
    a persistent, event-only stream -- after which no further request may be
    issued on it either.

    So a connection is used one of two ways, and never both:

        one request   ->  request() / ping() / snapshot()
        event stream  ->  subscribe(), then iterate events()

    Most callers should use :class:`HerdrSession`, which manages both.
    """

    def __init__(
        self,
        socket_path: Path | None = None,
        *,
        event_queue_size: int = DEFAULT_EVENT_QUEUE_SIZE,
    ) -> None:
        self.socket_path = socket_path or default_socket_path()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[Response]] = {}
        self._events: asyncio.Queue[Event] = asyncio.Queue(maxsize=event_queue_size)
        self._ids = itertools.count(1)
        self._closed = asyncio.Event()
        self._dropped_events = 0
        self._last_unmatched_error: HerdrError | None = None
        self._spent = False

    # ---------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        if self._writer is not None:
            raise RuntimeError("already connected")
        if not self.socket_path.exists():
            raise FileNotFoundError(
                f"herdr socket not found at {self.socket_path} -- is the server running?"
            )
        self._reader, self._writer = await asyncio.open_unix_connection(str(self.socket_path))
        self._closed.clear()
        self._reader_task = asyncio.create_task(self._read_loop(), name="herdr-reader")

    async def close(self) -> None:
        self._closed.set()

        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                # Losing the socket during teardown is not interesting.
                await self._writer.wait_closed()
            self._writer = None
        self._reader = None

        self._fail_pending(ConnectionClosed("client closed"))

    async def __aenter__(self) -> HerdrClient:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def dropped_events(self) -> int:
        """Events discarded because the consumer fell behind."""
        return self._dropped_events

    # ------------------------------------------------------------------- reader

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    raise ConnectionClosed("server closed the connection")
                self._dispatch(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surfaced to every waiter, then to the caller
            # herdr answers a malformed request with an error whose id is ""
            # rather than the request's id, then drops the connection. The
            # generic "server closed the connection" hides the real cause, so
            # prefer the uncorrelatable error we just saw.
            self._fail_pending(self._last_unmatched_error or exc)
            self._closed.set()

    def _dispatch(self, line: bytes) -> None:
        try:
            message = decode_message(line)
        except ProtocolError:
            # A single malformed line must not take down the connection; the
            # stream is still framed correctly by newlines.
            return

        if isinstance(message, Event):
            try:
                self._events.put_nowait(message)
            except asyncio.QueueFull:
                # Prefer dropping the oldest: button state should track the
                # present, not replay a backlog after a stall.
                self._dropped_events += 1
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._events.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    self._events.put_nowait(message)
            return

        future = self._pending.pop(message.id, None)
        if future is None:
            # No pending request owns this id. For errors that is the id=""
            # rejection described in _read_loop; hold it so the imminent
            # disconnect can report something useful.
            if message.error is not None:
                self._last_unmatched_error = message.error
            return
        if not future.done():
            future.set_result(message)

    # ------------------------------------------------------------------ requests

    async def request(
        self, method: str, params: JSONObject | None = None, *, timeout: float = 10.0
    ) -> JSONObject:
        """Send a request and await its result, raising HerdrError on failure.

        May be called only once per connection -- see the class docstring.
        """
        if self._writer is None:
            raise RuntimeError("not connected")
        if self._spent:
            raise SingleUseViolation(
                "herdr closes a connection after one request, so this one is spent. "
                "Use HerdrSession.request(), which opens a fresh connection per call."
            )
        self._spent = True

        request_id = f"sd:{next(self._ids)}"
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        try:
            self._writer.write(encode_request(request_id, method, params))
            await self._writer.drain()
            response = await asyncio.wait_for(future, timeout=timeout)
        except BaseException:
            # Includes cancellation and timeout -- never leak the pending entry.
            self._pending.pop(request_id, None)
            raise

        return response.unwrap()

    async def ping(self) -> JSONObject:
        return await self.request("ping")

    async def snapshot(self) -> JSONObject:
        return await self.request("session.snapshot")

    async def subscribe(self, subscriptions: Sequence[JSONObject]) -> None:
        """Register subscriptions.

        The server validates the batch as a unit -- one malformed entry rejects
        them all -- so build entries with protocol.subscription().
        """
        await self.request("events.subscribe", {"subscriptions": list(subscriptions)})

    # -------------------------------------------------------------------- events

    async def events(self) -> AsyncIterator[Event]:
        """Yield subscription events until the connection closes."""
        while True:
            getter = asyncio.ensure_future(self._events.get())
            closed = asyncio.ensure_future(self._closed.wait())
            done, _ = await asyncio.wait({getter, closed}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                closed.cancel()
                yield getter.result()
                continue

            getter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await getter
            # Drain anything already queued before giving up.
            while not self._events.empty():
                yield self._events.get_nowait()
            return


class HerdrSession:
    """The usable API surface, given herdr's one-request-per-connection rule.

    Requests open a short-lived connection each; the event stream keeps one
    connection open for its lifetime. Connecting per request sounds wasteful
    but a Unix-socket connect is microseconds, and it is what the server
    requires -- see :class:`HerdrClient`.
    """

    def __init__(
        self,
        socket_path: Path | None = None,
        *,
        event_queue_size: int = DEFAULT_EVENT_QUEUE_SIZE,
    ) -> None:
        self.socket_path = socket_path or default_socket_path()
        self._stream = HerdrClient(self.socket_path, event_queue_size=event_queue_size)

    @property
    def dropped_events(self) -> int:
        return self._stream.dropped_events

    async def connect(self) -> None:
        """Open the event-stream connection. Requests need no setup."""
        await self._stream.connect()

    async def close(self) -> None:
        await self._stream.close()

    async def __aenter__(self) -> HerdrSession:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def request(
        self, method: str, params: JSONObject | None = None, *, timeout: float = 10.0
    ) -> JSONObject:
        """Open a connection, issue one request, and close it."""
        async with HerdrClient(self.socket_path) as connection:
            return await connection.request(method, params, timeout=timeout)

    async def ping(self) -> JSONObject:
        return await self.request("ping")

    async def snapshot(self) -> JSONObject:
        return await self.request("session.snapshot")

    async def subscribe(self, subscriptions: Sequence[JSONObject]) -> None:
        await self._stream.subscribe(subscriptions)

    def events(self) -> AsyncIterator[Event]:
        return self._stream.events()


async def request_once(
    method: str, params: JSONObject | None = None, *, socket_path: Path | None = None
) -> JSONObject:
    """One call on its own connection -- e.g. from a plugin action."""
    async with HerdrClient(socket_path) as client:
        return await client.request(method, params)


__all__ = [
    "ConnectionClosed",
    "Event",
    "HerdrClient",
    "HerdrError",
    "HerdrSession",
    "SingleUseViolation",
    "default_socket_path",
    "request_once",
]
