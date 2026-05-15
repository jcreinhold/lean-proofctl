"""Resolve and bootstrap the bundled `SemanticProbe` executable."""

import os
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from os import environ
from pathlib import Path

from kan_prover.engine.config import ToolPaths, WorkspaceBinding
from kan_prover.engine.sandbox import HELPER_SEARCH_ROOT

SEMANTIC_PROBE_SOURCE = "SemanticProbe.lean"
SEMANTIC_PROBE_TARGET = "SemanticProbe"


class SemanticProbeRuntimeError(OSError):
    """Raised when the bundled semantic probe executable is unavailable."""


@dataclass(frozen=True)
class SemanticProbeRuntime:
    """Resolved bundled runtime paths for the semantic probe executable."""

    runtime_root: Path
    source_path: Path
    executable_path: Path
    needs_build: bool


def semantic_probe_runtime(tool_paths: ToolPaths) -> SemanticProbeRuntime:
    """Resolve the bundled runtime root, source path, and built executable path."""
    runtime_root = tool_paths.bundled_demo_runtime.resolve()
    source_path = tool_paths.packaged_probe_sources / SEMANTIC_PROBE_SOURCE
    executable_name = SEMANTIC_PROBE_TARGET + (".exe" if os.name == "nt" else "")
    executable_path = runtime_root / ".lake" / "build" / "bin" / executable_name
    return SemanticProbeRuntime(
        runtime_root=runtime_root,
        source_path=source_path,
        executable_path=executable_path,
        needs_build=_semantic_probe_needs_build(
            source_path=source_path,
            executable_path=executable_path,
            runtime_root=runtime_root,
        ),
    )


def ensure_semantic_probe_executable(
    *,
    tool_paths: ToolPaths,
    timeout_seconds: float,
    build_if_needed: bool = True,
) -> Path:
    """Return the built executable path, building it once when needed."""
    runtime = semantic_probe_runtime(tool_paths)
    if not runtime.source_path.is_file():
        msg = f"Missing SemanticProbe source `{runtime.source_path}`."
        raise SemanticProbeRuntimeError(msg)
    if not runtime.needs_build:
        return runtime.executable_path
    if not build_if_needed:
        msg = (
            f"SemanticProbe executable is missing or stale at `{runtime.executable_path}`. "
            f"Run `{semantic_probe_build_hint(tool_paths)}`."
        )
        raise SemanticProbeRuntimeError(msg)
    try:
        completed = build_semantic_probe_executable(tool_paths=tool_paths, timeout_seconds=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        msg = (
            f"Timed out building SemanticProbe in `{runtime.runtime_root}`. "
            f"Run `{semantic_probe_build_hint(tool_paths)}`. Cause: {error!s}"
        )
        raise SemanticProbeRuntimeError(msg) from error
    if completed.returncode != 0:
        output = _trim_build_output(completed.stderr) or _trim_build_output(completed.stdout)
        detail = f" Build output: {output}" if output else ""
        msg = (
            f"Building SemanticProbe failed in `{runtime.runtime_root}`. "
            f"Run `{semantic_probe_build_hint(tool_paths)}` to reproduce.{detail}"
        )
        raise SemanticProbeRuntimeError(msg)
    if not runtime.executable_path.is_file():
        msg = (
            f"Lake reported success but `{runtime.executable_path}` is still missing. "
            f"Run `{semantic_probe_build_hint(tool_paths)}`."
        )
        raise SemanticProbeRuntimeError(msg)
    return runtime.executable_path


def build_semantic_probe_executable(
    *,
    tool_paths: ToolPaths,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    """Run the one-shot Lake build for the bundled semantic probe executable."""
    runtime = semantic_probe_runtime(tool_paths)
    return subprocess.run(
        ["lake", "build", SEMANTIC_PROBE_TARGET],
        cwd=runtime.runtime_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )


def semantic_probe_build_hint(tool_paths: ToolPaths) -> str:
    """Return the explicit one-shot build command for the runtime executable."""
    runtime = semantic_probe_runtime(tool_paths)
    return f"cd {runtime.runtime_root} && lake build {SEMANTIC_PROBE_TARGET}"


def semantic_probe_runtime_fingerprint(tool_paths: ToolPaths) -> str:
    """Return one stable fingerprint for the bundled probe runtime inputs."""
    runtime = semantic_probe_runtime(tool_paths)
    digest = sha256()
    for input_path in _semantic_probe_build_inputs(runtime.runtime_root):
        try:
            stat = input_path.stat()
        except OSError:
            digest.update(f"missing:{input_path}".encode())
            continue
        digest.update(str(input_path.relative_to(runtime.runtime_root)).encode())
        digest.update(str(stat.st_mtime_ns).encode())
        digest.update(str(stat.st_size).encode())
    return digest.hexdigest()


def semantic_probe_environment(*, workspace: WorkspaceBinding) -> dict[str, str]:
    """Build the direct-process environment for probing one target workspace."""
    lean_path_entries = _workspace_lean_path_entries(workspace)
    existing = environ.get("LEAN_PATH")
    if existing:
        lean_path_entries.append(existing)
    return {**environ, "LEAN_PATH": os.pathsep.join(lean_path_entries)}


def launch_semantic_probe_server(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    timeout_seconds: float,
) -> subprocess.Popen[str]:
    """Start one long-lived SemanticProbe server process for a bound workspace."""
    executable_path = ensure_semantic_probe_executable(
        tool_paths=tool_paths,
        timeout_seconds=timeout_seconds,
    )
    return subprocess.Popen(
        [str(executable_path), "serve", str(workspace.root)],
        cwd=workspace.root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=semantic_probe_environment(workspace=workspace),
    )


def _workspace_lean_path_entries(workspace: WorkspaceBinding) -> list[str]:
    build_lib = workspace.build_dir / "lib" / "lean"
    entries: list[str] = [str(build_lib)] if build_lib.is_dir() else []
    if workspace.packages_dir.is_dir():
        package_dirs = sorted(
            path / ".lake" / "build" / "lib" / "lean" for path in workspace.packages_dir.iterdir() if path.is_dir()
        )
        for package_dir in package_dirs:
            if package_dir.is_dir():
                entries.append(str(package_dir))
    helper_search_root = workspace.root / HELPER_SEARCH_ROOT
    if helper_search_root.is_dir():
        entries.append(str(helper_search_root))
    return entries


def _semantic_probe_needs_build(*, source_path: Path, executable_path: Path, runtime_root: Path) -> bool:
    if not executable_path.is_file():
        return True
    executable_mtime = executable_path.stat().st_mtime_ns
    for input_path in _semantic_probe_build_inputs(runtime_root):
        if input_path.is_file() and input_path.stat().st_mtime_ns > executable_mtime:
            return True
    return False


def _semantic_probe_build_inputs(runtime_root: Path) -> list[Path]:
    inputs = [runtime_root / "lakefile.toml", runtime_root / "lean-toolchain", runtime_root / "lake-manifest.json"]
    inputs.extend(sorted(path for path in runtime_root.rglob("*.lean") if ".lake" not in path.parts))
    return inputs


def _trim_build_output(output: str) -> str:
    return " ".join(output.strip().split())[:400]
