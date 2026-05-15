"""Implementation of `proofctl prove`."""

import argparse
from pathlib import Path

from lean_proofctl.adapters import create_backend
from lean_proofctl.engine.config import discover_tool_paths, ensure_tool_layout, resolve_workspace_binding
from lean_proofctl.engine.models import ProveResult
from lean_proofctl.engine.prove import ProveOptions, run_prove


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `prove` subcommand."""
    parser = subparsers.add_parser("prove", help="Run a graph-backed prove loop over theorem sessions.")
    parser.add_argument("file", help="Lean file to prove against.")
    parser.add_argument(
        "--workspace",
        help="Explicit Lake workspace root. When omitted, infer from the target file, then cwd.",
    )
    parser.add_argument("--theorem", required=True, help="Exact theorem or lemma name to prove.")
    parser.add_argument(
        "--agent",
        default="none",
        choices=["none", "claude", "codex"],
        help="Bounded backend used to plan and expand the proof graph.",
    )
    parser.add_argument(
        "--candidate-file",
        action="append",
        default=[],
        help="Whole-body candidate files for the `none` backend.",
    )
    parser.add_argument(
        "--replay-attempt",
        action="append",
        default=[],
        help="Stored `candidate.json` files from prior runs for the `none` backend.",
    )
    parser.add_argument(
        "--plan-file",
        action="append",
        default=[],
        help="A JSON file that provides the strict theorem plan for the `none` backend.",
    )
    parser.add_argument(
        "--theorem-step-file",
        action="append",
        default=[],
        help="A JSON file that provides theorem-step actions for the `none` backend.",
    )
    parser.add_argument(
        "--decomposition-file",
        action="append",
        default=[],
        help="A JSON file that provides helper-decomposition steps for the `none` backend.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="Timeout for each planner, session, and verification call.",
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        default=8,
        help="Maximum proof-graph nodes to materialize.",
    )
    parser.add_argument(
        "--max-decomposition-depth",
        type=int,
        default=2,
        help="Maximum helper-decomposition depth. `1` keeps single-level helper planning.",
    )
    parser.set_defaults(handler=run_command)


def run_command(args: argparse.Namespace) -> tuple[ProveResult, int]:
    """Execute one graph-backed prove loop."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    target_file = Path(args.file).expanduser().resolve()
    if not target_file.is_file():
        msg = f"Target file does not exist: {target_file}"
        raise FileNotFoundError(msg)
    if args.max_nodes < 2:
        msg = "`--max-nodes` must be at least 2."
        raise ValueError(msg)
    max_decomposition_depth = int(getattr(args, "max_decomposition_depth", 2))
    if max_decomposition_depth < 1:
        msg = "`--max-decomposition-depth` must be at least 1."
        raise ValueError(msg)
    workspace = resolve_workspace_binding(
        tool_paths=tool_paths,
        workspace=getattr(args, "workspace", None),
        target_file=target_file,
    )
    backend = create_backend(
        agent=args.agent,
        candidate_files=[Path(path).expanduser().resolve() for path in args.candidate_file],
        replay_attempts=[Path(path).expanduser().resolve() for path in args.replay_attempt],
        plan_files=[Path(path).expanduser().resolve() for path in args.plan_file],
        theorem_step_files=[Path(path).expanduser().resolve() for path in args.theorem_step_file],
        decomposition_files=[Path(path).expanduser().resolve() for path in getattr(args, "decomposition_file", [])],
    )
    return run_prove(
        tool_paths=tool_paths,
        workspace=workspace,
        options=ProveOptions(
            target_file=target_file,
            theorem=args.theorem,
            timeout_seconds=args.timeout_seconds,
            max_nodes=int(args.max_nodes),
            max_decomposition_depth=max_decomposition_depth,
        ),
        backend=backend,
    )
