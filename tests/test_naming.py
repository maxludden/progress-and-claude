"""Tests for heuristic and LLM naming."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from progress_and_claude.core.models import TaskSpec
from progress_and_claude.naming import HeuristicNamer, LLMNamer, diff_names, normalize_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  now I'll   refactor the auth module. ", "Refactor the auth module"),
        ("Step 2: write tests", "Write tests"),
        ("3) update docs", "Update docs"),
        ("Let's migrate the database;", "Migrate the database"),
        ("- fix bug", "Fix bug"),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw)[0] == expected


def test_long_names_are_shortened_and_kept_as_description() -> None:
    raw = "Refactor the authentication middleware so that sessions survive server restarts"
    name, overflow = normalize_name(raw)
    assert len(name) <= 40 and name.endswith("…")
    assert overflow == raw


async def test_heuristic_dedupes_siblings_and_keeps_structure() -> None:
    specs = [
        TaskSpec(name="Test", subtasks=[TaskSpec(name="unit", total=3), TaskSpec(name="Unit")]),
        TaskSpec(name="test"),
    ]
    out = await HeuristicNamer().refine(specs, plan_title="x")
    assert [s.name for s in out] == ["Test", "Test (2)"]
    assert [s.name for s in out[0].subtasks] == ["Unit", "Unit (2)"]
    assert out[0].subtasks[0].total == 3


def test_diff_names() -> None:
    before = [TaskSpec(name="now I'll fix it"), TaskSpec(name="Same")]
    after = [TaskSpec(name="Fix it"), TaskSpec(name="Same")]
    assert diff_names(before, after) == [("now I'll fix it", "Fix it")]


class FakeMessages:
    def __init__(
        self, items: Any = None, *, delay: float = 0.0, error: Exception | None = None
    ) -> None:
        self.items, self.delay, self.error, self.calls = items, delay, error, 0

    async def create(self, **_: Any) -> Any:
        self.calls += 1
        if self.error:
            raise self.error
        await asyncio.sleep(self.delay)
        block = SimpleNamespace(type="tool_use", input={"items": self.items})
        return SimpleNamespace(content=[block])


def namer(messages: FakeMessages, timeout: float = 1.0, cache: Any = None) -> LLMNamer:
    return LLMNamer(
        model="m", timeout=timeout, cache=cache,
        client_factory=lambda: SimpleNamespace(messages=messages),
    )  # fmt: skip


SPECS = [TaskSpec(name="do the thing please", subtasks=[TaskSpec(name="a sub step")])]


async def test_llm_names_are_applied_and_clamped() -> None:
    msgs = FakeMessages(
        [
            {"index": 0, "name": "Ship the feature.", "description": "d" * 300},
            {"index": 1, "name": "Write the changelog", "description": "Notes."},
            {"index": 99, "name": "ignored", "description": ""},
        ]
    )
    out = await namer(msgs).refine(SPECS, plan_title="p")
    assert out[0].name == "Ship the feature"
    assert len(out[0].description) <= 120
    assert out[0].subtasks[0].name == "Write the changelog"


async def test_llm_timeout_falls_back_to_heuristic() -> None:
    out = await namer(FakeMessages([], delay=1.0), timeout=0.05).refine(SPECS, plan_title="p")
    assert out[0].name == "Do the thing"


async def test_llm_error_and_garbage_fall_back() -> None:
    out = await namer(FakeMessages(error=RuntimeError("boom"))).refine(SPECS, plan_title="p")
    assert out[0].name == "Do the thing"
    out = await namer(FakeMessages([{"index": "x"}, 5])).refine(SPECS, plan_title="p")
    assert out[0].name == "Do the thing"


async def test_llm_results_are_cached() -> None:
    class Cache:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}

        def get(self, key: str) -> str | None:
            return self.data.get(key)

        def put(self, key: str, value: str) -> None:
            self.data[key] = value

    msgs = FakeMessages([{"index": 0, "name": "Ship it", "description": ""}])
    n = namer(msgs, cache=Cache())
    await n.refine(SPECS, plan_title="p")
    await n.refine(SPECS, plan_title="p")
    assert msgs.calls == 1


async def test_no_api_key_uses_heuristic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    n = LLMNamer(model="m", timeout=1.0)
    out = await n.refine(SPECS, plan_title="p")
    assert out[0].name == "Do the thing"
