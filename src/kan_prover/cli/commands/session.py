"""Implementation of `kan-prover session ...`."""

import argparse
from pathlib import Path

from kan_prover.engine.config import discover_tool_paths, ensure_tool_layout, resolve_workspace_binding
from kan_prover.engine.models import SessionActionResult
from kan_prover.engine.session import checkpoint_session, finish_session, goals_session, start_session, step_session


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `session` command group."""
    parser = subparsers.add_parser("session", help="Run bounded theorem-session actions in a sandbox.")
    session_subparsers = parser.add_subparsers(dest="session_action", required=True)

    start = session_subparsers.add_parser("start", help="Create one theorem session from a real file and theorem.")
    start.add_argument("file", help="Lean file to open inside the session sandbox.")
    start.add_argument(
        "--workspace",
        help="Explicit Lake workspace root. When omitted, infer from the target file, then cwd.",
    )
    start.add_argument("--theorem", required=True, help="Exact theorem or lemma name to open.")
    start.add_argument("--source-run-id", help="Optional run id that spawned this session.")
    start.add_argument("--timeout-seconds", type=float, default=60.0, help="Timeout for Lean checks and probes.")
    start.set_defaults(handler=run_start)

    step = session_subparsers.add_parser("step", help="Replace the current theorem body inside the session sandbox.")
    step.add_argument("session_id", help="Stored theorem session id under artifacts/sessions/.")
    step.add_argument("--body-file", required=True, help="Path to the replacement theorem body text.")
    step.add_argument("--timeout-seconds", type=float, default=60.0, help="Timeout for Lean checks and probes.")
    step.set_defaults(handler=run_step)

    goals = session_subparsers.add_parser("goals", help="Refresh and print the current session proof state.")
    goals.add_argument("session_id", help="Stored theorem session id under artifacts/sessions/.")
    goals.add_argument("--timeout-seconds", type=float, default=60.0, help="Timeout for Lean checks and probes.")
    goals.set_defaults(handler=run_goals)

    checkpoint = session_subparsers.add_parser("checkpoint", help="Record one named checkpoint in the session.")
    checkpoint.add_argument("session_id", help="Stored theorem session id under artifacts/sessions/.")
    checkpoint.add_argument("--label", help="Optional checkpoint label.")
    checkpoint.set_defaults(handler=run_checkpoint)

    finish = session_subparsers.add_parser("finish", help="Finalize the session after one last file-level recheck.")
    finish.add_argument("session_id", help="Stored theorem session id under artifacts/sessions/.")
    finish.add_argument("--timeout-seconds", type=float, default=60.0, help="Timeout for Lean checks and probes.")
    finish.set_defaults(handler=run_finish)


def run_start(args: argparse.Namespace) -> tuple[SessionActionResult, int]:
    """Execute `kan-prover session start`."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    target_file = Path(args.file).expanduser().resolve()
    if not target_file.is_file():
        msg = f"Target file does not exist: {target_file}"
        raise FileNotFoundError(msg)
    workspace = resolve_workspace_binding(
        tool_paths=tool_paths,
        workspace=getattr(args, "workspace", None),
        target_file=target_file,
    )
    return start_session(
        tool_paths=tool_paths,
        workspace=workspace,
        target_file=target_file,
        theorem=args.theorem,
        timeout_seconds=args.timeout_seconds,
        source_run_id=args.source_run_id,
    )


def run_step(args: argparse.Namespace) -> tuple[SessionActionResult, int]:
    """Execute `kan-prover session step`."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    body = Path(args.body_file).expanduser().resolve().read_text(encoding="utf-8")
    return step_session(
        tool_paths=tool_paths,
        session_id=args.session_id,
        body=body,
        timeout_seconds=args.timeout_seconds,
    )


def run_goals(args: argparse.Namespace) -> tuple[SessionActionResult, int]:
    """Execute `kan-prover session goals`."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    return goals_session(
        tool_paths=tool_paths,
        session_id=args.session_id,
        timeout_seconds=args.timeout_seconds,
    )


def run_checkpoint(args: argparse.Namespace) -> tuple[SessionActionResult, int]:
    """Execute `kan-prover session checkpoint`."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    return checkpoint_session(
        tool_paths=tool_paths,
        session_id=args.session_id,
        label=args.label,
    )


def run_finish(args: argparse.Namespace) -> tuple[SessionActionResult, int]:
    """Execute `kan-prover session finish`."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    return finish_session(
        tool_paths=tool_paths,
        session_id=args.session_id,
        timeout_seconds=args.timeout_seconds,
    )
