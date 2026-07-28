"""Wires the herdr event stream to the button surface.

Model: one key per pane that has a detected agent, coloured by agent status.
Pressing a key focuses that pane.

Two threading notes drive the structure here:

* The Stream Deck library delivers key callbacks on its own reader thread, so
  presses are handed to the event loop with ``call_soon_threadsafe`` rather
  than touched directly.
* Agent status arrives on the *global* ``pane.updated`` event, which carries a
  full pane object. That avoids maintaining a per-pane
  ``pane.agent_status_changed`` subscription per pane and re-subscribing as
  panes come and go.

Layout mirrors herdr rather than storing anything: columns follow
``workspace.list`` (sidebar order) and rows follow ``pane.list`` (a depth-first
walk of the tab's split tree). Events are split into *structural* ones, which
can reorder things and so trigger a re-read, and *cosmetic* ones, which only
change a pane in place. See ``STRUCTURAL_EVENTS``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Protocol

from .client import HerdrSession
from .deck import RGB, ButtonFace, ButtonSurface, open_surface
from .icons import mark_for
from .layout import Grid, Group, GroupingMode, GroupKey, Pane, build_columns
from .protocol import Event, HerdrError, JSONObject, subscription

logger = logging.getLogger("herdr_streamdeck")

# Global subscriptions only -- see the module docstring.
SUBSCRIPTIONS = (
    "pane.created",
    "pane.closed",
    "pane.updated",
    "pane.focused",
    "pane.exited",
    "pane.agent_detected",
    "workspace.focused",
    "tab.focused",
)

# Drawn as a strip along the top edge, so the key field itself stays neutral.
STATUS_COLORS: dict[str, RGB] = {
    "working": (217, 132, 24),  # amber -- busy
    "blocked": (204, 44, 44),  # red   -- needs input
    "done": (34, 168, 82),  # green -- finished, unseen
    "idle": (82, 82, 91),  # grey  -- waiting, seen
}

EMPTY_FACE = ButtonFace(background=(20, 20, 23))
"""An unoccupied key: darker than the neutral field, and inert."""

# Events that can change *which* pane sits where, as opposed to merely
# restyling one. These trigger a re-read of herdr's ordering, since neither
# workspace order nor split-tree order can be derived from a pane record.
STRUCTURAL_EVENTS = frozenset(
    {
        "pane.created",
        "pane.closed",
        "pane.exited",
        "pane.moved",
        "pane.agent_detected",
        "tab.created",
        "tab.closed",
        "tab.moved",
        "workspace.created",
        "workspace.closed",
        "workspace.moved",
        "workspace.focused",
        "layout.updated",
    }
)


class HerdrLike(Protocol):
    """The slice of the client the controller actually needs.

    Depending on this rather than the concrete client keeps the controller
    testable with a stub, without casts or type suppressions at the seam.
    """

    async def request(self, method: str, params: JSONObject | None = None) -> JSONObject: ...

    async def snapshot(self) -> JSONObject: ...

    async def subscribe(self, subscriptions: Sequence[JSONObject]) -> None: ...

    def events(self) -> AsyncIterator[Event]: ...


class DeckController:
    """Keeps the button surface in sync with herdr's pane state."""

    def __init__(
        self,
        client: HerdrLike,
        surface: ButtonSurface,
        *,
        mode: GroupingMode = GroupingMode.WORKSPACE,
        reconcile_interval: float = 60.0,
    ) -> None:
        self._client = client
        self._surface = surface
        self._mode = mode
        self._reconcile_interval = reconcile_interval
        # Insertion order matters: it is herdr's pane order, and sorting it
        # would replace herdr's arrangement with ours.
        self._panes: dict[str, Pane] = {}
        self._order: list[GroupKey] = []
        self._columns: list[Group | None] = []
        self._focused_workspace = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dirty = asyncio.Event()
        self._restructure = False

    @property
    def grid(self) -> Grid:
        rows, columns = self._surface.key_layout
        return Grid(rows=rows, columns=columns)

    # ------------------------------------------------------------------- state

    def _upsert(self, record: JSONObject) -> bool:
        pane = Pane.from_record(record)
        if pane is None:
            return False
        existing = self._panes.get(pane.pane_id)
        if existing == pane:
            return False
        self._panes[pane.pane_id] = pane
        return True

    def _remove(self, pane_id: str) -> bool:
        return self._panes.pop(pane_id, None) is not None

    def _rebuild_columns(self) -> None:
        """Recompute columns from the current model, preserving herdr's order."""
        self._columns = build_columns(
            list(self._panes.values()),
            self._order,
            self.grid,
            self._mode,
            workspace_id=self._focused_workspace,
        )

    # ------------------------------------------------------------------ drawing

    def face_for(self, pane: Pane | None) -> ButtonFace:
        """The face for one key."""
        if pane is None:
            return EMPTY_FACE
        mark = mark_for(pane.mark_key)
        return ButtonFace(
            mark=mark.glyph,
            mark_color=mark.color,
            mark_scale=mark.scale,
            badge=pane.badge,
            status_color=STATUS_COLORS.get(pane.status),
        )

    def repaint(self) -> None:
        self._rebuild_columns()
        grid = self.grid
        for index in range(self._surface.key_count):
            face = self.face_for(grid.pane_at(self._columns, index))
            with contextlib.suppress(Exception):
                # A single failed key must not abort the whole repaint.
                self._surface.set_face(index, face)

    # ------------------------------------------------------------------ presses

    def _on_press(self, index: int, pressed: bool) -> None:
        """Invoked on the deck's reader thread -- hop to the event loop."""
        if not pressed:
            return
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._dispatch_press, index)

    def _dispatch_press(self, index: int) -> None:
        pane = self.grid.pane_at(self._columns, index)
        if pane is None:
            logger.debug("key %d pressed but maps to no pane", index)
            return
        pane_id = pane.pane_id
        logger.info("key %d pressed -> focusing %s", index, pane_id)
        task = asyncio.create_task(self._focus(pane_id), name=f"focus-{pane_id}")
        # Hold a reference so the task is not garbage collected mid-flight.
        task.add_done_callback(lambda _: None)

    async def _focus(self, pane_id: str) -> None:
        try:
            await self._client.request("pane.focus", {"pane_id": pane_id})
        except HerdrError as exc:
            if exc.code == "pane_not_found":
                # Our model outlived the pane. Drop it and repaint rather than
                # leaving a key that fails every press.
                logger.info("pane %s is gone; dropping it", pane_id)
                if self._remove(pane_id):
                    self._dirty.set()
                return
            logger.warning("failed to focus %s: %s", pane_id, exc)
        except Exception:
            logger.warning("failed to focus %s", pane_id, exc_info=True)

    # --------------------------------------------------------------------- run

    async def drain_replay(self, quiet: float = 0.4, limit: float = 5.0) -> int:
        """Swallow the backlog herdr replays when a subscription starts.

        Subscribing does not begin a live-only stream: herdr immediately
        replays historical events, and **not in causal order** -- a
        ``pane.closed`` for a pane can arrive before its ``pane.created``.
        Applying that backlog resurrects long-dead panes, whose keys then fail
        every press.

        So the backlog is discarded and the snapshot taken afterwards is
        treated as the truth. Returns once no event has arrived for ``quiet``
        seconds, or after ``limit`` seconds regardless.
        """
        discarded = 0
        deadline = asyncio.get_running_loop().time() + limit
        events = self._client.events()

        while asyncio.get_running_loop().time() < deadline:
            try:
                await asyncio.wait_for(events.__anext__(), timeout=quiet)
            except TimeoutError:  # asyncio.TimeoutError is an alias on 3.11+
                break
            except StopAsyncIteration:
                break
            discarded += 1

        if discarded:
            logger.debug("discarded %d replayed events", discarded)
        return discarded

    async def read_order(self) -> tuple[list[GroupKey], str]:
        """Ask herdr for its column order, and which workspace is focused.

        Neither is derivable from a pane record: sidebar order lives only in
        ``workspace.list`` (the order ``workspace.move`` rearranges), and tab
        order only in ``tab.list``.
        """
        listing = await self._client.request("workspace.list")
        workspaces = listing.get("workspaces")
        focused = ""
        order: list[GroupKey] = []

        if isinstance(workspaces, list):
            for item in workspaces:
                if not isinstance(item, dict):
                    continue
                ws_id = item.get("workspace_id")
                if not isinstance(ws_id, str):
                    continue
                if item.get("focused"):
                    focused = ws_id
                if self._mode is GroupingMode.WORKSPACE:
                    label = item.get("label")
                    order.append(
                        GroupKey(id=ws_id, label=label if isinstance(label, str) else ws_id)
                    )

        if self._mode is GroupingMode.TAB:
            tabs = (await self._client.request("tab.list", {"workspace_id": focused})).get(
                "tabs"
            )
            if isinstance(tabs, list):
                for item in tabs:
                    if not isinstance(item, dict):
                        continue
                    tab_id = item.get("tab_id")
                    if not isinstance(tab_id, str):
                        continue
                    label = item.get("label")
                    order.append(
                        GroupKey(id=tab_id, label=label if isinstance(label, str) else tab_id)
                    )

        return order, focused

    async def prime(self) -> None:
        """Re-read herdr's arrangement. Snapshot and listing are the truth.

        The pane map is rebuilt rather than patched, so its iteration order is
        exactly the snapshot's -- which is herdr's split-tree order. Patching
        would leave panes wherever they were first seen.
        """
        order, focused = await self.read_order()
        snapshot = await self._client.snapshot()

        self._panes = {}
        for record in _iter_panes(snapshot):
            self._upsert(record)
        self._order = order
        self._focused_workspace = focused

        self.repaint()

    def handle(self, event: Event) -> None:
        """Fold one event into the model.

        Structural events only *schedule* a re-read: ordering cannot be
        recovered from the event payload, so the model is rebuilt from herdr
        rather than guessed at here.
        """
        if event.kind in STRUCTURAL_EVENTS:
            self._restructure = True
            self._dirty.set()
            return

        if event.kind == "pane.updated":
            record = event.data.get("pane")
            if isinstance(record, dict) and self._upsert(record):
                self._dirty.set()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._surface.set_press_handler(self._on_press)

        # Order matters: subscribe first so no live change is missed, then
        # throw away the replayed backlog, then snapshot. Snapshotting before
        # the backlog drains would just be overwritten by stale events.
        await self._client.subscribe([subscription(kind) for kind in SUBSCRIPTIONS])
        await self.drain_replay()
        await self.prime()

        painter = asyncio.create_task(self._paint_loop(), name="painter")
        reconciler = asyncio.create_task(self._reconcile_loop(), name="reconciler")
        try:
            async for event in self._client.events():
                self.handle(event)
            # The stream ended, so the server is gone. Returning here (rather
            # than reconnecting) is deliberate: herdr does NOT reap plugin
            # startup processes -- probes outlived their server, reparented to
            # init. A daemon that retried forever would survive a herdr restart
            # still holding the Stream Deck, and lock out its own replacement,
            # since hidapi opens the device exclusively. Exiting makes this
            # self-reaping.
            logger.info("event stream closed; herdr is gone, shutting down")
        finally:
            for task in (painter, reconciler):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._surface.set_press_handler(None)

    async def _reconcile_loop(self) -> None:
        """Re-snapshot periodically so the model cannot drift indefinitely.

        A safety net, not the main mechanism: events keep the deck responsive,
        this keeps it honest if one is ever missed or dropped under load.
        """
        while True:
            await asyncio.sleep(self._reconcile_interval)
            try:
                await self.prime()
            except Exception:
                logger.warning("reconcile failed", exc_info=True)

    async def _paint_loop(self) -> None:
        """Coalesce bursts of events into at most one update per interval."""
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            if self._restructure:
                self._restructure = False
                try:
                    await self.prime()
                except Exception:
                    logger.warning("could not re-read layout", exc_info=True)
            else:
                self.repaint()
            await asyncio.sleep(0.05)


