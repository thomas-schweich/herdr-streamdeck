"""Wires the herdr event stream to the button surface.

Model: one key per pane that has a detected agent, coloured by agent status.
Pressing a key focuses that pane.

Two threading notes drive the structure here:

* The Stream Deck library delivers key callbacks on its own reader thread, so
  presses are handed to the event loop with ``call_soon_threadsafe`` rather
  than touched directly.
* Agent status arrives **only** on ``pane.agent_status_changed``, which is
  pane-scoped. ``pane.updated`` carries an ``agent_status`` field in its
  payload but does not fire when status changes -- verified by driving
  transitions with ``pane.report_agent`` and watching both. Relying on the
  global event meant status only refreshed on the 60s reconcile.

  So a subscription is held per pane, rebuilt whenever the pane set changes.
  That needs a new connection each time (see HerdrSession.resubscribe), which
  is why it is done on restructure rather than per event.

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
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from .animation import EMPTY_ANIMATION, Animation, animation_for, frame_index
from .client import HerdrSession
from .deck import EMPTY_BACKGROUND, RGB, ButtonFace, ButtonSurface, KeyFrames, open_surface
from .icons import mark_for, resolve_override
from .layout import Grid, Group, GroupingMode, GroupKey, Pane, build_columns
from .protocol import Event, HerdrError, JSONObject, subscription
from .summary import PaneSummary, Summariser
from .summary import build as build_summariser

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

SUMMARISE_ON = frozenset({"blocked", "done"})
"""Statuses worth paying a model to explain.

Only transitions *into* these, and only these two: `blocked` is the state the
deck cannot resolve on its own, and `done` is the one you act on. Summarising
`working` would fire continuously for no gain, and `idle` says all there is to
say already."""

ANIMATION_FPS = 20
"""Frame rate for pulsing and blinking.

