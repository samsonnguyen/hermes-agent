"""Kanban review lifecycle operations and structured review evidence."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
from typing import Any, Optional

import hermes_cli.kanban_db as _kb

_OUTCOMES = frozenset({"APPROVED", "CHANGES_REQUESTED", "AWAITING_HUMAN"})
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def record_review_verdict(conn: sqlite3.Connection, task_id: str, *, outcome: str,
                          head_sha: str, reviewer: str, findings: list[Any],
                          expected_run_id: int) -> bool:
    """Persist an immutable review verdict from the claimed independent review run."""
    if outcome not in _OUTCOMES or not isinstance(head_sha, str) or not _FULL_SHA.fullmatch(head_sha):
        return False
    if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(findings, list):
        return False
    serialized = json.dumps(findings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with _kb.write_txn(conn, allow_nested=True):
        task = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        run = conn.execute(
            "SELECT status, profile FROM task_runs WHERE id = ? AND task_id = ?",
            (expected_run_id, task_id),
        ).fetchone()
        if (task is None or run is None or task["status"] != "running"
                or task["current_run_id"] != expected_run_id
                or run["status"] != "running"
                or run["profile"] != reviewer):
            return False

        claimed_event = _kb._latest_event(conn, task_id, "claimed", expected_run_id)
        claimed_payload = _kb._json_dict(_kb._row_get(claimed_event, "payload"))
        if claimed_payload.get("source_status") != "review":
            return False

        requested_event = _kb._latest_event(conn, task_id, "review_requested")
        if requested_event is None:
            return False
        requested_payload = _kb._json_dict(_kb._row_get(requested_event, "payload"))
        implementer = _kb._canonical_assignee(
            _kb._nonblank_str(requested_payload.get("implementer"))
        )
        expected_reviewer = _kb._canonical_assignee(
            _kb._nonblank_str(requested_payload.get("reviewer"))
        )
        if implementer is None or expected_reviewer != reviewer or implementer == reviewer:
            return False
        request_run = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (requested_event["run_id"],)
        ).fetchone()
        request_metadata = _kb._json_dict(_kb._row_get(request_run, "metadata"))
        reviewed_head = request_metadata.get("head_sha")
        if (not isinstance(reviewed_head, str) or not _FULL_SHA.fullmatch(reviewed_head)
                or reviewed_head != head_sha):
            return False

        existing = conn.execute(
            "SELECT outcome, reviewer, findings FROM task_review_verdicts "
            "WHERE task_id = ? AND run_id = ? AND head_sha = ?",
            (task_id, expected_run_id, head_sha),
        ).fetchone()
        if existing is not None:
            return (existing["outcome"], existing["reviewer"], existing["findings"]) == (
                outcome, reviewer, serialized
            )
        conn.execute(
            "INSERT INTO task_review_verdicts "
            "(task_id, run_id, outcome, head_sha, reviewer, findings, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, expected_run_id, outcome, head_sha, reviewer, serialized, int(time.time())),
        )
    return True


def record_native_review_verdict(
    conn: sqlite3.Connection, task_id: str, *, outcome: str,
    expected_run_id: int, findings: Optional[list[Any]] = None,
) -> bool:
    """Persist a head-bound verdict at a native review lifecycle boundary.

    Cards without a head-bound handoff retain legacy lifecycle behavior but do
    not claim structured, commit-specific evidence.
    """
    claimed = _kb._latest_event(conn, task_id, "claimed", expected_run_id)
    claimed_from = _kb._json_dict(_kb._row_get(claimed, "payload")).get("source_status")
    if claimed_from != "review":
        return True
    requested = _kb._latest_event(conn, task_id, "review_requested")
    if requested is None:
        return True
    request_run = conn.execute(
        "SELECT metadata FROM task_runs WHERE id = ?", (requested["run_id"],)
    ).fetchone()
    metadata = _kb._json_dict(_kb._row_get(request_run, "metadata"))
    head_sha = metadata.get("head_sha")
    if head_sha is None:
        return True
    reviewer_run = conn.execute(
        "SELECT profile FROM task_runs WHERE id = ?", (expected_run_id,)
    ).fetchone()
    reviewer = _kb._nonblank_str(reviewer_run["profile"] if reviewer_run else None)
    if reviewer is None:
        return False
    return record_review_verdict(
        conn, task_id, outcome=outcome, head_sha=head_sha, reviewer=reviewer,
        findings=findings or [], expected_run_id=expected_run_id,
    )


def list_review_verdicts(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """Return structured review cycles for a task in insertion order."""
    rows = conn.execute(
        "SELECT outcome, head_sha, reviewer, run_id, findings, created_at "
        "FROM task_review_verdicts WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()
    return [
        {"outcome": row["outcome"], "head_sha": row["head_sha"],
         "reviewer": row["reviewer"], "run_id": row["run_id"],
         "findings": json.loads(row["findings"]), "created_at": row["created_at"]}
        for row in rows
    ]


def request_changes(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str,
    rejected_head_sha: Optional[str] = None,
    findings: Optional[list[str]] = None,
    expected_run_id: Optional[int] = None,
) -> tuple[bool, Optional[str]]:
    """Atomically close an active review run and route its findings to the implementer.

    Head-bound reviews persist the exact rejected commit and actionable findings.
    Legacy handoffs without a head SHA remain supported. A stale or malformed
    transition has no side effects.
    """
    reason = str(_kb.redact_review_value(reason or "")).strip()
    if rejected_head_sha is not None:
        rejected_head_sha = str(rejected_head_sha).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", rejected_head_sha):
            return False, "rejected_head_sha must be a full 40-character hexadecimal commit SHA"
        findings = _kb.redact_review_value(findings)
        if not isinstance(findings, list) or not findings or any(
            not isinstance(item, str) or not item.strip() for item in findings
        ):
            return False, "findings must be a non-empty list of actionable strings"
        findings = [item.strip() for item in findings]
    elif findings is not None:
        return False, "findings require rejected_head_sha"
    if not reason:
        return False, "reason is required"

    with _kb.write_txn(conn):
        task_row = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if task_row is None:
            return False, "task not found"
        current_run_id = task_row["current_run_id"]
        if task_row["status"] != "running" or current_run_id is None:
            return False, "task is not in an active review run"
        if expected_run_id is not None and int(current_run_id) != int(expected_run_id):
            return False, "run_id mismatch"

        claimed_event = _kb._latest_event(conn, task_id, "claimed", current_run_id)
        claimed_payload = _kb._json_dict(_kb._row_get(claimed_event, "payload"))
        if claimed_payload.get("source_status") != "review":
            return False, "active run was not claimed from review"

        requested_event = _kb._latest_event(conn, task_id, "review_requested")
        if requested_event is None:
            return False, "no prior review_requested event"
        requested_payload = _kb._json_dict(requested_event["payload"])
        implementer = _kb._nonblank_str(requested_payload.get("implementer"))
        if implementer is None:
            return False, "review handoff has no valid implementer provenance"
        run = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (requested_event["run_id"],),
        ).fetchone()
        requested_metadata = _kb._json_dict(_kb._row_get(run, "metadata"))
        reviewed_head_sha = _kb._nonblank_str(requested_metadata.get("head_sha"))
        if reviewed_head_sha and not re.fullmatch(r"[0-9a-fA-F]{40}", reviewed_head_sha):
            return False, "review handoff head_sha is malformed"
        if reviewed_head_sha and rejected_head_sha != reviewed_head_sha.lower():
            return False, "rejected_head_sha does not match the reviewed handoff head_sha"
        if rejected_head_sha and not reviewed_head_sha:
            return False, "review handoff has no head_sha to bind this verdict to"
        reviewer_run = conn.execute(
            "SELECT profile FROM task_runs WHERE id = ?", (current_run_id,),
        ).fetchone()
        reviewer = _kb._canonical_assignee(
            _kb._nonblank_str(reviewer_run["profile"] if reviewer_run else None),
        )
        if reviewer is None:
            return False, "active review run has no valid reviewer provenance"
        if not record_native_review_verdict(
            conn, task_id, outcome="CHANGES_REQUESTED",
            expected_run_id=int(current_run_id), findings=findings or [],
        ):
            return False, "could not persist the head-bound review verdict"

        new_status = _kb._landing_status_after_parents(conn, task_id)
        # Review is not evidence that the failure pathology cleared.
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = ?,
                   assignee = COALESCE(?, assignee),
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL, worker_started_at = NULL
             WHERE id = ? AND status = 'running' AND current_run_id = ?
            """,
            (new_status, implementer, task_id, int(current_run_id)),
        )
        if cur.rowcount != 1:
            return False, "task changed during review handoff"
        run_id = _kb._end_run(
            conn, task_id, outcome="changes_requested", status=new_status, summary=reason,
        )
        payload: dict[str, object] = {
            "reason": reason,
            "implementer": implementer,
            "reviewer": reviewer,
            "status": new_status,
        }
        if rejected_head_sha is not None:
            payload.update(rejected_head_sha=rejected_head_sha, findings=findings)
        _kb._append_event(conn, task_id, "changes_requested", payload, run_id=run_id)
    return True, implementer


