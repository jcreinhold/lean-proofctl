"""Target-workspace sandboxes for repair, solve, and decompose runs."""

import shutil
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from lean_proofctl.engine.config import WorkspaceBinding, bind_workspace
from lean_proofctl.engine.models import HelperRef, SandboxMetadata, SandboxStrategy

MANAGED_HELPER_ROOT = Path("ProofctlGenerated") / "Helpers"
MANAGED_HELPER_MODULE_ROOT = "ProofctlGenerated.Helpers"
HELPER_SEARCH_ROOT = Path(".proofctl-helper-root")
PRESERVED_WORKSPACE_RUNTIME_PATHS = (Path(".lake"),)


@dataclass(frozen=True)
class SandboxWorkspace:
    """One sandboxed target workspace used for verification attempts."""

    real_workspace: WorkspaceBinding
    sandbox_workspace: WorkspaceBinding
    sandbox_root: Path
    strategy: SandboxStrategy
    helper_root_dir: Path
    helper_module_root: str = MANAGED_HELPER_MODULE_ROOT

    def metadata(self) -> SandboxMetadata:
        """Return durable sandbox metadata for artifacts."""
        return SandboxMetadata(
            real_workspace_root=str(self.real_workspace.root),
            sandbox_root=str(self.sandbox_root),
            sandbox_workspace_root=str(self.sandbox_workspace.root),
            strategy=self.strategy,
            helper_root_dir=str(self.helper_root_dir),
            helper_module_root=self.helper_module_root,
        )

    def target_file(self, real_target_file: Path) -> Path:
        """Map one real target file into the sandboxed workspace."""
        return self.sandbox_workspace.root / self.target_relative_path(real_target_file)

    def target_relative_path(self, real_target_file: Path) -> Path:
        """Return the target file path relative to the real workspace root."""
        resolved = real_target_file.expanduser().resolve()
        try:
            return resolved.relative_to(self.real_workspace.root)
        except ValueError as error:
            msg = (
                f"Target file `{resolved}` is outside workspace `{self.real_workspace.root}`. "
                "Shadow sandboxes require the target file to live inside the selected workspace."
            )
            raise ValueError(msg) from error

    def helper_file(self, module_name: str) -> Path:
        """Return the sandbox path for one managed helper module."""
        return self.helper_root_dir / helper_module_relative_path(
            module_name=module_name,
            helper_module_root=self.helper_module_root,
        )

    def real_helper_file(self, module_name: str) -> Path:
        """Return the real-workspace path for one managed helper module."""
        return (
            self.real_workspace.root
            / MANAGED_HELPER_ROOT
            / helper_module_relative_path(
                module_name=module_name,
                helper_module_root=self.helper_module_root,
            )
        )

    def materialize_helper(self, helper: HelperRef) -> Path:
        """Copy one stored helper into the current sandbox helper root."""
        destination = self.helper_file(helper.module_name)
        source = Path(helper.file_path)
        source_olean = source.with_suffix(".olean")
        destination_olean = destination.with_suffix(".olean")
        if destination.is_file():
            if source_olean.is_file() and not destination_olean.is_file():
                shutil.copy2(source_olean, destination_olean)
            return destination
        if not source.is_file():
            msg = f"Stored helper source does not exist: {source}"
            raise FileNotFoundError(msg)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if source_olean.is_file():
            shutil.copy2(source_olean, destination_olean)
        return destination


