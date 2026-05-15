"""Implementation of `kan-prover plan`."""

import argparse
from pathlib import Path

from kan_prover.adapters import create_backend
from kan_prover.engine.config import discover_tool_paths, ensure_tool_layout, resolve_workspace_binding
from kan_prover.engine.inspection import run_plan
from kan_prover.engine.models import PlanResult


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `plan` subcommand."""
    parser = subparsers.add_parser("plan", help="Produce a strict structured theorem plan.")
    parser.add_argument("file", help="Lean file to plan against.")
    parser.add_argument(
        "--workspace",
        help="Explicit Lake workspace root. When omitted, infer from the target file, then cwd.",
    )
    parser.add_argument("--theorem", required=True, help="Exact theorem or lemma name to plan.")
    parser.add_argument(
        "--agent",
        default="none",
        choices=["none", "claude", "codex"],
        help="Bounded backend used to produce theorem plans.",
    )
    parser.add_argument(
        "--plan-file",
        action="append",
        default=[],
        help="A JSON file that provides the strict theorem plan for the `none` backend.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="Timeout for semantic probes and the sandboxed baseline check.",
    )
    parser.set_defaults(handler=run_command)


def run_command(args: argparse.Namespace) -> tuple[PlanResult, int]:
    """Execute one bounded theorem planning call."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    target_file = Path(args.file).expanduser().resolve()
    if not target_file.is_file():
        msg = f"Target file does not exist: {target_file}"
        raise FileNotFoundError(msg)
    workspace = resolve_workspace_binding(
        tool_paths=tool_paths,
        workspace=getattr(args, "workspace", None),
        target_file=target_file,
    )
    backend = create_backend(
        agent=args.agent,
        plan_files=[Path(path).expanduser().resolve() for path in args.plan_file],
    )
    return run_plan(
        tool_paths=tool_paths,
        workspace=workspace,
        target_file=target_file,
        theorem=args.theorem,
        timeout_seconds=args.timeout_seconds,
        backend=backend,
    )
