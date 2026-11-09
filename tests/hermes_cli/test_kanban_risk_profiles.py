"""Risk-profile policy is persisted with the canonical Kanban task contract."""

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban as kc
from hermes_cli.kanban_db_connect import connect as kb_connect
from hermes_cli.kanban_output import _task_to_dict
from hermes_cli.kanban_parser import build_parser


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_task_creation_defaults_ambiguous_work_to_high_with_required_checks(kanban_home):
    conn = kb_connect()
    task_id = kb.create_task(conn, title="Investigate unclear workflow")
    task = kb.get_task(conn, task_id)

    assert task.risk_profile == "high"
    assert task.risk_checks == ["design_contract", "migration_release_gates", "vivi_review"]


def test_explicit_low_profile_cannot_downgrade_auth_sensitive_path(kanban_home):
    conn = kb_connect()
    task_id = kb.create_task(
        conn, title="Update authentication flow", risk_profile="low",
        body="Change auth callback handling",
    )
    task = kb.get_task(conn, task_id)

    assert task.risk_profile == "high"
    assert "design_contract" in task.risk_checks
    assert "vivi_review" in task.risk_checks


def test_low_profile_is_selected_for_docs_and_exposes_checks(kanban_home):
    conn = kb_connect()
    task_id = kb.create_task(
        conn, title="Document install steps", risk_profile="low",
        body="Update documentation only",
    )
    task = kb.get_task(conn, task_id)

    assert task.risk_profile == "low"
    assert task.risk_checks == ["changed_package_tests", "lint", "vivi_review"]
    serialized = _task_to_dict(task)
    assert serialized["risk_profile"] == "low"
    assert serialized["risk_checks"] == task.risk_checks


def test_normal_profile_keeps_normal_checks_and_unknown_evidence_is_rejected(kanban_home):
    conn = kb_connect()
    task_id = kb.create_task(conn, title="Adjust ordinary API handler", risk_profile="normal")
    task = kb.get_task(conn, task_id)
    assert task.risk_profile == "normal"
    assert "synthetic_merge_check" in task.risk_checks
    with pytest.raises(ValueError, match="unknown check"):
        kb.create_task(conn, title="Another API handler", risk_profile="normal", risk_checks=["invented"])


def test_mixed_sensitive_path_and_user_checks_preserve_high_evidence(kanban_home):
    conn = kb_connect()
    task_id = kb.create_task(
        conn, title="Docs and deployment guide", risk_profile="low", risk_checks=["lint"],
    )
    task = kb.get_task(conn, task_id)
    assert task.risk_profile == "high"
    assert "migration_release_gates" in task.risk_checks
    assert "vivi_review" in task.risk_checks


def test_create_cli_persists_selected_profile_and_displays_required_checks(kanban_home, capsys):
    import argparse

    root = argparse.ArgumentParser()
    args = build_parser(root.add_subparsers()).parse_args(
        ["create", "Review docs", "--risk-profile", "low"]
    )
    assert kc.kanban_command(args) == 0
    output = capsys.readouterr().out

    assert "Risk: low" in output
    assert "changed_package_tests, lint, vivi_review" in output
