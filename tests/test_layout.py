"""Layout tests.

The through-line: the deck mirrors herdr, so the code must never impose an
order of its own. Several of these exist specifically to fail if someone
"tidies up" by sorting.
"""

from __future__ import annotations

import pytest

from herdr_streamdeck.layout import (
    BADGE_LENGTH,
    Grid,
    Group,
    GroupingMode,
    GroupKey,
    Pane,
    abbreviate,
    build_columns,
)

MK2 = Grid(rows=3, columns=5)


def pane(pane_id: str, workspace: str = "w1", tab: str = "w1:t1", **kw: str) -> Pane:
    return Pane(pane_id=pane_id, workspace_id=workspace, tab_id=tab, **kw)


def occupant(columns: list[Group | None], index: int) -> Pane:
    """Pane at a key, failing the test if the key is empty."""
    found = MK2.pane_at(columns, index)
    assert found is not None, f"expected a pane at key {index}"
    return found


# ------------------------------------------------------------------------ grid


def test_keys_are_row_major_from_top_left() -> None:
    assert MK2.index(0, 0) == 0
    assert MK2.index(0, 4) == 4
    assert MK2.index(1, 0) == 5
    assert MK2.index(2, 4) == 14
    assert MK2.key_count == 15


def test_position_round_trips() -> None:
    for index in range(MK2.key_count):
        row, column = MK2.position(index)
        assert MK2.index(row, column) == index


def test_grid_rejects_out_of_range() -> None:
    with pytest.raises(IndexError):
        MK2.index(3, 0)
    with pytest.raises(IndexError):
        MK2.position(15)


# -------------------------------------------------------------- column mirror


def test_columns_follow_herdr_order_not_sorted_order() -> None:
    """The whole point: sidebar order wins, even when it is not sorted."""
    order = [GroupKey("w6", "zeta"), GroupKey("w2", "alpha"), GroupKey("w4", "mid")]
    panes = [pane("p1", "w6"), pane("p2", "w2"), pane("p3", "w4")]

    columns = build_columns(panes, order, MK2)

    assert [c.id for c in columns[:3] if c] == ["w6", "w2", "w4"]
    assert columns[3] is None and columns[4] is None


def test_rows_follow_pane_order_not_sorted_order() -> None:
    """pane.list is a depth-first walk of the split tree; preserve it."""
    order = [GroupKey("w1", "ws")]
    panes = [pane("w1:p9"), pane("w1:p1"), pane("w1:p5")]

    columns = build_columns(panes, order, MK2)

    assert columns[0] is not None
    assert [p.pane_id for p in columns[0].panes] == ["w1:p9", "w1:p1", "w1:p5"]


def test_empty_group_still_holds_its_column() -> None:
    """A workspace with no panes must not let the next one slide left."""
    order = [GroupKey("w1", "a"), GroupKey("w2", "b")]
    columns = build_columns([pane("p1", "w2")], order, MK2)

    assert columns[0] is not None and columns[0].id == "w1"
    assert columns[0].panes == ()
    assert columns[1] is not None and columns[1].id == "w2"


def test_panes_beyond_the_row_count_are_dropped() -> None:
    order = [GroupKey("w1", "ws")]
    panes = [pane(f"p{i}") for i in range(6)]

    columns = build_columns(panes, order, MK2)

    assert columns[0] is not None
    assert [p.pane_id for p in columns[0].panes] == ["p0", "p1", "p2"]


def test_groups_beyond_the_column_count_are_dropped() -> None:
    order = [GroupKey(f"w{i}", f"ws{i}") for i in range(7)]
    panes = [pane(f"p{i}", f"w{i}") for i in range(7)]

    columns = build_columns(panes, order, MK2)

    assert len(columns) == 5
    assert [c.id for c in columns if c] == ["w0", "w1", "w2", "w3", "w4"]


def test_tab_mode_restricts_to_one_workspace() -> None:
    """A tab column would mean something different per column otherwise."""
    order = [GroupKey("w1:t1", "one"), GroupKey("w1:t2", "two")]
    panes = [
        pane("p1", "w1", "w1:t1"),
        pane("p2", "w1", "w1:t2"),
        pane("p3", "w9", "w9:t1"),  # different workspace, must be excluded
    ]

    columns = build_columns(panes, order, MK2, GroupingMode.TAB, workspace_id="w1")

    assert [c.id for c in columns[:2] if c] == ["w1:t1", "w1:t2"]
    assert all(p.workspace_id == "w1" for c in columns if c for p in c.panes)


