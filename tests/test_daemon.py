"""Controller tests. No hardware and no herdr server required."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from herdr_streamdeck.daemon import (
    HOLD_SECONDS,
    STATUS_COLORS,
    STRUCTURAL_EVENTS,
    DeckController,
    ReplyMenu,
    _iter_panes,
    menu_layout,
    worth_summarising,
)
from herdr_streamdeck.deck import (
    EMPTY_BACKGROUND,
    ButtonFace,
    DeckDisconnected,
    KeyFrames,
    NullSurface,
    PressHandler,
)
from herdr_streamdeck.icons import mark_for
from herdr_streamdeck.layout import Grid
from herdr_streamdeck.protocol import Event, HerdrError, JSONObject
from herdr_streamdeck.summary import PaneSummary, Reply, Summariser


def pane_record(pane_id: str, workspace: str = "w1", **overrides: Any) -> JSONObject:
    record: JSONObject = {
        "pane_id": pane_id,
        "terminal_id": f"term_{pane_id}",
        "workspace_id": workspace,
        "tab_id": f"{workspace}:t1",
        "agent": "claude",
        "agent_status": "idle",
    }
    record.update(overrides)
    return record


class StubClient:
    """Structurally satisfies daemon.HerdrLike; no cast needed."""

    def __init__(
        self,
        snapshot: JSONObject | None = None,
        events: list[Event] | None = None,
        workspaces: Sequence[str] = ("w1",),
    ) -> None:
        self._snapshot = snapshot or {}
        self._events = events or []
        self._workspaces = list(workspaces)
        self.requests: list[tuple[str, JSONObject | None]] = []
        self.subscriptions: list[JSONObject] = []
        self.resubscribes = 0
        self.pane_text = "agent output"

    async def request(self, method: str, params: JSONObject | None = None) -> JSONObject:
        self.requests.append((method, params))
        if method == "workspace.list":
            return {
                "workspaces": [
                    {"workspace_id": w, "label": w, "focused": i == 0}
                    for i, w in enumerate(self._workspaces)
                ]
            }
        if method == "tab.list":
            return {"tabs": []}
        if method == "pane.read":
            return {"read": {"text": self.pane_text}}
        return {}

    async def snapshot(self) -> JSONObject:
        return self._snapshot

    async def subscribe(self, subscriptions: Sequence[JSONObject]) -> None:
        self.subscriptions = list(subscriptions)

    async def resubscribe(self, subscriptions: Sequence[JSONObject]) -> None:
        self.subscriptions = list(subscriptions)
        self.resubscribes += 1

    async def events(self) -> AsyncIterator[Event]:
        for event in self._events:
            yield event


def make_controller(
    *,
    snapshot: JSONObject | None = None,
    workspaces: Sequence[str] = ("w1",),
    rows: int = 3,
    columns: int = 5,
) -> tuple[DeckController, NullSurface, StubClient]:
    client = StubClient(snapshot, workspaces=workspaces)
    surface = NullSurface(key_count_=rows * columns, key_layout_=(rows, columns))
    return DeckController(client, surface), surface, client


def tap(controller: DeckController, index: int) -> None:
    """Press and release, the gesture that focuses a pane."""
    controller._key_down(index)
    controller._key_up(index)


def updated(record: JSONObject) -> Event:
    return Event(kind="pane.updated", raw_kind="pane_updated", data={"pane": record})


# -------------------------------------------------------------------- snapshot


def test_iter_panes_walks_nested_snapshot() -> None:
    snapshot: JSONObject = {"workspaces": [{"tabs": [{"panes": [pane_record("w1:p1")]}]}]}
    assert [p["pane_id"] for p in _iter_panes(snapshot)] == ["w1:p1"]


def test_iter_panes_dedupes_panes_listed_twice() -> None:
    """Panes appear under both snapshot.agents[] and snapshot.panes[]."""
    record = pane_record("w1:p1")
    found = _iter_panes({"agents": [record], "panes": [record]})
    assert [p["pane_id"] for p in found] == ["w1:p1"]


def test_iter_panes_ignores_bare_pane_ids() -> None:
    # pane.closed events carry a bare pane_id; that is not a pane record.
    assert _iter_panes({"data": {"pane_id": "w1:p1"}}) == []


def test_iter_panes_preserves_snapshot_order() -> None:
    """Order is herdr's split-tree order and must survive the walk."""
    snapshot: JSONObject = {
        "panes": [pane_record("w1:p9"), pane_record("w1:p1"), pane_record("w1:p5")]
    }
    assert [p["pane_id"] for p in _iter_panes(snapshot)] == ["w1:p9", "w1:p1", "w1:p5"]


# ---------------------------------------------------------------------- render


async def test_prime_mirrors_herdr_column_order() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w2:p1", "w2"), pane_record("w1:p1", "w1")]}
    controller, _, _ = make_controller(snapshot=snapshot, workspaces=("w2", "w1"))
    await controller.prime()

    # w2 leads the listing, so it owns column 0 despite sorting later.
    assert controller._columns[0] is not None
    assert controller._columns[0].id == "w2"
    assert controller._columns[1] is not None
    assert controller._columns[1].id == "w1"


