"""Behavioural tests for TaskService and the rollup rules."""

from __future__ import annotations

import pytest

from progress_and_claude.core.models import (
    ProgressError,
    Status,
    TaskNotFoundError,
    TaskSpec,
    TaskUpdate,
)
from progress_and_claude.core.service import TaskService

from .conftest import FakeClock


def plan(service: TaskService) -> None:
    service.start_plan(
        "b",
        "Release",
        [
            TaskSpec(
                name="Build", subtasks=[TaskSpec(name="Compile", total=10), TaskSpec(name="Link")]
            ),
            TaskSpec(name="Test", weight=3),
            TaskSpec(name="Docs"),
        ],
    )


def pct(service: TaskService, task_id: str | None = None) -> float | None:
    snap = service.snapshot("b")
    return snap.progress if task_id is None else snap.find(task_id).progress  # type: ignore[union-attr]


def test_ids_are_dotted_and_stable(service: TaskService) -> None:
    plan(service)
    ids = [t.id for t in service.snapshot("b").walk()]
    assert ids == ["1", "1.1", "1.2", "2", "3"]


def test_leaf_units_roll_up_with_weights(service: TaskService) -> None:
    plan(service)
    service.update_tasks("b", [TaskUpdate(id="1.1", completed=5)])
    assert pct(service, "1.1") == pytest.approx(0.5)
    assert pct(service, "1") == pytest.approx(0.25)  # (0.5*1 + 0*1) / 2
    # Board: Build(w1)=0.25, Test(w3)=0, Docs(w1)=0 -> 0.25/5
    assert pct(service) == pytest.approx(0.05)
    service.update_tasks("b", [TaskUpdate(id="2", status=Status.DONE)])
    assert pct(service) == pytest.approx((0.25 + 3) / 5)


def test_reaching_total_marks_done_and_progress_never_hits_one_early(service: TaskService) -> None:
    plan(service)
    service.update_tasks("b", [TaskUpdate(id="1.1", advance=9.99)])
    snap = service.snapshot("b")
    assert snap.find("1.1").status is Status.RUNNING  # type: ignore[union-attr]
    service.update_tasks("b", [TaskUpdate(id="1.1", advance=1)])
    assert service.snapshot("b").find("1.1").status is Status.DONE  # type: ignore[union-attr]


def test_all_children_done_makes_parent_done(service: TaskService) -> None:
    plan(service)
    service.update_tasks(
        "b", [TaskUpdate(id="1.1", status=Status.DONE), TaskUpdate(id="1.2", status=Status.DONE)]
    )
    parent = service.snapshot("b").find("1")
    assert parent is not None and parent.status is Status.DONE and parent.progress == 1.0


def test_decrease_is_ignored_with_warning_unless_reset(service: TaskService) -> None:
    plan(service)
    service.update_tasks("b", [TaskUpdate(id="1.1", completed=8)])
    result = service.update_tasks("b", [TaskUpdate(id="1.1", completed=2)])
    assert result.warnings and "ignored a decrease" in result.warnings[0]
    assert pct(service, "1.1") == pytest.approx(0.8)
    service.update_tasks("b", [TaskUpdate(id="1.1", completed=2, reset=True)])
    assert pct(service, "1.1") == pytest.approx(0.2)


def test_skipped_tasks_are_excluded(service: TaskService) -> None:
    plan(service)
    service.update_tasks("b", [TaskUpdate(id="2", status=Status.SKIPPED)])
    service.update_tasks(
        "b", [TaskUpdate(id="1.1", status=Status.DONE), TaskUpdate(id="1.2", status=Status.DONE)]
    )
    # Build done (1.0, w1), Docs pending (0, w1); Test skipped -> 0.5
    assert pct(service) == pytest.approx(0.5)
    assert service.snapshot("b").total == 3  # Compile, Link, Docs


def test_skipping_a_parent_cascades_but_keeps_done_children(service: TaskService) -> None:
    plan(service)
    service.update_tasks("b", [TaskUpdate(id="1.2", status=Status.DONE)])
    service.update_tasks("b", [TaskUpdate(id="1", status=Status.SKIPPED)])
    snap = service.snapshot("b")
    assert snap.find("1.1").status is Status.SKIPPED  # type: ignore[union-attr]
    assert snap.find("1.2").status is Status.DONE  # type: ignore[union-attr]


