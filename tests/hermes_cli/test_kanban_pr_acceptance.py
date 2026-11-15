"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "merge": "c" * 40,
             "pr_state": "MERGED", "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            if self.path == "/graphql":
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main", "state": state["pr_state"],
                    "merged": state["pr_state"] == "MERGED",
                    "mergeCommit": {"oid": state["merge"]} if state["pr_state"] == "MERGED" else None,
                    "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [] if state.get("no_required") else [
                        {"context": "required", "app": {"databaseId": 1}}]}}}}}}
            elif "/rules/branches/" in self.path:
                value = [[]]
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": "required", "head_sha": state["merge"],
                       "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"},
                         "state": "closed" if state["pr_state"] == "MERGED" else "open",
                         "merged": state["pr_state"] == "MERGED",
                         "merge_commit_sha": state["merge"] if state["pr_state"] == "MERGED" else None}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys,urllib.request\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "print(urllib.request.urlopen(u).read().decode())\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    hermes_home = tmp_path / "home"
    for profile in ("builder", "reviewer"):
        (hermes_home / "profiles" / profile).mkdir(parents=True)
        (hermes_home / "profiles" / profile / "config.yaml").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _approve_head_bound_review(conn, task_id, head_sha):
    implementation = kb.claim_task(conn, task_id, claimer="builder:test")
    assert implementation is not None
    assert kb.request_review(conn, task_id, reviewer="reviewer", summary="Ready.",
                             metadata={"head_sha": head_sha},
                             expected_run_id=implementation.current_run_id)
    review = kb.claim_review_task(conn, task_id)
    assert review is not None and review.current_run_id is not None
    assert kb.record_review_verdict(
        conn, task_id, outcome="APPROVED", head_sha=head_sha, reviewer="reviewer",
        findings=[], expected_run_id=review.current_run_id,
    )
    return review.current_run_id


@pytest.mark.platforms("linux")
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", assignee="builder", completion_contract="acme/repo")
            _approve_head_bound_review(conn, tid, "a" * 40)
            ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            assert receipts[-1]["merge_sha"] == "c" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, assignee="builder", completion_contract="acme/repo")
            _approve_head_bound_review(conn, tid, "a" * 40)
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Reproduction for #205: passing checks on an OPEN PR must never close the card.
        github["pr_state"] = "OPEN"
        tid = kb.create_task(conn, title="still open", assignee="builder", completion_contract="acme/repo")
        _approve_head_bound_review(conn, tid, "a" * 40)
        assert not kb.complete_task(
            conn, tid, result="done",
            metadata={"published_pr": "https://github.com/acme/repo/pull/7"},
        )
        assert kb.get_task(conn, tid).status != "done"

        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.platforms("linux")
def test_pr_completion_requires_merged_reviewed_head_and_nonempty_policy(github):
    from hermes_cli.kanban_pr_acceptance import collect_acceptance

    with connect() as conn:
        for state, head, expected in (("CLOSED", "a" * 40, "pending"),
                                      ("MERGED", "b" * 40, "stale")):
            github.update(pr_state=state, head=head, no_required=False)
            tid = kb.create_task(conn, title="stale review", assignee="builder", completion_contract="acme/repo")
            _approve_head_bound_review(conn, tid, "a" * 40)
            assert not kb.complete_task(
                conn, tid, result="done",
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"},
            )
            receipt = json.loads(conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'",
                (tid,),
            ).fetchone()[0])
            assert receipt["classification"] == expected
            assert kb.get_task(conn, tid).status != "done"

        github.update(pr_state="MERGED", head="a" * 40, no_required=True)
        receipt = collect_acceptance(
            "acme/repo", "https://github.com/acme/repo/pull/7",
            assignee="reviewer", reviewed_sha="a" * 40,
        )
        assert receipt["classification"] == "missing"
        assert receipt["ok"] is False


def test_awaiting_human_completion_reason_is_nonterminal():
    kb.init_db()
    with connect() as conn:
        task_id = kb.create_task(conn, title="wait for human", completion_contract="local-only")
        assert not kb.complete_task(
            conn, task_id, result="waiting", metadata={"completion_reason": "awaiting_human"},
        )
        assert kb.get_task(conn, task_id).status != "done"