async def test_agent_mark_and_badge_are_drawn() -> None:
    snapshot: JSONObject = {
        "panes": [pane_record("w1:p1", display_agent="qwencode", title="deploy-review")]
    }
    controller, surface, _ = make_controller(snapshot=snapshot)
    await controller.prime()

    face = surface.faces[0]
    assert face.mark == mark_for("qwencode").glyph
    assert face.badge == "deploy-r", "badge is abbreviated to fit the key"


async def test_status_sets_the_strip_not_the_field() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w1:p1")]}
    controller, surface, _ = make_controller(snapshot=snapshot)
    await controller.prime()
    assert surface.faces[0].status_color == STATUS_COLORS["idle"]

    controller.handle(updated(pane_record("w1:p1", agent_status="blocked")))
    controller.repaint()

    face = surface.faces[0]
    assert face.status_color == STATUS_COLORS["blocked"]
    # The field stays neutral: status lives in the strip.
    assert face.background == ButtonFace().background


async def test_unknown_status_draws_no_strip() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w1:p1", agent_status="unknown")]}
    controller, surface, _ = make_controller(snapshot=snapshot)
    await controller.prime()
    assert surface.faces[0].status_color is None


async def test_unoccupied_keys_are_blank() -> None:
    controller, surface, _ = make_controller(snapshot={"panes": []})
    await controller.prime()
    assert all(f.mark == "" and f.badge == "" for f in surface.faces.values())


# ---------------------------------------------------------------------- events


@pytest.mark.parametrize("kind", sorted(STRUCTURAL_EVENTS))
async def test_structural_events_schedule_a_reread(kind: str) -> None:
    """Ordering is not in the payload, so the model is rebuilt from herdr."""
    controller, _, _ = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    await controller.prime()

    controller.handle(Event(kind=kind, raw_kind=kind.replace(".", "_"), data={}))

    assert controller._restructure is True
    assert controller._dirty.is_set()


async def test_cosmetic_update_does_not_trigger_a_reread() -> None:
    """A status change must not cost a snapshot round-trip."""
    controller, _, _ = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    await controller.prime()
    controller._dirty.clear()

    controller.handle(updated(pane_record("w1:p1", agent_status="working")))

    assert controller._restructure is False
    assert controller._dirty.is_set()


async def test_identical_update_is_not_a_change() -> None:
    controller, _, _ = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    await controller.prime()
    controller._dirty.clear()

    controller.handle(updated(pane_record("w1:p1")))

    assert not controller._dirty.is_set()


async def test_prime_drops_panes_absent_from_the_snapshot() -> None:
    controller, _, _ = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    await controller.prime()

    # A stale pane sneaks in, as an out-of-order replayed create would.
    controller.handle(updated(pane_record("w1:p99")))
    assert "w1:p99" in controller._panes

    await controller.prime()
    assert "w1:p99" not in controller._panes


async def test_drain_replay_discards_the_backlog() -> None:
    replayed = [
        Event(
            kind="pane.created",
            raw_kind="pane_created",
            data={"pane": pane_record("w1:p3")},
        ),
        Event(kind="pane.closed", raw_kind="pane_closed", data={"pane_id": "w1:p3"}),
    ]
    client = StubClient({"panes": [pane_record("w1:p1")]}, events=replayed)
    controller = DeckController(client, NullSurface(key_count_=15))

    assert await controller.drain_replay(quiet=0.05, limit=1.0) == 2
    await controller.prime()
    assert list(controller._panes) == ["w1:p1"]


# --------------------------------------------------------------------- presses


async def test_press_focuses_the_pane_under_that_key() -> None:
    snapshot: JSONObject = {
        "panes": [
            pane_record("w1:p1", "w1"),
            pane_record("w1:p2", "w1"),
            pane_record("w2:p1", "w2"),
        ]
    }
    controller, surface, client = make_controller(snapshot=snapshot, workspaces=("w1", "w2"))
    controller._loop = asyncio.get_running_loop()
    surface.set_press_handler(controller._on_press)
    await controller.prime()

    surface.tap(5)  # row 1, column 0 -> second pane of w1
    await asyncio.sleep(0.01)
    assert ("pane.focus", {"pane_id": "w1:p2"}) in client.requests

    surface.tap(1)  # row 0, column 1 -> first pane of w2
    await asyncio.sleep(0.01)
    assert ("pane.focus", {"pane_id": "w2:p1"}) in client.requests


async def test_release_does_not_focus() -> None:
    controller, surface, client = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    controller._loop = asyncio.get_running_loop()
    surface.set_press_handler(controller._on_press)
    await controller.prime()
    client.requests.clear()

    surface.press(0, pressed=False)
    await asyncio.sleep(0.01)
    assert client.requests == []


async def test_press_on_an_empty_key_is_ignored() -> None:
    controller, surface, client = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    controller._loop = asyncio.get_running_loop()
    surface.set_press_handler(controller._on_press)
    await controller.prime()
    client.requests.clear()

    surface.press(14)
    await asyncio.sleep(0.01)
    assert client.requests == []


async def test_focus_drops_a_pane_the_server_says_is_gone() -> None:
    class GoneClient(StubClient):
        async def request(self, method: str, params: JSONObject | None = None) -> JSONObject:
            result = await super().request(method, params)
            if method == "pane.focus":
                raise HerdrError("pane_not_found", "pane is gone")
            return result

    client = GoneClient({"panes": [pane_record("w1:p1"), pane_record("w1:p2")]})
    controller = DeckController(client, NullSurface(key_count_=15))
    controller._loop = asyncio.get_running_loop()
    await controller.prime()

    tap(controller, 0)
    await asyncio.sleep(0.01)
    assert "w1:p1" not in controller._panes


