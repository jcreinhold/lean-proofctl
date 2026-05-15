"""Tests for sandbox workspace creation strategies."""

import subprocess
import tempfile
from pathlib import Path

from lean_proofctl.engine.config import bind_workspace, discover_tool_paths
from lean_proofctl.engine.models import SandboxStrategy
from lean_proofctl.engine.sandbox import create_sandbox_workspace


class TestSandboxWorkspace:
    """Unit tests for git-worktree and copy sandbox creation."""

    @classmethod
    def setup_class(cls) -> None:
        cls.tool_paths = discover_tool_paths()

    def test_git_backed_clean_workspace_prefers_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            workspace_root = self._git_workspace(temp_root)
            workspace = bind_workspace(workspace_root)
            sandbox = create_sandbox_workspace(workspace=workspace, run_dir=temp_root / "run")
            assert sandbox.strategy == SandboxStrategy.GIT_WORKTREE
            assert sandbox.sandbox_workspace.root.is_dir()
            assert (sandbox.sandbox_workspace.root / "lakefile.toml").is_file()

    def test_git_worktree_sandbox_preserves_ignored_lake_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            workspace_root = self._git_workspace_without_tracked_lake(temp_root)
            workspace = bind_workspace(workspace_root)
            sandbox = create_sandbox_workspace(workspace=workspace, run_dir=temp_root / "run")
            assert sandbox.strategy == SandboxStrategy.GIT_WORKTREE
            sandbox_lake = sandbox.sandbox_workspace.root / ".lake"
            assert sandbox_lake.is_symlink()
            assert sandbox_lake.resolve() == (workspace_root / ".lake").resolve()

    def test_git_worktree_sandbox_retargets_relative_lake_symlink_to_real_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            runtime_root = temp_root / "shared-runtime"
            runtime_root.mkdir(parents=True, exist_ok=True)
            shutil_target = self.tool_paths.bundled_demo_runtime
            for name in ["lakefile.toml", "lake-manifest.json", "lean-toolchain", "KanRuntime.lean"]:
                (runtime_root / name).symlink_to(shutil_target / name)
            for name in [".lake", "Tools"]:
                (runtime_root / name).symlink_to(shutil_target / name, target_is_directory=True)

            repo_root = temp_root / "repo"
            workspace_root = repo_root / "bench"
            workspace_root.mkdir(parents=True, exist_ok=True)
            for name in ["lakefile.toml", "lake-manifest.json", "lean-toolchain", "KanRuntime.lean"]:
                (workspace_root / name).symlink_to(runtime_root / name)
            (workspace_root / "Problems").mkdir(parents=True, exist_ok=True)
            (workspace_root / ".gitignore").write_text(".lake/\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "config", "user.email", "proofctl-tests@example.com"],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "proofctl tests"],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(["git", "add", "."], cwd=repo_root, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "commit", "-m", "seed"],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            )
            (workspace_root / ".lake").symlink_to(
                Path("..") / ".." / "shared-runtime" / ".lake",
                target_is_directory=True,
            )

            workspace = bind_workspace(workspace_root)
            sandbox = create_sandbox_workspace(workspace=workspace, run_dir=temp_root / "run")
            sandbox_lake = sandbox.sandbox_workspace.root / ".lake"
            assert sandbox.strategy == SandboxStrategy.GIT_WORKTREE
            assert sandbox_lake.is_symlink()
            assert sandbox_lake.resolve() == (workspace_root / ".lake").resolve()

    def test_non_git_workspace_falls_back_to_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            workspace_root = temp_root / "workspace"
            workspace_root.mkdir(parents=True, exist_ok=True)
            for name in ["lakefile.toml", "lake-manifest.json", "lean-toolchain", "KanRuntime.lean"]:
                (workspace_root / name).symlink_to(self.tool_paths.bundled_demo_runtime / name)
            for name in [".lake", "Tools"]:
                (workspace_root / name).symlink_to(
                    self.tool_paths.bundled_demo_runtime / name,
                    target_is_directory=True,
                )
            (workspace_root / "Problems").mkdir(parents=True, exist_ok=True)
            (workspace_root / "Problems" / "Temp.lean").write_text(
                "theorem temp : True := by\n  exact True.intro\n", encoding="utf-8"
            )
            workspace = bind_workspace(workspace_root)
            sandbox = create_sandbox_workspace(workspace=workspace, run_dir=temp_root / "run")
            assert sandbox.strategy == SandboxStrategy.COPIED_WORKSPACE
            assert sandbox.sandbox_workspace.root.is_dir()
            assert (sandbox.sandbox_workspace.root / "Problems" / "Temp.lean").is_file()

    def _git_workspace(self, temp_root: Path) -> Path:
        workspace_root = temp_root / "repo"
        workspace_root.mkdir(parents=True, exist_ok=True)
        for name in ["lakefile.toml", "lake-manifest.json", "lean-toolchain", "KanRuntime.lean"]:
            (workspace_root / name).symlink_to(self.tool_paths.bundled_demo_runtime / name)
        for name in [".lake", "Tools"]:
            (workspace_root / name).symlink_to(
                self.tool_paths.bundled_demo_runtime / name,
                target_is_directory=True,
            )
        (workspace_root / "Problems").mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=workspace_root, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "config", "user.email", "proofctl-tests@example.com"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "proofctl tests"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "add", "."], cwd=workspace_root, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "commit", "-m", "seed"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return workspace_root

    def _git_workspace_without_tracked_lake(self, temp_root: Path) -> Path:
        workspace_root = temp_root / "repo"
        workspace_root.mkdir(parents=True, exist_ok=True)
        for name in ["lakefile.toml", "lake-manifest.json", "lean-toolchain", "KanRuntime.lean"]:
            (workspace_root / name).symlink_to(self.tool_paths.bundled_demo_runtime / name)
        (workspace_root / "Problems").mkdir(parents=True, exist_ok=True)
        (workspace_root / ".gitignore").write_text(".lake/\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=workspace_root, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "config", "user.email", "proofctl-tests@example.com"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "proofctl tests"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "add", "."], cwd=workspace_root, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "commit", "-m", "seed"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
        )
        (workspace_root / ".lake").symlink_to(
            self.tool_paths.bundled_demo_runtime / ".lake",
            target_is_directory=True,
        )
        return workspace_root
