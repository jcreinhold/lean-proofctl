"""Implementation of `kan-prover doctor`."""

import argparse
from pathlib import Path

from kan_prover.engine.config import discover_tool_paths, ensure_tool_layout
from kan_prover.engine.doctor import run_doctor
from kan_prover.engine.models import DoctorResult


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `doctor` subcommand."""
    parser = subparsers.add_parser("doctor", help="Report whether one Lake workspace is ready for kan-prover.")
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--workspace",
        help="Target Lake workspace root. When omitted, inspect the current working directory.",
    )
    target_group.add_argument(
        "--demo",
        action="store_true",
        help="Inspect the bundled demo workspace used for self-tests and examples.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=20.0,
        help="Timeout for readiness subprocesses such as `lake env lean --version`.",
    )
    parser.add_argument(
        "--agent",
        action="append",
        choices=["none", "claude", "codex"],
        default=[],
        help="Backend to require during doctor checks. Repeat to require more than one backend.",
    )
    parser.set_defaults(handler=run_command)


def run_command(args: argparse.Namespace) -> tuple[DoctorResult, int]:
    """Execute the `doctor` command."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    return run_doctor(
        tool_paths=tool_paths,
        workspace=getattr(args, "workspace", None),
        demo=bool(getattr(args, "demo", False)),
        timeout_seconds=args.timeout_seconds,
        required_backends=list(getattr(args, "agent", [])),
        cwd=Path.cwd(),
    )
