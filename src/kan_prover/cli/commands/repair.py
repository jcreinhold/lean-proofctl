"""Implementation of `kan-prover repair`."""

import argparse
from pathlib import Path

from kan_prover.adapters import create_backend
from kan_prover.engine.config import discover_tool_paths, ensure_tool_layout, resolve_workspace_binding
from kan_prover.engine.loop import LoopOptions, run_loop
from kan_prover.engine.models import LoopResult


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `repair` subcommand."""
    parser = subparsers.add_parser("repair", help="Patch one theorem body in a sandbox and verify it.")
    parser.add_argument("file", help="Lean file to repair.")
    parser.add_argument(
        "--workspace",
        help="Explicit Lake workspace root. When omitted, infer from the target file, then cwd.",
    )
    parser.add_argument("--theorem", required=True, help="Exact theorem or lemma name to patch.")
    parser.add_argument(
        "--agent",
        default="none",
        choices=["none", "claude", "codex"],
        help="Bounded backend used to produce theorem-body candidates.",
    )
    parser.add_argument(
        "--candidate-file",
        action="append",
        default=[],
        help="A file whose contents replace the theorem body for the `none` backend.",
    )
    parser.add_argument(
        "--replay-attempt",
        action="append",
        default=[],
        help="A stored `candidate.json` file from a prior run to recheck deterministically.",
    )
    parser.add_argument(
        "--decomposition-file",
        action="append",
        default=[],
        help="A JSON file that provides a strict helper plan for the `none` backend.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="Timeout for each sandbox verification attempt.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the verified patch back to the working tree after sandbox verification succeeds.",
    )
    parser.add_argument(
        "--max-decomposition-depth",
        type=int,
        default=2,
        help="Maximum helper-decomposition depth. `1` keeps single-level helper planning.",
    )
    parser.set_defaults(handler=run_command)


def run_command(args: argparse.Namespace) -> tuple[LoopResult, int]:
    """Execute one bounded repair attempt."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    target_file = Path(args.file).expanduser().resolve()
    if not target_file.is_file():
        msg = f"Target file does not exist: {target_file}"
        raise FileNotFoundError(msg)
    candidate_files = [Path(path).expanduser().resolve() for path in args.candidate_file]
    replay_attempts = [Path(path).expanduser().resolve() for path in args.replay_attempt]
    if args.agent == "none" and len(candidate_files) + len(replay_attempts) > 1:
        msg = "`kan-prover repair` accepts at most one explicit candidate input."
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
        candidate_files=candidate_files,
        replay_attempts=replay_attempts,
        decomposition_files=[Path(path).expanduser().resolve() for path in args.decomposition_file],
    )
    options = LoopOptions(
        command="repair",
        target_file=target_file,
        theorem=args.theorem,
        timeout_seconds=args.timeout_seconds,
        apply_patch=bool(args.apply),
        max_attempts=1,
        max_decomposition_depth=max_decomposition_depth,
    )
    return run_loop(tool_paths=tool_paths, workspace=workspace, options=options, backend=backend)