def test_pane_at_maps_keys_to_the_grid() -> None:
    order = [GroupKey("w1", "a"), GroupKey("w2", "b")]
    panes = [
        pane("a0", "w1"),
        pane("a1", "w1"),
        pane("b0", "w2"),
        pane("b1", "w2"),
        pane("b2", "w2"),
    ]
    columns = build_columns(panes, order, MK2)

    assert occupant(columns, 0).pane_id == "a0"
    assert occupant(columns, 1).pane_id == "b0"
    assert occupant(columns, 5).pane_id == "a1"
    assert occupant(columns, 6).pane_id == "b1"
    assert MK2.pane_at(columns, 10) is None  # w1 has only two panes
    assert occupant(columns, 11).pane_id == "b2"
    assert MK2.pane_at(columns, 2) is None  # unoccupied column
    assert MK2.pane_at(columns, 99) is None


# ------------------------------------------------------------------ pane model


def test_display_agent_overrides_agent() -> None:
    """How an agent herdr cannot detect still gets its own mark."""
    p = Pane(pane_id="p1", agent="claude", display_agent="qwencode")
    assert p.mark_key == "qwencode"


def test_mark_key_falls_back_to_agent() -> None:
    assert Pane(pane_id="p1", agent="codex").mark_key == "codex"
    assert Pane(pane_id="p1").mark_key == ""


def test_pane_from_record_reads_metadata_fields() -> None:
    p = Pane.from_record(
        {
            "pane_id": "w1:p1",
            "workspace_id": "w1",
            "tab_id": "w1:t1",
            "title": "deploy-review",
            "display_agent": "qwencode",
            "agent": "claude",
            "agent_status": "working",
        }
    )
    assert p is not None
    assert (p.title, p.display_agent, p.status) == ("deploy-review", "qwencode", "working")
    assert p.mark_key == "qwencode"


def test_pane_from_record_rejects_non_pane() -> None:
    assert Pane.from_record({"terminal_id": "t"}) is None


def test_group_defaults_to_no_panes() -> None:
    assert Group(id="w1", label="a").panes == ()


# ------------------------------------------------------------------- badges
# Ticket names are the interesting case: their first four characters are the
# project prefix, identical on every pane and so useless for telling them
# apart. The number, or better a trailing description, distinguishes them.


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # Fits whole: keep it whole, ticket or not.
        ("ENG-4521", "ENG-4521"),
        ("ENG-45", "ENG-45"),
        ("reviewer", "reviewer"),
        ("api", "api"),
        ("ENG-abc", "ENG-abc"),
        # Too long: the number identifies it.
        ("ENG-45211", "45211"),
        ("ENG-4521123456", "45211234"),
        # A description beats a bare number.
        ("ENG-4521-refactor", "refactor"),
        ("ENG-4521-authenticate", "authenti"),
        ("ENG-4521-x", "x"),
        ("eng-77-Deploy", "Deploy"),
        # Only up to the next hyphen, never across a separator.
        ("ENG-4521-refactor-client", "refactor"),
        # Not ticket-shaped: leading characters.
        ("ENGINEERING-123", "ENGINEER"),  # four letters, not three
        ("EN-1234567890", "EN-12345"),  # two letters, not three
        ("build-and-deploy-all", "build-an"),
        ("", ""),
        ("   ", ""),
        ("  spaced out name  ", "spaced o"),
    ],
)
def test_abbreviate(name: str, expected: str) -> None:
    assert abbreviate(name) == expected


def test_abbreviate_preserves_case() -> None:
    """Prefixes are conventionally upper and descriptions lower; both cue."""
    assert abbreviate("ENG-1-Deployment") == "Deployme"
    assert abbreviate("Reviewer") == "Reviewer"


def test_a_name_that_fits_is_never_abbreviated() -> None:
    """Shortening something that already fits discards information for free."""
    for name in ("ENG-4521", "ENG-45", "reviewer", "api", "a-b-c"):
        assert abbreviate(name) == name


def test_abbreviate_never_exceeds_the_limit() -> None:
    for name in ("ENG-9999999999999", "averyverylongpanename", "ENG-1-descriptivename"):
        assert len(abbreviate(name)) <= BADGE_LENGTH


def test_badge_uses_the_abbreviation() -> None:
    assert Pane(pane_id="p1", title="ENG-4521-refactor").badge == "refactor"
    assert Pane(pane_id="p1", label="reviewer").badge == "reviewer"
    assert Pane(pane_id="p1").badge == ""


def test_badge_prefers_title_over_label() -> None:
    pane = Pane(pane_id="p1", title="ENG-77-deployment", label="ignored")
    assert pane.badge == "deployme"


def test_ticket_badges_stay_distinct_within_a_project() -> None:
    """The point of the rule: same prefix must not collapse to one badge."""
    names = ["ENG-4521", "ENG-4522", "ENG-4523-authentication", "ENG-4524-caching"]
    badges = [abbreviate(n) for n in names]
    assert len(set(badges)) == len(badges), badges
