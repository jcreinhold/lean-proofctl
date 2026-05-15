"""Tool-home discovery and target workspace binding."""

import os
import subprocess
import tomllib
from dataclasses import dataclass
from os import environ
from pathlib import Path


@dataclass(frozen=True)
class ToolPaths:
    """Filesystem roots owned by the `kan-prover` tool itself."""

    root: Path
    prompts: Path
    artifacts: Path
    artifact_runs: Path
    artifact_index: Path
    artifact_sessions: Path
    bundled_demo_runtime: Path
    packaged_probe_sources: Path


@dataclass(frozen=True)
class WorkspaceBinding:
    """One target Lake workspace bound for a verifier run."""

    root: Path
    lakefile: Path
    lean_toolchain_path: Path
    package_name: str
    toolchain: str
    build_dir: Path
    packages_dir: Path
    writable: bool
    git_root: Path | None
    git_backed: bool


def discover_tool_paths(start: Path | None = None) -> ToolPaths:
    """Locate the `kan-prover` tool home from this file or a caller-provided seed."""
    override = environ.get("KAN_PROVER_PROJECT_ROOT")
    if override:
        return _tool_paths_from_root(Path(override).expanduser().resolve())
    seed = (start or Path(__file__)).resolve()
    root = seed if seed.is_dir() else seed.parent
    for candidate in [root, *root.parents]:
        pyproject = candidate / "pyproject.toml"
        runtime = candidate / "lean-runtime"
        if pyproject.is_file() and runtime.is_dir():
            return _tool_paths_from_root(candidate)
    msg = f"Could not find kan-prover project root from {seed}"
    raise FileNotFoundError(msg)


def ensure_tool_layout(paths: ToolPaths) -> None:
    """Create artifact directories and validate the bundled demo workspace."""
    ensure_workspace_binding(bind_workspace(paths.bundled_demo_runtime))
    paths.artifact_runs.mkdir(parents=True, exist_ok=True)
    paths.artifact_sessions.mkdir(parents=True, exist_ok=True)
    paths.artifact_index.parent.mkdir(parents=True, exist_ok=True)
    if not paths.artifact_index.exists():
        paths.artifact_index.write_text("", encoding="utf-8")


def bind_workspace(path: Path) -> WorkspaceBinding:
    """Bind one explicit workspace path after walking upward to its root."""
    resolved = path.expanduser().resolve()
    root = _find_workspace_root(resolved)
    lakefile = root / "lakefile.toml"
    lean_toolchain_path = root / "lean-toolchain"
    git_root = _git_root(root)
    return WorkspaceBinding(
        root=root,
        lakefile=lakefile,
        lean_toolchain_path=lean_toolchain_path,
        package_name=_read_package_name(lakefile, fallback=root.name),
        toolchain=lean_toolchain_path.read_text(encoding="utf-8").strip(),
        build_dir=root / ".lake" / "build",
        packages_dir=root / ".lake" / "packages",
        writable=os.access(root, os.W_OK),
        git_root=git_root,
        git_backed=git_root is not None,
    )


def ensure_workspace_binding(binding: WorkspaceBinding) -> None:
    """Validate that a bound workspace has the required pinned Lake inputs."""
    if not binding.lakefile.is_file():
        msg = f"Missing Lake configuration: {binding.lakefile}"
        raise FileNotFoundError(msg)
    if not binding.lean_toolchain_path.is_file():
        msg = f"Missing Lean toolchain pin: {binding.lean_toolchain_path}"
        raise FileNotFoundError(msg)


def resolve_workspace_binding(
    *,
    tool_paths: ToolPaths,
    workspace: str | None = None,
    target_file: Path | None = None,
    cwd: Path | None = None,
) -> WorkspaceBinding:
    """Resolve one target workspace from an explicit path or local inference."""
    if workspace is not None:
        binding = bind_workspace(Path(workspace))
        ensure_workspace_binding(binding)
        return binding

    inference_starts: list[Path] = []
    if target_file is not None:
        inference_starts.append(target_file.expanduser().resolve())
    inference_starts.append((cwd or Path.cwd()).resolve())
    for start in inference_starts:
        root = _maybe_find_workspace_root(start)
        if root is None:
            continue
        binding = bind_workspace(root)
        ensure_workspace_binding(binding)
        return binding

    searched = ", ".join(str(path) for path in inference_starts)
    msg = (
        "Could not infer a Lake workspace. "
        f"Searched upward from: {searched}. "
        "Pass `--workspace PATH` or run the command inside a workspace with both "
        "`lakefile.toml` and `lean-toolchain`."
    )
    raise FileNotFoundError(msg)


def _tool_paths_from_root(root: Path) -> ToolPaths:
    artifacts = root / "artifacts"
    bundled_demo_runtime = root / "lean-runtime"
    return ToolPaths(
        root=root,
        prompts=root / "prompts",
        artifacts=artifacts,
        artifact_runs=artifacts / "runs",
        artifact_index=artifacts / "index.jsonl",
        artifact_sessions=artifacts / "sessions",
        bundled_demo_runtime=bundled_demo_runtime,
        packaged_probe_sources=bundled_demo_runtime / "Tools",
    )


def _find_workspace_root(start: Path) -> Path:
    root = find_workspace_root(start)
    if root is not None:
        return root
    msg = (
        f"Could not find a Lake workspace from {start}. "
        "A workspace root must contain both `lakefile.toml` and `lean-toolchain`."
    )
    raise FileNotFoundError(msg)


def _maybe_find_workspace_root(start: Path) -> Path | None:
    seed = start if start.is_dir() else start.parent
    for candidate in [seed, *seed.parents]:
        if is_workspace_root(candidate):
            return candidate
    return None


def _is_workspace_root(candidate: Path) -> bool:
    return (candidate / "lakefile.toml").is_file() and (candidate / "lean-toolchain").is_file()


def find_workspace_root(start: Path) -> Path | None:
    """Try to find one workspace root by walking upward from a seed path."""
    return _maybe_find_workspace_root(start)


def is_workspace_root(candidate: Path) -> bool:
    """Return whether one directory is a pinned Lake workspace root."""
    return _is_workspace_root(candidate)


def _read_package_name(lakefile: Path, *, fallback: str) -> str:
    payload = tomllib.loads(lakefile.read_text(encoding="utf-8"))
    direct_name = payload.get("name")
    if isinstance(direct_name, str) and direct_name:
        return direct_name
    package = payload.get("package")
    if isinstance(package, dict):
        package_name = package.get("name")
        if isinstance(package_name, str) and package_name:
            return package_name
    return fallback


def _git_root(root: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    resolved = completed.stdout.strip()
    return Path(resolved).resolve() if resolved else None
