"""``progress-and-claude`` command line: manage and watch progress boards."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.live import Live
from rich.text import Text

from progress_and_claude import __version__
from progress_and_claude.cli.parse import parse_task_lines
from progress_and_claude.config import Settings, slugify
from progress_and_claude.core.models import ProgressError, Status, TaskSpec, TaskUpdate
from progress_and_claude.core.service import MutationResult, TaskService
from progress_and_claude.core.store import Store
from progress_and_claude.logging import configure_logging
from progress_and_claude.naming import Namer, build_namer, diff_names
from progress_and_claude.render import boards_table, render_snapshot

app = typer.Typer(
    name="progress-and-claude",
    help="Accurate task/subtask progress bars for Claude and for you.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)
console = Console()
err_console = Console(stderr=True)

TASK_SYNTAX = (
    "Tasks as [bold]Name[=total][/bold]; prefix with dots to nest "
    "(e.g. [bold]'Build' '.Compile=40' '.Link' 'Test=12'[/bold])."
)


@dataclass(slots=True)
class State:
    """Per-invocation objects shared by commands via ``ctx.obj``."""

    settings: Settings
    service: TaskService
    namer: Namer
    board: str | None


def _state(ctx: typer.Context) -> State:
    """Return the :class:`State` created by the root callback."""
    state = ctx.obj
    assert isinstance(state, State)
    return state


def _die(message: str) -> NoReturn:
    """Print an error and exit with status 1."""
    err_console.print(f"[bold red]error:[/bold red] {message}")
    raise typer.Exit(1)


def _guard[T](fn: Callable[[], T]) -> T:
    """Run ``fn``, converting user-input errors into a clean exit."""
    try:
        return fn()
    except ProgressError as exc:
        _die(str(exc))


def _write_board(state: State) -> str:
    """Board id for commands that write (explicit, else the session default)."""
    return slugify(state.board) if state.board else state.settings.default_board


def _read_board(state: State) -> str:
    """Board id for commands that read (falls back to the most recent board)."""
    explicit = slugify(state.board) if state.board else None
    return state.service.pick_board(explicit, state.settings.default_board)


def _report(result: MutationResult, renames: list[tuple[str, str]] | None = None) -> None:
    """Print the board after a write, plus any renames and warnings."""
    console.print(render_snapshot(result.snapshot))
    for old, new in renames or []:
        console.print(f"[dim]renamed[/dim] {old!r} [dim]->[/dim] {new!r}")
    for warning in result.warnings:
        err_console.print(f"[yellow]warning:[/yellow] {warning}")


def _version(value: bool) -> None:
    """Print the version and exit when ``--version`` is given."""
    if value:
        console.print(f"progress-and-claude {__version__}")
        raise typer.Exit


@app.callback()
def root(
    ctx: typer.Context,
    board: Annotated[
        str | None,
        typer.Option("--board", "-b", help="Board id (default: from the current directory)."),
    ] = None,
    db: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite database path.", envvar="PROGRESS_AND_CLAUDE_DB"),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Debug logging to stderr.")
    ] = False,
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version.")
    ] = False,
) -> None:
    """Configure logging and open the shared database."""
    del version
    base = Settings.from_env()
    settings = Settings(
        db_path=db or base.db_path,
        default_board=base.default_board,
        namer=base.namer,
        namer_model=base.namer_model,
        namer_timeout=base.namer_timeout,
        verbose=verbose or base.verbose,
    )
    configure_logging(verbose=settings.verbose, quiet=True)
    store = Store(settings.db_path)
    ctx.obj = State(settings, TaskService(store), build_namer(settings, store), board)


@app.command()
def new(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="Title of the plan.")],
    tasks: Annotated[list[str] | None, typer.Argument(help=TASK_SYNTAX)] = None,
    from_file: Annotated[
        Path | None, typer.Option("--from-file", "-f", help="JSON file: a list of task objects.")
    ] = None,
) -> None:
    """Start a plan on the board (replacing any existing one)."""
    state = _state(ctx)

    def build() -> tuple[MutationResult, list[tuple[str, str]]]:
        if from_file:
            specs = [TaskSpec.model_validate(item) for item in json.loads(from_file.read_text())]
        else:
            specs = parse_task_lines(tasks or [])
        refined = asyncio.run(state.namer.refine(specs, plan_title=title))
        return state.service.start_plan(_write_board(state), title, refined), diff_names(
            specs, refined
        )

    result, renames = _guard(build)
    _report(result, renames)


@app.command()
def add(
    ctx: typer.Context,
    tasks: Annotated[list[str], typer.Argument(help=TASK_SYNTAX)],
    parent: Annotated[
        str | None, typer.Option("--parent", "-p", help="Dotted id of the parent task.")
    ] = None,
) -> None:
    """Add tasks to the board, at top level or under a parent."""
    state = _state(ctx)

    def build() -> tuple[MutationResult, list[tuple[str, str]]]:
        specs = parse_task_lines(tasks)
        board = _write_board(state)
        found = state.service.snapshot(board).find(parent) if parent else None
        refined = asyncio.run(
            state.namer.refine(specs, plan_title=board, parent_name=found.name if found else None)
        )
        return state.service.add_tasks(board, refined, parent), diff_names(specs, refined)

    result, renames = _guard(build)
    _report(result, renames)


@app.command()
def update(
    ctx: typer.Context,
    task_id: Annotated[str, typer.Argument(help="Dotted task id, e.g. 2.1")],
    completed: Annotated[float | None, typer.Option(help="Set finished units.", min=0)] = None,
    advance: Annotated[float | None, typer.Option(help="Add finished units.", min=0)] = None,
    fraction: Annotated[float | None, typer.Option(help="Progress 0..1.", min=0, max=1)] = None,
    total: Annotated[float | None, typer.Option(help="Change the unit total.", min=0)] = None,
    status: Annotated[Status | None, typer.Option(help="New status.")] = None,
    note: Annotated[str | None, typer.Option(help="Short note shown on the row.")] = None,
    name: Annotated[str | None, typer.Option(help="Rename the task.")] = None,
    reset: Annotated[bool, typer.Option(help="Allow progress to move backwards.")] = False,
) -> None:
    """Update one task's progress, status or text."""
    state = _state(ctx)

    def run() -> MutationResult:
        update_ = TaskUpdate(
            id=task_id, completed=completed, advance=advance, fraction=fraction, total=total,
            status=status, note=note, name=name, reset=reset,
        )  # fmt: skip
        return state.service.update_tasks(_write_board(state), [update_])

    _report(_guard(run))


