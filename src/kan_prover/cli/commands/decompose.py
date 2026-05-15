"""Implementation of `kan-prover decompose`."""

import argparse
from pathlib import Path

from kan_prover.adapters import create_backend
from kan_prover.engine.config import discover_tool_paths, ensure_tool_layout, resolve_workspace_binding
from kan_prover.engine.loop import run_decompose
from kan_prover.engine.models import DecomposeResult


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `decompose` subcommand."""
    parser = subparsers.add_parser("decompose", help="Produce a bounded helper plan for one theorem.")
    parser.add_argument("file", help="Lean file to decompose.")
    parser.add_argument(
        "--workspace",
        help="Explicit Lake workspace root. When omitted, infer from the target file, then cwd.",
    )
    parser.add_argument("--theorem", required=True, help="Exact theorem or lemma name to inspect.")
    parser.add_argument(
        "--agent",
        default="none",
        choices=["none", "claude", "codex"],
        help="Bounded backend used to produce helper plans.",
    )
    parser.add_argument(
        "--decomposition-file",
        action="append",
        default=[],
        help="A JSON file that provides the strict helper plan for the `none` backend.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="Timeout for the baseline Lean check.",
    )
    parser.add_argument(
        "--max-decomposition-depth",
        type=int,
        default=2,
        help="Maximum helper-decomposition depth to record for this run.",
    )
    parser.set_defaults(handler=run_command)


def run_command(args: argparse.Namespace) -> tuple[DecomposeResult, int]:
    """Execute one bounded decomposition call."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    target_file = Path(args.file).expanduser().resolve()
    if not target_file.is_file():
        msg = f"Target file does not exist: {target_file}"
        raise FileNotFoundError(msg)
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
        decomposition_files=[Path(path).expanduser().resolve() for path in args.decomposition_file],
    )
    return run_decompose(
        tool_paths=tool_paths,
        workspace=workspace,
        target_file=target_file,
        theorem=args.theorem,
        timeout_seconds=args.timeout_seconds,
        max_decomposition_depth=max_decomposition_depth,
        backend=backend,
    )
