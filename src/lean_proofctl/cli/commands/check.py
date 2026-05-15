"""Implementation of `proofctl check`."""

import argparse
from datetime import UTC, datetime
from pathlib import Path

from lean_proofctl.engine.config import (
    WorkspaceBinding,
    discover_tool_paths,
    ensure_tool_layout,
    resolve_workspace_binding,
)
from lean_proofctl.engine.lean_runner import run_lean_check
from lean_proofctl.engine.ledger import append_index_row, write_check_artifacts
from lean_proofctl.engine.models import (
    CheckResult,
    CheckStatus,
    ExitCode,
    LeanRunResult,
    RunManifest,
    RunRequest,
    WorkspaceMetadata,
)
from lean_proofctl.engine.run_id import new_run_id


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `check` subcommand."""
    parser = subparsers.add_parser("check", help="Run authoritative Lean verification for one file.")
    parser.add_argument("file", help="Lean file to check.")
    parser.add_argument(
        "--workspace",
        help="Explicit Lake workspace root. When omitted, infer from the target file, then cwd.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="Timeout for the Lean subprocess.",
    )
    parser.set_defaults(handler=run_command)


def run_command(args: argparse.Namespace) -> tuple[CheckResult, int]:
    """Execute the `check` command and persist the artifact bundle."""
    started_at = datetime.now(UTC)
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

    run_id = new_run_id(started_at)
    workspace_metadata = _workspace_metadata(workspace)
    request = RunRequest(
        command="check",
        target_file=_project_relative(target_file, workspace.root),
        cwd=str(workspace.root),
        tool_root=str(tool_paths.root),
        workspace_root=workspace_metadata.root,
        workspace_package=workspace_metadata.package,
        workspace_toolchain=workspace_metadata.toolchain,
        target_module_path=_project_relative(target_file, workspace.root),
        timeout_seconds=args.timeout_seconds,
        json_output=bool(getattr(args, "json", False)),
        jsonl_output=bool(getattr(args, "jsonl", False)),
        started_at=started_at,
    )

    lean_result = run_lean_check(
        workspace_root=workspace.root,
        target_file=target_file,
        timeout_seconds=args.timeout_seconds,
        workspace_root_for_diagnostics=workspace.root,
    )
    finished_at = datetime.now(UTC)

    status, exit_code = _status_and_code(lean_result)
    result = CheckResult(
        run_id=run_id,
        command="check",
        status=status,
        exit_code=exit_code,
        target_file=_project_relative(target_file, workspace.root),
        tool_root=str(tool_paths.root),
        workspace=workspace_metadata,
        artifact_dir=str(tool_paths.artifact_runs / run_id),
        wall_time_seconds=lean_result.wall_time_seconds,
        diagnostic_count=len(lean_result.diagnostics),
        diagnostics=lean_result.diagnostics,
        lean=lean_result,
        started_at=started_at,
        finished_at=finished_at,
    )

    run_dir = tool_paths.artifact_runs / run_id
    manifest = RunManifest(
        run_id=run_id,
        command="check",
        status=status,
        exit_code=exit_code,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    write_check_artifacts(run_dir=run_dir, manifest=manifest, request=request, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, _exit_code_number(exit_code)


def _status_and_code(lean_result: LeanRunResult) -> tuple[CheckStatus, ExitCode]:
    if lean_result.timed_out:
        return CheckStatus.TIMEOUT, ExitCode.TIMEOUT
    if lean_result.returncode == 0:
        return CheckStatus.OK, ExitCode.SUCCESS
    if lean_result.diagnostics:
        return CheckStatus.FAILED, ExitCode.CHECK_FAILED
    return CheckStatus.ERROR, ExitCode.INTERNAL_ERROR


def _exit_code_number(exit_code: ExitCode) -> int:
    if exit_code == ExitCode.SUCCESS:
        return 0
    if exit_code == ExitCode.CHECK_FAILED:
        return 1
    if exit_code == ExitCode.BAD_USAGE:
        return 2
    if exit_code == ExitCode.TIMEOUT:
        return 3
    return 4


def _project_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _workspace_metadata(workspace: WorkspaceBinding) -> WorkspaceMetadata:
    return WorkspaceMetadata(
        root=str(workspace.root),
        package=workspace.package_name,
        toolchain=workspace.toolchain,
    )
