"""Implementation of `proofctl bench`."""

import argparse
from pathlib import Path

from lean_proofctl.engine.bench import run_bench
from lean_proofctl.engine.config import discover_tool_paths, ensure_tool_layout, resolve_workspace_binding
from lean_proofctl.engine.models import BenchResult


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `bench` subcommand."""
    parser = subparsers.add_parser("bench", help="Run a small unattended local benchmark batch.")
    parser.add_argument("spec", help="Benchmark spec in YAML or JSON.")
    parser.add_argument(
        "--workspace",
        help="Explicit Lake workspace root for all targets. When omitted, infer from each target file.",
    )
    parser.add_argument("--output", help="Optional aggregate JSON output path.")
    parser.set_defaults(handler=run_command)


def run_command(args: argparse.Namespace) -> tuple[BenchResult, int]:
    """Execute one benchmark batch."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    spec_path = Path(args.spec).expanduser().resolve()
    if not spec_path.is_file():
        msg = f"Benchmark spec does not exist: {spec_path}"
        raise FileNotFoundError(msg)
    output_path = Path(args.output).expanduser().resolve() if args.output else None
    workspace = resolve_workspace_binding(tool_paths=tool_paths, workspace=args.workspace) if args.workspace else None
    return run_bench(
        tool_paths=tool_paths,
        explicit_workspace=workspace,
        spec_path=spec_path,
        output_path=output_path,
    )
