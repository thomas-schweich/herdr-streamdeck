"""Mapping from deck keys to herdr panes.

The deck is a grid -- 3 rows by 5 columns on an MK.2. Each **column** is a
group (a workspace, or a tab within one workspace) and each **row** is one of
that group's panes.

There is no stored layout and no pinning: **herdr's current arrangement is the
source of truth**, and the deck mirrors it. Two orderings make that work, both
verified against herdr 0.7.5:

* ``workspace.list`` returns workspaces in sidebar order -- the same order as
  ``session.json``, carrying an explicit 1-based ``number``. It is what
  ``workspace.move``'s ``insert_index`` rearranges, so it is the user's own
  ordering, not an accident of allocation.
* ``pane.list`` returns panes in a depth-first walk of the tab's split tree.
  For a tab laid out ``Split(Pane 5, Split(Pane 6, Pane 10))`` it returns
  ``p1, p2, p6`` in that order, so row order already matches what is on screen.

So neither ordering needs to be computed or remembered -- only preserved.
Nothing here may sort: sorting would silently substitute our opinion for
herdr's, which is precisely what mirroring must not do.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from .protocol import JSONObject

logger = logging.getLogger(__name__)


class GroupingMode(StrEnum):
    """What a column represents."""

    WORKSPACE = "workspace"
    """Each column is a workspace; rows are its panes."""

    TAB = "tab"
    """Each column is a tab of one workspace; rows are its panes."""


@dataclass(frozen=True, slots=True)
class Pane:
    """The bits of a pane record that reach the deck."""

    pane_id: str
    workspace_id: str = ""
    tab_id: str = ""
    label: str = ""
    title: str = ""
    agent: str = ""
    display_agent: str = ""
    status: str = "unknown"

    @classmethod
    def from_record(cls, record: JSONObject) -> Pane | None:
        pane_id = record.get("pane_id")
        if not isinstance(pane_id, str):
            return None

        def text(key: str) -> str:
            value = record.get(key)
            return value if isinstance(value, str) else ""

        return cls(
            pane_id=pane_id,
            workspace_id=text("workspace_id"),
            tab_id=text("tab_id"),
            label=text("label"),
            title=text("title"),
            agent=text("agent"),
            display_agent=text("display_agent"),
            status=text("agent_status") or "unknown",
        )

    @property
    def mark_key(self) -> str:
        """Which agent's mark to draw.

        ``display_agent`` wins because it is a free-form override, unlike
        ``agent`` which herdr constrains to its detection enum. That is how an
        agent herdr cannot detect -- qwencode, say -- still gets its own mark.
        """
        return self.display_agent or self.agent

    @property
    def badge(self) -> str:
        """Text for the corner badge: the most specific name available."""
        return self.title or self.label or ""


@dataclass(frozen=True, slots=True)
class GroupKey:
    """An ordered column candidate, as herdr reports it."""

    id: str
    label: str


@dataclass(frozen=True, slots=True)
class Group:
    """A column: one group and the panes visible in it."""

    id: str
    label: str
    panes: tuple[Pane, ...] = ()


@dataclass(frozen=True, slots=True)
class Grid:
    """The deck's key geometry. Keys are numbered row-major from top-left."""

    rows: int
    columns: int

    @property
    def key_count(self) -> int:
        return self.rows * self.columns

    def index(self, row: int, column: int) -> int:
        if not (0 <= row < self.rows and 0 <= column < self.columns):
            raise IndexError(f"({row}, {column}) outside {self.rows}x{self.columns}")
        return row * self.columns + column

    def position(self, index: int) -> tuple[int, int]:
        if not 0 <= index < self.key_count:
            raise IndexError(f"key {index} outside 0..{self.key_count - 1}")
        return divmod(index, self.columns)

    def pane_at(self, columns: Sequence[Group | None], index: int) -> Pane | None:
        """The pane occupying a key, or None for an empty key."""
        if not 0 <= index < self.key_count:
            return None
        row, column = divmod(index, self.columns)
        group = columns[column] if column < len(columns) else None
        if group is None or row >= len(group.panes):
            return None
        return group.panes[row]


def build_columns(
    panes: Sequence[Pane],
    order: Sequence[GroupKey],
    grid: Grid,
    mode: GroupingMode = GroupingMode.WORKSPACE,
    *,
    workspace_id: str = "",
) -> list[Group | None]:
    """Lay panes out into columns, mirroring herdr's order.

    ``panes`` must already be in herdr's order and ``order`` in sidebar order;
    both are preserved exactly. Anything past the edge of the grid is dropped
    and logged -- a deck with five columns cannot show a sixth workspace, and
    that should be visible in the log rather than silently invisible.

    In TAB mode ``workspace_id`` restricts the columns to one workspace's tabs,
    since a tab column would otherwise mean something different in each column.
    """
    buckets: dict[str, list[Pane]] = {}
    for pane in panes:
        if mode is GroupingMode.WORKSPACE:
            key = pane.workspace_id
        else:
            if workspace_id and pane.workspace_id != workspace_id:
                continue
            key = pane.tab_id
        if key:
            buckets.setdefault(key, []).append(pane)

    columns: list[Group | None] = [None] * grid.columns
    overflow_groups: list[str] = []
    truncated: list[str] = []

    for position, group_key in enumerate(order):
        members = buckets.get(group_key.id, [])
        if position >= grid.columns:
            if members:
                overflow_groups.append(group_key.label)
            continue
        if len(members) > grid.rows:
            truncated.append(f"{group_key.label} ({len(members)} panes)")
        columns[position] = Group(
            id=group_key.id,
            label=group_key.label,
            panes=tuple(members[: grid.rows]),
        )

    if overflow_groups:
        logger.info(
            "%d column(s) do not fit and are not shown: %s",
            len(overflow_groups),
            ", ".join(overflow_groups),
        )
    if truncated:
        logger.info("showing only the first %d panes of: %s", grid.rows, ", ".join(truncated))

    return columns
