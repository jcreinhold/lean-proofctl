"""Fixture-based tests for `kan-prover check`."""

import argparse
import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from kan_prover.cli.commands import check
from kan_prover.cli.main import main
from kan_prover.engine.config import ToolPaths, bind_workspace, discover_tool_paths
from kan_prover.engine.models import CheckResult, CheckStatus, DiagnosticClass, ExitCode, JsonValue


class TestCheckCommand:
    """Integration tests that run Lean through Lake against fixtures."""

    @classmethod
    def setup_class(cls) -> None:
        cls.tool_paths = discover_tool_paths()
        cls.demo_workspace = bind_workspace(cls.tool_paths.bundled_demo_runtime)
        cls.repo_workspace = bind_workspace(Path("/Users/jcreinhold/Code/kan"))
        cls.fixtures = cls.tool_paths.root / "tests" / "fixtures"
        cls.repo_green_file = Path("/Users/jcreinhold/Code/kan/KanProofs/Algebra/Grades.lean")

    @pytest.mark.integration
    def test_clean_fixture(self) -> None:
        result, exit_code, artifact_snapshot = self._run_fixture("clean.lean")
        assert exit_code == 0
        assert result.status == CheckStatus.OK
        assert result.exit_code == ExitCode.SUCCESS
        assert result.diagnostic_count == 0
        self.assertArtifactSnapshot(artifact_snapshot, result.run_id)

    @pytest.mark.integration
    def test_parser_failure_fixture(self) -> None:
        result, exit_code, _ = self._run_fixture("parser_fail.lean")
        assert exit_code == 1
        assert result.status == CheckStatus.FAILED
        assert result.exit_code == ExitCode.CHECK_FAILED
        assert result.diagnostics[0].class_name == DiagnosticClass.SYNTAX_ERROR

    @pytest.mark.integration
    def test_elaboration_failure_fixture(self) -> None:
        result, exit_code, _ = self._run_fixture("elaboration_fail.lean")
        assert exit_code == 1
        assert result.status == CheckStatus.FAILED
        assert result.diagnostics[0].class_name == DiagnosticClass.TYPE_MISMATCH

    @pytest.mark.integration
    def test_unsolved_goals_fixture(self) -> None:
        result, exit_code, _ = self._run_fixture("unsolved_goals.lean")
        assert exit_code == 1
        assert result.status == CheckStatus.FAILED
        assert result.diagnostics[0].class_name == DiagnosticClass.UNSOLVED_GOALS

    @pytest.mark.integration
    def test_cli_json_output_separates_stdout_and_stderr(self) -> None:
        fixture = self.fixtures / "clean.lean"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_artifacts = Path(temp_dir) / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = ["--json", "check", "--workspace", str(self.demo_workspace.root), str(fixture)]
            with (
                patch("kan_prover.cli.commands.check.discover_tool_paths", return_value=temp_paths),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(argv)
        assert exit_code == 0
        payload = json.loads(stdout.getvalue())
        assert payload["status"] == "ok"
        assert "kan-prover check ok:" in stderr.getvalue()

    def test_init_skip_build(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["--json", "init", "--demo", "--skip-build"])
        assert exit_code == 0
        payload = json.loads(stdout.getvalue())
        assert payload["status"] == "ok"
        assert not payload["build_ran"]
        assert payload["workspace_role"] == "bundled-demo"
        assert "kan-prover init ok:" in stderr.getvalue()

    @pytest.mark.integration
    def test_explicit_workspace_binding_uses_demo_runtime(self) -> None:
        fixture = self.fixtures / "clean.lean"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_artifacts = Path(temp_dir) / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            args = argparse.Namespace(
                file=str(fixture),
                workspace=str(self.demo_workspace.root),
                timeout_seconds=60.0,
                json=False,
                jsonl=False,
            )
            with patch("kan_prover.cli.commands.check.discover_tool_paths", return_value=temp_paths):
                result, exit_code = check.run_command(args)
        assert exit_code == 0
        assert result.workspace.root == str(self.demo_workspace.root)
        assert result.workspace.package == self.demo_workspace.package_name

    @pytest.mark.integration
    def test_infers_workspace_from_repo_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_artifacts = Path(temp_dir) / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            args = argparse.Namespace(
                file=str(self.repo_green_file),
                workspace=None,
                timeout_seconds=120.0,
                json=False,
                jsonl=False,
            )
            with patch("kan_prover.cli.commands.check.discover_tool_paths", return_value=temp_paths):
                result, exit_code = check.run_command(args)
        assert exit_code == 0
        assert result.workspace.root == str(self.repo_workspace.root)
        assert result.workspace.package == self.repo_workspace.package_name

    def test_fails_when_no_workspace_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            target = temp_root / "isolated.lean"
            target.write_text("theorem t : True := by exact True.intro\n", encoding="utf-8")
            args = argparse.Namespace(
                file=str(target),
                workspace=None,
                timeout_seconds=60.0,
                json=False,
                jsonl=False,
            )
            with (
                patch("kan_prover.cli.commands.check.discover_tool_paths", return_value=temp_paths),
                patch("kan_prover.engine.config.Path.cwd", return_value=temp_root),
                pytest.raises(FileNotFoundError),
            ):
                check.run_command(args)

    def _run_fixture(self, fixture_name: str) -> tuple[CheckResult, int, dict[str, JsonValue]]:
        fixture = self.fixtures / fixture_name
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_artifacts = Path(temp_dir) / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            args = argparse.Namespace(
                file=str(fixture),
                workspace=str(self.demo_workspace.root),
                timeout_seconds=60.0,
                json=False,
                jsonl=False,
            )
            with patch("kan_prover.cli.commands.check.discover_tool_paths", return_value=temp_paths):
                result, exit_code = check.run_command(args)
            run_dir = temp_artifacts / "runs" / result.run_id
            index_path = temp_artifacts / "index.jsonl"
            artifact_snapshot: dict[str, JsonValue] = {
                "manifest": (run_dir / "manifest.json").is_file(),
                "request": (run_dir / "request.json").is_file(),
                "result": (run_dir / "result.json").is_file(),
                "summary": (run_dir / "summary.md").is_file(),
                "stdout": (run_dir / "env" / "stdout.txt").is_file(),
                "stderr": (run_dir / "env" / "stderr.txt").is_file(),
                "parse_errors": (run_dir / "context" / "diagnostic-parse-errors.json").is_file(),
                "rows": [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines() if line],
            }
            return result, exit_code, artifact_snapshot

    def _temp_paths(self, temp_artifacts: Path) -> ToolPaths:
        return ToolPaths(
            root=self.tool_paths.root,
            prompts=self.tool_paths.prompts,
            artifacts=temp_artifacts,
            artifact_runs=temp_artifacts / "runs",
            artifact_index=temp_artifacts / "index.jsonl",
            artifact_sessions=temp_artifacts / "sessions",
            bundled_demo_runtime=self.tool_paths.bundled_demo_runtime,
            packaged_probe_sources=self.tool_paths.packaged_probe_sources,
        )

    def assertArtifactSnapshot(self, snapshot: dict[str, JsonValue], run_id: str) -> None:
        assert snapshot["manifest"]
        assert snapshot["request"]
        assert snapshot["result"]
        assert snapshot["summary"]
        assert snapshot["stdout"]
        assert snapshot["stderr"]
        assert snapshot["parse_errors"]
        rows = snapshot["rows"]
        assert isinstance(rows, list)
        assert isinstance(rows, list)
        assert len(rows) == 1
        assert isinstance(rows[0], dict)
        row = rows[0]
        assert isinstance(row, dict)
        assert row["run_id"] == run_id
