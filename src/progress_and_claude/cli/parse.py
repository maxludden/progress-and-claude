"""Parse the compact task syntax used by the CLI (``Name=total`` with leading dots for nesting)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from progress_and_claude.core.models import ProgressError, TaskSpec


def parse_task_lines(lines: Sequence[str]) -> list[TaskSpec]:
    """Turn compact task lines into a nested spec tree.

    Each line is ``[.]*Name[=total]``. Leading dots set the depth (``.`` is a
    subtask of the previous top-level task, ``..`` a sub-subtask, ...). A trailing
    ``=N`` gives the number of countable units.

    Example:
        ``["Build", ".Compile=40", ".Link", "Test=12"]`` is two top-level tasks;
        ``Build`` has the subtasks ``Compile`` (40 units) and ``Link``.

    Args:
        lines: Task lines in order.

    Returns:
        The top-level task specs.

    Raises:
        ProgressError: If a line is empty, nests too deeply, or has a bad total.
    """
    roots: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    for raw in lines:
        text = raw.strip()
        depth = len(text) - len(text.lstrip("."))
        text = text[depth:].strip()
        if not text:
            raise ProgressError(f"Empty task name in {raw!r}.")
        if depth > len(stack):
            raise ProgressError(f"Task {raw!r} is nested too deeply; add its parent first.")
        name, total = text, None
        if "=" in text:
            name, _, tail = text.rpartition("=")
            try:
                total = float(tail)
            except ValueError:
                raise ProgressError(f"Bad total {tail!r} in {raw!r}; use Name=NUMBER.") from None
            name = name.strip()
        node: dict[str, Any] = {"name": name, "total": total, "subtasks": []}
        del stack[depth:]
        (stack[-1]["subtasks"] if stack else roots).append(node)
        stack.append(node)
    return [_to_spec(n) for n in roots]


def _to_spec(node: dict[str, Any]) -> TaskSpec:
    """Convert a parsed node (and its children) to a validated :class:`TaskSpec`."""
    subtasks = [_to_spec(child) for child in node["subtasks"]]
    return TaskSpec(name=node["name"], total=None if subtasks else node["total"], subtasks=subtasks)
