"""Structured, head-bound Kanban review evidence.

Discussion comments and lifecycle events are not review attestations. These
records are immutable per task/run/head and can be counted without parsing text.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

_OUTCOMES = frozenset({"APPROVED", "CHANGES_REQUESTED", "AWAITING_HUMAN"})
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def record_review_verdict(conn: sqlite3.Connection, task_id: str, *, outcome: str,
                          head_sha: str, reviewer: str, findings: list[Any],
                          expected_run_id: int) -> bool:
    """Persist one review outcome only for the currently claimed review run.

    Repeating the same write is successful; a conflicting verdict for the same
    run/head is refused. Findings are structured JSON and contain no comments.
    """
    from hermes_cli import kanban_db as _kb

    if outcome not in _OUTCOMES or not isinstance(head_sha, str) or not _FULL_SHA.fullmatch(head_sha):
        return False
    if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(findings, list):
        return False
    serialized = json.dumps(findings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with _kb.write_txn(conn):
        task = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        run = conn.execute(
            "SELECT status, profile FROM task_runs WHERE id = ? AND task_id = ?",
            (expected_run_id, task_id),
        ).fetchone()
        if (task is None or run is None or task["status"] != "running"
                or task["current_run_id"] != expected_run_id
                or run["status"] != "running" or task["assignee"] != reviewer
                or run["profile"] != reviewer):
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