def create_sandbox_workspace(
    *,
    workspace: WorkspaceBinding,
    run_dir: Path,
    strategy: SandboxStrategy | None = None,
) -> SandboxWorkspace:
    """
    Create one sandboxed workspace for a verifier run.

    The returned workspace is where Lean verification happens. `proofctl`
    never mutates the real workspace during repair, solve, session, or prove;
    callers must go through explicit later apply/transplant steps to write back
    a verified result.
    """
    if strategy is SandboxStrategy.GIT_WORKTREE:
        if not _can_use_git_worktree(workspace):
            msg = f"Stored sandbox strategy `{strategy.value}` is not available for `{workspace.root}`."
            raise RuntimeError(msg)
        return _create_git_worktree_sandbox(workspace=workspace, run_dir=run_dir)
    if strategy is SandboxStrategy.COPIED_WORKSPACE:
        return _create_copied_workspace_sandbox(workspace=workspace, run_dir=run_dir)
    if _can_use_git_worktree(workspace):
        return _create_git_worktree_sandbox(workspace=workspace, run_dir=run_dir)
    return _create_copied_workspace_sandbox(workspace=workspace, run_dir=run_dir)


def helper_module_relative_path(*, module_name: str, helper_module_root: str = MANAGED_HELPER_MODULE_ROOT) -> Path:
    """Convert one managed helper module name into a path under `ProofctlGenerated/Helpers`."""
    prefix = helper_module_root.split(".")
    parts = module_name.split(".")
    if parts[: len(prefix)] != prefix:
        msg = f"Helper module `{module_name}` is outside managed root `{helper_module_root}`."
        raise ValueError(msg)
    suffix = parts[len(prefix) :]
    if not suffix:
        msg = f"Helper module `{module_name}` is missing a suffix under `{helper_module_root}`."
        raise ValueError(msg)
    return Path(*suffix).with_suffix(".lean")


def _create_copied_workspace_sandbox(*, workspace: WorkspaceBinding, run_dir: Path) -> SandboxWorkspace:
    sandbox_root = run_dir / "sandbox"
    sandbox_root.mkdir(parents=True, exist_ok=True)
    sandbox_workspace_root = sandbox_root / "workspace"
    shutil.copytree(
        workspace.root,
        sandbox_workspace_root,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git"),
    )
    sandbox_binding = bind_workspace(sandbox_workspace_root)
    helper_root_dir = sandbox_binding.root / MANAGED_HELPER_ROOT
    helper_root_dir.mkdir(parents=True, exist_ok=True)
    _ensure_helper_search_root(workspace_root=sandbox_binding.root)
    return SandboxWorkspace(
        real_workspace=workspace,
        sandbox_workspace=sandbox_binding,
        sandbox_root=sandbox_root,
        strategy=SandboxStrategy.COPIED_WORKSPACE,
        helper_root_dir=helper_root_dir,
    )