async def test_focus_keeps_the_pane_on_other_errors() -> None:
    """Only pane_not_found means gone; a transient error must not drop it."""

    class FlakyClient(StubClient):
        async def request(self, method: str, params: JSONObject | None = None) -> JSONObject:
            result = await super().request(method, params)
            if method == "pane.focus":
                raise HerdrError("internal_error", "try again")
            return result

    client = FlakyClient({"panes": [pane_record("w1:p1")]})
    controller = DeckController(client, NullSurface(key_count_=15))
    controller._loop = asyncio.get_running_loop()
    await controller.prime()

    tap(controller, 0)
    await asyncio.sleep(0.01)
    assert "w1:p1" in controller._panes


# ------------------------------------------------------------------- lifecycle


async def test_run_returns_when_the_event_stream_closes() -> None:
    """Self-reaping: herdr orphans startup processes, so exit rather than retry."""
    client = StubClient({"panes": [pane_record("w1:p1")]}, events=[])
    surface = NullSurface(key_count_=15)
    controller = DeckController(client, surface, reconcile_interval=3600)

    await asyncio.wait_for(controller.run(), timeout=10)

    assert client.subscriptions, "should have subscribed before streaming"
    assert surface._handler is None, "press handler released on shutdown"


def test_null_surface_rejects_out_of_range_key() -> None:
    surface = NullSurface(key_count_=4)
    with pytest.raises(IndexError):
        surface.set_face(9, ButtonFace(mark="x"))


# -------------------------------------------------------------------- animation


async def test_two_working_panes_share_a_frame() -> None:
    """Synchronisation, at the level that matters: same instant, same frame.

    They are deliberately given different pane ids and columns; if phase were
    ever keyed off per-pane state they would drift apart.
    """
    snapshot: JSONObject = {
        "panes": [
            pane_record("w1:p1", "w1", agent_status="working"),
            pane_record("w2:p1", "w2", agent_status="working"),
        ]
    }
    controller, surface, _ = make_controller(snapshot=snapshot, workspaces=("w1", "w2"))
    controller._epoch = 0.0
    await controller.prime()

    for instant in (0.0, 0.3, 1.1, 2.05, 7.7):
        controller.tick(instant)
        assert surface.shown[0] == surface.shown[1], f"drifted at t={instant}"


async def test_a_pulse_actually_changes_frames() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w1:p1", agent_status="working")]}
    controller, surface, _ = make_controller(snapshot=snapshot)
    controller._epoch = 0.0
    await controller.prime()

    seen = set()
    for step in range(48):
        controller.tick(step / 20)
        seen.add(surface.shown[0])
    assert len(seen) >= 12, f"pulse used only {len(seen)} frames"


async def test_steady_statuses_do_not_rewrite() -> None:
    """Writing costs 1.34 ms a key; idle keys must not consume that forever."""
    snapshot: JSONObject = {"panes": [pane_record("w1:p1", agent_status="idle")]}
    controller, surface, _ = make_controller(snapshot=snapshot)
    controller._epoch = 0.0
    await controller.prime()

    before = surface.writes
    for step in range(40):
        controller.tick(step / 20)
    assert surface.writes == before, "steady key was rewritten"


async def test_done_is_brighter_than_idle() -> None:
    snapshot: JSONObject = {
        "panes": [
            pane_record("w1:p1", "w1", agent_status="idle"),
            pane_record("w2:p1", "w2", agent_status="done"),
        ]
    }
    controller, surface, _ = make_controller(snapshot=snapshot, workspaces=("w1", "w2"))
    controller._epoch = 0.0
    await controller.prime()
    controller.tick(0.0)

    assert surface.shown[1] > surface.shown[0]
    assert surface.shown[1] == surface.levels - 1, "done should be full brightness"


async def test_blocked_alternates_between_two_frames() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w1:p1", agent_status="blocked")]}
    controller, surface, _ = make_controller(snapshot=snapshot)
    controller._epoch = 0.0
    await controller.prime()

    levels = set()
    for step in range(40):
        controller.tick(step / 20)
        levels.add(surface.shown[0])
    assert len(levels) == 2, f"blink should use exactly two frames, got {levels}"
    assert max(levels) == surface.levels - 1


async def test_unchanged_face_is_not_re_rendered() -> None:
    """Rendering is ~1.26 ms a key; a pulse must not pay it every frame."""
    snapshot: JSONObject = {"panes": [pane_record("w1:p1", agent_status="working")]}
    controller, _, _ = make_controller(snapshot=snapshot)
    await controller.prime()
    first = controller._frames[0]

    controller.repaint()

    assert controller._frames[0] is first, "identical face was re-rendered"


async def test_changed_face_is_re_rendered() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w1:p1")]}
    controller, _, _ = make_controller(snapshot=snapshot)
    await controller.prime()
    first = controller._frames[0]

    controller.handle(updated(pane_record("w1:p1", title="renamed")))
    controller.repaint()

    assert controller._frames[0] is not first
    assert controller._frames[0].face.badge == "renamed"