A 15-key refresh measured 25 ms (1.34 ms a key), so 20 fps leaves ample
headroom even in the worst case where every key animates -- and writes are
skipped when the level is unchanged, so the usual cost is far lower."""

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

    async def resubscribe(self, subscriptions: Sequence[JSONObject]) -> None: ...

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
        summariser: Summariser | None = None,
    ) -> None:
        self._client = client
        self._surface = surface
        self._mode = mode
        self._summariser = summariser
        self._summaries: dict[str, PaneSummary] = {}
        self._summarising: set[str] = set()
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
        # Prebuffered frames and the animation driving each key.
        self._frames: dict[int, KeyFrames] = {}
        self._animations: dict[int, Animation] = {}
        self._shown: dict[int, int] = {}
        # One clock for the whole deck: phase must not depend on when a pane
        # started working, or keys pulse out of step with each other.
        self._epoch = 0.0

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
        self._summaries.pop(pane_id, None)
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
            return ButtonFace(background=EMPTY_BACKGROUND)
        summary = self._summaries.get(pane.pane_id)
        mark = mark_for(pane.mark_key)
        return ButtonFace(
            mark=mark.glyph,
            mark_color=mark.color,
            mark_scale=mark.scale,
            # A user PNG in the plugin config dir replaces the glyph.
            icon=resolve_override(pane.mark_key),
            badge=pane.badge,
            summary=summary.words if summary else (),
            status_color=STATUS_COLORS.get(pane.status),
        )

    def repaint(self) -> None:
        """Re-render keys whose content changed, then show the current frame.

        Rendering is the expensive part (~1.26 ms a key), so a face that has
        not changed keeps its existing prebuffered frames even when its
        animation is running.
        """
        self._rebuild_columns()
        grid = self.grid

        for index in range(self._surface.key_count):
            pane = grid.pane_at(self._columns, index)
            face = self.face_for(pane)
            animation = animation_for(pane.status) if pane else EMPTY_ANIMATION
            self._animations[index] = animation

            cached = self._frames.get(index)
            if cached is None or cached.face != face:
                try:
                    self._frames[index] = self._surface.render(face)
                except Exception:
                    logger.warning("could not render key %d", index, exc_info=True)
                    continue
                # Force a write: the cached level now refers to old content.
                self._shown.pop(index, None)

        self.tick()

    def tick(self, now: float | None = None) -> int:
        """Advance every key to its frame for the current instant.

        Writes only where the level actually changed -- a USB write measured
        1.34 ms, so a full 15-key refresh is 25 ms and blind rewriting would
        cap the deck at ~40 fps for no benefit. Returns the number of writes.
        """
        if now is None:
            loop = self._loop
            now = loop.time() if loop is not None else 0.0
        elapsed = now - self._epoch
        levels = self._surface.levels
        writes = 0

        for index, frames in self._frames.items():
            animation = self._animations.get(index, EMPTY_ANIMATION)
            level = frame_index(animation, elapsed, levels)
            if self._shown.get(index) == level:
                continue
            try:
                self._surface.write(index, frames, level)
            except Exception:
                logger.warning("could not write key %d", index, exc_info=True)
                continue
            self._shown[index] = level
            writes += 1
        return writes

    # ---------------------------------------------------------------- summaries

    def _request_summary(self, pane_id: str) -> None:
        """Kick off a summary without blocking the caller.

        Fire-and-forget on purpose: the key repaints immediately with its status
        animation, and the words arrive a beat later if they arrive at all. The
        deck never waits on the network to draw.
        """
        if self._summariser is None or pane_id in self._summarising:
            return
        self._summarising.add(pane_id)
        task = asyncio.create_task(self._summarise(pane_id), name=f"summary-{pane_id}")
        task.add_done_callback(lambda _: self._summarising.discard(pane_id))

    async def _summarise(self, pane_id: str) -> None:
        summariser = self._summariser
        if summariser is None:
            return
        try:
            response = await self._client.request(
                "pane.read", {"pane_id": pane_id, "source": "recent", "lines": 60}
            )
        except HerdrError as exc:
            logger.debug("could not read %s for a summary: %s", pane_id, exc)
            return
        except Exception:
            logger.warning("could not read %s for a summary", pane_id, exc_info=True)
            return

        read = response.get("read")
        text = read.get("text") if isinstance(read, dict) else None
        if not isinstance(text, str):
            return

        summary = await summariser.summarise(text)
        if summary is None:
            return
        # The pane may have moved on, or gone, while we were waiting.
        if pane_id not in self._panes:
            return
        self._summaries[pane_id] = summary
        logger.info(
            "summary for %s: %s%s",
            pane_id,
            summary.text,
            f"  (+{len(summary.replies)} replies)" if summary.replies else "",
        )
        self._dirty.set()

    def replies_for(self, pane_id: str) -> tuple[object, ...]:
        """Suggested replies for a pane, if any were offered.

        Exposed but unused: nothing sends these yet. Committing text to a live
        agent session on a single keypress needs an interaction that cannot be
        mistapped, and that has not been designed.
        """
        summary = self._summaries.get(pane_id)
        return tuple(summary.replies) if summary else ()

    # ------------------------------------------------------------------ presses

    def _on_press(self, index: int, pressed: bool) -> None:
        """Invoked on the deck's reader thread -- hop to the event loop."""
        if not pressed:
            return
        loop = self._loop
        if loop is None:
            # Only reachable if a press arrives before run() starts. Logged
            # rather than dropped silently: "buttons do nothing" is otherwise
            # indistinguishable from the handler never being installed.
            logger.warning("key %d pressed before the controller was running", index)
            return
        logger.debug("key %d pressed", index)
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

    def _subscription_set(self) -> list[JSONObject]:
        """Global subscriptions plus one status subscription per live pane."""
        subs = [subscription(kind) for kind in SUBSCRIPTIONS]
        subs.extend(
            subscription("pane.agent_status_changed", pane_id=pane_id)
            for pane_id in self._panes
        )
        return subs

    async def run_subscriptions(self) -> None:
        """Establish the initial subscription set (globals plus per pane)."""
        await self.prime()
        await self._client.subscribe(self._subscription_set())

    async def prime(self) -> None:
        """Re-read herdr's arrangement. Snapshot and listing are the truth.

        The pane map is rebuilt rather than patched, so its iteration order is
        exactly the snapshot's -- which is herdr's split-tree order. Patching
        would leave panes wherever they were first seen.
        """
        order, focused = await self.read_order()
        snapshot = await self._client.snapshot()

        before = set(self._panes)
        self._panes = {}
        for record in _iter_panes(snapshot):
            self._upsert(record)
        self._order = order
        self._focused_workspace = focused

        # Status subscriptions are per pane, so the set has to follow the panes.
        if set(self._panes) != before:
            await self._client.resubscribe(self._subscription_set())

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

        if event.kind == "pane.agent_status_changed":
            # The only event that fires on a status transition.
            pane_id = event.data.get("pane_id")
            status = event.data.get("agent_status")
            if isinstance(pane_id, str) and isinstance(status, str):
                existing = self._panes.get(pane_id)
                if existing is not None and existing.status != status:
                    self._panes[pane_id] = replace(existing, status=status)
                    # The old summary described the previous state, so it is now
                    # actively misleading -- drop it before anything repaints.
                    self._summaries.pop(pane_id, None)
                    if status in SUMMARISE_ON:
                        self._request_summary(pane_id)
                    self._dirty.set()
            return

        if event.kind == "pane.updated":
            record = event.data.get("pane")
            if isinstance(record, dict) and self._upsert(record):
                self._dirty.set()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._epoch = self._loop.time()
        self._surface.set_press_handler(self._on_press)
        # Stated explicitly at startup: presses only work once run() installs
        # this, so a script that drives the controller directly has a live
        # display and dead keys. The log should make that obvious.
        logger.info("press handler installed; keys are live")

        # Order matters: subscribe first so no live change is missed, then
        # throw away the replayed backlog, then snapshot. Snapshotting before
        # the backlog drains would just be overwritten by stale events.
        await self._client.subscribe(self._subscription_set())
        await self.drain_replay()
        await self.prime()

        painter = asyncio.create_task(self._paint_loop(), name="painter")
        animator = asyncio.create_task(self._animate_loop(), name="animator")
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
            for task in (painter, animator, reconciler):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._surface.set_press_handler(None)

    async def _animate_loop(self) -> None:
        """Drive pulsing and blinking from the shared clock.

        Sleeps to the next frame boundary rather than a fixed interval, so
        phase does not drift when a tick runs long -- drift would gradually
        desynchronise keys, which is the one thing this must not do.
        """
        loop = asyncio.get_running_loop()
        interval = 1.0 / ANIMATION_FPS
        while True:
            if any(a.animated for a in self._animations.values()):
                self.tick(loop.time())
            # Align to the grid so ticks stay in phase with the epoch.
            now = loop.time()
            await asyncio.sleep(max(0.0, interval - ((now - self._epoch) % interval)))

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

    surface = open_surface(
        use_device=not args.no_device,
        serial=args.serial,
    )

    summariser = None if args.no_summaries else build_summariser()
    if summariser is None and not args.no_summaries:
        logger.info("no FIREWORKS_API_KEY found; running without pane summaries")
    surface.open()

    # Two connections -- herdr resets a connection that both subscribes and
    # issues requests. See HerdrSession.
    client = HerdrSession(Path(args.socket) if args.socket else None)
    await client.connect()

    controller = DeckController(
        client, surface, mode=GroupingMode(args.mode), summariser=summariser
    )

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
        "--no-summaries",
        action="store_true",
        help=(
            "skip the three-word pane summaries even if a key is configured. "
            "They are already skipped when FIREWORKS_API_KEY is absent"
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
