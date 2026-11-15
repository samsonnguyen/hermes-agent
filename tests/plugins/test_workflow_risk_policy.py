"""Behavioral contract for the advisory workflow risk plugin."""

from plugins.workflow_risk.policy import evaluate


def test_sensitive_filename_stems_are_high_anywhere_and_require_high_checks():
    high_checks = evaluate(["plugins/workflow_risk/policy.py"], "high")["required_checks"]
    for path in (
        "gateway/auth.py",
        "plugins/auth.py",
        "plugins/migrations.py",
        "plugins/rbac.py",
        "docs/auth.py",
    ):
        result = evaluate([path], requested_profile="low")
        assert result["profile"] == "high"
        assert result["required_checks"] == high_checks


def test_omitted_profile_preserves_inferred_low_and_normal_profiles():
    assert evaluate(["docs/design.md"])["profile"] == "low"
    assert evaluate(["gateway/adapter.py"])["profile"] == "normal"


def test_invalid_requested_profile_fails_closed_to_high():
    assert evaluate(["docs/design.md"], "unknown")["profile"] == "high"
