"""Shared pytest fixtures for `kan-prover` tests."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from kan_prover.engine.config import ToolPaths, WorkspaceBinding, bind_workspace, discover_tool_paths
from tests.workspaces import downstream_kan_workspace

type FixturePathReader = Callable[[str], Path]
type FixtureTextReader = Callable[[str], str]
type ToolPathFactory = Callable[[Path], ToolPaths]
type JsonWriter = Callable[[Path, object], Path]
type TextWriter = Callable[[Path, str], Path]
type LinkedDemoWorkspaceFactory = Callable[..., tuple[Path, Path | None]]
type ToolPathPatcher = Callable[..., ToolPaths]


@pytest.fixture(scope="session")
def tool_paths() -> ToolPaths:
    return discover_tool_paths()


@pytest.fixture(scope="session")
def kan_prover_root(tool_paths: ToolPaths) -> Path:
    return tool_paths.root


@pytest.fixture(scope="session")
def repo_root(tool_paths: ToolPaths) -> Path:
    return downstream_kan_workspace(tool_paths.root)


@pytest.fixture(scope="session")
def fixtures_dir(kan_prover_root: Path) -> Path:
    return kan_prover_root / "tests" / "fixtures"


@pytest.fixture(scope="session")
def acceptance_dir(kan_prover_root: Path) -> Path:
    return kan_prover_root / "tests" / "acceptance"


@pytest.fixture(scope="session")
def sample_workspace_fixture(acceptance_dir: Path) -> Path:
    return acceptance_dir / "sample_workspace"


@pytest.fixture(scope="session")
def sample_plan_fixture(fixtures_dir: Path) -> Path:
    return fixtures_dir / "sample_plan.json"


@pytest.fixture(scope="session")
def demo_workspace(tool_paths: ToolPaths) -> WorkspaceBinding:
    return bind_workspace(tool_paths.bundled_demo_runtime)


@pytest.fixture(scope="session")
def repo_workspace(repo_root: Path) -> WorkspaceBinding:
    return bind_workspace(repo_root)


@pytest.fixture
def artifact_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


@pytest.fixture
def make_tool_paths(tool_paths: ToolPaths) -> ToolPathFactory:
    def _make(artifacts_root: Path) -> ToolPaths:
        return ToolPaths(
            root=tool_paths.root,
            prompts=tool_paths.prompts,
            artifacts=artifacts_root,
            artifact_runs=artifacts_root / "runs",
            artifact_index=artifacts_root / "index.jsonl",
            artifact_sessions=artifacts_root / "sessions",
            bundled_demo_runtime=tool_paths.bundled_demo_runtime,
            packaged_probe_sources=tool_paths.packaged_probe_sources,
        )

    return _make


@pytest.fixture
def fixture_path(fixtures_dir: Path) -> FixturePathReader:
    def _fixture(name: str) -> Path:
        return fixtures_dir / name

    return _fixture


@pytest.fixture
def read_fixture_text(fixture_path: FixturePathReader) -> FixtureTextReader:
    def _read(name: str) -> str:
        return fixture_path(name).read_text(encoding="utf-8")

    return _read


@pytest.fixture
def write_json(tmp_path: Path) -> JsonWriter:
    def _write(path: Path, payload: object) -> Path:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    return _write


@pytest.fixture
def write_text() -> TextWriter:
    def _write(path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def copy_sample_workspace(tmp_path: Path, sample_workspace_fixture: Path) -> Path:
    workspace_root = tmp_path / "sample-workspace"
    shutil.copytree(sample_workspace_fixture, workspace_root)
    return workspace_root


@pytest.fixture
def make_linked_demo_workspace(demo_workspace: WorkspaceBinding, fixtures_dir: Path) -> LinkedDemoWorkspaceFactory:
    def _make(
        temp_root: Path,
        *,
        fixture_name: str | None = None,
        relative_target: str | None = None,
        include_tools: bool = True,
        include_lake_dir: bool = True,
    ) -> tuple[Path, Path | None]:
        workspace_root = temp_root / "workspace"
        workspace_root.mkdir(parents=True, exist_ok=True)
        for name in ("lakefile.toml", "lake-manifest.json", "lean-toolchain", "KanRuntime.lean"):
            (workspace_root / name).symlink_to(demo_workspace.root / name)
        if include_lake_dir:
            (workspace_root / ".lake").symlink_to(demo_workspace.root / ".lake", target_is_directory=True)
        if include_tools:
            (workspace_root / "Tools").symlink_to(demo_workspace.root / "Tools", target_is_directory=True)
        target_file: Path | None = None
        if fixture_name is not None and relative_target is not None:
            target_file = workspace_root / relative_target
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text((fixtures_dir / fixture_name).read_text(encoding="utf-8"), encoding="utf-8")
            target_file = target_file.resolve()
        return workspace_root.resolve(), target_file

    return _make


@pytest.fixture
def patch_tool_paths(monkeypatch: pytest.MonkeyPatch, make_tool_paths: ToolPathFactory) -> ToolPathPatcher:
    def _patch(artifacts_root: Path, *modules: object) -> ToolPaths:
        paths = make_tool_paths(artifacts_root)
        for module in modules:
            monkeypatch.setattr(module, "discover_tool_paths", lambda paths=paths: paths)
        return paths

    return _patch