def verify_re_review_publication(conn: sqlite3.Connection, task_id: str,
                                 metadata: Optional[dict]) -> Optional[str]:
    """Require the submitted re-review head to be the live head of its PR."""
    changes = conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? AND outcome = 'changes_requested' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if changes is None:
        return None
    event = _kb._latest_event(conn, task_id, "changes_requested", changes["id"])
    rejected = _kb._json_dict(_kb._row_get(event, "payload")).get("rejected_head_sha")
    if not rejected:
        return None
    submitted = metadata.get("head_sha") if isinstance(metadata, dict) else None
    submitted = str(submitted).strip().lower() if submitted is not None else None
    if submitted is None:
        return "re-review must include the pushed metadata.head_sha"
    if not re.fullmatch(r"[0-9a-f]{40}", submitted):
        return "metadata.head_sha must be a full 40-character hexadecimal commit SHA"
    if rejected == submitted:
        return "the pushed head_sha is unchanged from the rejected review head"
    task = conn.execute(
        "SELECT completion_contract, assignee FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if task is None:
        return "task not found"
    try:
        from hermes_cli.kanban_pr_acceptance import verify_published_head

        published = metadata.get("published_pr") if isinstance(metadata, dict) else None
        verified = verify_published_head(task["completion_contract"], published, submitted, task["assignee"])
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, KeyError, TypeError, IndexError):
        verified = False
    if not verified:
        return "metadata.head_sha is not verified as the current head of the task's published PR"
    return None