def _set_status(ctx: typer.Context, ids: list[str], status: Status, note: str | None) -> None:
    """Apply one status (and optional note) to several tasks."""
    state = _state(ctx)
    updates = [TaskUpdate(id=i, status=status, note=note) for i in ids]
    _report(_guard(lambda: state.service.update_tasks(_write_board(state), updates)))


@app.command()
def done(
    ctx: typer.Context, ids: Annotated[list[str], typer.Argument(help="Task ids to mark done.")]
) -> None:
    """Mark tasks as done."""
    _set_status(ctx, ids, Status.DONE, None)


@app.command()
def fail(
    ctx: typer.Context,
    ids: Annotated[list[str], typer.Argument(help="Task ids to mark failed.")],
    note: Annotated[str | None, typer.Option(help="Why it failed.")] = None,
) -> None:
    """Mark tasks as failed."""
    _set_status(ctx, ids, Status.FAILED, note)


@app.command()
def skip(
    ctx: typer.Context, ids: Annotated[list[str], typer.Argument(help="Task ids to skip.")]
) -> None:
    """Mark tasks (and their subtasks) as skipped; they leave the progress math."""
    _set_status(ctx, ids, Status.SKIPPED, None)


@app.command()
def show(
    ctx: typer.Context,
    json_out: Annotated[bool, typer.Option("--json", help="Print the snapshot as JSON.")] = False,
    width: Annotated[int, typer.Option(help="Bar width in cells.", min=4, max=80)] = 24,
) -> None:
    """Show the board once."""
    state = _state(ctx)
    snapshot = _guard(lambda: state.service.snapshot(_read_board(state)))
    if json_out:
        console.print_json(snapshot.model_dump_json())
    else:
        console.print(render_snapshot(snapshot, bar_width=width))


