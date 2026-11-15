"""Versioned, fail-closed workflow risk and evidence policy."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

CONTRACT_VERSION = "1.0.0"
_MODE = "ADVISORY"
_CONTRACT = json.loads((Path(__file__).parent / "contract.json").read_text(encoding="utf-8-sig"))
_PROFILE_RANK = {"low": 0, "normal": 1, "high": 2}
_REQUIRED_CHECKS = _CONTRACT["profiles"]
_LOW_ROOTS = ("docs/", "plans/", "tests/", "website/", "ui-tui/", "apps/desktop/")
_NORMAL_ROOTS = ("agent/", "gateway/", "tools/", "hermes_cli/", "plugins/", "skills/", "web/")
_SENSITIVE = re.compile(
    r"(^|/)(migrations?|alembic|auth|authentication|rbac|deploy|flux|zitadel|billing|payments?|money)"
    r"(\.[^/]+|[-_][^/]*|/|$)",
    re.IGNORECASE,
)


def _classify_path(raw_path: str) -> str:
    """Classify known roots; malformed and unknown paths resolve to high."""
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
        return "high"
    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts or raw_path.startswith("./"):
        return "high"
    normalized = path.as_posix()
    if _SENSITIVE.search(normalized):
        return "high"
    if normalized.startswith(_LOW_ROOTS):
        return "low"
    if normalized.startswith(_NORMAL_ROOTS):
        return "normal"
    return "high"


def evaluate(paths: Sequence[str], requested_profile: str | None = None) -> dict[str, Any]:
    """Return an advisory risk read-model; never authorizes a transition."""
    if isinstance(paths, (str, bytes)) or not isinstance(paths, Sequence):
        paths = []
        inferred = "high"
    else:
        inferred = "high" if not paths else max((_classify_path(p) for p in paths), key=_PROFILE_RANK.__getitem__)
    request = requested_profile if requested_profile in _PROFILE_RANK else "high"
    profile = max((inferred, request), key=_PROFILE_RANK.__getitem__)
    return {
        "contract_version": CONTRACT_VERSION,
        "mode": _MODE,
        "profile": profile,
        "requested_profile": requested_profile,
        "inferred_profile": inferred,
        "paths": list(paths),
        "required_checks": list(_REQUIRED_CHECKS[profile]),
        "enforcement": "NONE",
    }


def doctor(risk: Mapping[str, Any], evidence: Mapping[str, Any] | None) -> dict[str, Any]:
    """Summarize evidence without treating absence or unknown state as success."""
    evidence = evidence if isinstance(evidence, Mapping) else {}
    checks = risk.get("required_checks", [])
    rows: dict[str, dict[str, str]] = {}
    for check in checks:
        value = evidence.get(check)
        if value is None:
            rows[check] = {"status": "UNAVAILABLE"}
        elif isinstance(value, Mapping) and value.get("state") == "missing":
            rows[check] = {"status": "MISSING"}
        elif isinstance(value, Mapping) and value.get("status") in {"passed", "success"}:
            rows[check] = {"status": "OBSERVED", "detail": "reported passed; advisory only"}
        else:
            rows[check] = {"status": "UNAVAILABLE"}
    overall = "READY_FOR_REVIEW" if rows and all(row["status"] == "OBSERVED" for row in rows.values()) else "INCOMPLETE"
    return {
        "contract_version": CONTRACT_VERSION,
        "mode": _MODE,
        "profile": risk.get("profile", "high"),
        "status": overall,
        "enforcement": "NONE",
        "evidence": rows,
    }


def validate_receipt(receipt: Mapping[str, Any]) -> dict[str, str]:
    """Validate a synthetic read-only evidence fixture, not external evidence."""
    if not isinstance(receipt, Mapping):
        return {"status": "INVALID", "reason": "receipt must be an object"}
    if receipt.get("contract_version") != CONTRACT_VERSION:
        return {"status": "INVALID", "reason": "unsupported contract version"}
    if receipt.get("source") != "synthetic" or receipt.get("read_only") is not True:
        return {"status": "INVALID", "reason": "only synthetic read-only fixtures are accepted"}
    if not isinstance(receipt.get("checks"), Mapping):
        return {"status": "INVALID", "reason": "checks must be an object"}
    return {"status": "VALID", "source": "synthetic"}
