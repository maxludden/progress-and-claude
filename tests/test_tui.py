"""Headless Textual tests driven by Pilot."""

from __future__ import annotations

from pathlib import Path

from textual.widgets import OptionList, Static

from progress_and_claude.core.models import Status, TaskSpec, TaskUpdate
from progress_and_claude.core.service import TaskService
from progress_and_claude.core.store import Store
from progress_and_claude.tui.app import ProgressApp


def seed(service: TaskService) -> None:
    service.start_plan(
        "b",
        "Release",
        [
            TaskSpec(
                name="Build", subtasks=[TaskSpec(name="Compile", total=10), TaskSpec(name="Link")]
            ),
            TaskSpec(name="Test"),
        ],
    )


async def test_shows_board_and_follows_external_writes(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    viewer = TaskService(Store(db))
    writer = TaskService(Store(db))  # a second connection, like Claude's MCP process
    seed(writer)
    app = ProgressApp(viewer, initial_board="b")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.4)
        tasks = app.query_one("#tasks", OptionList)
        assert tasks.option_count == 4
        assert "Release" in str(app.query_one("#title", Static).render())

        writer.update_tasks("b", [TaskUpdate(id="1.1", completed=5)])
        await pilot.pause(0.6)
        assert app.snapshot is not None and app.snapshot.find("1.1").progress == 0.5  # type: ignore[union-attr]

        writer.update_tasks("b", [TaskUpdate(id="2", status=Status.FAILED)])
        await pilot.pause(0.6)
        await pilot.press("f", "f")  # all -> active -> failed
        await pilot.pause(0.2)
        assert tasks.option_count == 1  # only the failed task "Test"


async def test_fold_and_unfold_with_keys(tmp_path: Path) -> None:
    service = TaskService(Store(tmp_path / "t.db"))
    seed(service)
    app = ProgressApp(service, initial_board="b")
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.4)
        tasks = app.query_one("#tasks", OptionList)
        assert tasks.option_count == 4
        await pilot.press("c")  # collapse all parents
        await pilot.pause(0.2)
        assert tasks.option_count == 2
        await pilot.press("c")  # expand again
        await pilot.pause(0.2)
        assert tasks.option_count == 4


async def test_empty_database_shows_hint(tmp_path: Path) -> None:
    app = ProgressApp(TaskService(Store(tmp_path / "t.db")))
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause(0.3)
        assert "No boards yet" in str(app.query_one("#title", Static).render())
