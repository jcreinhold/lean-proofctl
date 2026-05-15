"""Subprocess wrapper for authoritative Lake and Lean verification."""

import subprocess
import time
from os import environ
from pathlib import Path

from lean_proofctl.engine.diagnostics import normalize_json_diagnostics, timeout_diagnostic
from lean_proofctl.engine.models import LeanInvocation, LeanRunResult
from lean_proofctl.engine.sandbox import HELPER_SEARCH_ROOT


def run_lake_build(workspace_root: Path, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    """Run `lake build` inside one bound Lake workspace."""
    return subprocess.run(
        ["lake", "build"],
        cwd=workspace_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )


def run_lean_make_olean(
    workspace_root: Path, target_file: Path, timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    """Compile one helper source file into an adjacent `.olean`."""
    olean_file = target_file.with_suffix(".olean")
    lean_path_entries = _helper_search_path_entries(workspace_root)
    if environ.get("LEAN_PATH"):
        lean_path_entries.append(environ["LEAN_PATH"])
    env = {**environ, "LEAN_PATH": ":".join(lean_path_entries)}
    return subprocess.run(
        ["lake", "env", "lean", "-o", str(olean_file), str(target_file)],
        cwd=workspace_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
        env=env,
    )


def run_lean_check(
    *,
    workspace_root: Path,
    target_file: Path,
    timeout_seconds: float,
    workspace_root_for_diagnostics: Path,
    use_lake_env: bool = False,
) -> LeanRunResult:
    """Run `lake lean` or `lake env lean` with JSON diagnostics."""
    argv = (
        ["lake", "env", "lean", str(target_file), "--json"]
        if use_lake_env
        else ["lake", "lean", str(target_file), "--", "--json"]
    )
    invocation = LeanInvocation(
        argv=argv,
        cwd=str(workspace_root),
        timeout_seconds=timeout_seconds,
    )
    started = time.perf_counter()
    env = None
    if use_lake_env:
        lean_path_entries = _helper_search_path_entries(workspace_root)
        if environ.get("LEAN_PATH"):
            lean_path_entries.append(environ["LEAN_PATH"])
        env = {**environ, "LEAN_PATH": ":".join(lean_path_entries)}
    try:
        completed = subprocess.run(
            argv,
            cwd=workspace_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        wall_time = time.perf_counter() - started
        file_name = _normalize_target(target_file, workspace_root_for_diagnostics)
        diagnostics = [timeout_diagnostic(file_name, timeout_seconds)]
        stdout = error.stdout if isinstance(error.stdout, str) else ""
        stderr = error.stderr if isinstance(error.stderr, str) else ""
        return LeanRunResult(
            invocation=invocation,
            returncode=None,
            wall_time_seconds=wall_time,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
            diagnostics=diagnostics,
            parse_errors=[],
        )
    wall_time = time.perf_counter() - started
    diagnostics, parse_errors = normalize_json_diagnostics(completed.stdout, workspace_root_for_diagnostics)
    return LeanRunResult(
        invocation=invocation,
        returncode=completed.returncode,
        wall_time_seconds=wall_time,
        stdout=completed.stdout,
        stderr=completed.stderr,
        timed_out=False,
        diagnostics=diagnostics,
        parse_errors=parse_errors,
    )


def _normalize_target(target_file: Path, project_root: Path) -> str:
    resolved = target_file.resolve()
    try:
        return str(resolved.relative_to(project_root))
    except ValueError:
        return str(resolved)


def _helper_search_path_entries(workspace_root: Path) -> list[str]:
    helper_search_root = workspace_root / HELPER_SEARCH_ROOT
    return [str(helper_search_root)] if helper_search_root.is_dir() else []
