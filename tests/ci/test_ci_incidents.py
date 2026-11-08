"""Behavior contracts for CI failure classification (synthetic GitHub payloads)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from scripts.ci.incidents import classify_workflow_run, main, poll_repository


def test_unknown_failed_run_is_unavailable_and_never_retry_safe() -> None:
    result = classify_workflow_run({"conclusion": "failure", "jobs": []})

    assert result.category == "unavailable"
    assert result.cause is None
    assert result.retry_safe is False


def test_callable_poller_deduplicates_and_releases_only_owned_hold(tmp_path) -> None:
    from hermes_cli.kanban_db_connect import init_db, connect
    from hermes_cli.kanban_db import create_task, get_task

    db = tmp_path / "board.db"
    init_db(db)
    with connect(db) as conn:
        waiting = create_task(conn, title="Synthetic dependent")
        unrelated = create_task(conn, title="Unrelated queued work")

        def fetch(_repo, _cursor):
            return [{"id": 701, "run_attempt": 1, "head_sha": "abc", "conclusion": "failure",
                     "jobs": [{"steps": [{"text": "GitHub Actions usage has exceeded the allowed limit"}]}]}]

        first = poll_repository(conn, "owner/repo", {"owner/repo": {"abc": [waiting]}}, fetch_runs=fetch)
        second = poll_repository(conn, "owner/repo", {"owner/repo": {"abc": [waiting]}}, fetch_runs=fetch)
        assert first.incidents_created == 1
        assert second.incidents_created == 0
        assert (task := get_task(conn, waiting)) and task.status == "todo"

        recovered = poll_repository(
            conn, "owner/repo", {"owner/repo": {"abc": [waiting]}},
            fetch_runs=lambda _repo, _cursor: [{"id": 702, "run_attempt": 1, "head_sha": "abc",
                "conclusion": "success", "jobs": []}],
        )
        assert recovered.holds_released == 1
        assert (task := get_task(conn, waiting)) and task.status == "ready"
        assert get_task(conn, unrelated).status == "ready"
        incident = conn.execute("SELECT incident_task_id FROM ci_incident_state WHERE active=0").fetchone()[0]
        assert (incident_task := get_task(conn, incident)) and incident_task.status == "done"
        assert tuple(conn.execute("SELECT run_id,run_attempt FROM ci_incident_cursors WHERE run_id=702").fetchone()) == (702, 1)

        repeated = poll_repository(
            conn, "owner/repo", {"owner/repo": {"abc": [waiting]}},
            fetch_runs=lambda _repo, _cursor: [{"id": 703, "run_attempt": 1, "head_sha": "abc",
                "conclusion": "failure", "jobs": [{"steps": [{"text": "spending limit"}]}]}],
        )
        assert repeated.incidents_created == 1
        assert (task := get_task(conn, waiting)) and task.status == "todo"



def test_poll_cli_is_refused_without_explicit_enable() -> None:
    with pytest.raises(SystemExit) as error:
        main(["--repository", "owner/repo", "--head-task", "abc=task-1"])
    assert error.value.code == 2


def test_concurrent_polls_create_one_shared_incident(tmp_path) -> None:
    from hermes_cli.kanban_db_connect import init_db, connect
    from hermes_cli.kanban_db import create_task

    db = tmp_path / "concurrent-board.db"
    init_db(db)
    with connect(db) as conn:
        dependent = create_task(conn, title="Synthetic concurrent dependent")
    barrier = Barrier(2)
    run = {"id": 801, "run_attempt": 1, "head_sha": "head-801", "conclusion": "failure",
           "jobs": [{"steps": [{"text": "billing issue"}]}]}

    def poll():
        with connect(db) as conn:
            def fetch(_repository, _cursor):
                barrier.wait(timeout=10)
                return [run]
            return poll_repository(conn, "owner/repo", {"owner/repo": {"head-801": [dependent]}}, fetch_runs=fetch)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: poll(), range(2)))
    assert sum(result.incidents_created for result in results) == 1
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ci_incident_state WHERE active=1").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key LIKE 'ci-incident:%'").fetchone()[0] == 1


def test_billing_failure_is_platform_incident_not_test_failure() -> None:
    result = classify_workflow_run({
        "id": 101,
        "run_attempt": 1,
        "event": "pull_request",
        "conclusion": "failure",
        "jobs": [{
            "name": "test-suite",
            "conclusion": "failure",
            "steps": [{"name": "Set up job", "conclusion": "failure",
                       "text": "GitHub Actions usage has exceeded the allowed limit"}],
        }],
    })

    assert result.category == "platform_incident"
    assert result.cause == "billing"
    assert result.retry_safe is False


def test_genuine_test_failure_remains_a_test_failure() -> None:
    result = classify_workflow_run({
        "id": 102,
        "run_attempt": 1,
        "event": "pull_request",
        "conclusion": "failure",
        "jobs": [{"name": "tests", "conclusion": "failure",
                  "steps": [{"name": "pytest", "conclusion": "failure",
                             "text": "3 tests failed"}]}],
    })

    assert result.category == "test_failure"
    assert result.cause is None
    assert result.retry_safe is False


def test_cancelled_superseded_run_is_neither_incident_nor_test_failure() -> None:
    result = classify_workflow_run({
        "id": 103,
        "run_attempt": 1,
        "event": "pull_request",
        "conclusion": "cancelled",
        "head_sha": "old-head",
        "superseded_by_head_sha": "new-head",
        "jobs": [],
    })

    assert result.category == "superseded"
    assert result.cause is None
    assert result.retry_safe is False
