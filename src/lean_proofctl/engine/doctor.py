"""Workspace readiness checks for portable `proofctl` usage."""

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lean_proofctl.engine.config import ToolPaths, bind_workspace, find_workspace_root
from lean_proofctl.engine.models import (
    DoctorCheck,
    DoctorCheckStatus,
    DoctorResult,
    DoctorStatus,
    ExitCode,
)
from lean_proofctl.engine.semantic_probe_runtime import (
    semantic_probe_build_hint,
    semantic_probe_runtime,
)


@dataclass(frozen=True)
class _WorkspaceProbe:
    requested_path: Path
    inspected_root: Path
    workspace_root: Path | None
    selected_via: str
    workspace_role: str


@dataclass(frozen=True)
class _WorkspaceFacts:
    lakefile: Path
    lean_toolchain: Path
    build_dir: Path
    packages_dir: Path
    exists: bool
    writable: bool
    package_name: str | None
    toolchain: str | None


def run_doctor(
    *,
    tool_paths: ToolPaths,
    workspace: str | None,
    demo: bool,
    timeout_seconds: float,
    required_backends: list[str] | None = None,
    cwd: Path | None = None,
) -> tuple[DoctorResult, int]:
    """Inspect one target workspace and report portable readiness."""
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    probe = _select_workspace(tool_paths=tool_paths, workspace=workspace, demo=demo, cwd=cwd)
    facts = _workspace_facts(probe.inspected_root)
    checks = _build_checks(
        tool_paths=tool_paths,
        probe=probe,
        facts=facts,
        timeout_seconds=timeout_seconds,
        required_backends=set(required_backends or []),
    )
    failed = any(check.status == DoctorCheckStatus.FAIL for check in checks)
    status = DoctorStatus.NOT_READY if failed else DoctorStatus.READY
    exit_code = ExitCode.CHECK_FAILED if failed else ExitCode.SUCCESS
    finished_at = datetime.now(UTC)
    result = DoctorResult(
        command="doctor",
        status=status,
        exit_code=exit_code,
        tool_root=str(tool_paths.root),
        inspected_path=str(probe.inspected_root),
        workspace_root=str(probe.workspace_root) if probe.workspace_root is not None else None,
        workspace_package=facts.package_name,
        workspace_toolchain=facts.toolchain,
        workspace_role=probe.workspace_role,
        selected_via=probe.selected_via,
        ready=not failed,
        checks=checks,
        started_at=started_at,
        finished_at=finished_at,
        wall_time_seconds=time.perf_counter() - started,
    )
    return result, 1 if failed else 0


def _select_workspace(*, tool_paths: ToolPaths, workspace: str | None, demo: bool, cwd: Path | None) -> _WorkspaceProbe:
    if demo:
        root = tool_paths.bundled_demo_runtime.resolve()
        return _WorkspaceProbe(
            requested_path=root,
            inspected_root=root,
            workspace_root=root,
            selected_via="bundled-demo",
            workspace_role="bundled-demo",
        )

    if workspace is not None:
        requested = Path(workspace).expanduser().resolve()
        workspace_root = find_workspace_root(requested)
        inspected_root = workspace_root or _candidate_root(requested)
        return _WorkspaceProbe(
            requested_path=requested,
            inspected_root=inspected_root,
            workspace_root=workspace_root,
            selected_via="explicit-workspace",
            workspace_role=_workspace_role(tool_paths=tool_paths, root=workspace_root or inspected_root),
        )

    requested = (cwd or Path.cwd()).resolve()
    workspace_root = find_workspace_root(requested)
    inspected_root = workspace_root or requested
    return _WorkspaceProbe(
        requested_path=requested,
        inspected_root=inspected_root,
        workspace_root=workspace_root,
        selected_via="cwd-inference",
        workspace_role=_workspace_role(tool_paths=tool_paths, root=workspace_root or inspected_root),
    )


def _candidate_root(path: Path) -> Path:
    if path.exists() and path.is_file():
        return path.parent.resolve()
    return path


def _workspace_role(*, tool_paths: ToolPaths, root: Path) -> str:
    if root.resolve() == tool_paths.bundled_demo_runtime.resolve():
        return "bundled-demo"
    return "external-workspace"


