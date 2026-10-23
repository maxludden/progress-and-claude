"""Build Rich ``Text`` for one task row; the single source of truth for row appearance."""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text

from progress_and_claude.core.models import Snapshot, Status
from progress_and_claude.render.format import (
    BAR_STYLE,
    EMPTY,
    STATUS_GLYPH,
    STATUS_STYLE,
    bar_cells,
    detail_text,
    percent,
    percent_label,
)
from progress_and_claude.render.layout import Row


def _fit(text: str, width: int) -> str:
    """Pad or truncate ``text`` to exactly ``width`` terminal cells."""
    size = cell_len(text)
    if size <= width:
        return text + " " * (width - size)
    out = ""
    for ch in text:
        if cell_len(out + ch) > width - 1:
            break
        out += ch
    return out + "…" + " " * (width - cell_len(out) - 1)


def styled_bar(progress: float | None, status: Status, width: int) -> Text:
    """Return a bar as styled text: coloured fill, dim remainder.

    Args:
        progress: Progress in 0..1.
        status: Task status, which picks the fill colour.
        width: Bar width in cells.

    Returns:
        A ``Text`` exactly ``width`` cells wide.
    """
    cells = bar_cells(progress, width)
    empty = len(cells) - len(cells.rstrip(EMPTY))
    text = Text()
    text.append(cells[: len(cells) - empty], style=BAR_STYLE[status])
    text.append(cells[len(cells) - empty :], style="grey23")
    return text


def row_text(
    row: Row, *, name_width: int, bar_width: int, show_fold: bool = False, selected: bool = False
) -> Text:
    """Render one row: tree prefix, status glyph, name, bar, percent and detail.

    Args:
        row: The row to render.
        name_width: Width of the name column in cells.
        bar_width: Width of the bar in cells.
        show_fold: Include a fold marker for tasks with subtasks.
        selected: Whether the row is highlighted (adds reverse emphasis to the name).

    Returns:
        The styled line.
    """
    view = row.view
    glyph = STATUS_GLYPH[view.status]
    fold = ""
    if show_fold:
        fold = ("▸ " if row.collapsed else "▾ ") if row.has_children else "  "
    left = f"{row.prefix}{fold}{glyph} {view.name}"
    left = _fit(left, name_width)
    text = Text()
    lead = len(row.prefix) + len(fold)
    text.append(left[:lead], style="grey50")
    text.append(left[lead : lead + 1], style=STATUS_STYLE[view.status])
    name_style = "bold" if view.depth == 0 else ""
    if view.status is Status.SKIPPED:
        name_style = "dim strike"
    if selected:
        name_style += " underline"
    text.append(left[lead + 1 :], style=name_style)
    text.append("  ")
    if view.indeterminate:
        text.append(bar_cells(None, bar_width).replace(EMPTY, "▒"), style=BAR_STYLE[view.status])
    else:
        text.append_text(styled_bar(view.progress, view.status, bar_width))
    pct_style = BAR_STYLE[view.status] if view.status is not Status.PENDING else "grey50"
    label = "   …" if view.indeterminate else percent_label(view)
    text.append(f" {label}", style=pct_style)
    detail = detail_text(view)
    if detail:
        text.append(f"  {detail}", style="red" if view.status is Status.FAILED else "grey62")
    return text


def header_text(snapshot: Snapshot, *, bar_width: int) -> Text:
    """Render the board header: title, overall bar and percentage.

    Args:
        snapshot: The board snapshot.
        bar_width: Bar width in cells.

    Returns:
        The styled header line.
    """
    status = (
        Status.DONE
        if snapshot.progress == 1.0
        else Status.FAILED
        if snapshot.failed and snapshot.done + snapshot.failed >= snapshot.total
        else Status.RUNNING
    )
    pct = percent(snapshot.progress, status)
    text = Text(snapshot.title, style="bold")
    text.append("  ")
    text.append_text(styled_bar(snapshot.progress, status, bar_width))
    text.append(f" {'-' if pct is None else pct}%", style=BAR_STYLE[status])
    return text
