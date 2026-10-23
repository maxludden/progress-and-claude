"""Textual dashboard: boards on the left, live task tree and details on the right.

The TUI is a pure viewer/editor over the shared SQLite database, so it shows
exactly what Claude (via the MCP server) and the CLI are doing. It polls the
database revision (a single cheap read) a few times a second.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import ClassVar

from rich.console import RenderableType
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, OptionList, Static
from textual.widgets.option_list import Option

from progress_and_claude.core.models import ProgressError, Snapshot, Status, TaskView
from progress_and_claude.core.service import TaskService
from progress_and_claude.render.format import format_duration, percent
from progress_and_claude.render.layout import Row, flatten, name_column_width, summary_line
from progress_and_claude.render.row import header_text, row_text

POLL_SECONDS = 0.25
REFRESH_SECONDS = 1.0
FILTERS: tuple[str, ...] = ("all", "active", "failed")


def _filter_predicate(name: str) -> Callable[[TaskView], bool] | None:
    """Return the task predicate for a filter name (``None`` keeps everything)."""
    if name == "active":
        return lambda t: t.status in {Status.PENDING, Status.RUNNING}
    if name == "failed":
        return lambda t: t.status is Status.FAILED or t.blocked
    return None


class ProgressApp(App[None]):
    """Interactive progress dashboard."""

    TITLE = "progress-and-claude"
    CSS = """
    #boards-pane { width: 30; border-right: tall $primary-background-lighten-2; }
    #boards-label, #tasks-label { padding: 0 1; color: $text-muted; text-style: bold; }
    #boards { height: 1fr; background: transparent; }
    #main { width: 1fr; }
    #title { padding: 0 1; height: 2; }
    #tasks { height: 1fr; background: transparent; text-wrap: nowrap; text-overflow: ellipsis; }
    #detail { height: 8; padding: 0 1; border-top: tall $primary-background-lighten-2; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("space,enter", "toggle_fold", "Fold", show=True, priority=False),
        Binding("c", "toggle_all", "Fold all"),
        Binding("f", "cycle_filter", "Filter"),
        Binding("tab", "focus_next", "Switch pane"),
    ]

    def __init__(self, service: TaskService, initial_board: str | None = None) -> None:
        """Create the app.

        Args:
            service: Service over the shared database.
            initial_board: Board to show first (defaults to the most recent).
        """
        super().__init__()
        self.service = service
        self.board: str | None = initial_board
        self.snapshot: Snapshot | None = None
        self.collapsed: set[str] = set()
        self.filter_index = 0
        self._rows: list[Row] = []
        self._revision = -1
        self._last_draw = 0.0
        self._selected_task: str | None = None

    def compose(self) -> ComposeResult:
        """Lay out the header, board list, task list, detail pane and footer."""
        yield Header()
        with Horizontal():
            with Vertical(id="boards-pane"):
                yield Static("Boards", id="boards-label")
                yield OptionList(id="boards")
            with Vertical(id="main"):
                yield Static("", id="title")
                yield OptionList(id="tasks")
                yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        """Start polling and focus the task list."""
        self.query_one("#tasks", OptionList).focus()
        self.set_interval(POLL_SECONDS, self._poll)
        self._poll(force=True)

    # --------------------------------------------------------------- polling

    def _poll(self, force: bool = False) -> None:
        """Redraw when the database changed (or once a second to refresh ETAs)."""
        revision = self.service.revision()
        stale = time.monotonic() - self._last_draw > REFRESH_SECONDS
        if not (force or revision != self._revision or stale):
            return
        changed = revision != self._revision
        self._revision, self._last_draw = revision, time.monotonic()
        if changed or force:
            self._refresh_boards()
        self._refresh_tasks()

    def _refresh_boards(self) -> None:
        """Rebuild the board list and pick a board if none is selected."""
        infos = self.service.boards()
        if self.board is None and infos:
            self.board = infos[0].id
        option_list = self.query_one("#boards", OptionList)
        option_list.clear_options()
        options: list[Option] = []
        for info in infos:
            pct = percent(info.progress, Status.DONE if info.progress == 1.0 else Status.RUNNING)
            label = Text(info.id, style="bold")
            label.append(f"  {'-' if pct is None else pct}%", style="cyan")
            label.append(f"\n{info.title}", style="dim")
            options.append(Option(label, id=info.id))
        option_list.add_options(options)
        if self.board is not None:
            try:
                option_list.highlighted = option_list.get_option_index(self.board)
            except Exception:  # board not listed yet
                option_list.highlighted = None

    def _refresh_tasks(self) -> None:
        """Rebuild the task list for the selected board, keeping the cursor on the same task."""
        title = self.query_one("#title", Static)
        tasks = self.query_one("#tasks", OptionList)
        if self.board is None:
            title.update(
                Text(
                    "No boards yet - ask Claude to start a plan, "
                    "or run `progress-and-claude demo`.",
                    style="dim",
                )
            )
            tasks.clear_options()
            self.snapshot = None
            return
        try:
            snapshot = self.service.snapshot(self.board)
        except ProgressError:
            title.update(Text(f"Waiting for board {self.board!r}...", style="dim"))
            tasks.clear_options()
            self.snapshot = None
            return
        self.snapshot = snapshot
        width = max(40, tasks.size.width - 4)
        self._rows = flatten(
            snapshot,
            collapsed=frozenset(self.collapsed),
            only=_filter_predicate(FILTERS[self.filter_index]),
        )
        name_width = min(name_column_width(self._rows, fold_marker=True), max(16, width // 2))
        bar_width = max(8, min(24, width - name_width - 44))  # leave room for the detail text
        title_bar = Text.assemble(
            header_text(snapshot, bar_width=min(40, bar_width + 10)), "\n",
            (summary_line(snapshot) + f"   filter: {FILTERS[self.filter_index]}", "grey62"),
        )  # fmt: skip
        title.update(title_bar)
        keep = self._selected_task
        tasks.clear_options()
        tasks.add_options(
            [
                Option(
                    self._row_prompt(row, name_width=name_width, bar_width=bar_width),
                    id=row.view.id,
                )
                for row in self._rows
            ]
        )
        if keep is not None:
            try:
                tasks.highlighted = tasks.get_option_index(keep)
            except Exception:  # the task is hidden by a filter or fold
                tasks.highlighted = 0 if self._rows else None
        elif self._rows:
            tasks.highlighted = 0
        self._update_detail()

    @staticmethod
    def _row_prompt(row: Row, *, name_width: int, bar_width: int) -> Text:
        """Build one single-line task row (ellipsis instead of wrapping)."""
        text = Text(f"{row.view.id:>5}  ", style="grey42")
        text.append_text(row_text(row, name_width=name_width, bar_width=bar_width, show_fold=True))
        text.no_wrap = True
        text.overflow = "ellipsis"
        return text

    def _update_detail(self) -> None:
        """Show full information about the highlighted task."""
        detail = self.query_one("#detail", Static)
        view = self._find(self._selected_task)
        if view is None:
            detail.update(Text("Select a task to see its details.", style="dim"))
            return
        detail.update(self._detail_renderable(view))

    def _find(self, task_id: str | None) -> TaskView | None:
        """Look up a task in the current snapshot."""
        if task_id is None or self.snapshot is None:
            return None
        return self.snapshot.find(task_id)

    @staticmethod
    def _detail_renderable(view: TaskView) -> RenderableType:
        """Build the detail pane content for one task."""
        pct = percent(view.progress, view.status)
        text = Text()
        text.append(f"{view.id}  {view.name}\n", style="bold")
        if view.description:
            text.append(f"{view.description}\n")
        facts = [f"status: {view.status.value}", f"progress: {'-' if pct is None else f'{pct}%'}"]
        if not view.children and view.total:
            facts.append(f"units: {view.completed:g}/{view.total:g}")
        if view.weight != 1:
            facts.append(f"weight: {view.weight:g}")
        if view.elapsed_seconds is not None:
            facts.append(f"elapsed: {format_duration(view.elapsed_seconds)}")
        if view.eta_seconds is not None:
            facts.append(f"eta: ~{format_duration(view.eta_seconds)}")
        if view.blocked:
            facts.append("blocked by a failed subtask")
        text.append("  ·  ".join(facts) + "\n", style="grey62")
        if view.note:
            text.append(f"note: {view.note}\n", style="yellow")
        text.append(f"updated {datetime.now():%H:%M:%S}", style="grey35")
        return text

    # --------------------------------------------------------------- events

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Track the highlighted task so details and the cursor survive redraws."""
        if event.option_list.id == "tasks" and event.option.id is not None:
            self._selected_task = event.option.id
            self._update_detail()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Switch boards when one is chosen; fold/unfold when a task is chosen."""
        if event.option_list.id == "boards" and event.option.id is not None:
            if event.option.id != self.board:
                self.board = event.option.id
                self._selected_task = None
                self.collapsed.clear()
                self._refresh_tasks()
            self.query_one("#tasks", OptionList).focus()
        elif event.option_list.id == "tasks":
            self.action_toggle_fold()

    def on_resize(self) -> None:
        """Re-layout bars when the terminal size changes."""
        if self.snapshot is not None:
            self.call_after_refresh(self._refresh_tasks)

    # -------------------------------------------------------------- actions

    def action_refresh(self) -> None:
        """Force a redraw from the database."""
        self._poll(force=True)

    def action_toggle_fold(self) -> None:
        """Collapse or expand the highlighted task's subtasks."""
        task_id = self._selected_task
        view = self._find(task_id)
        if task_id is None or view is None or not view.children:
            return
        if task_id in self.collapsed:
            self.collapsed.discard(task_id)
        else:
            self.collapsed.add(task_id)
        self._refresh_tasks()

    def action_toggle_all(self) -> None:
        """Collapse every parent, or expand all if everything is already collapsed."""
        if self.snapshot is None:
            return
        parents = {t.id for t in self.snapshot.walk() if t.children}
        self.collapsed = set() if parents and parents <= self.collapsed else parents
        self._refresh_tasks()

    def action_cycle_filter(self) -> None:
        """Cycle the task filter: all -> active -> failed."""
        self.filter_index = (self.filter_index + 1) % len(FILTERS)
        self._refresh_tasks()


def run_tui(service: TaskService, initial_board: str | None = None) -> None:
    """Run the dashboard until the user quits.

    Args:
        service: Service over the shared database.
        initial_board: Board to show first.
    """
    from progress_and_claude.logging import configure_logging

    configure_logging(stderr=False)  # the TUI owns the terminal; log to the file only
    ProgressApp(service, initial_board).run()
