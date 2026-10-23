"""Shared fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from progress_and_claude.core.service import TaskService
from progress_and_claude.core.store import Store


class FakeClock:
    """A manually advanced clock."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.db")


@pytest.fixture
def service(store: Store, clock: FakeClock) -> TaskService:
    return TaskService(store, clock=clock)
