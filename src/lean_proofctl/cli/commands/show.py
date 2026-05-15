"""Implementation of `proofctl show ...`."""

import argparse

from lean_proofctl.engine.config import discover_tool_paths, ensure_tool_layout
from lean_proofctl.engine.models import ShowRunResult, ShowSessionResult
from lean_proofctl.engine.show import show_run, show_session


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `show` command group."""
    parser = subparsers.add_parser(
        "show",
        help="Inspect stored run and session artifacts without opening raw JSON.",
    )
    show_subparsers = parser.add_subparsers(dest="show_target", required=True)

    run = show_subparsers.add_parser("run", help="Render one stored run bundle.")
    run.add_argument("run_id", help="Stored run id under artifacts/runs/.")
    run.add_argument(
        "--attempt",
        type=int,
        help="Optional attempt index or graph-node position. Defaults to the verified or current stored view.",
    )
    run.set_defaults(handler=run_show_run)

    session = show_subparsers.add_parser("session", help="Render one stored theorem session.")
    session.add_argument("session_id", help="Stored theorem session id under artifacts/sessions/.")
    node_group = session.add_mutually_exclusive_group()
    node_group.add_argument("--node-id", help="Exact stored session node id to render.")
    node_group.add_argument("--node-index", type=int, help="1-based stored session node position to render.")
    session.set_defaults(handler=run_show_session)


def run_show_run(args: argparse.Namespace) -> tuple[ShowRunResult, int]:
    """Execute `proofctl show run`."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    return show_run(tool_paths=tool_paths, source_run_id=args.run_id, attempt_index=args.attempt)


def run_show_session(args: argparse.Namespace) -> tuple[ShowSessionResult, int]:
    """Execute `proofctl show session`."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    return show_session(
        tool_paths=tool_paths,
        source_session_id=args.session_id,
        node_id=args.node_id,
        node_index=args.node_index,
    )
