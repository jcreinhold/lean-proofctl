"""Implementation of `proofctl apply`."""

import argparse

from lean_proofctl.engine.config import discover_tool_paths, ensure_tool_layout, resolve_workspace_binding
from lean_proofctl.engine.graph_workflow import GraphNodeSelector, apply_graph_run
from lean_proofctl.engine.models import ApplyResult


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `apply` subcommand."""
    parser = subparsers.add_parser("apply", help="Transplant one verified theorem branch from a stored proof graph.")
    parser.add_argument("run_id", help="Stored prove run id under artifacts/runs/.")
    parser.add_argument(
        "--workspace",
        help="Explicit Lake workspace root used when resolving the stored target file.",
    )
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--node-id", help="Apply one verified theorem-state node id.")
    selector.add_argument("--graph-index", type=int, help="Apply one verified theorem-state graph index.")
    parser.set_defaults(handler=run_command)


def run_command(args: argparse.Namespace) -> tuple[ApplyResult, int]:
    """Execute one graph apply request."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    workspace = resolve_workspace_binding(tool_paths=tool_paths, workspace=args.workspace) if args.workspace else None
    return apply_graph_run(
        tool_paths=tool_paths,
        workspace_override=workspace,
        source_run_id=args.run_id,
        selector=GraphNodeSelector(node_id=args.node_id, graph_index=args.graph_index),
    )