async def test_icon_override_reaches_the_face(monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="hsd-icons"))
    (root / "icons").mkdir()
    icon = root / "icons" / "claude.png"
    icon.write_bytes(b"stub")
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(root))

    controller, surface, _ = make_controller(
        snapshot={"panes": [pane_record("w1:p1", agent="claude")]}
    )
    await controller.prime()

    assert surface.faces[0].icon == icon


async def test_no_icon_override_leaves_the_glyph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERDR_PLUGIN_CONFIG_DIR", raising=False)
    controller, surface, _ = make_controller(
        snapshot={"panes": [pane_record("w1:p1", agent="claude")]}
    )
    await controller.prime()

    assert surface.faces[0].icon is None
    assert surface.faces[0].mark == mark_for("claude").glyph


# ------------------------------------------------------- status subscriptions
# pane.updated carries an agent_status field but does NOT fire when status
# changes -- only pane.agent_status_changed does, and it is pane-scoped.
# Verified by driving transitions with pane.report_agent. Relying on the global
# event left status refreshing only on the 60s reconcile.


async def test_status_is_subscribed_per_pane() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w1:p1"), pane_record("w1:p2")]}
    controller, _, client = make_controller(snapshot=snapshot)
    await controller.run_subscriptions()

    scoped = [s for s in client.subscriptions if s.get("type") == "pane.agent_status_changed"]
    assert {s["pane_id"] for s in scoped} == {"w1:p1", "w1:p2"}


async def test_status_event_updates_the_pane() -> None:
    controller, _, _ = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    await controller.prime()
    controller._dirty.clear()

    controller.handle(
        Event(
            kind="pane.agent_status_changed",
            raw_kind="pane.agent_status_changed",
            data={"pane_id": "w1:p1", "agent_status": "working"},
        )
    )

    assert controller._panes["w1:p1"].status == "working"
    assert controller._dirty.is_set()


async def test_repeated_status_event_is_not_a_change() -> None:
    controller, _, _ = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    await controller.prime()
    controller._dirty.clear()

    controller.handle(
        Event(
            kind="pane.agent_status_changed",
            raw_kind="pane.agent_status_changed",
            data={"pane_id": "w1:p1", "agent_status": "idle"},
        )
    )
    assert not controller._dirty.is_set()


async def test_status_event_for_an_unknown_pane_is_ignored() -> None:
    controller, _, _ = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    await controller.prime()

    controller.handle(
        Event(
            kind="pane.agent_status_changed",
            raw_kind="pane.agent_status_changed",
            data={"pane_id": "w9:p9", "agent_status": "working"},
        )
    )
    assert "w9:p9" not in controller._panes


async def test_subscriptions_are_rebuilt_when_the_pane_set_changes() -> None:
    client = StubClient({"panes": [pane_record("w1:p1")]})
    controller = DeckController(client, NullSurface(key_count_=15))
    await controller.prime()
    before = client.resubscribes

    # Same panes -> no reconnect; the stream swap is not free.
    await controller.prime()
    assert client.resubscribes == before

    client._snapshot = {"panes": [pane_record("w1:p1"), pane_record("w1:p2")]}
    await controller.prime()
    assert client.resubscribes == before + 1
    scoped = {
        s["pane_id"]
        for s in client.subscriptions
        if s.get("type") == "pane.agent_status_changed"
    }
    assert scoped == {"w1:p1", "w1:p2"}


@dataclass
class RecordingSurface(NullSurface):
    """NullSurface that remembers every press handler installed on it."""

    installs: list[PressHandler] = field(default_factory=list)

    def set_press_handler(self, handler: PressHandler | None) -> None:
        if handler is not None:
            self.installs.append(handler)
        super().set_press_handler(handler)


async def test_run_installs_the_press_handler() -> None:
    """Keys are dead until run() wires this up.

    Driving the controller directly -- prime()/tick() from a script -- gives a
    correct display with unresponsive keys, which looks like a hardware fault.
    """
    surface = RecordingSurface(key_count_=15)
    client = StubClient({"panes": [pane_record("w1:p1")]}, events=[])
    controller = DeckController(client, surface, reconcile_interval=3600)

    await asyncio.wait_for(controller.run(), timeout=10)

    assert surface.installs, "run() must install a press handler"
    assert surface.installs[0] == controller._on_press


# -------------------------------------------------------------------- summaries


def status_changed(pane_id: str, status: str) -> Event:
    return Event(
        kind="pane.agent_status_changed",
        raw_kind="pane.agent_status_changed",
        data={"pane_id": pane_id, "agent_status": status},
    )


def summariser_returning(summary: PaneSummary | None, calls: list[str]) -> Summariser:
    def send(body: bytes, timeout: float) -> bytes:
        raise AssertionError("transport should not be reached")

    class Fixed(Summariser):
        async def summarise(self, transcript: str) -> PaneSummary | None:
            calls.append(transcript)
            return summary

    return Fixed(transport=send)


SUMMARY = PaneSummary(
    phrase="remove or deprecate",
    waiting=True,
    replies=(
        Reply("affirmative", "Remove", "Remove it."),
        Reply("alternative", "Deprecate", "Keep it behind a deprecation warning."),
    ),
)
SHOWN = "remove or deprecate?"
"""What reaches the key: the question mark is appended from `waiting`, not
spent as one of the words."""


