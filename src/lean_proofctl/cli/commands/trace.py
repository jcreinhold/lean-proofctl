"""Implementation of `proofctl trace`."""

import argparse

from lean_proofctl.engine.config import discover_tool_paths, ensure_tool_layout
from lean_proofctl.engine.models import TraceResult
from lean_proofctl.engine.replay import trace_run


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `trace` subcommand."""
    parser = subparsers.add_parser("trace", help="Summarize one stored run bundle from artifacts only.")
    parser.add_argument("run_id", help="Stored run id under artifacts/runs/.")
    parser.set_defaults(handler=run_command)


def run_command(args: argparse.Namespace) -> tuple[TraceResult, int]:
    """Execute one artifact-only trace."""
    paths = discover_tool_paths()
    ensure_tool_layout(paths)
    return trace_run(tool_paths=paths, source_run_id=args.run_id)
