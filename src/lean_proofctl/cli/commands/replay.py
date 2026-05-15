"""Implementation of `proofctl replay`."""

import argparse

from lean_proofctl.engine.config import discover_tool_paths, ensure_tool_layout, resolve_workspace_binding
from lean_proofctl.engine.models import ReplayMode, ReplayResult
from lean_proofctl.engine.replay import replay_run


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `replay` subcommand."""
    parser = subparsers.add_parser("replay", help="Replay one stored run with an explicit mode.")
    parser.add_argument("run_id", help="Stored run id under artifacts/runs/.")
    parser.add_argument(
        "--workspace",
        help="Explicit Lake workspace root for replayed verification. When omitted, infer from the stored target file.",
    )
    parser.add_argument(
        "--attempt", type=int, help="Stored attempt index to replay. Defaults to the verified or last attempt."
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--recheck-only", action="store_true", help="Re-apply the stored patch and rerun Lean only."
    )
    mode_group.add_argument(
        "--reuse-agent-output",
        action="store_true",
        help="Reuse stored backend outputs. This may drift if the environment changed.",
    )
    mode_group.add_argument(
        "--requery-agent",
        action="store_true",
        help="Ask the recorded backend again. This is intentionally nondeterministic.",
    )
    parser.set_defaults(handler=run_command)


def run_command(args: argparse.Namespace) -> tuple[ReplayResult, int]:
    """Execute one replay request."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    mode = ReplayMode.RECHECK_ONLY
    if args.reuse_agent_output:
        mode = ReplayMode.REUSE_AGENT_OUTPUT
    if args.requery_agent:
        mode = ReplayMode.REQUERY_AGENT
    workspace = resolve_workspace_binding(tool_paths=tool_paths, workspace=args.workspace) if args.workspace else None
    return replay_run(
        tool_paths=tool_paths,
        workspace_override=workspace,
        source_run_id=args.run_id,
        mode=mode,
        attempt_index=args.attempt,
    )
