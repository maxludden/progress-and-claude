"""End-to-end test: spawn the real `progress-and-claude-mcp` process over stdio."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from progress_and_claude.core.service import TaskService
from progress_and_claude.core.store import Store


async def test_stdio_server_round_trip_and_cross_process_read(tmp_path: Path) -> None:
    exe = shutil.which("progress-and-claude-mcp", path=str(Path(sys.executable).parent))
    if exe is None:
        pytest.skip("progress-and-claude-mcp is not installed in this environment")
    db = tmp_path / "e2e.db"
    params = StdioServerParameters(
        command=exe,
        env={
            "PROGRESS_AND_CLAUDE_DB": str(db),
            "PROGRESS_AND_CLAUDE_HOME": str(tmp_path),
            "PROGRESS_AND_CLAUDE_NAMER": "heuristic",
            "PROGRESS_AND_CLAUDE_BOARD": "e2e",
            "PATH": str(Path(sys.executable).parent),
        },
    )
    async with Client(params) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert {"start_plan", "update_tasks", "show_progress"} <= tools
        res = await client.call_tool(
            "start_plan", {"title": "E2E", "tasks": [{"name": "Only step", "total": 2}]}
        )
        assert not res.is_error
        await client.call_tool("update_tasks", {"updates": [{"id": "1", "advance": 1}]})

    # A different process (this one) reads what the server wrote.
    snap = TaskService(Store(db)).snapshot("e2e")
    assert snap.progress == pytest.approx(0.5)
