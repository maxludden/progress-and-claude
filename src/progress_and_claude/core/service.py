"""High-level operations on boards of tasks: create, extend, update, read."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from loguru import logger
from pydantic import BaseModel, Field

from progress_and_claude.core.eta import estimate_eta
from progress_and_claude.core.models import (
    BoardInfo,
    ProgressError,
    Snapshot,
    Status,
    Task,
    TaskNotFoundError,
    TaskSpec,
    TaskUpdate,
)
from progress_and_claude.core.rollup import (
    build_views,
    leaf_counts,
    make_snapshot,
    overall_progress,
    progress_signature,
)
from progress_and_claude.core.store import Store, Tx

MAX_TASKS_PER_BOARD = 500
MAX_DEPTH = 6
_TERMINAL = {Status.DONE, Status.FAILED, Status.SKIPPED}
_EPSILON = 1e-9


class MutationResult(BaseModel):
    """Outcome of a write.

    Attributes:
        snapshot: Board state after the write.
        warnings: Things the caller should know (ignored decreases, converted tasks, ...).
        ids: Ids of the tasks that were created or changed.
    """

    snapshot: Snapshot
    warnings: list[str] = Field(default_factory=list)
    ids: list[str] = Field(default_factory=list)


class TaskService:
    """Create and update boards of hierarchical tasks, keeping progress consistent."""

    def __init__(self, store: Store, clock: Callable[[], float] = time.time) -> None:
        """Create a service.

        Args:
            store: The shared persistence layer.
            clock: Source of timestamps (injectable for tests).
        """
        self.store = store
        self._clock = clock

    # ------------------------------------------------------------------ reads

    def snapshot(self, board: str) -> Snapshot:
        """Read the derived state of a board.

        Args:
            board: Board id.

        Returns:
            The current snapshot.

        Raises:
            TaskNotFoundError: If the board does not exist.
        """
        with self.store.read() as tx:
            return self._snapshot(tx, board)

    def boards(self) -> list[BoardInfo]:
        """List every board with its overall progress.

        Returns:
            Boards, most recently updated first.
        """
        with self.store.read() as tx:
            infos = tx.list_boards()
            now = self._clock()
            for info in infos:
                roots = build_views(tx.tasks(info.id), now=now)
                info.progress = overall_progress(roots)
                info.done, info.total, _failed = leaf_counts(roots)
            return infos

    def pick_board(self, explicit: str | None, default: str) -> str:
        """Choose which board a viewer should show.

        Args:
            explicit: A board the user asked for; always honoured.
            default: The session's default board id.

        Returns:
            ``explicit`` if given, else ``default`` if it exists, else the most
            recently updated board, else ``default``.
        """
        if explicit:
            return explicit
        boards = self.store_boards()
        ids = {b.id for b in boards}
        if default in ids or not boards:
            return default
        return boards[0].id

    def store_boards(self) -> list[BoardInfo]:
        """List boards without progress figures, most recently updated first."""
        with self.store.read() as tx:
            return tx.list_boards()

    def revision(self) -> int:
        """Return the database revision, which changes on every write."""
        return self.store.revision()

    def _snapshot(self, tx: Tx, board: str) -> Snapshot:
        """Build a snapshot inside an open transaction."""
        title = tx.board_title(board)
        if title is None:
            known = ", ".join(b.id for b in tx.list_boards()) or "none"
            raise TaskNotFoundError(f"Unknown board {board!r}. Existing boards: {known}.")
        now = self._clock()
        etas = {key: estimate_eta(s, now) for key, s in tx.samples(board).items()}
        return make_snapshot(
            board=board,
            title=title,
            revision=tx.revision(),
            now=now,
            tasks=tx.tasks(board),
            etas=etas,
        )

    # ----------------------------------------------------------------- writes

    def start_plan(self, board: str, title: str, specs: Sequence[TaskSpec]) -> MutationResult:
        """Create (or replace) a board's plan from a tree of task specs.

        Args:
            board: Board id; any existing tasks on it are discarded.
            title: Human title of the plan.
            specs: Top-level tasks with nested subtasks.

        Returns:
            The new snapshot and the created task ids.

        Raises:
            ProgressError: If the plan is empty, too large or too deep.
        """
        if not title.strip():
            raise ProgressError("A plan needs a non-empty title.")
        _validate_specs(specs)
        now = self._clock()
        with self.store.transaction() as tx:
            tx.clear_tasks(board)
            tx.upsert_board(board, " ".join(title.split()), now)
            ids: list[str] = []
            self._insert(tx, board, specs, None, 1, now, ids)
            self._finalize(tx, board, {}, now)
            snapshot = self._snapshot(tx, board)
        logger.info("Started plan {!r} on board {!r} with {} tasks", title, board, len(ids))
        return MutationResult(snapshot=snapshot, ids=ids)

    def add_tasks(
        self, board: str, specs: Sequence[TaskSpec], parent_id: str | None = None
    ) -> MutationResult:
        """Append tasks to a board, at the top level or under an existing task.

        Args:
            board: Board id (must exist).
            specs: Tasks to add.
            parent_id: Dotted id of the parent, or ``None`` for top level.

        Returns:
            The new snapshot and the created task ids.

        Raises:
            TaskNotFoundError: If the board or parent does not exist.
            ProgressError: If the parent already has its own progress, or limits are exceeded.
        """
        _validate_specs(specs)
        now = self._clock()
        warnings: list[str] = []
        with self.store.transaction() as tx:
            if tx.board_title(board) is None:
                raise TaskNotFoundError(f"Unknown board {board!r}; call start_plan first.")
            tasks = tx.tasks(board)
            before = progress_signature(build_views(tasks, now=now))
            if len(tasks) + _count_specs(specs) > MAX_TASKS_PER_BOARD:
                raise ProgressError(f"A board holds at most {MAX_TASKS_PER_BOARD} tasks.")
            depth = 0
            if parent_id is not None:
                parent = next((t for t in tasks if t.id == parent_id), None)
                if parent is None:
                    raise TaskNotFoundError(f"No task {parent_id!r} on board {board!r}.")
                depth = parent_id.count(".") + 1
                if depth + _spec_depth(specs) > MAX_DEPTH:
                    raise ProgressError(f"Tasks may nest at most {MAX_DEPTH} levels deep.")
                has_kids = any(t.parent_id == parent_id for t in tasks)
                if not has_kids:
                    if parent.completed > 0 or parent.status in {Status.DONE, Status.FAILED}:
                        raise ProgressError(
                            f"Task {parent_id!r} is already {parent.status.value} with its own "
                            "progress; add sibling tasks instead."
                        )
                    if parent.total is not None:
                        parent.total, parent.completed, parent.updated_at = None, 0.0, now
                        tx.save_task(parent)
                        warnings.append(
                            f"Task {parent_id!r} became a parent; its own 'total' was cleared."
                        )
            siblings = [t.position for t in tasks if t.parent_id == parent_id]
            ids: list[str] = []
            self._insert(tx, board, specs, parent_id, max(siblings, default=0) + 1, now, ids)
            self._finalize(tx, board, before, now)
            snapshot = self._snapshot(tx, board)
        return MutationResult(snapshot=snapshot, warnings=warnings, ids=ids)

    def update_tasks(self, board: str, updates: Sequence[TaskUpdate]) -> MutationResult:
        """Apply a batch of task updates atomically.

        Args:
            board: Board id (must exist).
            updates: Changes to apply in order.

        Returns:
            The new snapshot, warnings for adjusted input, and the changed ids.

        Raises:
            TaskNotFoundError: If the board or a task does not exist.
            ProgressError: If an update is invalid (for example progress on a parent task).
        """
        if not updates:
            raise ProgressError("No updates given.")
        now = self._clock()
        warnings: list[str] = []
        with self.store.transaction() as tx:
            if tx.board_title(board) is None:
                raise TaskNotFoundError(f"Unknown board {board!r}; call start_plan first.")
            tasks = tx.tasks(board)
            before = progress_signature(build_views(tasks, now=now))
            by_id = {t.id: t for t in tasks}
            kids: dict[str, list[Task]] = {}
            for t in tasks:
                if t.parent_id is not None:
                    kids.setdefault(t.parent_id, []).append(t)
            for upd in updates:
                task = by_id.get(upd.id)
                if task is None:
                    sample = ", ".join(list(by_id)[:12])
                    raise TaskNotFoundError(
                        f"No task {upd.id!r} on board {board!r}. Ids start: {sample} ..."
                    )
                self._apply(tx, task, upd, bool(kids.get(upd.id)), kids, now, warnings)
            self._finalize(tx, board, before, now)
            snapshot = self._snapshot(tx, board)
        return MutationResult(snapshot=snapshot, warnings=warnings, ids=[u.id for u in updates])

    def clear_board(self, board: str) -> bool:
        """Delete a board and everything on it.

        Args:
            board: Board id.

        Returns:
            Whether the board existed.
        """
        with self.store.transaction() as tx:
            return tx.delete_board(board)

    # -------------------------------------------------------------- internals

    def _insert(
        self,
        tx: Tx,
        board: str,
        specs: Sequence[TaskSpec],
        parent_id: str | None,
        start: int,
        now: float,
        ids: list[str],
    ) -> None:
        """Insert specs (recursively) under ``parent_id`` numbering siblings from ``start``."""
        for offset, spec in enumerate(specs):
            index = start + offset
            task_id = f"{parent_id}.{index}" if parent_id else str(index)
            tx.insert_task(
                Task(
                    board=board,
                    id=task_id,
                    parent_id=parent_id,
                    position=index,
                    name=" ".join(spec.name.split()),
                    description=" ".join(spec.description.split()),
                    total=spec.total,
                    weight=spec.weight,
                    created_at=now,
                    updated_at=now,
                )
            )
            ids.append(task_id)
            self._insert(tx, board, spec.subtasks, task_id, 1, now, ids)

    def _apply(
        self,
        tx: Tx,
        task: Task,
        upd: TaskUpdate,
        is_parent: bool,
        kids: dict[str, list[Task]],
        now: float,
        warnings: list[str],
    ) -> None:
        """Apply one update to ``task`` and persist it (cascading skips to descendants)."""
        has_progress = any(
            v is not None for v in (upd.completed, upd.advance, upd.fraction, upd.total)
        )
        if is_parent and has_progress:
            raise ProgressError(
                f"Task {task.id!r} has subtasks, so its progress is derived from them. "
                "Update a subtask instead."
            )
        if is_parent and upd.status not in (None, Status.SKIPPED):
            raise ProgressError(
                f"Task {task.id!r} has subtasks; its status is derived. "
                "Only 'skipped' can be set on it."
            )
        if upd.name is not None:
            task.name = " ".join(upd.name.split())
        if upd.description is not None:
            task.description = " ".join(upd.description.split())
        if upd.note is not None:
            task.note = " ".join(upd.note.split())
        if upd.total is not None:
            task.total = upd.total

        completed = task.completed
        if upd.completed is not None:
            completed = upd.completed
        elif upd.advance is not None:
            completed = task.completed + upd.advance
        elif upd.fraction is not None:
            if task.total is None:
                task.total = 100.0
            completed = upd.fraction * task.total
        if task.total is not None and completed > task.total:
            completed = task.total
        if completed < task.completed - _EPSILON and not upd.reset:
            warnings.append(
                f"Task {task.id}: ignored a decrease from {task.completed:g} to {completed:g}; "
                "pass reset=true to move progress backwards."
            )
            completed = task.completed

        target = upd.status
        if target is None and not is_parent:
            if upd.reset and has_progress and task.status in _TERMINAL:
                target = Status.RUNNING if completed > 0 else Status.PENDING
            elif task.status in {Status.PENDING, Status.RUNNING}:
                if task.total is not None and completed >= task.total:
                    target = Status.DONE
                elif has_progress and completed > 0 and task.status is Status.PENDING:
                    target = Status.RUNNING
        if (
            target is not None
            and task.status is Status.DONE
            and target is not Status.DONE
            and not upd.reset
        ):
            warnings.append(
                f"Task {task.id}: already done; ignored status {target.value!r} "
                "(pass reset=true to reopen it)."
            )
            target = None
        if target is Status.DONE and task.total is not None:
            completed = task.total

        task.completed = completed
        if target is not None:
            self._set_status(task, target, now)
            if is_parent and target is Status.SKIPPED:
                self._skip_descendants(tx, task.id, kids, now)
        task.updated_at = now
        tx.save_task(task)

    def _set_status(self, task: Task, status: Status, now: float) -> None:
        """Change a task's status and maintain its start/finish timestamps."""
        task.status = status
        if status is not Status.PENDING and task.started_at is None:
            task.started_at = now
        if status in _TERMINAL:
            task.finished_at = now
        else:
            task.finished_at = None

    def _skip_descendants(
        self, tx: Tx, parent_id: str, kids: dict[str, list[Task]], now: float
    ) -> None:
        """Mark every unfinished descendant of ``parent_id`` as skipped."""
        for child in kids.get(parent_id, []):
            if child.status is not Status.DONE:
                self._set_status(child, Status.SKIPPED, now)
                child.updated_at = now
                tx.save_task(child)
            self._skip_descendants(tx, child.id, kids, now)

    def _finalize(
        self,
        tx: Tx,
        board: str,
        before: dict[str, tuple[float | None, Status]],
        now: float,
    ) -> None:
        """Sync parent timestamps, record ETA samples for what changed, and touch the board."""
        tasks = tx.tasks(board)
        views = build_views(tasks, now=now)
        by_id = {t.id: t for t in tasks}
        stack = list(views)
        while stack:
            view = stack.pop()
            stack.extend(view.children)
            if not view.children:
                continue
            parent = by_id[view.id]
            changed = False
            if view.status is not Status.PENDING and parent.started_at is None:
                parent.started_at, changed = now, True
            if view.status in _TERMINAL and parent.finished_at is None:
                parent.finished_at, changed = now, True
            elif view.status not in _TERMINAL and parent.finished_at is not None:
                parent.finished_at, changed = None, True
            if changed:
                tx.save_task(parent)
        samples: list[tuple[str, float, float]] = []
        for key, (progress, status) in progress_signature(
            build_views(tx.tasks(board), now=now)
        ).items():
            if progress is None:
                continue
            prev = before.get(key)
            if prev is None:
                record = status is Status.RUNNING
            else:
                moved = prev[0] is None or abs(prev[0] - progress) > _EPSILON
                record = moved or (prev[1] is not status and status is Status.RUNNING)
            if record:
                samples.append((key, now, progress))
        tx.add_events(board, samples)
        tx.touch_board(board, now)


def _count_specs(specs: Sequence[TaskSpec]) -> int:
    """Count specs including all nested subtasks."""
    return sum(1 + _count_specs(s.subtasks) for s in specs)


def _spec_depth(specs: Sequence[TaskSpec]) -> int:
    """Return the nesting depth of a spec forest (1 for a flat list)."""
    return max((1 + _spec_depth(s.subtasks) for s in specs), default=0)


def _validate_specs(specs: Sequence[TaskSpec]) -> None:
    """Reject empty, oversized or too deeply nested plans."""
    if not specs:
        raise ProgressError("Give at least one task.")
    if _count_specs(specs) > MAX_TASKS_PER_BOARD:
        raise ProgressError(f"A board holds at most {MAX_TASKS_PER_BOARD} tasks.")
    if _spec_depth(specs) > MAX_DEPTH:
        raise ProgressError(f"Tasks may nest at most {MAX_DEPTH} levels deep.")
