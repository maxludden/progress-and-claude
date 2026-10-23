"""Namer protocol and helpers shared by every implementation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from progress_and_claude.core.models import TaskSpec

MAX_NAME_CHARS = 40
MAX_DESCRIPTION_CHARS = 120


class Namer(Protocol):
    """Refines the names and descriptions of a tree of task specs."""

    async def refine(
        self,
        specs: Sequence[TaskSpec],
        *,
        plan_title: str,
        parent_name: str | None = None,
    ) -> list[TaskSpec]:
        """Return a same-shaped tree with clean names and descriptions.

        Implementations must never raise for ordinary failures and must keep the
        tree's structure, order, totals and weights unchanged.

        Args:
            specs: The tasks as the caller wrote them.
            plan_title: Title of the plan, for context.
            parent_name: Name of the task these specs are being added under, if any.

        Returns:
            The refined specs.
        """
        ...


class NameCache(Protocol):
    """Key/value cache for LLM naming results."""

    def get(self, key: str) -> str | None:
        """Return the cached value for ``key`` or ``None``."""
        ...

    def put(self, key: str, value: str) -> None:
        """Store ``value`` under ``key``."""
        ...


def diff_names(before: Sequence[TaskSpec], after: Sequence[TaskSpec]) -> list[tuple[str, str]]:
    """List the renames between two same-shaped spec trees.

    Args:
        before: Specs as written by the caller.
        after: Specs after refinement.

    Returns:
        ``(original, final)`` pairs for every task whose name changed, depth-first.
    """
    out: list[tuple[str, str]] = []
    for old, new in zip(before, after, strict=True):
        if old.name != new.name:
            out.append((old.name, new.name))
        out.extend(diff_names(old.subtasks, new.subtasks))
    return out