def _iter_panes(snapshot: JSONObject) -> list[JSONObject]:
    """Pull pane records out of a session.snapshot response.

    The snapshot nests panes under workspaces and tabs; the exact shape has
    changed across protocol versions, so this walks defensively rather than
    indexing a fixed path.
    """
    # Keyed by pane_id because the snapshot lists the same pane under both
    # `agents` and `panes`; without this every agent pane is visited twice.
    found: dict[str, JSONObject] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            pane_id = node.get("pane_id")
            if isinstance(pane_id, str) and "terminal_id" in node:
                # Prefer the richer record when the same pane appears twice.
                existing = found.get(pane_id)
                if existing is None or len(node) > len(existing):
                    found[pane_id] = node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(snapshot)
    return list(found.values())


def probe_devices() -> int:
    """List attached Stream Decks and exit.

    Cheap enough to run fork-per-invoke, so it is exposed as a plugin action --
    it answers "does this machine see the device at all?", which is the first
    question whenever the deck goes dark.
    """
    try:
        from StreamDeck.DeviceManager import DeviceManager
    except ImportError as exc:
        print(f"streamdeck package unavailable: {exc}")
        return 1

    try:
        decks = DeviceManager().enumerate()
    except Exception as exc:
        print(f"enumeration failed: {exc}")
        print("On Linux check the udev rule; under WSL check that usbipd has attached it.")
        return 1

    if not decks:
        print("no Stream Deck found")
        print("macOS: quit the Elgato app, which claims the device exclusively.")
        print("Linux/WSL: check /dev/hidraw* ownership and the usbipd attachment.")
        return 1

    for deck in decks:
        deck.open()
        try:
            print(
                f"{deck.deck_type()}  serial={deck.get_serial_number()}  "
                f"keys={deck.key_count()}"
            )
        finally:
            deck.close()
    return 0


