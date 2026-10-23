"""Pure progress math: turn stored task rows into derived, rolled-up views.

Rules
-----
* A leaf's progress is ``completed / total`` clamped to 0..1; ``done`` is 1.0;
  a leaf with no ``total`` is 0.0 until it is done (a plain step).
* A parent's progress is the weight-averaged progress of its non-skipped
  children. Skipped tasks are excluded from both numerator and denominator.
* A parent's status is derived: done when every active child is done, failed
  when nothing is left to run and something failed, running once any child has
  started or made progress, otherwise pending. ``blocked`` marks any ancestor of
  a failed task without altering the percentages.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from progress_and_claude.core.models import Snapshot, Status, Task, TaskView

_ACTIVE = {Status.PENDING, Status.RUNNING}


def leaf_progress(task: Task) -> float | None:
    """Compute progress for a task that has no children.

    Args:
        task: The stored leaf task.

    Returns:
        0..1 progress, or ``None`` if the task is skipped.
    """
    if task.status is Status.SKIPPED:
        return None
    if task.status is Status.DONE:
        return 1.0
    if task.total:
        return max(0.0, min(1.0, task.completed / task.total))
    return 0.0


def build_views(
    tasks: Sequence[Task],
    *,
    etas: Mapping[str, float | None] | None = None,
    now: float = 0.0,
) -> list[TaskView]:
    """Build the nested, derived view tree for a board.

    Args:
        tasks: All stored tasks of one board.
        etas: Optional estimated seconds remaining keyed by task id.
        now: Current timestamp, used for elapsed time.

    Returns:
        Root task views with nested children, ordered by position.
    """
    etas = etas or {}
    by_parent: dict[str | None, list[Task]] = {}
    for task in tasks:
        by_parent.setdefault(task.parent_id, []).append(task)
    for siblings in by_parent.values():
        siblings.sort(key=lambda t: t.position)

    def build(task: Task, depth: int) -> TaskView:
        kids = [build(child, depth + 1) for child in by_parent.get(task.id, [])]
        elapsed = (
            (task.finished_at or now) - task.started_at if task.started_at is not None else None
        )
        if not kids:
            progress = leaf_progress(task)
            eta = etas.get(task.id) if task.status is Status.RUNNING else None
            return TaskView(
                id=task.id,
                parent_id=task.parent_id,
                depth=depth,
                name=task.name,
                description=task.description,
                status=task.status,
                progress=progress,
                indeterminate=task.status is Status.RUNNING and not task.total,
                blocked=False,
                completed=task.completed,
                total=task.total,
                weight=task.weight,
                note=task.note,
                eta_seconds=eta,
                elapsed_seconds=elapsed,
            )
        progress, status = derive_parent(kids)
        return TaskView(
            id=task.id,
            parent_id=task.parent_id,
            depth=depth,
            name=task.name,
            description=task.description,
            status=status,
            progress=progress,
            blocked=any(k.blocked or k.status is Status.FAILED for k in kids),
            weight=task.weight,
            note=task.note,
            eta_seconds=etas.get(task.id) if status is Status.RUNNING else None,
            elapsed_seconds=elapsed,
            children=kids,
        )

    return [build(root, 0) for root in by_parent.get(None, [])]


def derive_parent(kids: Sequence[TaskView]) -> tuple[float | None, Status]:
    """Roll children up into a parent's progress and status.

    Also used with the board's root tasks to derive board-level progress and status.

    Args:
        kids: The parent's direct child views (at least one).

    Returns:
        ``(progress, status)`` for the parent.
    """
    active = [k for k in kids if k.status is not Status.SKIPPED]
    if not active:
        return None, Status.SKIPPED
    progress = overall_progress(active)
    if all(k.status is Status.DONE for k in active):
        return 1.0, Status.DONE
    if any(k.status in _ACTIVE for k in active):
        started = any(
            k.status is Status.RUNNING or (k.progress or 0.0) > 0.0 or k.status is Status.DONE
            for k in active
        )
        return progress, Status.RUNNING if started else Status.PENDING
    # Nothing left to run: everything is done or failed, with at least one failure.
    return progress, Status.FAILED


def overall_progress(views: Sequence[TaskView]) -> float | None:
    """Weight-average the progress of sibling views, ignoring skipped ones.

    Args:
        views: Sibling task views (for example the roots of a board).

    Returns:
        0..1 progress, or ``None`` when there is nothing to average.
    """
    counted = [v for v in views if v.progress is not None and v.status is not Status.SKIPPED]
    total_weight = sum(v.weight for v in counted)
    if not counted or total_weight <= 0:
        return None
    return sum(v.weight * (v.progress or 0.0) for v in counted) / total_weight


def leaf_counts(views: Sequence[TaskView]) -> tuple[int, int, int]:
    """Count leaf tasks across a view forest.

    Args:
        views: Root task views.

    Returns:
        ``(done, total, failed)`` where ``total`` excludes skipped leaves.
    """
    done = total = failed = 0
    stack = list(views)
    while stack:
        view = stack.pop()
        if view.children:
            stack.extend(view.children)
            continue
        if view.status is Status.SKIPPED:
            continue
        total += 1
        done += view.status is Status.DONE
        failed += view.status is Status.FAILED
    return done, total, failed


def progress_signature(views: Sequence[TaskView]) -> dict[str, tuple[float | None, Status]]:
    """Map task ids (and ``""`` for the whole board) to ``(progress, status)``.

    Used by the service to detect which tasks changed so ETA samples can be recorded.

    Args:
        views: Root task views.

    Returns:
        A dict keyed by task id; the empty string keys the whole board. Empty
        when the board has no tasks.
    """
    if not views:
        return {}
    sig: dict[str, tuple[float | None, Status]] = {"": derive_parent(views)}
    stack = list(views)
    while stack:
        view = stack.pop()
        sig[view.id] = (view.progress, view.status)
        stack.extend(view.children)
    return sig


def make_snapshot(
    *,
    board: str,
    title: str,
    revision: int,
    now: float,
    tasks: Sequence[Task],
    etas: Mapping[str, float | None] | None = None,
) -> Snapshot:
    """Assemble a full :class:`Snapshot` from stored rows.

    Args:
        board: Board id.
        title: Board title.
        revision: Database revision at read time.
        now: Current timestamp.
        tasks: All stored tasks of the board.
        etas: Estimated seconds remaining keyed by task id (``""`` for the board).

    Returns:
        The derived snapshot.
    """
    etas = etas or {}
    roots = build_views(tasks, etas=etas, now=now)
    done, total, failed = leaf_counts(roots)
    progress = overall_progress(roots)
    return Snapshot(
        board=board,
        title=title,
        revision=revision,
        generated_at=now,
        progress=progress,
        done=done,
        total=total,
        failed=failed,
        eta_seconds=etas.get("") if progress is not None and progress < 1.0 else None,
        tasks=roots,
    )
