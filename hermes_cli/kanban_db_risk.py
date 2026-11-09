"""Kanban task risk-profile contract and evidence policy."""

from typing import Optional

RISK_CHECKS = frozenset({
    "changed_package_tests", "lint", "contract_tests", "synthetic_merge_check",
    "design_contract", "real_postgres", "migration_release_gates", "vivi_review",
})
RISK_PROFILES = {
    "low": ("changed_package_tests", "lint", "vivi_review"),
    "normal": ("changed_package_tests", "contract_tests", "synthetic_merge_check", "vivi_review"),
    "high": ("design_contract", "migration_release_gates", "vivi_review"),
}
_SENSITIVE = (
    "migration", "auth", "money", "billing", "payment", "currency", "rbac", "deploy",
    "flux", "zitadel",
)
_LEVEL = {"low": 0, "normal": 1, "high": 2}


def resolve_risk_policy(
    title: str, body: Optional[str], requested: Optional[str], supplied: Optional[list[str]],
) -> tuple[str, list[str]]:
    """Resolve profiles/checks; path evidence can only raise risk."""
    if requested not in (None, *_LEVEL):
        raise ValueError("risk_profile must be one of low, normal, high")
    text = f"{title} {body or ''}".casefold()
    detected = "high" if any(term in text for term in _SENSITIVE) else None
    profile = requested or "high"
    if detected and _LEVEL[detected] > _LEVEL[profile]:
        profile = detected
    checks = list(supplied) if supplied is not None else list(RISK_PROFILES[profile])
    if any(check not in RISK_CHECKS for check in checks):
        raise ValueError("risk_checks contains an unknown check")
    required = list(RISK_PROFILES[profile])
    if profile == "high" and "postgres" in text:
        required.insert(-1, "real_postgres")
    checks.extend(check for check in required if check not in checks)
    return profile, checks
