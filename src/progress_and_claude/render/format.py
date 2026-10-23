"""Small formatting helpers shared by every renderer."""

from __future__ import annotations

import math

from progress_and_claude.core.models import Status, TaskView

STATUS_GLYPH: dict[Status, str] = {
    Status.PENDING: "○",
    Status.RUNNING: "▶",
    Status.DONE: "✔",
    Status.FAILED: "✘",
    Status.SKIPPED: "⊘",
}
STATUS_STYLE: dict[Status, str] = {
    Status.PENDING: "dim",
    Status.RUNNING: "bold cyan",
    Status.DONE: "bold green",
    Status.FAILED: "bold red",
    Status.SKIPPED: "dim strike",
}
BAR_STYLE: dict[Status, str] = {
    Status.PENDING: "grey50",
    Status.RUNNING: "cyan",
    Status.DONE: "green",
    Status.FAILED: "red",
    Status.SKIPPED: "grey37",
}
_PARTIALS = "▏▎▍▌▋▊▉"
FULL = "█"
EMPTY = "░"


def percent(progress: float | None, status: Status = Status.RUNNING) -> int | None:
    """Convert a 0..1 progress to a whole percent that never rounds up to 100.

    Args:
        progress: Progress in 0..1, or ``None``.
        status: Task status; ``DONE`` always reports 100.

    Returns:
        An integer 0..100, or ``None`` when progress is unknown/skipped.
    """
    if status is Status.DONE:
        return 100
    if progress is None:
        return None
    return min(99, max(0, math.floor(progress * 100 + 1e-9)))


def percent_label(view: TaskView) -> str:
    """Return a 4-character right-aligned percentage such as `` 42%``."""
    value = percent(view.progress, view.status)
    return "   -" if value is None else f"{value:>3}%"


def bar_cells(progress: float | None, width: int) -> str:
    """Draw a unicode bar with 1/8-cell precision.

    The bar is only completely full when ``progress`` is 1.0, so it never claims
    more than has been achieved.

    Args:
        progress: Progress in 0..1 (``None`` draws an empty bar).
        width: Bar width in terminal cells.

    Returns:
        A string exactly ``width`` cells wide.
    """
    if progress is None:
        return EMPTY * width
    progress = max(0.0, min(1.0, progress))
    eighths = width * 8 if progress >= 1.0 else int(progress * width * 8 + 1e-9)
    full, rem = divmod(eighths, 8)
    bar = FULL * full + (_PARTIALS[rem - 1] if rem else "")
    return bar + EMPTY * (width - len(bar))


def format_duration(seconds: float) -> str:
    """Format seconds as ``45s``, ``2m 05s`` or ``1h 03m``."""
    total = round(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def detail_text(view: TaskView, *, note_chars: int = 32) -> str:
    """Build the trailing detail for a row: units, ETA, block marker and note.

    Args:
        view: The task view.
        note_chars: Maximum characters of the note to include.

    Returns:
        A short string (possibly empty).
    """
    parts: list[str] = []
    if not view.children and view.total:
        parts.append(f"{view.completed:g}/{view.total:g}")
    if view.blocked:
        parts.append("blocked")
    if view.eta_seconds is not None and view.status is Status.RUNNING:
        parts.append(f"~{format_duration(view.eta_seconds)} left")
    if view.note:
        note = view.note if len(view.note) <= note_chars else view.note[: note_chars - 1] + "…"
        parts.append(note)
    return " · ".join(parts)
