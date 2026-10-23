"""MCP server: tools Claude uses to plan work and report accurate progress.

stdout belongs to the stdio transport, so everything here logs to stderr/file only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger
from mcp.server import MCPServer
from mcp.server.apps import Apps, Visibility
from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, TextContent

from progress_and_claude import __version__
from progress_and_claude.config import Settings, slugify
from progress_and_claude.core.models import ProgressError, Snapshot, TaskSpec, TaskUpdate
from progress_and_claude.core.service import MutationResult, TaskService
from progress_and_claude.core.store import Store
from progress_and_claude.logging import configure_logging
from progress_and_claude.mcp.ui import UI_URI, load_html
from progress_and_claude.naming import Namer, build_namer, diff_names
from progress_and_claude.render import render_plain

INSTRUCTIONS = """\
Track multi-step work with accurate progress bars.
1. Call start_plan once, before non-trivial work, with the top-level tasks and subtasks.
   Use short imperative names (e.g. "Migrate user table"). Give `total` for countable work
   (files, tests, rows); omit it for a plain step. A task has either `total` or `subtasks`.
2. Call update_tasks at natural milestones (not every tool call). Batch several tasks per call.
   Set absolute progress with `completed`, add with `advance`, or use `fraction`.
   Mark `status` done/failed/skipped honestly; parents are derived from their subtasks.
