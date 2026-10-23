"""Flatten a snapshot tree into display rows with tree-drawing prefixes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from progress_and_claude.core.models import Snapshot, TaskView
from progress_and_claude.render.format import format_duration, percent

_MAX_NAME_WIDTH = 46


@dataclass(frozen=True, slots=True)
class Row:
    """One visible line of the task tree.

    Attributes:
        view: The task shown on this line.
        prefix: Tree guides such as ``"│  ├─ "``.
        has_children: Whether the task has subtasks.
        collapsed: Whether its subtasks are currently hidden.
    """

    view: TaskView
    prefix: str
    has_children: bool
    collapsed: bool


def flatten(
    snapshot: Snapshot,
    *,
    collapsed: frozenset[str] = frozenset(),
    only: Callable[[TaskView], bool] | None = None,
) -> list[Row]:
    """Flatten a snapshot into rows.

    Args:
        snapshot: The board snapshot.
        collapsed: Ids of tasks whose subtasks are hidden.
        only: Optional predicate; a task is kept when it or a descendant matches.

    Returns:
        Rows in display order.
    """

    def keep(node: TaskView) -> bool:
        return only is None or only(node) or any(keep(c) for c in node.children)

    rows: list[Row] = []

    def visit(nodes: list[TaskView], guides: str) -> None:
        shown = [n for n in nodes if keep(n)]
        for index, node in enumerate(shown):
            last = index == len(shown) - 1
            if node.depth == 0:
                prefix, child_guides = "", ""
            else:
                prefix = guides + ("└─ " if last else "├─ ")
                child_guides = guides + ("   " if last else "│  ")
            is_collapsed = node.id in collapsed and bool(node.children)
            rows.append(Row(node, prefix, bool(node.children), is_collapsed))
            if not is_collapsed:
                visit(node.children, child_guides)

    visit(snapshot.tasks, "")
    return rows


def name_column_width(rows: list[Row], *, fold_marker: bool = False) -> int:
    """Return the width needed for the name column (prefix, glyph and name).

    Args:
        rows: Rows to display.
        fold_marker: Whether a fold marker column is included.

    Returns:
        The width in cells, capped so bars stay visible on narrow terminals.
    """
    widest = max((len(r.prefix) + 2 + len(r.view.name) for r in rows), default=10)
    return min(_MAX_NAME_WIDTH, widest + (2 if fold_marker else 0))


def summary_line(snapshot: Snapshot) -> str:
    """Summarise a board in one line (percent, leaf counts, failures, ETA).

    Args:
        snapshot: The board snapshot.

    Returns:
        For example ``"42% · 3/7 done · 1 failed · ~2m 05s left"``.
    """
    pct = percent(snapshot.progress)
    parts = ["-" if pct is None else f"{pct}%", f"{snapshot.done}/{snapshot.total} done"]
    if snapshot.failed:
        parts.append(f"{snapshot.failed} failed")
    if snapshot.eta_seconds is not None:
        parts.append(f"~{format_duration(snapshot.eta_seconds)} left")
    return " · ".join(parts)
