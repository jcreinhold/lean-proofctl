"""Implementation of `kan-prover export`."""

import argparse
from pathlib import Path

from kan_prover.engine.config import discover_tool_paths, ensure_tool_layout, resolve_workspace_binding
from kan_prover.engine.models import ExportResult
from kan_prover.engine.replay import export_run


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `export` subcommand."""
    parser = subparsers.add_parser("export", help="Export a self-contained theorem context bundle from a stored run.")
    parser.add_argument("run_id", help="Stored run id under artifacts/runs/.")
    parser.add_argument(
        "--workspace",
        help="Explicit Lake workspace root used when resolving the stored target file.",
    )
    parser.add_argument(
        "--attempt", type=int, help="Stored attempt index to export. Defaults to the verified or last attempt."
    )
    parser.add_argument("--output", help="Optional JSON output path.")
    parser.set_defaults(handler=run_command)


def run_command(args: argparse.Namespace) -> tuple[ExportResult, int]:
    """Execute one export request."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    output_path = Path(args.output).expanduser().resolve() if args.output else None
    workspace = resolve_workspace_binding(tool_paths=tool_paths, workspace=args.workspace) if args.workspace else None
    return export_run(
        tool_paths=tool_paths,
        workspace_override=workspace,
        source_run_id=args.run_id,
        attempt_index=args.attempt,
        output_path=output_path,
    )
