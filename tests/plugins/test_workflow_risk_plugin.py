"""Behavior contract for the advisory workflow risk plugin."""

from plugins.workflow_risk import policy


def test_unknown_and_sensitive_paths_fail_closed_even_with_low_request():
    result = policy.evaluate(
        paths=["src/unknown/component.py", "migrations/004_add_index.sql"],
        requested_profile="low",
    )

    assert result["profile"] == "high"
    assert result["required_checks"]
    assert result["mode"] == "ADVISORY"


def test_plugin_registers_advisory_workflow_cli_through_public_context():
    from plugins.workflow_risk import register

    class Context:
        def __init__(self):
            self.commands = []

        def register_cli_command(self, **kwargs):
            self.commands.append(kwargs)

    ctx = Context()
    register(ctx)

    assert ctx.commands[0]["name"] == "workflow"
    assert ctx.commands[0]["setup_fn"]
    assert "Advisory" in ctx.commands[0]["help"]


def test_empty_paths_and_unknown_evidence_are_not_reported_as_pass():
    risk = policy.evaluate(paths=[], requested_profile=None)
    report = policy.doctor(
        risk,
        evidence={"design_contract": None, "vivi_review": {"state": "missing"}},
    )

    assert risk["profile"] == "high"
    assert report["evidence"]["design_contract"]["status"] == "UNAVAILABLE"
    assert report["evidence"]["vivi_review"]["status"] == "MISSING"
    assert report["status"] != "PASS"


def test_low_normal_high_profiles_include_required_check_labels():
    low = policy.evaluate(paths=["docs/design.md"], requested_profile="low")
    normal = policy.evaluate(paths=["gateway/adapter.py"], requested_profile="normal")
    high = policy.evaluate(paths=["src/auth/session.py"], requested_profile="low")

    assert low["profile"] == "low"
    assert "lint" in low["required_checks"]
    assert normal["profile"] == "normal"
    assert "contract_tests" in normal["required_checks"]
    assert high["profile"] == "high"
    assert "migration_release_gates" in high["required_checks"]


def test_receipt_validator_rejects_non_synthetic_or_mutable_evidence():
    validated = policy.validate_receipt(
        {
            "contract_version": "1.0.0",
            "source": "github",
            "read_only": False,
            "checks": {},
        }
    )

    assert validated["status"] == "INVALID"


def test_receipt_validator_accepts_only_synthetic_read_only_receipts():
    receipt = {
        "contract_version": "1.0.0",
        "source": "synthetic",
        "read_only": True,
        "checks": {"changed_package_tests": "passed"},
    }

    validated = policy.validate_receipt(receipt)

    assert validated["status"] == "VALID"
    assert validated["source"] == "synthetic"
