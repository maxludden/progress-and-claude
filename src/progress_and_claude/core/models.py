"""Pydantic models shared by the store, service, renderers and MCP tools."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator


class ProgressError(ValueError):
    """Base class for errors caused by invalid caller input."""


class TaskNotFoundError(ProgressError):
    """Raised when a task or board id does not exist."""


class Status(StrEnum):
    """Lifecycle state of a task."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskSpec(BaseModel):
    """A task to create, optionally with nested subtasks.

    Attributes:
        name: Short imperative title, e.g. ``"Migrate user table"``.
        description: One sentence on what the task covers.
        total: Number of countable units (files, tests, rows). Omit for a step
            that is simply done or not done.
        weight: Relative share of the parent's progress (default 1).
        subtasks: Nested child tasks. A task has either ``total`` or subtasks.
    """

    name: str = Field(min_length=1, description="Short imperative title (aim for under 40 chars).")
    description: str = Field(default="", description="One sentence describing the task.")
    total: float | None = Field(
        default=None,
        gt=0,
        description="Countable units of work; omit for a plain done/not-done step.",
    )
    weight: float = Field(default=1.0, gt=0, description="Relative weight within its parent.")
    subtasks: list[TaskSpec] = Field(default_factory=list, description="Nested subtasks.")

    @model_validator(mode="after")
    def _total_xor_subtasks(self) -> Self:
        """Reject tasks that declare both their own units and subtasks."""
        if self.subtasks and self.total is not None:
            raise ValueError(
                f"Task {self.name!r} has both 'total' and 'subtasks'; progress comes from subtasks."
            )
        return self


class TaskUpdate(BaseModel):
    """A change to one existing task.

    At most one of ``completed``, ``advance`` and ``fraction`` may be given.

    Attributes:
        id: Dotted task id such as ``"2.1"``.
        completed: Set the absolute number of finished units.
        advance: Add this many finished units.
        fraction: Set progress as 0..1 of ``total`` (``total`` defaults to 100).
        total: Change the number of units.
        status: New status. Parents derive their status; only ``skipped`` may be set on them.
        note: Short free-text note shown next to the task.
        name: Rename the task.
        description: Replace the description.
        reset: Allow progress to move backwards (otherwise decreases are ignored).
    """

    id: str = Field(description="Dotted task id, e.g. '2' or '2.1'.")
    completed: float | None = Field(default=None, ge=0)
    advance: float | None = Field(default=None, gt=0)
    fraction: float | None = Field(default=None, ge=0, le=1)
    total: float | None = Field(default=None, gt=0)
    status: Status | None = None
    note: str | None = None
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    reset: bool = False

    @model_validator(mode="after")
    def _single_progress_field(self) -> Self:
        """Ensure only one way of expressing progress is used."""
        given = [v for v in (self.completed, self.advance, self.fraction) if v is not None]
        if len(given) > 1:
            raise ValueError("Give only one of 'completed', 'advance' or 'fraction'.")
        return self


class Task(BaseModel):
    """A persisted task row (stored fields only, no derived values)."""

    board: str
    id: str
    parent_id: str | None
    position: int
    name: str
    description: str = ""
    status: Status = Status.PENDING
    completed: float = 0.0
    total: float | None = None
    weight: float = 1.0
    note: str = ""
    created_at: float
    updated_at: float
    started_at: float | None = None
    finished_at: float | None = None


class TaskView(BaseModel):
    """A task with derived progress, status and children, ready to render.

    Attributes:
        progress: 0..1, or ``None`` when the task is skipped (excluded from rollups).
        indeterminate: A running leaf with no ``total``; render a spinner, not a percentage.
        blocked: Some descendant failed.
        eta_seconds: Estimated seconds remaining, when enough samples exist.
    """

    id: str
    parent_id: str | None
    depth: int
    name: str
    description: str
    status: Status
    progress: float | None
    indeterminate: bool = False
    blocked: bool = False
    completed: float = 0.0
    total: float | None = None
    weight: float = 1.0
    note: str = ""
    eta_seconds: float | None = None
    elapsed_seconds: float | None = None
    children: list[TaskView] = Field(default_factory=list)


class BoardInfo(BaseModel):
    """Summary of one board, used for listings."""

    id: str
    title: str
    created_at: float
    updated_at: float
    progress: float | None = None
    done: int = 0
    total: int = 0


class Snapshot(BaseModel):
    """A consistent, fully derived view of one board.

    Attributes:
        board: Board id.
        title: Human title of the plan.
        revision: Database revision this snapshot was read at (changes on every write).
        progress: Overall 0..1 progress, ``None`` for an empty board.
        done: Number of finished leaf tasks.
        total: Number of non-skipped leaf tasks.
        failed: Number of failed leaf tasks.
        eta_seconds: Estimated seconds remaining for the whole board.
        tasks: Root tasks with nested children.
    """

    board: str
    title: str
    revision: int
    generated_at: float
    progress: float | None
    done: int
    total: int
    failed: int
    eta_seconds: float | None = None
    tasks: list[TaskView]

    def walk(self) -> list[TaskView]:
        """Return every task in depth-first order.

        Returns:
            A flat list of all task views.
        """
        out: list[TaskView] = []

        def visit(nodes: list[TaskView]) -> None:
            for node in nodes:
                out.append(node)
                visit(node.children)

        visit(self.tasks)
        return out

    def find(self, task_id: str) -> TaskView | None:
        """Look up a task by dotted id.

        Args:
            task_id: Dotted id such as ``"1.2"``.

        Returns:
            The matching view, or ``None``.
        """
        return next((t for t in self.walk() if t.id == task_id), None)


TaskSpec.model_rebuild()
TaskView.model_rebuild()
