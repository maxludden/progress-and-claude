"""Plain-text progress trees (no ANSI), used in MCP tool results."""

from __future__ import annotations

from progress_and_claude.core.models import Snapshot
from progress_and_claude.render.format import bar_cells, percent
from progress_and_claude.render.layout import flatten, name_column_width, summary_line
from progress_and_claude.render.row import header_text, row_text


def render_plain(
    snapshot: Snapshot,
    *,
    bar_width: int = 20,
    collapsed: frozenset[str] = frozenset(),
) -> str:
    """Render a snapshot as monospaced text with unicode progress bars.

    Args:
        snapshot: The board snapshot.
        bar_width: Width of each bar in characters.
        collapsed: Ids of tasks whose subtasks are hidden.

    Returns:
        A multi-line string: a header, a summary line and one line per task.
    """
    if not snapshot.tasks:
        return f"{snapshot.title}\n(no tasks)"
    rows = flatten(snapshot, collapsed=collapsed)
    width = name_column_width(rows)
    lines = [
        header_text(snapshot, bar_width=bar_width).plain,
        summary_line(snapshot),
        "",
    ]
    lines.extend(
        f"{row.view.id:>5}  " + row_text(row, name_width=width, bar_width=bar_width).plain.rstrip()
        for row in rows
    )
    return "\n".join(lines)


__all__ = ["bar_cells", "percent", "render_plain"]
