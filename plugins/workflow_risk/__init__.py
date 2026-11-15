"""Advisory workflow risk plugin entry point."""

from . import policy


def _configure_cli(parser):
    parser.add_argument("--paths", nargs="*", default=[])
    parser.add_argument("--profile", choices=("low", "normal", "high"))
    parser.add_argument("--doctor", action="store_true")
    parser.set_defaults(func=_run_cli)


def _run_cli(args):
    result = policy.evaluate(args.paths, args.profile)
    if args.doctor:
        result = policy.doctor(result, evidence={})
    import json

    print(json.dumps(result, sort_keys=True, indent=2))


def register(ctx):
    """Register only additive CLI access; no board mutation hooks are used."""
    ctx.register_cli_command(
        name="workflow",
        help="Advisory risk profile and evidence doctor",
        setup_fn=_configure_cli,
        description="Advisory workflow risk profile and evidence doctor",
    )