async def test_blocking_asks_for_a_summary_and_shows_it() -> None:
    calls: list[str] = []
    controller, surface, client = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    controller._summariser = summariser_returning(SUMMARY, calls)
    await controller.prime()

    controller.handle(status_changed("w1:p1", "blocked"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    controller.repaint()

    assert calls == ["agent output"], "the pane's own output should be summarised"
    assert surface.faces[0].summary == SHOWN
    assert [r[0] for r in client.requests].count("pane.read") == 1


async def test_a_pane_starting_work_is_not_summarised() -> None:
    """Summaries cost money and a working pane changes constantly."""
    calls: list[str] = []
    controller, _, _ = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    controller._summariser = summariser_returning(SUMMARY, calls)
    await controller.prime()

    controller.handle(status_changed("w1:p1", "working"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == []


async def test_finishing_a_turn_is_summarised() -> None:
    """The transition that actually happens. herdr emits `working` then `idle`
    when an agent completes -- verified by driving a real agent through a full
    turn. An earlier version keyed on `done`, which a pane never reaches, so
    summaries fired essentially never."""
    calls: list[str] = []
    controller, surface, _ = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    controller._summariser = summariser_returning(SUMMARY, calls)
    await controller.prime()

    controller.handle(status_changed("w1:p1", "working"))
    controller.handle(status_changed("w1:p1", "idle"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    controller.repaint()

    assert calls == ["agent output"]
    assert surface.faces[0].summary == SHOWN


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("working", "idle", True),  # what herdr actually emits on completion
        ("working", "blocked", True),
        ("working", "done", True),  # in the enum, never observed on a pane
        ("idle", "blocked", True),  # arriving blocked always deserves words
        ("idle", "working", False),
        ("unknown", "idle", False),  # a pane merely being seen for the first time
        ("idle", "idle", False),
        ("working", "working", False),
    ],
)
def test_which_transitions_are_worth_a_model_call(
    before: str, after: str, expected: bool
) -> None:
    assert worth_summarising(before, after) is expected


async def test_a_new_status_drops_the_old_summary_immediately() -> None:
    """The words described the previous state, so leaving them up is worse than
    showing nothing -- the key would assert something no longer true."""
    calls: list[str] = []
    controller, surface, _ = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    controller._summariser = summariser_returning(SUMMARY, calls)
    await controller.prime()

    controller.handle(status_changed("w1:p1", "blocked"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    controller.repaint()
    assert surface.faces[0].summary

    controller.handle(status_changed("w1:p1", "working"))
    controller.repaint()
    assert surface.faces[0].summary == ""


async def test_a_failing_summariser_leaves_the_deck_working() -> None:
    calls: list[str] = []
    controller, surface, _ = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    controller._summariser = summariser_returning(None, calls)
    await controller.prime()

    controller.handle(status_changed("w1:p1", "blocked"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    controller.repaint()

    assert surface.faces[0].summary == ""
    assert surface.faces[0].mark == mark_for("claude").glyph, "the key still renders"


async def test_no_summariser_is_a_supported_state() -> None:
    controller, surface, client = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    await controller.prime()
    controller.handle(status_changed("w1:p1", "blocked"))
    await asyncio.sleep(0)
    controller.repaint()

    assert surface.faces[0].summary == ""
    assert "pane.read" not in [r[0] for r in client.requests]


async def test_a_summary_for_a_pane_that_has_gone_is_discarded() -> None:
    """The read and the model call take time; the pane can close meanwhile."""
    calls: list[str] = []
    controller, _, _ = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    controller._summariser = summariser_returning(SUMMARY, calls)
    await controller.prime()

    controller.handle(status_changed("w1:p1", "blocked"))
    controller._remove("w1:p1")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert controller._summaries == {}


async def test_replies_are_kept_but_nothing_sends_them() -> None:
    """They are stored for an interaction that has not been designed yet."""
    calls: list[str] = []
    controller, _, client = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    controller._summariser = summariser_returning(SUMMARY, calls)
    await controller.prime()

    controller.handle(status_changed("w1:p1", "blocked"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(controller.replies_for("w1:p1")) == 2
    assert not any(m.startswith("agent.") or m == "pane.send_text" for m, _ in client.requests)


# ----------------------------------------------------------- the reply menu


def test_the_menu_uses_the_whole_deck() -> None:
    """Choices left, controls right, the text you are about to send between.

    The key that was held is not reserved: once the menu is open the question
    is which reply, not which pane, and keeping it would spend the widest part
    of the display answering a question nobody is asking.
    """
    layout = menu_layout(Grid(rows=3, columns=5), count=3)
    assert sorted(layout.options) == [0, 5, 10]
    assert layout.accept == 4
    assert layout.back == 14
    assert layout.preview == (1, 2, 3, 6, 7, 8, 11, 12, 13)
    assert len(set(layout.options) | set(layout.preview) | {layout.accept, layout.back}) == 14


def test_the_menu_offers_no_more_options_than_it_has_rows() -> None:
    layout = menu_layout(Grid(rows=3, columns=5), count=9)
    assert len(layout.options) == 3


async def menued() -> tuple[DeckController, NullSurface, StubClient]:
    calls: list[str] = []
    controller, surface, client = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    controller._loop = asyncio.get_running_loop()
    controller._summariser = summariser_returning(SUMMARY, calls)
    surface.set_press_handler(controller._on_press)
    await controller.prime()
    controller.handle(status_changed("w1:p1", "working"))
    controller.handle(status_changed("w1:p1", "blocked"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return controller, surface, client


async def open_menu(controller: DeckController, surface: NullSurface) -> None:
    surface.press(0)
    await asyncio.sleep(HOLD_SECONDS + 0.05)
    surface.press(0, pressed=False)
    await asyncio.sleep(0.02)


async def test_holding_opens_a_menu_that_stays_open() -> None:
    """No timeout to race. It closes when you say so."""
    controller, surface, _ = await menued()
    await open_menu(controller, surface)

    assert controller._menu is not None
    await asyncio.sleep(0.4)
    assert controller._menu is not None, "it must not time out"
    assert surface.faces[4].summary == "Send"
    assert surface.faces[14].summary == "Back"


async def test_the_selected_option_is_the_one_with_a_border() -> None:
    controller, surface, _ = await menued()
    await open_menu(controller, surface)

    # Captured rather than asserted in place: asserting None then not-None on
    # the same attribute narrows it for mypy and kills the rest of the test.
    first_before = surface.faces[0].border
    second_before = surface.faces[5].border

    surface.press(5)
    await asyncio.sleep(0.02)
    first_after = surface.faces[0].border
    second_after = surface.faces[5].border

    assert first_before is not None, "the first option starts selected"
    assert second_before is None
    assert first_after is None
    assert second_after is not None


async def test_the_preview_shows_the_selected_reply_verbatim() -> None:
    """Character boundaries, not word boundaries: the cells are meant to read
    as one screen, so the text is simply chunked across them."""
    controller, surface, _ = await menued()
    await open_menu(controller, surface)

    rows = [(1, 2, 3), (6, 7, 8), (11, 12, 13)]
    lines = []
    for row in rows:
        cells = [surface.faces[k].summary.split("\n") for k in row]
        for slot in range(2):
            lines.append("".join(c[slot] if slot < len(c) else "" for c in cells))
    shown = " ".join(" ".join(lines).split())
    assert shown == SUMMARY.replies[0].text, "the exact text, not the label"


async def test_every_preview_key_uses_the_same_type_size() -> None:
    """A line crossing the deck must not step up and down as it goes."""
    controller, surface, _ = await menued()
    await open_menu(controller, surface)

    sizes = {
        surface.faces[k].summary_size for k in (1, 2, 3, 6, 7, 8) if surface.faces[k].summary
    }
    assert len(sizes) == 1 and 0 not in sizes


async def test_selecting_a_different_option_updates_the_preview() -> None:
    controller, surface, _ = await menued()
    await open_menu(controller, surface)
    before = surface.faces[1].summary

    surface.press(5)
    await asyncio.sleep(0.02)
    assert surface.faces[1].summary != before


async def test_send_sends_the_selection_and_closes() -> None:
    controller, surface, client = await menued()
    await open_menu(controller, surface)

    surface.press(4)
    await asyncio.sleep(0.05)

    assert ("agent.prompt", {"target": "w1:p1", "text": "Remove it."}) in client.requests
    assert controller._menu is None


async def test_back_closes_without_sending() -> None:
    controller, surface, client = await menued()
    await open_menu(controller, surface)

    surface.press(14)
    await asyncio.sleep(0.05)

    assert controller._menu is None
    assert not any(m == "agent.prompt" for m, _ in client.requests)


async def test_pressing_the_preview_does_nothing() -> None:
    """It is the text about to be sent, not a button. Nudging it must not send."""
    controller, surface, client = await menued()
    await open_menu(controller, surface)

    for key in (1, 7, 13):
        surface.press(key)
    await asyncio.sleep(0.05)

    assert controller._menu is not None
    assert not any(m == "agent.prompt" for m, _ in client.requests)


async def test_the_menu_closes_when_the_agent_gets_back_to_work() -> None:
    controller, surface, _ = await menued()
    await open_menu(controller, surface)

    controller.handle(status_changed("w1:p1", "working"))
    assert controller._menu is None


async def test_holding_a_pane_with_no_replies_does_nothing() -> None:
    controller, surface, client = make_controller(snapshot={"panes": [pane_record("w1:p1")]})
    controller._loop = asyncio.get_running_loop()
    surface.set_press_handler(controller._on_press)
    await controller.prime()

    surface.press(0)
    await asyncio.sleep(HOLD_SECONDS + 0.05)

    assert controller._menu is None
    surface.press(0, pressed=False)
    await asyncio.sleep(0.02)
    assert not any(m == "agent.prompt" for m, _ in client.requests)


async def test_a_tap_focuses_and_never_opens_the_menu() -> None:
    controller, surface, client = await menued()

    surface.tap(0)
    await asyncio.sleep(0.05)

    assert controller._menu is None
    assert ("pane.focus", {"pane_id": "w1:p1"}) in client.requests


# -------------------------------------------------------- summaries persist


async def test_a_summary_survives_being_looked_at() -> None:
    """Glancing at the deck and looking away used to lose the one thing you
    came to read. Nothing about viewing it makes it wrong."""
    controller, surface, _ = await menued()
    controller.repaint()
    assert surface.faces[0].summary == SHOWN

    for _ in range(20):
        controller.tick(now=5.0)
        controller.repaint()

    assert surface.faces[0].summary == SHOWN


async def test_a_summary_survives_a_status_change_that_is_not_work() -> None:
    controller, surface, _ = await menued()
    controller.handle(status_changed("w1:p1", "idle"))
    controller.repaint()
    assert surface.faces[0].summary == SHOWN


async def test_a_summary_is_cleared_when_the_agent_starts_working() -> None:
    """Then, and only then, it describes something that is no longer true."""
    controller, surface, _ = await menued()
    controller.handle(status_changed("w1:p1", "working"))
    controller.repaint()
    assert surface.faces[0].summary == ""


# ------------------------------------------------------------- disconnection


@dataclass
class FlakySurface(NullSurface):
    """A surface whose device can be yanked and plugged back in."""

    plugged: bool = True
    writes_attempted: int = 0
    reopen_attempts: int = 0

    @property
    def connected(self) -> bool:
        return self.plugged

    def write(self, index: int, frames: KeyFrames, level_index: int) -> None:
        self.writes_attempted += 1
        if not self.plugged:
            raise DeckDisconnected("gone")
        super().write(index, frames, level_index)

    def render(self, face: ButtonFace) -> KeyFrames:
        if not self.plugged:
            raise DeckDisconnected("gone")
        return super().render(face)

    def set_brightness(self, percent: int) -> None:
        if not self.plugged:
            raise DeckDisconnected("gone")
        super().set_brightness(percent)

    def reopen(self) -> bool:
        self.reopen_attempts += 1
        return self.plugged


async def flaky() -> tuple[DeckController, FlakySurface]:
    surface = FlakySurface(key_count_=15, key_layout_=(3, 5))
    controller = DeckController(
        StubClient({"panes": [pane_record("w1:p1")]}), surface, reconcile_interval=99
    )
    controller._loop = asyncio.get_running_loop()
    await controller.prime()
    return controller, surface


def force_writes(controller: DeckController) -> None:
    """Make the next tick actually reach the device.

    tick() skips a key whose level has not moved, and an idle pane is steady,
    so without this the deck is never touched and a disconnect goes unnoticed.
    """
    controller._shown.clear()


async def test_a_disconnect_is_reported_once_not_per_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every write goes to USB, so a pulled deck fails all fifteen of them at
    20 fps. Logging each one produced 300 stack traces a second."""
    controller, surface = await flaky()
    caplog.set_level(logging.WARNING, logger="herdr_streamdeck")

    surface.plugged = False
    for _ in range(50):
        force_writes(controller)
        controller.tick(now=1.0)

    said = [r for r in caplog.records if "disconnected" in r.message]
    assert len(said) == 1, f"logged {len(said)} times"
    assert not any(r.exc_info for r in caplog.records), "no stack traces"


async def test_nothing_is_written_while_the_deck_is_away() -> None:
    controller, surface = await flaky()
    surface.plugged = False
    force_writes(controller)
    controller.tick(now=1.0)
    before = surface.writes_attempted

    for _ in range(20):
        force_writes(controller)
        controller.tick(now=2.0)
        controller.repaint()

    assert surface.writes_attempted == before, "it kept talking to an absent deck"


async def test_it_comes_back_when_the_deck_does() -> None:
    controller, surface = await flaky()
    import herdr_streamdeck.daemon as daemon

    original = daemon.RECONNECT_SECONDS
    daemon.RECONNECT_SECONDS = 0.02
    try:
        surface.plugged = False
        force_writes(controller)
        controller.tick(now=1.0)
        # Captured rather than asserted in place: asserting False here and True
        # below narrows the attribute for mypy and makes the rest dead code.
        dropped = controller._connected

        surface.plugged = True
        await asyncio.sleep(0.15)
    finally:
        daemon.RECONNECT_SECONDS = original

    assert dropped is False, "the disconnect was never noticed"
    assert controller._connected is True
    assert surface.reopen_attempts >= 1
    assert surface.faces, "the deck was redrawn on return"


async def test_the_model_stays_current_while_disconnected() -> None:
    """So the deck is right the moment it returns, rather than a state behind."""
    controller, surface = await flaky()
    surface.plugged = False
    force_writes(controller)
    controller.tick(now=1.0)

    controller.handle(updated(pane_record("w1:p2")))
    controller.repaint()

    assert "w1:p2" in controller._panes


async def test_frames_are_rebuilt_rather_than_reused_after_a_reconnect() -> None:
    """They were encoded against the previous device handle."""
    controller, surface = await flaky()
    import herdr_streamdeck.daemon as daemon

    original = daemon.RECONNECT_SECONDS
    daemon.RECONNECT_SECONDS = 0.02
    stale = controller._frames[0]
    try:
        surface.plugged = False
        force_writes(controller)
        controller.tick(now=1.0)
        surface.plugged = True
        await asyncio.sleep(0.15)
    finally:
        daemon.RECONNECT_SECONDS = original

    assert controller._frames[0] is not stale


async def test_the_reply_menu_does_not_survive_a_disconnect() -> None:
    controller, surface = await flaky()
    controller._menu = ReplyMenu(pane_id="w1:p1", replies=(Reply("proceed", "go", "go"),))
    surface.plugged = False
    force_writes(controller)
    controller.tick(now=1.0)
    assert controller._menu is None


# ------------------------------------------------------------------ screen lock


async def test_locking_blacks_the_keys_before_killing_the_backlight() -> None:
    """Dimming alone would leave the summaries in the deck's own buffers."""
    snapshot: JSONObject = {"panes": [pane_record("w1:p1", title="deploy")]}
    controller, surface, _ = make_controller(snapshot=snapshot)
    await controller.prime()
    assert surface.faces[0].badge == "deploy"

    controller.set_locked(True)

    assert all(
        face == ButtonFace(background=EMPTY_BACKGROUND) for face in surface.faces.values()
    ), "every key, not just the occupied ones"
    assert len(surface.faces) == surface.key_count
    assert surface.brightness_written == 0


async def test_unlocking_puts_the_panes_back() -> None:
    snapshot: JSONObject = {"panes": [pane_record("w1:p1", title="deploy")]}
    controller, surface, _ = make_controller(snapshot=snapshot)
    await controller.prime()

    controller.set_locked(True)
    controller.set_locked(False)

    assert surface.brightness_written == surface.brightness
    assert surface.faces[0].badge == "deploy"


async def test_a_locked_deck_does_not_repaint() -> None:
    """Otherwise a status change while you are away relights the keys."""
    snapshot: JSONObject = {"panes": [pane_record("w1:p1")]}
    controller, surface, _ = make_controller(snapshot=snapshot)
    await controller.prime()
    controller.set_locked(True)
    blanked = surface.writes

    controller._upsert(pane_record("w1:p1", agent_status="blocked"))
    controller.repaint()
    controller._shown.clear()
    controller.tick(now=1.0)

    assert surface.writes == blanked, "nothing reaches the deck while locked"
    assert all(
        face == ButtonFace(background=EMPTY_BACKGROUND) for face in surface.faces.values()
    )


async def test_a_locked_deck_ignores_presses() -> None:
    """A dark deck is still a live one; a bag on it must not answer an agent."""
    snapshot: JSONObject = {"panes": [pane_record("w1:p1")]}
    controller, surface, client = make_controller(snapshot=snapshot)
    controller._loop = asyncio.get_running_loop()
    await controller.prime()
    before = len(client.requests)

    controller.set_locked(True)
    surface.tap(0)
    await asyncio.sleep(0)

    assert len(client.requests) == before, "the press went nowhere"


async def test_locking_abandons_a_reply_menu() -> None:
    """Coming back to a half-made choice invites finishing it by reflex."""
    snapshot: JSONObject = {"panes": [pane_record("w1:p1")]}
    controller, _, _ = make_controller(snapshot=snapshot)
    await controller.prime()
    controller._menu = ReplyMenu(pane_id="w1:p1", replies=(Reply("proceed", "go", "go ahead"),))

    controller.set_locked(True)

    assert controller._menu is None


async def test_a_deck_that_returns_mid_lock_comes_back_dark() -> None:
    """open() resets the deck to full brightness, so the lock is reapplied.

    Waiting for the next lock transition would not do: there may not be one
    until the screen is unlocked, which is exactly when it is too late.
    """
    import herdr_streamdeck.daemon as daemon

    controller, surface = await flaky()
    surface.plugged = False
    force_writes(controller)
    controller.tick(now=1.0)
    assert controller._reconnect_task is not None, "the loss was noticed"

    # The screen locks while the deck is away, so there is nothing to blank yet.
    controller.set_locked(True)

    original = daemon.RECONNECT_SECONDS
    daemon.RECONNECT_SECONDS = 0.01
    try:
        surface.plugged = True
        await asyncio.sleep(0.15)
    finally:
        daemon.RECONNECT_SECONDS = original

    assert controller._connected
    assert surface.brightness_written == 0
    assert all(
        face == ButtonFace(background=EMPTY_BACKGROUND) for face in surface.faces.values()
    )


async def test_a_deck_lost_while_locked_is_noticed_at_unlock() -> None:
    """Nothing writes while locked, so the loss goes unseen until it does.

    Harmless rather than ideal: a deck that comes back mid-lock comes back
    blank, because opening it resets the key images. Unlocking is what
    discovers the gap, and the reconnect loop closes it from there.
    """
    controller, surface = await flaky()
    controller.set_locked(True)
    surface.plugged = False

    controller.set_locked(False)

    assert not controller._connected, "restoring the backlight found the gap"
    assert controller._reconnect_task is not None


async def test_the_lock_loop_follows_the_watcher() -> None:
    state = {"locked": False}

    class Watcher:
        def locked(self) -> bool:
            return state["locked"]

    snapshot: JSONObject = {"panes": [pane_record("w1:p1")]}
    client = StubClient(snapshot)
    surface = NullSurface(key_count_=15, key_layout_=(3, 5))
    controller = DeckController(client, surface, lock=Watcher(), lock_interval=0.01)
    controller._loop = asyncio.get_running_loop()
    await controller.prime()

    task = asyncio.create_task(controller._lock_loop())
    try:
        state["locked"] = True
        await asyncio.sleep(0.05)
        assert surface.brightness_written == 0
        state["locked"] = False
        await asyncio.sleep(0.05)
        assert surface.brightness_written == surface.brightness
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
