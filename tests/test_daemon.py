"""Controller tests. No hardware and no herdr server required."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from herdr_streamdeck.daemon import STATUS_COLORS, DeckController, Pane, _iter_panes
from herdr_streamdeck.deck import ButtonFace, NullSurface
from herdr_streamdeck.protocol import Event, HerdrError, JSONObject


def pane_record(pane_id: str, **overrides: Any) -> JSONObject:
    record: JSONObject = {
        "pane_id": pane_id,
        "terminal_id": f"term_{pane_id}",
        "workspace_id": pane_id.split(":")[0],
        "agent": "claude",
        "agent_status": "idle",
    }
    record.update(overrides)
    return record


class StubClient:
    """Stands in for HerdrClient; records requests, replays canned events.

    Structurally satisfies daemon.HerdrLike, so no cast is needed.
    """

    def __init__(
        self, snapshot: JSONObject | None = None, events: list[Event] | None = None
    ) -> None:
        self._snapshot = snapshot or {}
        self._events = events or []
        self.requests: list[tuple[str, JSONObject | None]] = []
        self.subscriptions: list[JSONObject] = []

    async def request(self, method: str, params: JSONObject | None = None) -> JSONObject:
        self.requests.append((method, params))
        return {}

    async def snapshot(self) -> JSONObject:
        return self._snapshot

    async def subscribe(self, subscriptions: Sequence[JSONObject]) -> None:
        self.subscriptions.extend(subscriptions)

    async def events(self) -> AsyncIterator[Event]:
        for event in self._events:
            yield event


def make_controller(
    *, snapshot: JSONObject | None = None, keys: int = 4
) -> tuple[DeckController, NullSurface, StubClient]:
    client = StubClient(snapshot)
    surface = NullSurface(key_count_=keys)
    controller = DeckController(client, surface)
    return controller, surface, client


def as_pane(record: JSONObject) -> Pane:
    """Parse a record, failing the test if it is rejected."""
    pane = Pane.from_record(record)
    assert pane is not None
    return pane


def test_pane_from_record_requires_pane_id() -> None:
    assert Pane.from_record({"terminal_id": "t"}) is None


def test_pane_display_prefers_label_then_agent() -> None:
    assert as_pane(pane_record("w1:p1", label="reviewer")).display == "reviewer"
    assert as_pane(pane_record("w1:p1")).display == "claude"
    assert as_pane(pane_record("w1:p1", agent="", label="")).display == "w1:p1"


def test_iter_panes_walks_nested_snapshot() -> None:
    snapshot: JSONObject = {
        "workspaces": [
            {
                "workspace_id": "w1",
                "tabs": [{"tab_id": "w1:t1", "panes": [pane_record("w1:p1")]}],
            }
        ]
    }
    found = _iter_panes(snapshot)
    assert [p["pane_id"] for p in found] == ["w1:p1"]


def test_iter_panes_ignores_pane_id_without_terminal() -> None:
    # pane.closed events carry a bare pane_id; those are not pane records.
    assert _iter_panes({"data": {"pane_id": "w1:p1"}}) == []


async def test_prime_paints_from_snapshot() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w1:p1"), pane_record("w1:p2")]}
    controller, surface, _ = make_controller(snapshot=snapshot)
    await controller.prime()

    assert surface.faces[0].label == "claude"
    assert surface.faces[0].color == STATUS_COLORS["idle"]
    # Unused keys are blanked, not left stale.
    assert surface.faces[3].label == ""


async def test_status_change_recolours_the_key() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w1:p1")]}
    controller, surface, _ = make_controller(snapshot=snapshot)
    await controller.prime()
    assert surface.faces[0].color == STATUS_COLORS["idle"]

    controller.handle(
        Event(
            kind="pane.updated",
            raw_kind="pane_updated",
            data={"pane": pane_record("w1:p1", agent_status="blocked")},
        )
    )
    controller.repaint()

    assert surface.faces[0].color == STATUS_COLORS["blocked"]
    assert surface.faces[0].sublabel == "blocked"


async def test_closed_pane_frees_its_key() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w1:p1"), pane_record("w1:p2")]}
    controller, surface, _ = make_controller(snapshot=snapshot)
    await controller.prime()
    assert surface.faces[1].label == "claude"

    controller.handle(
        Event(kind="pane.closed", raw_kind="pane_closed", data={"pane_id": "w1:p1"})
    )
    controller.repaint()

    # w1:p2 slides down to key 0 and key 1 is blanked.
    assert surface.faces[1].label == ""


async def test_panes_without_agents_are_hidden_by_default() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w1:p1", agent=""), pane_record("w1:p2")]}
    controller, surface, _ = make_controller(snapshot=snapshot)
    await controller.prime()

    assert surface.faces[0].label == "claude"
    assert surface.faces[1].label == ""


async def test_key_assignment_is_stable_across_repaints() -> None:
    """Buttons must not shuffle when an unrelated pane updates."""
    snapshot: JSONObject = {
        "panes": [pane_record("w1:p2"), pane_record("w1:p1"), pane_record("w1:p3")]
    }
    controller, _, _ = make_controller(snapshot=snapshot)
    await controller.prime()
    before = list(controller._slots)

    controller.handle(
        Event(
            kind="pane.updated",
            raw_kind="pane_updated",
            data={"pane": pane_record("w1:p3", agent_status="working")},
        )
    )
    controller.repaint()

    assert controller._slots == before


async def test_more_panes_than_keys_is_truncated() -> None:
    snapshot: JSONObject = {"panes": [pane_record(f"w1:p{i}") for i in range(10)]}
    controller, _, _ = make_controller(snapshot=snapshot, keys=4)
    await controller.prime()
    assert len(controller._slots) == 4


async def test_press_focuses_the_mapped_pane() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w1:p1"), pane_record("w1:p2")]}
    controller, surface, client = make_controller(snapshot=snapshot)
    controller._loop = asyncio.get_running_loop()
    surface.set_press_handler(controller._on_press)
    await controller.prime()

    surface.press(1)
    await asyncio.sleep(0.01)

    assert ("pane.focus", {"pane_id": "w1:p2"}) in client.requests


async def test_release_does_not_focus() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w1:p1")]}
    controller, surface, client = make_controller(snapshot=snapshot)
    controller._loop = asyncio.get_running_loop()
    surface.set_press_handler(controller._on_press)
    await controller.prime()

    surface.press(0, pressed=False)
    await asyncio.sleep(0.01)

    assert client.requests == []


async def test_press_on_empty_key_is_ignored() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w1:p1")]}
    controller, surface, client = make_controller(snapshot=snapshot)
    controller._loop = asyncio.get_running_loop()
    surface.set_press_handler(controller._on_press)
    await controller.prime()

    surface.press(3)
    await asyncio.sleep(0.01)

    assert client.requests == []


def test_null_surface_rejects_out_of_range_key() -> None:
    surface = NullSurface(key_count_=4)
    with pytest.raises(IndexError):
        surface.set_face(9, ButtonFace(label="nope"))


# --------------------------------------------------------------- replay / drift
# herdr replays a historical backlog when a subscription starts, and not in
# causal order -- a pane.closed can arrive before its own pane.created. These
# pin the handling, since naive application resurrects dead panes forever.


async def test_snapshot_dedupes_panes_listed_twice() -> None:
    """A pane appears under both snapshot.agents[] and snapshot.panes[]."""
    record = pane_record("w1:p1")
    snapshot: JSONObject = {"agents": [record], "panes": [record]}
    found = _iter_panes(snapshot)
    assert [p["pane_id"] for p in found] == ["w1:p1"]


async def test_prime_drops_panes_absent_from_snapshot() -> None:
    """Snapshot is authoritative; anything it omits is gone."""
    controller, _, _ = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    await controller.prime()

    # A stale pane sneaks in, as an out-of-order replayed create would.
    controller.handle(
        Event(
            kind="pane.created",
            raw_kind="pane_created",
            data={"pane": pane_record("w1:p99")},
        )
    )
    controller.repaint()
    assert "w1:p99" in controller._slots

    await controller.prime()  # reconcile
    assert "w1:p99" not in controller._slots
    assert controller._slots == ["w1:p1"]


async def test_drain_replay_discards_the_backlog() -> None:
    replayed = [
        Event(
            kind="pane.created", raw_kind="pane_created", data={"pane": pane_record("w1:p3")}
        ),
        Event(kind="pane.closed", raw_kind="pane_closed", data={"pane_id": "w1:p3"}),
    ]
    client = StubClient({"panes": [pane_record("w1:p1")]}, events=replayed)
    surface = NullSurface(key_count_=4)
    controller = DeckController(client, surface)

    assert await controller.drain_replay(quiet=0.05, limit=1.0) == 2
    await controller.prime()
    # The replayed pane must not survive into the model.
    assert controller._slots == ["w1:p1"]


async def test_focus_drops_a_pane_the_server_says_is_gone() -> None:
    class GoneClient(StubClient):
        async def request(self, method: str, params: JSONObject | None = None) -> JSONObject:
            await super().request(method, params)
            raise HerdrError("pane_not_found", "pane is gone")

    client = GoneClient({"panes": [pane_record("w1:p1"), pane_record("w1:p2")]})
    surface = NullSurface(key_count_=4)
    controller = DeckController(client, surface)
    controller._loop = asyncio.get_running_loop()
    await controller.prime()
    assert "w1:p1" in controller._slots

    controller._dispatch_press(0)
    await asyncio.sleep(0.01)
    controller.repaint()

    assert "w1:p1" not in controller._slots


async def test_focus_keeps_pane_on_other_errors() -> None:
    """Only pane_not_found means gone; a transient error must not drop it."""

    class FlakyClient(StubClient):
        async def request(self, method: str, params: JSONObject | None = None) -> JSONObject:
            await super().request(method, params)
            raise HerdrError("internal_error", "try again")

    client = FlakyClient({"panes": [pane_record("w1:p1")]})
    surface = NullSurface(key_count_=4)
    controller = DeckController(client, surface)
    controller._loop = asyncio.get_running_loop()
    await controller.prime()

    controller._dispatch_press(0)
    await asyncio.sleep(0.01)
    controller.repaint()

    assert controller._slots == ["w1:p1"]


async def test_run_returns_when_the_event_stream_closes() -> None:
    """Self-reaping: herdr orphans startup processes, so exit rather than retry.

    A daemon that reconnected forever would survive a herdr restart still
    holding the Stream Deck, locking out its replacement.
    """
    client = StubClient({"panes": [pane_record("w1:p1")]}, events=[])
    surface = NullSurface(key_count_=4)
    controller = DeckController(client, surface, reconcile_interval=3600)

    # Returns rather than hanging once the stream ends.
    await asyncio.wait_for(controller.run(), timeout=10)

    assert client.subscriptions, "should have subscribed before streaming"
    assert surface._handler is None, "press handler released on shutdown"