3. Use add_tasks when you discover more work, show_progress to re-display the board.
Task ids are dotted ("2", "2.1"). Percentages shown are exact roll-ups, never estimates.
"""


@dataclass(slots=True)
class Runtime:
    """Everything a tool call needs.

    Attributes:
        settings: Process settings.
        service: The task service.
        namer: Name refiner used when creating tasks.
    """

    settings: Settings
    service: TaskService
    namer: Namer

    @classmethod
    def create(cls, settings: Settings | None = None) -> Runtime:
        """Build a runtime from settings (defaults to environment-derived ones).

        Args:
            settings: Optional explicit settings.

        Returns:
            A ready runtime.
        """
        settings = settings or Settings.from_env()
        store = Store(settings.db_path)
        return cls(settings, TaskService(store), build_namer(settings, store))

    def board(self, board: str | None) -> str:
        """Resolve an optional board argument to a board id."""
        return slugify(board) if board else self.settings.default_board


def _result(
    snapshot: Snapshot,
    *,
    warnings: list[str] | None = None,
    renames: list[tuple[str, str]] | None = None,
    ids: list[str] | None = None,
    heading: str | None = None,
) -> CallToolResult:
    """Package a snapshot as a tool result: readable text plus structured data.

    Args:
        snapshot: Board state to report.
        warnings: Notes about adjusted input.
        renames: ``(original, final)`` task names that were refined.
        ids: Ids of created/changed tasks.
        heading: Optional first line (for example ``"Created tasks 1, 1.1"``).

    Returns:
        A ``CallToolResult`` with a text block and ``structured_content``.
    """
    lines: list[str] = []
    if heading:
        lines.append(heading)
    lines.append(render_plain(snapshot))
    if renames:
        lines.append("")
        lines.append("Renamed: " + "; ".join(f"{old!r} -> {new!r}" for old, new in renames))
    if warnings:
        lines.append("")
        lines.extend(f"Warning: {w}" for w in warnings)
    structured: dict[str, Any] = snapshot.model_dump(mode="json")
    structured["warnings"] = warnings or []
    structured["renames"] = [{"from": a, "to": b} for a, b in renames or []]
    structured["changed_ids"] = ids or []
    return CallToolResult(
        content=[TextContent(text="\n".join(lines))], structured_content=structured
    )


def _error(message: str) -> CallToolResult:
    """Build an error result Claude can read and correct."""
    return CallToolResult(content=[TextContent(text=message)], is_error=True)


def build_server(runtime: Runtime | None = None) -> MCPServer:
    """Create the MCP server with all tools and the UI resource registered.

    Args:
        runtime: Dependencies to use (defaults to a runtime built from the environment).

    Returns:
        A configured ``MCPServer`` ready for ``run()``.
    """
    rt = runtime or Runtime.create()
    apps = Apps()
    app_only: list[Visibility] = ["app"]
    both: list[Visibility] = ["model", "app"]

    async def _mutate(
        board: str, result: MutationResult, renames: list[tuple[str, str]], heading: str
    ) -> CallToolResult:
        del board
        return _result(
            result.snapshot,
            warnings=result.warnings,
            renames=renames,
            ids=result.ids,
            heading=heading,
        )

    @apps.tool(
        resource_uri=UI_URI,
        visibility=both,
        title="Start progress plan",
        description=(
            "Create (or replace) the progress plan for this session: a tree of tasks and "
            "subtasks shown as accurate progress bars. Call once before non-trivial work."
        ),
    )
    async def start_plan(
        title: str, tasks: list[TaskSpec], board: str | None = None
    ) -> CallToolResult:
        """Create a plan.

        Args:
            title: Short title of the overall job.
            tasks: Top-level tasks, each optionally with nested `subtasks`.
            board: Optional board id; defaults to one derived from the working directory.
        """
        board_id = rt.board(board)
        try:
            refined = await rt.namer.refine(tasks, plan_title=title)
            result = rt.service.start_plan(board_id, title, refined)
        except ProgressError as exc:
            return _error(str(exc))
        ids = ", ".join(result.ids)
        return await _mutate(
            board_id,
            result,
            diff_names(tasks, refined),
            f"Plan started on board {board_id!r}: {ids}",
        )

    @apps.tool(
        resource_uri=UI_URI,
        visibility=both,
        title="Add tasks",
        description="Add tasks (with optional subtasks) to the plan, at top level or under a task.",
    )
    async def add_tasks(
        tasks: list[TaskSpec], parent_id: str | None = None, board: str | None = None
    ) -> CallToolResult:
        """Add tasks to an existing plan.

        Args:
            tasks: Tasks to add.
            parent_id: Dotted id of the parent task; omit for top level.
            board: Optional board id.
        """
        board_id = rt.board(board)
        try:
            parent_name = None
            if parent_id:
                found = rt.service.snapshot(board_id).find(parent_id)
                parent_name = found.name if found else None
            refined = await rt.namer.refine(tasks, plan_title=board_id, parent_name=parent_name)
            result = rt.service.add_tasks(board_id, refined, parent_id)
        except ProgressError as exc:
            return _error(str(exc))
        return await _mutate(
            board_id, result, diff_names(tasks, refined), f"Added tasks: {', '.join(result.ids)}"
        )

    @apps.tool(
        resource_uri=UI_URI,
        visibility=both,
        title="Update task progress",
        description=(
            "Report progress on one or more tasks in a single call. Per task give one of "
            "`completed` (absolute units), `advance` (units to add) or `fraction` (0..1), "
            "and/or `status` (running, done, failed, skipped). Parents are derived."
        ),
    )
    async def update_tasks(updates: list[TaskUpdate], board: str | None = None) -> CallToolResult:
        """Apply progress/status updates.

        Args:
            updates: Changes to apply, each addressed by dotted task id.
            board: Optional board id.
        """
        board_id = rt.board(board)
        try:
            result = rt.service.update_tasks(board_id, updates)
        except ProgressError as exc:
            return _error(str(exc))
        return await _mutate(board_id, result, [], f"Updated: {', '.join(result.ids)}")

    @apps.tool(
        resource_uri=UI_URI,
        visibility=both,
        title="Show progress",
        description="Display the current progress board (use to re-show it on request).",
    )
    async def show_progress(board: str | None = None) -> CallToolResult:
        """Show the board.

        Args:
            board: Optional board id.
        """
        try:
            return _result(rt.service.snapshot(rt.board(board)))
        except ProgressError as exc:
            return _error(str(exc))

    @apps.tool(
        resource_uri=UI_URI,
        visibility=app_only,
        title="Get board state",
        description="Return the current board snapshot (used by the progress UI to refresh).",
    )
    async def get_board_state(board: str | None = None) -> CallToolResult:
        """Return structured board state for the UI.

        Args:
            board: Optional board id.
        """
        try:
            snapshot = rt.service.snapshot(rt.board(board))
        except ProgressError as exc:
            return _error(str(exc))
        return CallToolResult(
            content=[TextContent(text=render_plain(snapshot))],
            structured_content=snapshot.model_dump(mode="json"),
        )

    apps.add_html_resource(
        UI_URI,
        load_html(),
        name="Progress board",
        title="Progress board",
        description="Live task and subtask progress bars.",
        prefers_border=True,
    )
    mcp = MCPServer(
        "progress-and-claude",
        title="Progress and Claude",
        instructions=INSTRUCTIONS,
        version=__version__,
        extensions=[apps],
    )

    @mcp.tool(title="List boards", description="List progress boards and their overall progress.")
    async def list_boards(ctx: Context) -> CallToolResult:
        """List all boards."""
        del ctx
        boards = rt.service.boards()
        if not boards:
            return CallToolResult(
                content=[TextContent(text="No boards yet.")], structured_content={"boards": []}
            )
        lines = [
            f"{b.id}: {b.title} - "
            f"{'-' if b.progress is None else f'{int(b.progress * 100)}%'} ({b.done}/{b.total})"
            for b in boards
        ]
        return CallToolResult(
            content=[TextContent(text="\n".join(lines))],
            structured_content={"boards": [b.model_dump(mode="json") for b in boards]},
        )

    @mcp.tool(title="Clear board", description="Delete a progress board and all of its tasks.")
    async def clear_board(board: str | None = None) -> CallToolResult:
        """Delete a board.

        Args:
            board: Board id; defaults to this session's board.
        """
        board_id = rt.board(board)
        existed = rt.service.clear_board(board_id)
        text = f"Cleared board {board_id!r}." if existed else f"No board {board_id!r}."
        return CallToolResult(
            content=[TextContent(text=text)], structured_content={"cleared": existed}
        )

    return mcp


def main() -> None:
    """Entry point for ``progress-and-claude-mcp``: serve over stdio."""
    settings = Settings.from_env()
    configure_logging(verbose=settings.verbose, stderr=True)
    logger.info("Starting MCP server (db={}, board={})", settings.db_path, settings.default_board)
    build_server(Runtime.create(settings)).run()


if __name__ == "__main__":
    main()