@pytest.mark.platforms("linux")
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            github.update(conclusion=conclusion, head="a" * 40, pr_state="MERGED")
            tid = kb.create_task(conn, title="race", assignee="builder", completion_contract="acme/repo")
            run_id = _approve_head_bound_review(conn, tid, "a" * 40)
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, result="done", expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


# --- #122689: acceptance must read the repo as the ASSIGNEE profile's gh login ---

@pytest.mark.platforms("posix")
def test_acceptance_runs_gh_as_the_assignee_profile(tmp_path, monkeypatch):
    """The gh child env carries the assignee's own GH credentials (its .env),
    never the ambient/launch residue, and an invisible repo is classified
    `auth` naming the repository — not a retryable `infra` failure."""
    from pathlib import Path

    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("GH_TOKEN=b-token\n", encoding="utf-8")
    # Ambient residue that must NOT decide the login.
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport json, os\n"
                  f"json.dump(dict(os.environ), open({str(env_dump)!r}, 'w'))\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    from hermes_cli.kanban_pr_acceptance import collect_acceptance

    acceptance = collect_acceptance(
        "acme/repo", "https://github.com/acme/repo/pull/7", assignee="b",
        reviewed_sha="a" * 40,
    )
    assert acceptance["classification"] == "auth"
    assert "acme/repo" in acceptance["detail"]
    captured = json.loads(env_dump.read_text())
    assert captured["GH_TOKEN"] == "b-token"
    assert captured.get("GH_CONFIG_DIR") != "/nonexistent/launch/gh"


@pytest.mark.platforms("posix")
def test_assignee_without_own_gh_login_never_falls_through_to_ambient_login(tmp_path, monkeypatch):
    """An assignee profile with no GH_TOKEN/GH_CONFIG_DIR of its own must not inherit the
    launch user's ~/.config/gh (HOME/XDG_CONFIG_HOME stay the launch process's): gh is pinned
    to a profile-owned config dir, its 'not logged in' exit is classified `auth` naming the profile."""
    launch_home = tmp_path / "home"
    launch_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    assignee_home = launch_home / "profiles" / "b"
    assignee_home.mkdir(parents=True)
    (assignee_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN", "launch-token")
    monkeypatch.setenv("GH_CONFIG_DIR", "/nonexistent/launch/gh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "launch-xdg"))

    env_dump = tmp_path / "gh_env.json"
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    # Real gh: GH_CONFIG_DIR wins; a config dir without hosts.yml means "not logged in" (exit 4).
    gh.write_text(f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
                  f"pathlib.Path({str(env_dump)!r}).write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n"
                  "if 'GH_CONFIG_DIR' in os.environ and not os.path.exists(os.environ['GH_CONFIG_DIR']):\n"
                  "    sys.exit(4)\n"
                  "print(json.dumps({'data': {'repository': None}}))\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    from hermes_cli.kanban_pr_acceptance import collect_acceptance

    acceptance = collect_acceptance(
        "acme/repo", "https://github.com/acme/repo/pull/7", assignee="b",
        reviewed_sha="a" * 40,
    )
    assert acceptance["classification"] == "auth"
    assert "'b'" in acceptance["detail"] and "no login" in acceptance["detail"]
    captured = json.loads(env_dump.read_text(encoding="utf-8-sig"))
    assert captured["GH_CONFIG_DIR"] == str(assignee_home / "gh")
    assert "GH_TOKEN" not in captured and "GITHUB_TOKEN" not in captured


def test_pr_api_failure_does_not_close_contract_task(tmp_path, monkeypatch):
    from hermes_cli.kanban_pr_acceptance import collect_acceptance

    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    receipt = collect_acceptance(
        "acme/repo", "https://github.com/acme/repo/pull/7",
        reviewed_sha="a" * 40,
    )
    assert receipt["classification"] == "infra"
    assert receipt["ok"] is False


def test_assigned_card_with_unresolvable_profile_is_auth_not_ambient(tmp_path, monkeypatch):
    """A card assigned to a profile that no longer exists must not run gh as the completing
    process's ambient login: classification `auth` naming the profile, gh never invoked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # any gh spawn would fail as infra
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="as-ghost", completion_contract="acme/repo", assignee="ghost")
        assert not kb.complete_task(conn, tid, result="done",
                                    metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0])
    assert receipt["classification"] == "auth"
    assert "'ghost'" in receipt["detail"] and "cannot be resolved" in receipt["detail"]
