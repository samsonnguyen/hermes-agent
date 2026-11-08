"""Classify GitHub Actions outcomes without treating infrastructure outages as code failures."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkflowRunClassification:
    """Safe disposition for one workflow-run payload."""

    category: str
    cause: str | None
    retry_safe: bool


_BILLING_MARKERS = (
    "usage has exceeded the allowed limit",
    "billing issue",
    "spending limit",
    "payment method",
)
_RUNNER_MARKERS = (
    "no hosted parallelism",
    "runner application failed",
    "runner lost communication",
    "failed to download action",
    "cannot find a runner",
)


def classify_workflow_run(run: dict[str, Any]) -> WorkflowRunClassification:
    """Classify only explicit platform signatures; ambiguous failures stay unclassified.

    GitHub API job/step log excerpts are normalized from optional ``text`` fields
    by callers. The function accepts the normalized payload so no network calls,
    credentials, or private board data enter this module.
    """
    conclusion = str(run.get("conclusion") or "").casefold()
    if conclusion == "cancelled":
        if run.get("superseded_by_head_sha"):
            return WorkflowRunClassification("superseded", None, False)
        return WorkflowRunClassification("cancelled", None, False)
    if conclusion in {"success", "neutral", "skipped"}:
        return WorkflowRunClassification("success", None, False)
    if conclusion != "failure":
        return WorkflowRunClassification("inconclusive", None, False)

    text = "\n".join(
        str(step.get("text") or "")
        for job in run.get("jobs", []) if isinstance(job, dict)
        for step in job.get("steps", []) if isinstance(step, dict)
    ).casefold()
    if any(marker in text for marker in _BILLING_MARKERS):
        return WorkflowRunClassification("platform_incident", "billing", False)
    if any(marker in text for marker in _RUNNER_MARKERS):
        return WorkflowRunClassification("platform_incident", "runner", False)
    if any(marker in text for marker in ("tests failed", "test failed", "assertionerror", "pytest failed")):
        return WorkflowRunClassification("test_failure", None, False)
    return WorkflowRunClassification("unavailable", None, False)


@dataclass(frozen=True)
class PollResult:
    incidents_created: int = 0
    holds_released: int = 0


def _fetch_annotation_evidence(repository: str, job: dict[str, Any], run_process) -> list[str]:
    check_url = str(job.get("check_run_url") or "")
    match = re.search(r"/check-runs/(\d+)$", check_url)
    if not match:
        return []
    annotations = run_process(
        ["gh", "api", f"repos/{repository}/check-runs/{match.group(1)}/annotations?per_page=100"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if annotations.returncode:
        return []
    values = json.loads(annotations.stdout)
    if not isinstance(values, list):
        return []
    markers = (*_BILLING_MARKERS, *_RUNNER_MARKERS,
               "tests failed", "test failed", "assertionerror", "pytest failed")
    evidence = []
    for annotation in values[:100]:
        if isinstance(annotation, dict):
            message = str(annotation.get("message") or "")[:4096].casefold()
            evidence.extend(marker for marker in markers if marker in message)
    return evidence


def _fetch_github_runs(repository: str, cursor: str | None) -> list[dict[str, Any]]:
    """Fetch bounded workflow runs and failed-job details using the installed gh auth."""
    import subprocess

    if not _REPOSITORY.fullmatch(repository):
        raise ValueError("repository must be owner/name")
    result = subprocess.run(
        ["gh", "api", f"repos/{repository}/actions/runs?per_page=100"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode:
        raise RuntimeError("GitHub Actions query unavailable")
    raw = json.loads(result.stdout)
    if not isinstance(raw, dict) or not isinstance(raw.get("workflow_runs"), list):
        raise RuntimeError("GitHub Actions response malformed")
    runs = []
    cursor_id, cursor_attempt = (map(int, cursor.split(":")) if cursor else (0, 0))
    for run in raw["workflow_runs"][:100]:
        if not isinstance(run, dict):
            continue
        run_id = int(run["id"])
        attempt = int(run.get("run_attempt") or 1)
        if (run_id, attempt) <= (cursor_id, cursor_attempt):
            continue
        item = {"id": run_id, "run_attempt": run.get("run_attempt"),
                "head_sha": run.get("head_sha"), "conclusion": run.get("conclusion"), "jobs": []}
        if item["conclusion"] == "failure":
            jobs_result = subprocess.run(
                ["gh", "api", f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if jobs_result.returncode:
                item["conclusion"] = "unknown"
            else:
                jobs = json.loads(jobs_result.stdout).get("jobs", [])
                normalized = [
                    {"conclusion": job.get("conclusion"),
                     "steps": [{"text": " ".join(_fetch_annotation_evidence(repository, job, subprocess.run))}]}
                    for job in jobs[:100] if isinstance(job, dict)
                ]
                item["jobs"] = normalized
        runs.append(item)
    return runs


_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def poll_repository(conn, repository: str, task_allowlist: dict[str, dict[str, list[str]]], *,
                    fetch_runs=_fetch_github_runs) -> PollResult:
    """Poll allowlisted repo/head dependencies and mutate only their incident-owned holds.

    ``task_allowlist`` maps repo -> current head SHA -> dependent task IDs. The caller
    must supply that scoped mapping; no board-wide discovery or automatic retries occur.
    """
    from hermes_cli.kanban_db import (
        complete_task, create_task, get_task, link_tasks, unlink_tasks, write_txn,
    )

    if repository not in task_allowlist or not _REPOSITORY.fullmatch(repository):
        raise ValueError("repository is not explicitly allowlisted")
    conn.execute("""CREATE TABLE IF NOT EXISTS ci_incident_state (
        repository TEXT NOT NULL, cause TEXT NOT NULL, head_sha TEXT NOT NULL, epoch INTEGER NOT NULL,
        run_id INTEGER NOT NULL, run_attempt INTEGER NOT NULL, incident_task_id TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(repository,cause,head_sha,epoch))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS ci_incident_holds (
        incident_task_id TEXT NOT NULL, child_task_id TEXT NOT NULL,
        PRIMARY KEY(incident_task_id,child_task_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS ci_incident_cursors (
        repository TEXT NOT NULL, run_id INTEGER NOT NULL, run_attempt INTEGER NOT NULL,
        PRIMARY KEY(repository,run_id))""")
    runs = fetch_runs(repository, None)
    created = released = 0
    for run in runs:
        try:
            run_id, attempt = int(run["id"]), int(run.get("run_attempt") or 1)
            head = str(run["head_sha"])
        except (KeyError, TypeError, ValueError):
            continue
        processed = conn.execute("SELECT run_attempt FROM ci_incident_cursors WHERE repository=? AND run_id=?",
                                 (repository, run_id)).fetchone()
        if processed and attempt <= int(processed[0]):
            continue
        current_tasks = task_allowlist[repository].get(head, [])
        disposition = classify_workflow_run(run)
        if disposition.category == "platform_incident" and current_tasks:
            with write_txn(conn):
                row = conn.execute("SELECT incident_task_id FROM ci_incident_state WHERE repository=? AND cause=? AND head_sha=? AND active=1",
                                   (repository, disposition.cause, head)).fetchone()
                if row is None:
                    epoch = conn.execute("SELECT COALESCE(MAX(epoch),0)+1 FROM ci_incident_state WHERE repository=? AND cause=? AND head_sha=?",
                                         (repository, disposition.cause, head)).fetchone()[0]
                    key = f"ci-incident:{repository}:{disposition.cause}:{head}:{epoch}"
                    incident = create_task(conn, title=f"CI platform incident: {disposition.cause}",
                        body=f"CI platform incident for {repository} at head {head}; run {run_id} attempt {attempt}.",
                        created_by="ci-poller", idempotency_key=key, initial_status="running")
                    conn.execute("INSERT INTO ci_incident_state VALUES (?,?,?,?,?,?,?,1)",
                                 (repository, disposition.cause, head, epoch, run_id, attempt, incident))
                    created += 1
                else:
                    incident = row[0]
            for child in current_tasks:
                task = get_task(conn, child)
                if task and task.status in {"ready", "todo"}:
                    link_tasks(conn, incident, child)
                    conn.execute("INSERT OR IGNORE INTO ci_incident_holds VALUES (?,?)", (incident, child))
        elif disposition.category == "success" and current_tasks:
            rows = conn.execute("SELECT incident_task_id FROM ci_incident_state WHERE repository=? AND head_sha=? AND active=1",
                                (repository, head)).fetchall()
            for (incident,) in rows:
                holds = conn.execute("SELECT child_task_id FROM ci_incident_holds WHERE incident_task_id=?", (incident,)).fetchall()
                for (child,) in holds:
                    if child in current_tasks:
                        unlink_tasks(conn, incident, child)
                        conn.execute("DELETE FROM ci_incident_holds WHERE incident_task_id=? AND child_task_id=?", (incident, child))
                remaining = conn.execute("SELECT 1 FROM ci_incident_holds WHERE incident_task_id=?", (incident,)).fetchone()
                if not remaining:
                    conn.execute("UPDATE ci_incident_state SET active=0 WHERE incident_task_id=?", (incident,))
                    complete_task(conn, incident, result="Verified successful run on the allowlisted current head.")
                    released += 1
        conn.execute("INSERT INTO ci_incident_cursors VALUES (?,?,?) ON CONFLICT(repository,run_id) DO UPDATE SET run_attempt=excluded.run_attempt WHERE excluded.run_attempt>ci_incident_cursors.run_attempt",
                     (repository, run_id, attempt))
    conn.commit()
    return PollResult(created, released)


def main(argv: list[str] | None = None) -> int:
    """Opt-in one-shot CLI; there is deliberately no scheduled registration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable", action="store_true", help="explicitly enable this one poll")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--head-task", action="append", default=[], metavar="SHA=TASK_ID")
    args = parser.parse_args(argv)
    if not args.enable:
        parser.error("refusing to poll without explicit --enable")
    mapping: dict[str, list[str]] = {}
    for value in args.head_task:
        head, separator, task_id = value.partition("=")
        if not separator or not head or not task_id:
            parser.error("--head-task must be SHA=TASK_ID")
        mapping.setdefault(head, []).append(task_id)
    if not mapping:
        parser.error("at least one explicit --head-task allowlist entry is required")
    from hermes_cli.kanban_db_connect import connect

    with connect() as conn:
        result = poll_repository(conn, args.repository, {args.repository: mapping})
    print(f"incidents_created={result.incidents_created} holds_released={result.holds_released}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
