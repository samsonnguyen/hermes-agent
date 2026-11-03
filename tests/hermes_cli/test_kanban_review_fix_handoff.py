"""Atomic same-card review/fix handoff contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


_HEAD_V1 = "a" * 40
_HEAD_V2 = "b" * 40


def test_requested_fixes_bind_review_verdict_to_rejected_head_and_findings(tmp_path: Path) -> None:
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="Synthetic review fixture", assignee="builder")
        author = kb.claim_task(conn, task_id, claimer="builder:1")
        assert author is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="Synthetic test implementation ready.",
            metadata={"head_sha": _HEAD_V1},
            reviewer="reviewer_vivi",
            expected_run_id=author.current_run_id,
        )
        reviewer = kb.claim_review_task(conn, task_id, claimer="reviewer_vivi:1")
        assert reviewer is not None

        ok, routed_to = kb.request_changes(
            conn,
            task_id,
            reason="Fix the boundary condition.",
            rejected_head_sha=_HEAD_V1,
            findings=["tests/test_export.py: add a boundary assertion"],
            expected_run_id=reviewer.current_run_id,
        )
        assert (ok, routed_to) == (True, "builder")
        event = [e for e in kb.list_events(conn, task_id) if e.kind == "changes_requested"][-1]
        assert event.payload == {
            "reason": "Fix the boundary condition.",
            "implementer": "builder",
            "reviewer": "reviewer_vivi",
            "status": "ready",
            "rejected_head_sha": _HEAD_V1,
            "findings": ["tests/test_export.py: add a boundary assertion"],
        }
        assert kb.get_task(conn, task_id).status == "ready"

        author_2 = kb.claim_task(conn, task_id, claimer="builder:2")
        assert author_2 is not None
        assert not kb.request_review(
            conn,
            task_id,
            summary="No new push; same reviewed commit.",
            metadata={"head_sha": _HEAD_V1},
            expected_run_id=author_2.current_run_id,
        )
        unchanged = kb.get_task(conn, task_id)
        assert unchanged is not None
        assert unchanged.status == "running"
        assert unchanged.current_run_id == author_2.current_run_id
        assert len([e for e in kb.list_events(conn, task_id) if e.kind == "review_requested"]) == 1
        assert len(kb.list_comments(conn, task_id)) == 0
        assert len(kb.list_runs(conn, task_id)) == 3

        assert kb.request_review(
            conn,
            task_id,
            summary="Synthetic test fix pushed.",
            metadata={"head_sha": _HEAD_V2},
            expected_run_id=author_2.current_run_id,
        )
        final_event = [e for e in kb.list_events(conn, task_id) if e.kind == "review_requested"][-1]
        assert final_event.payload["reviewer"] == "reviewer_vivi"
        assert kb.get_task(conn, task_id).status == "review"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE title LIKE 'FIXES FOR %'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_reviewer_identity_comes_from_claimed_run_not_mutable_assignee(tmp_path: Path) -> None:
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="Synthetic poisoned identity fixture", assignee="builder")
        author = kb.claim_task(conn, task_id, claimer="builder:1")
        assert author is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="Synthetic test implementation ready.",
            metadata={"head_sha": _HEAD_V1},
            reviewer="reviewer_vivi",
            expected_run_id=author.current_run_id,
        )
        reviewer = kb.claim_review_task(conn, task_id, claimer="reviewer_vivi:1")
        assert reviewer is not None
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee = 'poisoned_legacy_reviewer' WHERE id = ?", (task_id,))

        ok, routed_to = kb.request_changes(
            conn,
            task_id,
            reason="Correct the synthetic fixture.",
            rejected_head_sha=_HEAD_V1,
            findings=["synthetic finding"],
            expected_run_id=reviewer.current_run_id,
        )
        assert (ok, routed_to) == (True, "builder")
        event = [e for e in kb.list_events(conn, task_id) if e.kind == "changes_requested"][-1]
        assert event.payload["reviewer"] == "reviewer_vivi"
        assert event.payload["implementer"] == "builder"
        assert kb.get_task(conn, task_id).assignee == "builder"
    finally:
        conn.close()


def test_failed_verdict_event_write_rolls_back_routing_and_run_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(conn, title="Synthetic atomicity fixture", assignee="builder")
        author = kb.claim_task(conn, task_id, claimer="builder:1")
        assert author is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="Synthetic implementation ready.",
            metadata={"head_sha": _HEAD_V1},
            reviewer="reviewer_vivi",
            expected_run_id=author.current_run_id,
        )
        reviewer = kb.claim_review_task(conn, task_id, claimer="reviewer_vivi:1")
        assert reviewer is not None

        def fail_event(*args, **kwargs):
            raise RuntimeError("synthetic event write failure")

        monkeypatch.setattr(kb, "_append_event", fail_event)
        with pytest.raises(RuntimeError, match="synthetic event write failure"):
            kb.request_changes(
                conn,
                task_id,
                reason="Synthetic rollback check.",
                rejected_head_sha=_HEAD_V1,
                findings=["synthetic finding"],
                expected_run_id=reviewer.current_run_id,
            )

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.assignee == "reviewer_vivi"
        assert task.current_run_id == reviewer.current_run_id
        assert not [e for e in kb.list_events(conn, task_id) if e.kind == "changes_requested"]
        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.outcome is None
    finally:
        conn.close()
