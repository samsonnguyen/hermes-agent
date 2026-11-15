# Workflow risk plugin contract v1

This package exposes a **versioned advisory contract**, independent of the Hermes plugin
manifest/API versions. `contract.json` is the machine-readable export; `policy.py` implements it.

## Risk read-model

`evaluate(paths, requested_profile)` returns `contract_version`, `mode`, `profile`,
`requested_profile`, `inferred_profile`, `paths`, `required_checks`, and `enforcement`.
Profiles are ordered `low < normal < high`. A known sensitive path or any unknown/ambiguous path
classifies as `high`; mixed paths choose the highest class. An empty path set is unknown and high.
A user request can raise risk, never lower it. In particular, a requested low profile cannot
remove migration, auth, money, RBAC, or deployment checks.

The profile check lists are contract data: low = changed-package tests, lint, Vivi review;
normal adds contract tests and synthetic merge check; high requires design/contract, relevant real
Postgres, migration/release gates, and Vivi review.

## Doctor/evidence fields

`doctor(risk, evidence)` returns `contract_version`, `mode`, `profile`, `status`, `enforcement`,
and per-required-check `evidence`. Each evidence status is `OBSERVED`, `MISSING`, or `UNAVAILABLE`.
Absent/unrecognized values are `UNAVAILABLE`, not pass or zero. `READY_FOR_REVIEW` means only that
every required evidence item was reported observed; it is not approval, enforcement, or permission
to transition a native Kanban card.

`validate_receipt(receipt)` accepts only the synthetic fixture shape: version 1.0.0, source
`synthetic`, `read_only: true`, and a `checks` object. It performs no writes and treats the fixture
as schema validation, not proof that any check actually ran.

## Hermes compatibility and limits

The native package uses `plugin.yaml` plus `register(ctx)` and only the public additive
`register_cli_command` interface. Its declared compatibility floor is Hermes v0.23.0. Validate a
checkout with `hermes plugins validate <package>` and `hermes plugins doctor <package> --ci`;
`hermes plugins capabilities` is an independent host capability readout, not plugin behavior proof.

**ADVISORY, NON-ENFORCING:** this plugin neither observes nor intercepts Kanban mutation paths.
It does not enforce risk policy, veto transitions, or close the native-core transactional
policy gap. Plugin installation, enablement, deployment, live board reads/writes, and runtime
experiments are outside this source package.

## Downstream contract for W5 and later consumers

Consume contract version `1.0.0` and these fields only:

- Risk: `contract_version`, `mode`, `profile`, `requested_profile`, `inferred_profile`, `paths`,
  `required_checks`, `enforcement`.
- Doctor: `contract_version`, `mode`, `profile`, `status`, `enforcement`, `evidence`.
- Evidence row: `status` (`OBSERVED | MISSING | UNAVAILABLE`) and optional `detail`.
- Synthetic receipt: `contract_version`, `source`, `read_only`, `checks`.

Consumers must preserve `UNAVAILABLE` and `MISSING` distinctly, must not infer a native transition
fence from `READY_FOR_REVIEW`, and must reject unsupported contract versions rather than silently
reinterpret fields. Native transactional enforcement remains an explicit unresolved upstream gap.
