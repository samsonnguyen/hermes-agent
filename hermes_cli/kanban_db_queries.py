"""Read-only Kanban query helpers split from the database facade."""
from __future__ import annotations

import sqlite3
from typing import Iterable


def current_run_started_ats(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, int]:
    """Map task ids to their active run start time; omit absent run pointers."""
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT t.id AS task_id, r.started_at AS started_at FROM tasks t "
        "JOIN task_runs r ON r.id = t.current_run_id "
        f"WHERE t.id IN ({placeholders})",
        ids,
    ).fetchall()
    return {row["task_id"]: row["started_at"] for row in rows}
