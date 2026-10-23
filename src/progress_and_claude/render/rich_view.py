"""Rich renderables for the CLI (and any Rich-compatible host)."""

from __future__ import annotations

import time

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from progress_and_claude.core.models import BoardInfo, Snapshot, Status
from progress_and_claude.render.format import format_duration, percent
from progress_and_claude.render.layout import flatten, name_column_width, summary_line
from progress_and_claude.render.row import header_text, row_text, styled_bar


def render_snapshot(
    snapshot: Snapshot,
    *,
    bar_width: int = 24,
    collapsed: frozenset[str] = frozenset(),
    show_ids: bool = True,
) -> RenderableType:
    """Render a snapshot as a Rich panel with a header and one line per task.

    Args:
        snapshot: The board snapshot.
        bar_width: Width of each bar in cells.
        collapsed: Ids of tasks whose subtasks are hidden.
        show_ids: Prefix each row with its dotted id (needed to address tasks).

    Returns:
        A renderable for ``Console.print`` or ``Live``.
    """
    if not snapshot.tasks:
        return Panel(Text("(no tasks)", style="dim"), title=snapshot.title)
    rows = flatten(snapshot, collapsed=collapsed)
    width = name_column_width(rows)
    lines: list[RenderableType] = [
        Text(summary_line(snapshot), style="grey62"),
        Rule(style="grey30"),
    ]
    for row in rows:
        line = Text(f"{row.view.id:>5}  " if show_ids else "", style="grey42")
        line.append_text(row_text(row, name_width=width, bar_width=bar_width))
        line.no_wrap = True
        line.overflow = "ellipsis"
        lines.append(line)
    return Panel(
        Group(*lines),
        title=header_text(snapshot, bar_width=bar_width),
        title_align="left",
        border_style="grey37",
        padding=(0, 1),
    )


def boards_table(boards: list[BoardInfo]) -> RenderableType:
    """Render a table of boards with progress bars.

    Args:
        boards: Boards from :meth:`TaskService.boards`.

    Returns:
        A Rich table.
    """
    table = Table(box=None, header_style="bold", pad_edge=False)
    for column in ("Board", "Title", "Progress", "", "Tasks", "Updated"):
        table.add_column(column, no_wrap=True)
    now = time.time()
    for board in boards:
        status = Status.DONE if board.progress == 1.0 else Status.RUNNING
        pct = percent(board.progress, status)
        table.add_row(
            Text(board.id, style="bold"),
            board.title,
            styled_bar(board.progress, status, 16),
            "-" if pct is None else f"{pct}%",
            f"{board.done}/{board.total}",
            f"{format_duration(now - board.updated_at)} ago",
        )
    return table