def _create_git_worktree_sandbox(*, workspace: WorkspaceBinding, run_dir: Path) -> SandboxWorkspace:
    assert workspace.git_root is not None
    git_root = workspace.git_root
    sandbox_root = _worktree_root(git_root=git_root, run_dir=run_dir)
    sandbox_root.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["git", "-C", str(git_root), "worktree", "add", "--detach", str(sandbox_root), "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        msg = completed.stderr.strip() or completed.stdout.strip() or "git worktree add failed"
        raise RuntimeError(msg)
    relative_workspace_root = workspace.root.relative_to(git_root)
    sandbox_workspace_root = sandbox_root / relative_workspace_root
    _overlay_workspace_changes(
        git_root=git_root,
        workspace_root=workspace.root,
        sandbox_root=sandbox_root,
    )
    _overlay_workspace_runtime_state(
        workspace_root=workspace.root,
        sandbox_workspace_root=sandbox_workspace_root,
    )
    sandbox_binding = bind_workspace(sandbox_workspace_root)
    helper_root_dir = sandbox_binding.root / MANAGED_HELPER_ROOT
    helper_root_dir.mkdir(parents=True, exist_ok=True)
    _ensure_helper_search_root(workspace_root=sandbox_binding.root)
    return SandboxWorkspace(
        real_workspace=workspace,
        sandbox_workspace=sandbox_binding,
        sandbox_root=sandbox_root,
        strategy=SandboxStrategy.GIT_WORKTREE,
        helper_root_dir=helper_root_dir,
    )


def _can_use_git_worktree(workspace: WorkspaceBinding) -> bool:
    return workspace.git_backed and workspace.git_root is not None


def _worktree_root(*, git_root: Path, run_dir: Path) -> Path:
    unique_suffix = sha256(str(run_dir.resolve()).encode("utf-8")).hexdigest()[:8]
    return git_root.parent / f".{git_root.name}-worktrees" / f"{run_dir.name}-{unique_suffix}"


def _overlay_workspace_changes(*, git_root: Path, workspace_root: Path, sandbox_root: Path) -> None:
    relative_workspace_root = workspace_root.relative_to(git_root)
    for relative_path in _dirty_workspace_paths(git_root=git_root, workspace_root=workspace_root):
        real_path = git_root / relative_path
        sandbox_path = sandbox_root / relative_path
        if real_path.exists():
            sandbox_path.parent.mkdir(parents=True, exist_ok=True)
            if real_path.is_symlink():
                sandbox_path.unlink(missing_ok=True)
                target = real_path.resolve()
                sandbox_path.symlink_to(target, target_is_directory=target.is_dir())
            else:
                shutil.copy2(real_path, sandbox_path)
            continue
        if not sandbox_path.exists():
            continue
        if sandbox_path.is_dir():
            shutil.rmtree(sandbox_path)
        else:
            sandbox_path.unlink()
    sandbox_workspace_root = sandbox_root / relative_workspace_root
    sandbox_workspace_root.mkdir(parents=True, exist_ok=True)


def _overlay_workspace_runtime_state(*, workspace_root: Path, sandbox_workspace_root: Path) -> None:
    """
    Preserve ignored Lean runtime state needed for verifier-equivalent sandboxes.

    `.lake` is ignored by git but affects import resolution and cached build
    reuse. Copying it keeps sandbox verification aligned with the bound target
    workspace instead of silently re-solving dependencies from tool-home state.
    """
    for relative_path in PRESERVED_WORKSPACE_RUNTIME_PATHS:
        source = workspace_root / relative_path
        destination = sandbox_workspace_root / relative_path
        if not source.exists():
            continue
        if destination.exists() or destination.is_symlink():
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target = source.resolve()
            destination.symlink_to(target, target_is_directory=target.is_dir())
            continue
        destination.symlink_to(source, target_is_directory=source.is_dir())


def _ensure_helper_search_root(*, workspace_root: Path) -> None:
    helper_search_root = workspace_root / HELPER_SEARCH_ROOT
    helper_namespace_root = helper_search_root / "ProofctlGenerated"
    managed_namespace_root = workspace_root / "ProofctlGenerated"
    helper_search_root.mkdir(parents=True, exist_ok=True)
    if helper_namespace_root.exists() or helper_namespace_root.is_symlink():
        if helper_namespace_root.is_dir() and not helper_namespace_root.is_symlink():
            shutil.rmtree(helper_namespace_root)
        else:
            helper_namespace_root.unlink()
    helper_namespace_root.symlink_to(managed_namespace_root, target_is_directory=True)


def _dirty_workspace_paths(*, git_root: Path, workspace_root: Path) -> set[Path]:
    relative_workspace_root = workspace_root.relative_to(git_root)
    tracked = subprocess.run(
        ["git", "-C", str(git_root), "diff", "--name-only", "-z", "HEAD", "--", str(relative_workspace_root)],
        capture_output=True,
        text=False,
        check=False,
    )
    untracked = subprocess.run(
        [
            "git",
            "-C",
            str(git_root),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            str(relative_workspace_root),
        ],
        capture_output=True,
        text=False,
        check=False,
    )
    dirty_paths = {*_null_delimited_paths(tracked.stdout), *_null_delimited_paths(untracked.stdout)}
    return {
        path
        for path in dirty_paths
        if path.parts[: len(relative_workspace_root.parts)] == relative_workspace_root.parts
    }


def _null_delimited_paths(payload: bytes) -> set[Path]:
    return {Path(chunk.decode("utf-8")) for chunk in payload.split(b"\x00") if chunk}
