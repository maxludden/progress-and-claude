"""In-memory tests of the MCP server through a real client session."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp.client import Client

from progress_and_claude.config import Settings
from progress_and_claude.core.service import TaskService
from progress_and_claude.core.store import Store
from progress_and_claude.mcp.server import Runtime, build_server
from progress_and_claude.mcp.ui import UI_URI
from progress_and_claude.naming import HeuristicNamer


@asynccontextmanager
async def connected(tmp_path: Path) -> AsyncIterator[Client]:
    """Connect a client to a fresh in-memory server (entered and exited in the test's task)."""
    settings = Settings(db_path=tmp_path / "m.db", default_board="test")
    store = Store(settings.db_path)
    runtime = Runtime(settings, TaskService(store), HeuristicNamer())
    async with Client(build_server(runtime)) as c:
        yield c


PLAN: dict[str, Any] = {
    "title": "Ship feature",
    "tasks": [
        {
            "name": "now I'll build it",
            "subtasks": [{"name": "Compile", "total": 4}, {"name": "Link"}],
        },
        {"name": "Test"},
    ],
}


async def test_tools_listed_with_ui_metadata(tmp_path: Path) -> None:
    async with connected(tmp_path) as client:
        await _test_tools_listed_with_ui_metadata(client)


async def _test_tools_listed_with_ui_metadata(client: Client) -> None:
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "start_plan", "add_tasks", "update_tasks", "show_progress",
        "get_board_state", "list_boards", "clear_board",
    }  # fmt: skip
    assert tools["start_plan"].meta["ui"]["resourceUri"] == UI_URI  # type: ignore[index]
    assert tools["get_board_state"].meta["ui"]["visibility"] == ["app"]  # type: ignore[index]
    assert "ui" not in (tools["list_boards"].meta or {})


async def test_ui_resource_is_served_as_mcp_app(tmp_path: Path) -> None:
    async with connected(tmp_path) as client:
        await _test_ui_resource_is_served_as_mcp_app(client)


async def _test_ui_resource_is_served_as_mcp_app(client: Client) -> None:
    result = await client.read_resource(UI_URI)
    content = result.contents[0]
    assert content.mime_type == "text/html;profile=mcp-app"


async def test_full_flow(tmp_path: Path) -> None:
    async with connected(tmp_path) as client:
        await _test_full_flow(client)


async def _test_full_flow(client: Client) -> None:
    res = await client.call_tool("start_plan", PLAN)
    assert not res.is_error
    assert res.structured_content["total"] == 3
    assert res.structured_content["renames"] == [{"from": "now I'll build it", "to": "Build it"}]
    text = res.content[0].text  # type: ignore[union-attr]
    assert "Ship feature" in text and "Build it" in text

    res = await client.call_tool(
        "update_tasks",
        {"updates": [{"id": "1.1", "completed": 2}, {"id": "1.2", "status": "done"}]},
    )
    snap = res.structured_content
    assert snap is not None and snap["progress"] == pytest.approx(
        0.375
    )  # Build=(0.5+1)/2=0.75; board=(0.75+0)/2

    state = await client.call_tool("get_board_state", {})
    assert state.structured_content["revision"] == snap["revision"]

    res = await client.call_tool("add_tasks", {"tasks": [{"name": "Docs"}], "parent_id": None})
    assert res.structured_content["changed_ids"] == ["3"]

    boards = await client.call_tool("list_boards", {})
    assert boards.structured_content["boards"][0]["id"] == "test"
    cleared = await client.call_tool("clear_board", {})
    assert cleared.structured_content == {"cleared": True}


async def test_errors_are_readable_tool_errors(tmp_path: Path) -> None:
    async with connected(tmp_path) as client:
        await _test_errors_are_readable_tool_errors(client)


async def _test_errors_are_readable_tool_errors(client: Client) -> None:
    await client.call_tool("start_plan", PLAN)
    res = await client.call_tool("update_tasks", {"updates": [{"id": "1", "completed": 1}]})
    assert res.is_error and "derived" in res.content[0].text  # type: ignore[union-attr]
    res = await client.call_tool("update_tasks", {"updates": [{"id": "99", "completed": 1}]})
    assert res.is_error and "No task '99'" in res.content[0].text  # type: ignore[union-attr]
    res = await client.call_tool("show_progress", {"board": "missing"})
    assert res.is_error and "Unknown board" in res.content[0].text  # type: ignore[union-attr]
