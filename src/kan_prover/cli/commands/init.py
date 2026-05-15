"""Implementation of `kan-prover init`."""

import argparse
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from kan_prover.engine.config import (
    bind_workspace,
    discover_tool_paths,
    ensure_tool_layout,
    resolve_workspace_binding,
)
from kan_prover.engine.lean_runner import run_lake_build
from kan_prover.engine.models import CheckStatus, ExitCode, InitResult
from kan_prover.engine.semantic_probe_runtime import build_semantic_probe_executable


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `init` subcommand."""
    parser = subparsers.add_parser(
        "init",
        help="Bootstrap one target workspace or the bundled demo workspace for kan-prover.",
    )
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--workspace",
        help="Target Lake workspace root. When omitted, infer from the current working directory.",
    )
    target_group.add_argument(
        "--demo",
        action="store_true",
        help="Bootstrap the bundled demo workspace used for self-tests and examples.",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Only validate the selected workspace without running `lake build`.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=300.0,
        help="Timeout for `lake build` when init bootstraps the selected workspace.",
    )
    parser.set_defaults(handler=run_command)


def run_command(args: argparse.Namespace) -> tuple[InitResult, int]:
    """Execute the `init` command."""
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    paths = discover_tool_paths()
    ensure_tool_layout(paths)
    if getattr(args, "demo", False):
        workspace = bind_workspace(paths.bundled_demo_runtime)
        workspace_role = "bundled-demo"
        selected_via = "bundled-demo"
    else:
        workspace = resolve_workspace_binding(
            tool_paths=paths,
            workspace=getattr(args, "workspace", None),
            cwd=Path.cwd(),
        )
        workspace_role = (
            "bundled-demo" if workspace.root.resolve() == paths.bundled_demo_runtime.resolve() else "external-workspace"
        )
        selected_via = "explicit-workspace" if getattr(args, "workspace", None) else "cwd-inference"
    build_stdout = ""
    build_stderr = ""
    status = CheckStatus.OK
    exit_code = 0
    build_ran = not args.skip_build
    if build_ran:
        try:
            probe_completed = build_semantic_probe_executable(tool_paths=paths, timeout_seconds=args.timeout_seconds)
        except subprocess.TimeoutExpired:
            status = CheckStatus.TIMEOUT
            exit_code = 3
        else:
            build_stdout = _format_build_output(
                "semantic-probe",
                probe_completed.stdout,
            )
            build_stderr = _format_build_output(
                "semantic-probe",
                probe_completed.stderr,
            )
            if probe_completed.returncode != 0:
                status = CheckStatus.ERROR
                exit_code = 4
            else:
                try:
                    completed = run_lake_build(workspace.root, timeout_seconds=args.timeout_seconds)
                except subprocess.TimeoutExpired:
                    status = CheckStatus.TIMEOUT
                    exit_code = 3
                else:
                    build_stdout = _join_build_output(
                        build_stdout,
                        _format_build_output("workspace", completed.stdout),
                    )
                    build_stderr = _join_build_output(
                        build_stderr,
                        _format_build_output("workspace", completed.stderr),
                    )
                    if completed.returncode != 0:
                        status = CheckStatus.ERROR
                        exit_code = 4
    finished_at = datetime.now(UTC)
    result = InitResult(
        command="init",
        status=status,
        exit_code=_exit_code_name(exit_code),
        tool_root=str(paths.root),
        workspace_root=str(workspace.root),
        workspace_package=workspace.package_name,
        workspace_toolchain=workspace.toolchain,
        workspace_role=workspace_role,
        selected_via=selected_via,
        artifacts_root=str(paths.artifacts),
        build_ran=build_ran,
        started_at=started_at,
        finished_at=finished_at,
        wall_time_seconds=time.perf_counter() - started,
        build_stdout=build_stdout,
        build_stderr=build_stderr,
    )
    return result, exit_code


def _exit_code_name(code: int) -> ExitCode:
    if code == 0:
        return ExitCode.SUCCESS
    if code == 2:
        return ExitCode.BAD_USAGE
    if code == 3:
        return ExitCode.TIMEOUT
    return ExitCode.INTERNAL_ERROR


def _format_build_output(label: str, output: str) -> str:
    if not output:
        return ""
    return f"[{label}]\n{output.strip()}"


def _join_build_output(existing: str, extra: str) -> str:
    if not existing:
        return extra
    if not extra:
        return existing
    return f"{existing}\n\n{extra}"