async def amain(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.probe:
        return probe_devices()

    surface = open_surface(use_device=not args.no_device, serial=args.serial)
    surface.open()

    # Two connections -- herdr resets a connection that both subscribes and
    # issues requests. See HerdrSession.
    client = HerdrSession(Path(args.socket) if args.socket else None)
    await client.connect()

    controller = DeckController(client, surface, mode=GroupingMode(args.mode))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    runner = asyncio.create_task(controller.run(), name="controller")
    waiter = asyncio.create_task(stop.wait(), name="stop")
    try:
        await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        runner.cancel()
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        await client.close()
        surface.close()

    if client.dropped_events:
        logger.warning("dropped %d events while repainting", client.dropped_events)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="herdr-streamdeck")
    parser.add_argument("--socket", help="path to herdr.sock (default: $HERDR_SOCKET_PATH)")
    parser.add_argument("--serial", help="target a specific Stream Deck by serial number")
    parser.add_argument(
        "--no-device",
        action="store_true",
        help="run against an in-memory surface; no hardware required",
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in GroupingMode],
        default=GroupingMode.WORKSPACE.value,
        help=(
            "what a column represents: 'workspace' (sidebar order) or 'tab' "
            "(tabs of the focused workspace). Default: workspace"
        ),
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="list attached Stream Decks and exit",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
