"""Typer CLI tests via CliRunner against a temporary database."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from progress_and_claude.cli.app import app
from progress_and_claude.cli.parse import parse_task_lines
from progress_and_claude.core.models import ProgressError

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("PROGRESS_AND_CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("PROGRESS_AND_CLAUDE_NAMER", "heuristic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return {"PROGRESS_AND_CLAUDE_DB": str(tmp_path / "cli.db")}


def run(env: dict[str, str], *args: str, input: str | None = None):  # type: ignore[no-untyped-def]
    return runner.invoke(app, ["--board", "t", *args], env=env, input=input)


def test_parse_task_lines_nesting_and_totals() -> None:
    specs = parse_task_lines(["Build", ".Compile=40", ".Link", "..Deep", "Test=12"])
    assert [s.name for s in specs] == ["Build", "Test"]
    assert specs[0].total is None and specs[1].total == 12
    compile_, link = specs[0].subtasks
    assert compile_.total == 40 and link.subtasks[0].name == "Deep"


@pytest.mark.parametrize("lines", [[".Orphan"], ["A", "..TooDeep"], ["A=x"], ["  "]])
def test_parse_task_lines_errors(lines: list[str]) -> None:
    with pytest.raises(ProgressError):
        parse_task_lines(lines)


def test_end_to_end_commands(env: dict[str, str]) -> None:
    res = run(env, "new", "Release", "Build", ".Compile=10", ".Link", "Test")
    assert res.exit_code == 0, res.output
    assert "Release" in res.output and "Compile" in res.output

    assert run(env, "update", "1.1", "--completed", "5").exit_code == 0
    assert run(env, "done", "1.2", "2").exit_code == 0
    assert run(env, "add", "Docs").exit_code == 0
    assert run(env, "fail", "3", "--note", "no token").exit_code == 0

    data = json.loads(run(env, "export").output)
    assert data["board"] == "t" and data["failed"] == 1
    by_id = {t["id"]: t for t in data["tasks"]}
    assert by_id["1"]["progress"] == pytest.approx(0.75)
    assert by_id["2"]["status"] == "done"
    assert by_id["3"]["note"] == "no token"

    assert "t" in run(env, "boards").output
    assert run(env, "skip", "3").exit_code == 0
    assert run(env, "clear", "--yes").exit_code == 0
    assert "No boards" in run(env, "boards").output


def test_errors_exit_nonzero_with_message(env: dict[str, str]) -> None:
    run(env, "new", "P", "A")
    res = run(env, "update", "9", "--completed", "1")
    assert res.exit_code == 1 and "No task '9'" in res.output
    res = run(env, "new", "P", ".bad")
    assert res.exit_code == 1 and "nested too deeply" in res.output


def test_show_falls_back_to_most_recent_board(env: dict[str, str]) -> None:
    runner.invoke(app, ["--board", "other", "new", "Elsewhere", "A"], env=env)
    res = runner.invoke(app, ["show"], env=env)  # default board (cwd-derived) does not exist
    assert res.exit_code == 0 and "Elsewhere" in res.output


def test_new_from_file(env: dict[str, str], tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([{"name": "Ship", "subtasks": [{"name": "Tag"}]}]))
    res = run(env, "new", "From file", "--from-file", str(plan))
    assert res.exit_code == 0 and "Ship" in res.output and "Tag" in res.output
