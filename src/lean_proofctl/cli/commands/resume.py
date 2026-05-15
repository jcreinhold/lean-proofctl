"""Implementation of `proofctl resume`."""

import argparse
from pathlib import Path
from typing import cast

from lean_proofctl.adapters import create_backend
from lean_proofctl.engine.config import discover_tool_paths, ensure_tool_layout, resolve_workspace_binding
from lean_proofctl.engine.graph_workflow import GraphNodeSelector, resume_graph_run
from lean_proofctl.engine.models import JsonValue, ResumeResult
from lean_proofctl.engine.replay import load_run_bundle


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `resume` subcommand."""
    parser = subparsers.add_parser("resume", help="Continue search from a stored proof graph.")
    parser.add_argument("run_id", help="Stored prove run id under artifacts/runs/.")
    parser.add_argument(
        "--workspace",
        help="Explicit Lake workspace root used when resolving the stored target file.",
    )
    parser.add_argument(
        "--agent",
        choices=["none", "claude", "codex"],
        help="Bounded backend used to continue the stored search. Defaults to the stored backend.",
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
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--node-id", help="Resume from one stored theorem-state node id.")
    selector.add_argument("--graph-index", type=int, help="Resume from one stored theorem-state graph index.")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="Timeout for each resumed planner, session, and verification call.",
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        help="Maximum total node count for the resumed graph. Defaults to stored nodes plus 8.",
    )
    parser.set_defaults(handler=run_command)


def run_command(args: argparse.Namespace) -> tuple[ResumeResult, int]:
    """Execute one graph resume request."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    workspace = resolve_workspace_binding(tool_paths=tool_paths, workspace=args.workspace) if args.workspace else None
    bundle = load_run_bundle(tool_paths=tool_paths, run_id=args.run_id)
    stored_backend = str(bundle.request.get("backend") or bundle.result.get("backend") or "none")
    backend = create_backend(
        agent=args.agent or stored_backend,
        candidate_files=[Path(path).expanduser().resolve() for path in args.candidate_file],
        replay_attempts=[Path(path).expanduser().resolve() for path in args.replay_attempt],
        plan_files=[Path(path).expanduser().resolve() for path in args.plan_file],
        theorem_step_files=[Path(path).expanduser().resolve() for path in args.theorem_step_file],
    )
    graph_payload = cast(dict[str, JsonValue], bundle.result.get("graph") or {})
    node_payload = cast(list[JsonValue], bundle.result.get("nodes") or [])
    stored_node_count = graph_payload.get("node_count")
    stored_count = stored_node_count if isinstance(stored_node_count, int) else len(node_payload)
    max_nodes = int(args.max_nodes) if args.max_nodes is not None else stored_count + 8
    if max_nodes < 2:
        msg = "`--max-nodes` must be at least 2."
        raise ValueError(msg)
    return resume_graph_run(
        tool_paths=tool_paths,
        workspace_override=workspace,
        source_run_id=args.run_id,
        selector=GraphNodeSelector(node_id=args.node_id, graph_index=args.graph_index),
        backend=backend,
        max_nodes=max_nodes,
        timeout_seconds=args.timeout_seconds,
    )