def test_failure_blocks_ancestors_without_faking_percentages(service: TaskService) -> None:
    plan(service)
    service.update_tasks(
        "b", [TaskUpdate(id="1.1", completed=4), TaskUpdate(id="1.2", status=Status.FAILED)]
    )
    build = service.snapshot("b").find("1")
    assert build is not None and build.blocked
    assert build.progress == pytest.approx(0.2)  # (0.4 + 0) / 2
    assert build.status is Status.RUNNING  # Compile is still running
    service.update_tasks("b", [TaskUpdate(id="1.1", status=Status.FAILED)])
    assert service.snapshot("b").find("1").status is Status.FAILED  # type: ignore[union-attr]


def test_progress_on_parent_is_rejected(service: TaskService) -> None:
    plan(service)
    with pytest.raises(ProgressError, match="derived"):
        service.update_tasks("b", [TaskUpdate(id="1", completed=1)])
    with pytest.raises(ProgressError, match="derived"):
        service.update_tasks("b", [TaskUpdate(id="1", status=Status.DONE)])


def test_unknown_task_and_board(service: TaskService) -> None:
    plan(service)
    with pytest.raises(TaskNotFoundError):
        service.update_tasks("b", [TaskUpdate(id="9")])
    with pytest.raises(TaskNotFoundError):
        service.snapshot("nope")


def test_fraction_defaults_total_to_100(service: TaskService) -> None:
    plan(service)
    service.update_tasks("b", [TaskUpdate(id="3", fraction=0.4)])
    task = service.snapshot("b").find("3")
    assert task is not None and task.total == 100 and task.progress == pytest.approx(0.4)


def test_add_tasks_under_leaf_converts_it_and_rerolls(service: TaskService) -> None:
    plan(service)
    result = service.add_tasks(
        "b", [TaskSpec(name="Draft"), TaskSpec(name="Review")], parent_id="3"
    )
    assert result.ids == ["3.1", "3.2"]
    assert pct(service, "3") == 0.0


def test_add_tasks_under_progressed_leaf_is_rejected(service: TaskService) -> None:
    plan(service)
    service.update_tasks("b", [TaskUpdate(id="1.1", completed=3)])
    with pytest.raises(ProgressError):
        service.add_tasks("b", [TaskSpec(name="x")], parent_id="1.1")


def test_add_tasks_numbering_never_reuses_ids(service: TaskService) -> None:
    plan(service)
    result = service.add_tasks("b", [TaskSpec(name="Ship")])
    assert result.ids == ["4"]


def test_spec_rejects_total_with_subtasks() -> None:
    with pytest.raises(ValueError, match="both"):
        TaskSpec(name="x", total=3, subtasks=[TaskSpec(name="y")])


def test_start_plan_replaces_board(service: TaskService) -> None:
    plan(service)
    service.start_plan("b", "Second", [TaskSpec(name="Only")])
    assert [t.id for t in service.snapshot("b").walk()] == ["1"]


def test_revision_changes_on_every_write(service: TaskService) -> None:
    plan(service)
    before = service.revision()
    service.update_tasks("b", [TaskUpdate(id="3", note="hi")])
    assert service.revision() == before + 1


def test_eta_appears_after_enough_samples(service: TaskService, clock: FakeClock) -> None:
    plan(service)
    service.update_tasks("b", [TaskUpdate(id="1.1", status=Status.RUNNING)])
    for done in (2, 4, 6):
        clock.advance(2)
        service.update_tasks("b", [TaskUpdate(id="1.1", completed=done)])
    task = service.snapshot("b").find("1.1")
    assert task is not None and task.eta_seconds == pytest.approx(4 / 2 * 2 / 1, rel=0.2)
    assert service.snapshot("b").find("2").eta_seconds is None  # type: ignore[union-attr]


def test_two_connections_see_each_others_writes(service: TaskService, store) -> None:  # type: ignore[no-untyped-def]
    other = TaskService(store)
    plan(service)
    other.update_tasks("b", [TaskUpdate(id="3", status=Status.DONE)])
    assert service.snapshot("b").find("3").status is Status.DONE  # type: ignore[union-attr]


def test_snapshot_from_a_write_carries_the_new_revision(service: TaskService) -> None:
    plan(service)
    result = service.update_tasks("b", [TaskUpdate(id="3", note="x")])
    assert result.snapshot.revision == service.revision()
