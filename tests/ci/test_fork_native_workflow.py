"""Contracts for the secret-free fork-native CI bootstrap."""

from __future__ import annotations

import re
from pathlib import Path

from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
UPSTREAM_ONLY = "github.repository == 'NousResearch/hermes-agent'"


def _load(name: str) -> dict:
    return YAML(typ="safe").load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_fork_native_workflow_runs_bounded_read_only_dynamic_and_strict_lanes() -> None:
    workflow = _load("fork-native-checks.yml")
    assert "pull_request" in workflow["on"]
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert "pull_request_target" not in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets: inherit" not in (WORKFLOWS / "fork-native-checks.yml").read_text(encoding="utf-8")

    job = workflow["jobs"]["native-checks"]
    assert job["name"] == "Workflow native checks"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] <= 30
    assert workflow["concurrency"]["group"].startswith("fork-native-")
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert workflow["env"]["HERMES_HOME"]

    actions = [step["uses"] for step in job["steps"] if "uses" in step]
    assert "actions/checkout@f548e57e544e1ff5a4c46bf1e1b8685f8e4a348a" in actions
    assert "./.github/actions/setup-pm" in actions
    assert all(
        uses.startswith("./") or re.fullmatch(r"[^@]+@[0-9a-f]{40}", uses)
        for uses in actions
    )
    setup = next(step for step in job["steps"] if step.get("uses") == "./.github/actions/setup-pm")
    assert setup["with"]["test-environment"] == "true"
    assert setup["with"]["extras"] == '["discord"]'

    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "find tests/ci" in commands
    assert "tests/hermes_cli/test_kanban_db.py" in commands
    assert "tests/hermes_cli/test_kanban_review_lifecycle.py" in commands
    assert "tests/hermes_cli/test_kanban_review_lifecycle_complete.py" in commands
    assert "tests/hermes_cli/test_kanban_review_surfaces.py" in commands
    assert "tests/hermes_cli/test_kanban_host_cap.py" in commands
    assert "tests/tools/test_kanban_tools.py" in commands
    assert "tests/agent/test_kanban_stop.py" in commands
    assert "tests/e2e/core/kanban/test_kanban_rate_limit_review.py" in commands
    e2e = next(step for step in job["steps"] if step.get("name") == "Run strict real-process Kanban lifecycle E2E")
    assert "tests/e2e/core/kanban/test_kanban_rate_limit_review.py" in e2e["run"]
    assert e2e["env"]["HERMES_E2E_STRICT_ACCEPTANCE"] == "1"
    assert e2e["env"]["HERMES_TEST_FILE_RETRIES"] == "0"
    assert e2e["env"]["HERMES_TEST_FILE_TIMEOUT"] == "600"
    for secret_name in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "NOUS_API_KEY"):
        assert e2e["env"][secret_name] == ""


def test_only_fork_triggerable_unsupported_runner_roots_are_upstream_guarded() -> None:
    jobs = {
        "ci.yaml": ("detect", "all-checks-pass", "ci-timings"),
        "docker.yml": ("mode", "release-phase-gate"),
        "nix.yml": ("detect", "flake-check"),
        "plugin-catalog-ci.yml": ("structural", "pinned-source-validate"),
        "pm-bundle.yml": ("bundle",),
        "termux-verify.yml": ("contracts", "tui", "r2-provisioned", "native-runtime"),
        "label-rerun.yml": ("rerun-with-e2e",),
        "windows-bundle-sdk.yml": ("windows-bundle-tools",),
    }
    for filename, names in jobs.items():
        workflow_jobs = _load(filename)["jobs"]
        for name in names:
            condition = str(workflow_jobs[name].get("if", ""))
            assert UPSTREAM_ONLY in condition, f"{filename}:{name} is not fork-isolated: {condition}"

    ci = _load("ci.yaml")["jobs"]
    assert "always()" in str(ci["all-checks-pass"]["if"])
    assert "always()" in str(ci["ci-timings"]["if"])


def test_docker_publication_remains_main_and_owner_fenced() -> None:
    jobs = _load("docker.yml")["jobs"]
    publish = jobs["publish"]
    assert "github.repository == 'NousResearch/hermes-agent'" in publish["if"]
    assert "github.event_name == 'push'" in publish["if"]
    assert "github.ref == 'refs/heads/main'" in publish["if"]
    assert publish["environment"] == "container-publish"

    release_manifest = jobs["release-publish-manifest"]
    assert "needs.mode.outputs.phase == 'publish'" in release_manifest["if"]
    assert release_manifest["environment"] == "container-publish"