def _workspace_facts(root: Path) -> _WorkspaceFacts:
    exists = root.exists() and root.is_dir()
    lakefile = root / "lakefile.toml"
    lean_toolchain = root / "lean-toolchain"
    package_name: str | None = None
    toolchain: str | None = None
    if lakefile.is_file() and lean_toolchain.is_file():
        binding = bind_workspace(root)
        package_name = binding.package_name
        toolchain = binding.toolchain
    return _WorkspaceFacts(
        lakefile=lakefile,
        lean_toolchain=lean_toolchain,
        build_dir=root / ".lake" / "build",
        packages_dir=root / ".lake" / "packages",
        exists=exists,
        writable=_is_writable_directory(root),
        package_name=package_name,
        toolchain=toolchain,
    )


def _build_checks(
    *,
    tool_paths: ToolPaths,
    probe: _WorkspaceProbe,
    facts: _WorkspaceFacts,
    timeout_seconds: float,
    required_backends: set[str],
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    workspace_found = probe.workspace_root is not None
    checks.append(
        DoctorCheck(
            name="workspace-detection",
            status=DoctorCheckStatus.PASS if workspace_found else DoctorCheckStatus.FAIL,
            summary=(
                f"Using workspace root `{probe.workspace_root}`."
                if workspace_found
                else f"Could not detect a Lake workspace from `{probe.requested_path}`."
            ),
            detail=(
                f"Selected via {probe.selected_via}."
                if workspace_found
                else "Pass `--workspace PATH` or run the command inside a directory that has both "
                "`lakefile.toml` and `lean-toolchain`."
            ),
        )
    )
    checks.append(
        DoctorCheck(
            name="workspace-role",
            status=DoctorCheckStatus.PASS,
            summary=(
                "Bundled demo workspace selected."
                if probe.workspace_role == "bundled-demo"
                else "External workspace selected."
            ),
            detail=(
                "This workspace is for demos, self-tests, and probe development."
                if probe.workspace_role == "bundled-demo"
                else "Arbitrary Lake workspaces are supported without copying them under lean-proofctl."
            ),
        )
    )
    checks.append(
        DoctorCheck(
            name="workspace-directory",
            status=DoctorCheckStatus.PASS if facts.exists else DoctorCheckStatus.FAIL,
            summary=(
                f"Workspace directory exists at `{probe.inspected_root}`."
                if facts.exists
                else f"Workspace directory does not exist at `{probe.inspected_root}`."
            ),
        )
    )
    checks.append(
        DoctorCheck(
            name="lakefile",
            status=DoctorCheckStatus.PASS if facts.lakefile.is_file() else DoctorCheckStatus.FAIL,
            summary=(f"Found `{facts.lakefile.name}`." if facts.lakefile.is_file() else f"Missing `{facts.lakefile}`."),
        )
    )
    checks.append(
        DoctorCheck(
            name="lean-toolchain",
            status=DoctorCheckStatus.PASS if facts.lean_toolchain.is_file() else DoctorCheckStatus.FAIL,
            summary=(
                "Found `lean-toolchain`." if facts.lean_toolchain.is_file() else f"Missing `{facts.lean_toolchain}`."
            ),
        )
    )
    checks.append(
        DoctorCheck(
            name="workspace-writable",
            status=DoctorCheckStatus.PASS if facts.writable else DoctorCheckStatus.FAIL,
            summary=(
                f"Workspace root `{probe.inspected_root}` is writable."
                if facts.writable
                else f"Workspace root `{probe.inspected_root}` is not writable."
            ),
            detail=(
                "Sandboxed repair runs need write access."
                if not facts.writable
                else "Writable workspaces can host sandbox overlays for repair and solve."
            ),
        )
    )

    lake_argv = shutil.which("lake")
    checks.append(
        DoctorCheck(
            name="lake-cli",
            status=DoctorCheckStatus.PASS if lake_argv is not None else DoctorCheckStatus.FAIL,
            summary="Found `lake` on PATH." if lake_argv is not None else "Could not find `lake` on PATH.",
        )
    )

    lean_env_check = _lean_env_check(
        root=probe.inspected_root,
        ready=facts.lakefile.is_file() and facts.lean_toolchain.is_file() and lake_argv is not None,
        timeout_seconds=timeout_seconds,
    )
    checks.append(lean_env_check)
    checks.append(_build_state_check(facts))
    checks.append(_sandbox_check(probe=probe, facts=facts))
    checks.append(_semantic_probe_check(tool_paths=tool_paths, facts=facts, lean_env_check=lean_env_check))
    checks.append(_toolchain_match_check(tool_paths=tool_paths, facts=facts))
    checks.extend(_backend_health_checks(timeout_seconds=timeout_seconds, required_backends=required_backends))
    return checks


def _lean_env_check(*, root: Path, ready: bool, timeout_seconds: float) -> DoctorCheck:
    if not ready:
        return DoctorCheck(
            name="lean-env",
            status=DoctorCheckStatus.SKIP,
            summary="Skipped `lake env lean --version` because the workspace is not pinned yet.",
        )
    try:
        completed = subprocess.run(
            ["lake", "env", "lean", "--version"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return DoctorCheck(
            name="lean-env",
            status=DoctorCheckStatus.FAIL,
            summary="`lake env lean --version` timed out.",
            detail="The selected toolchain may not be installed or the workspace may be stuck during setup.",
        )
    output = (completed.stdout.strip() or completed.stderr.strip())[:240]
    if completed.returncode == 0:
        return DoctorCheck(
            name="lean-env",
            status=DoctorCheckStatus.PASS,
            summary="`lake env lean --version` succeeded.",
            detail=output or None,
        )
    return DoctorCheck(
        name="lean-env",
        status=DoctorCheckStatus.FAIL,
        summary="`lake env lean --version` failed.",
        detail=output or "Lean could not start inside the selected workspace.",
    )


def _build_state_check(facts: _WorkspaceFacts) -> DoctorCheck:
    has_runtime_state = facts.build_dir.exists() or facts.packages_dir.exists()
    if has_runtime_state:
        return DoctorCheck(
            name="runtime-state",
            status=DoctorCheckStatus.PASS,
            summary="Found workspace runtime state under `.lake`.",
            detail="Sandboxed runs can reuse existing build outputs and package downloads.",
        )
    return DoctorCheck(
        name="runtime-state",
        status=DoctorCheckStatus.WARN,
        summary="Workspace runtime state under `.lake` is missing.",
        detail="Run `lake build` in the target workspace before repair or semantic-probe-heavy runs.",
    )


def _sandbox_check(*, probe: _WorkspaceProbe, facts: _WorkspaceFacts) -> DoctorCheck:
    if not facts.exists:
        return DoctorCheck(
            name="sandbox",
            status=DoctorCheckStatus.SKIP,
            summary="Skipped sandbox selection because the workspace directory is missing.",
        )
    if not facts.writable:
        return DoctorCheck(
            name="sandbox",
            status=DoctorCheckStatus.FAIL,
            summary="No sandbox mode is available because the workspace is not writable.",
            detail=("Repair and solve need either a writable copied workspace or a writable git worktree."),
        )
    git_root = _git_root(probe.inspected_root)
    if git_root is not None:
        return DoctorCheck(
            name="sandbox",
            status=DoctorCheckStatus.PASS,
            summary="`git-worktree` sandbox mode is available.",
            detail=f"Workspace is backed by git root `{git_root}`.",
        )
    return DoctorCheck(
        name="sandbox",
        status=DoctorCheckStatus.PASS,
        summary="`copied-workspace` sandbox mode is available.",
        detail="The workspace is not git-backed, so proofctl will copy the workspace for isolated runs.",
    )


def _semantic_probe_check(
    *,
    tool_paths: ToolPaths,
    facts: _WorkspaceFacts,
    lean_env_check: DoctorCheck,
) -> DoctorCheck:
    runtime = semantic_probe_runtime(tool_paths)
    if not runtime.source_path.is_file():
        return DoctorCheck(
            name="semantic-probes",
            status=DoctorCheckStatus.FAIL,
            summary=f"Missing bundled probe source `{runtime.source_path}`.",
        )
    if lean_env_check.status == DoctorCheckStatus.FAIL:
        return DoctorCheck(
            name="semantic-probes",
            status=DoctorCheckStatus.WARN,
            summary="Semantic probes may fail because the selected workspace did not pass `lake env` startup.",
            detail=(
                "Fix the workspace toolchain first, then run the bundled SemanticProbe executable against it again."
            ),
        )
    if not runtime.executable_path.is_file():
        return DoctorCheck(
            name="semantic-probes",
            status=DoctorCheckStatus.WARN,
            summary=f"SemanticProbe is not built yet at `{runtime.executable_path}`.",
            detail=(
                f"Run `{semantic_probe_build_hint(tool_paths)}` or `uv run proofctl init` "
                "to build the executable once."
            ),
        )
    if runtime.needs_build:
        return DoctorCheck(
            name="semantic-probes",
            status=DoctorCheckStatus.WARN,
            summary=f"SemanticProbe at `{runtime.executable_path}` is stale.",
            detail=f"Run `{semantic_probe_build_hint(tool_paths)}` or `uv run proofctl init` to rebuild it.",
        )
    if not facts.build_dir.exists():
        return DoctorCheck(
            name="semantic-probes",
            status=DoctorCheckStatus.WARN,
            summary="SemanticProbe is built, but the workspace `.lake/build` outputs are missing.",
            detail="Run `lake build` in the target workspace so the probe can load workspace-local imports.",
        )
    return DoctorCheck(
        name="semantic-probes",
        status=DoctorCheckStatus.PASS,
        summary="SemanticProbe executable is ready.",
        detail=(
            f"`proofctl` will invoke `{runtime.executable_path}` directly and overlay "
            "the target workspace build paths."
        ),
    )


def _toolchain_match_check(*, tool_paths: ToolPaths, facts: _WorkspaceFacts) -> DoctorCheck:
    demo_toolchain_path = tool_paths.bundled_demo_runtime / "lean-toolchain"
    if not demo_toolchain_path.is_file() or facts.toolchain is None:
        return DoctorCheck(
            name="toolchain-match",
            status=DoctorCheckStatus.SKIP,
            summary="Skipped bundled-demo toolchain comparison.",
        )
    demo_toolchain = demo_toolchain_path.read_text(encoding="utf-8").strip()
    if demo_toolchain == facts.toolchain:
        return DoctorCheck(
            name="toolchain-match",
            status=DoctorCheckStatus.PASS,
            summary="Workspace toolchain matches the bundled demo toolchain.",
        )
    return DoctorCheck(
        name="toolchain-match",
        status=DoctorCheckStatus.WARN,
        summary="Workspace toolchain differs from the bundled demo toolchain.",
        detail=(
            f"Workspace uses `{facts.toolchain}` while the demo workspace uses `{demo_toolchain}`. "
            "That is allowed, but verify that probe code still compiles in the target workspace."
        ),
    )


def _backend_health_checks(*, timeout_seconds: float, required_backends: set[str]) -> list[DoctorCheck]:
    checks = [
        DoctorCheck(
            name="backend-none",
            status=DoctorCheckStatus.PASS,
            summary="Deterministic `none` backend is available.",
            detail="Fixture-backed planner and theorem-step tasks can run without hosted prerequisites.",
        )
    ]
    checks.append(
        _hosted_backend_check(
            backend_name="claude",
            executable_name="claude",
            version_argv=["--version"],
            timeout_seconds=timeout_seconds,
            required="claude" in required_backends,
        )
    )
    checks.append(
        _hosted_backend_check(
            backend_name="codex",
            executable_name="codex",
            version_argv=["--version"],
            timeout_seconds=timeout_seconds,
            required="codex" in required_backends,
        )
    )
    return checks


def _hosted_backend_check(
    *,
    backend_name: str,
    executable_name: str,
    version_argv: list[str],
    timeout_seconds: float,
    required: bool,
) -> DoctorCheck:
    executable = shutil.which(executable_name)
    missing_status = DoctorCheckStatus.FAIL if required else DoctorCheckStatus.WARN
    if executable is None:
        return DoctorCheck(
            name=f"backend-{backend_name}",
            status=missing_status,
            summary=f"Hosted backend `{backend_name}` is not available on PATH.",
            detail=(
                f"Install the `{executable_name}` CLI before using `--agent {backend_name}`."
                if required
                else (
                    f"`proofctl` can still run with `--agent none`; "
                    f"install `{executable_name}` to enable hosted runs."
                )
            ),
        )
    try:
        completed = subprocess.run(
            [executable, *version_argv],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return DoctorCheck(
            name=f"backend-{backend_name}",
            status=DoctorCheckStatus.FAIL if required else DoctorCheckStatus.WARN,
            summary=f"Hosted backend `{backend_name}` CLI did not respond to `--version`.",
            detail="The CLI is installed but appears broken or blocked during startup.",
        )
    output = (completed.stdout.strip() or completed.stderr.strip())[:240]
    if completed.returncode == 0:
        return DoctorCheck(
            name=f"backend-{backend_name}",
            status=DoctorCheckStatus.PASS,
            summary=f"Hosted backend `{backend_name}` CLI responded successfully.",
            detail=output or f"`{executable_name} --version` succeeded.",
        )
    return DoctorCheck(
        name=f"backend-{backend_name}",
        status=DoctorCheckStatus.FAIL if required else DoctorCheckStatus.WARN,
        summary=f"Hosted backend `{backend_name}` CLI failed its readiness probe.",
        detail=output or f"`{executable_name} --version` exited with code {completed.returncode}.",
    )


def _git_root(root: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    return Path(output).resolve() if output else None


def _is_writable_directory(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    return os.access(path, os.W_OK)