@app.command()
def export(ctx: typer.Context) -> None:
    """Print the board as JSON (same as show --json)."""
    state = _state(ctx)
    snapshot = _guard(lambda: state.service.snapshot(_read_board(state)))
    typer.echo(snapshot.model_dump_json(indent=2))


@app.command()
def watch(
    ctx: typer.Context,
    interval: Annotated[float, typer.Option(help="Seconds between checks.", min=0.05)] = 0.25,
) -> None:
    """Live view that redraws whenever the board changes (Ctrl-C to quit)."""
    state = _state(ctx)
    last_rev, last_draw = -1, 0.0
    try:
        with Live(Text("Waiting for a board..."), console=console, refresh_per_second=8) as live:
            while True:
                rev = state.service.revision()
                # Redraw on change, and once a second so ETAs and elapsed times stay fresh.
                if rev != last_rev or time.monotonic() - last_draw > 1.0:
                    last_rev, last_draw = rev, time.monotonic()
                    board = _read_board(state)
                    try:
                        live.update(render_snapshot(state.service.snapshot(board)))
                    except ProgressError:
                        live.update(Text(f"Waiting for board {board!r}...", style="dim"))
                time.sleep(interval)
    except KeyboardInterrupt:
        pass


@app.command()
def boards(ctx: typer.Context) -> None:
    """List all boards."""
    infos = _state(ctx).service.boards()
    console.print(boards_table(infos) if infos else "[dim]No boards yet.[/dim]")


@app.command()
def clear(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Delete the board and all of its tasks."""
    state = _state(ctx)
    board = _write_board(state)
    if not yes:
        typer.confirm(f"Delete board {board!r}?", abort=True)
    console.print("cleared" if state.service.clear_board(board) else f"no board {board!r}")


@app.command()
def tui(ctx: typer.Context) -> None:
    """Open the interactive Textual dashboard."""
    from progress_and_claude.tui.app import run_tui

    state = _state(ctx)
    run_tui(state.service, initial_board=_read_board(state))


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio (what Claude launches)."""
    from progress_and_claude.mcp.server import main as serve

    serve()


@app.command()
def demo(
    ctx: typer.Context,
    seconds: Annotated[float, typer.Option(help="Roughly how long the demo runs.", min=2)] = 20.0,
) -> None:
    """Fill the board with a fake release and advance it live (try it beside tui or watch)."""
    state = _state(ctx)
    board = _write_board(state)
    svc = state.service
    specs = parse_task_lines(
        ["Build", ".Compile modules=40", ".Link binaries", "Test", ".Unit tests=120",
         ".Integration tests=30", "Package", "Publish"]
    )  # fmt: skip
    _guard(lambda: svc.start_plan(board, "Release v2 (demo)", specs))
    steps = int(seconds * 4)
    plan: list[list[TaskUpdate]] = []
    for i in range(1, steps + 1):
        f = i / steps
        batch = [TaskUpdate(id="1.1", completed=round(40 * min(1.0, f * 3)))]
        if f > 0.33:
            batch.append(TaskUpdate(id="1.2", status=Status.DONE))
        if f > 0.30:
            batch.append(TaskUpdate(id="2.1", completed=round(120 * min(1.0, (f - 0.3) * 2.2))))
        if f > 0.55:
            batch.append(TaskUpdate(id="2.2", completed=round(30 * min(1.0, (f - 0.55) * 2.5))))
        if f > 0.85:
            batch.append(TaskUpdate(id="3", status=Status.DONE))
        if f > 0.95:
            batch.append(TaskUpdate(id="4", status=Status.DONE))
        plan.append(batch)
    try:
        with Live(console=console, refresh_per_second=8) as live:
            for batch in plan:
                live.update(render_snapshot(svc.update_tasks(board, batch).snapshot))
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
