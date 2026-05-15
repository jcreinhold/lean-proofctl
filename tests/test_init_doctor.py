"""Tests for portable workspace bootstrap and readiness diagnostics."""

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from kan_prover.cli.commands import doctor, init
from kan_prover.engine.config import bind_workspace, discover_tool_paths
from kan_prover.engine.models import DoctorCheckStatus, DoctorStatus


class TestInitDoctor:
    """Contract tests for `init` and `doctor` after the workspace split."""

    @classmethod
    def setup_class(cls) -> None:
        cls.tool_paths = discover_tool_paths()
        cls.demo_workspace = bind_workspace(cls.tool_paths.bundled_demo_runtime)

    @pytest.mark.integration
    def test_doctor_reports_healthy_external_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = self._make_workspace_root(Path(temp_dir) / "workspace")
            args = argparse.Namespace(
                workspace=str(workspace_root),
                demo=False,
                timeout_seconds=20.0,
                json=False,
                jsonl=False,
            )
            result, exit_code = doctor.run_command(args)
        assert exit_code == 0
        assert result.status == DoctorStatus.READY
        assert result.ready
        assert result.workspace_role == "external-workspace"
        assert result.selected_via == "explicit-workspace"
        checks = {check.name: check for check in result.checks}
        assert checks["workspace-detection"].status == DoctorCheckStatus.PASS
        assert checks["sandbox"].status == DoctorCheckStatus.PASS
        assert checks["semantic-probes"].status in {DoctorCheckStatus.PASS, DoctorCheckStatus.WARN}
        assert checks["backend-none"].status == DoctorCheckStatus.PASS
        assert checks["backend-claude"].status in {DoctorCheckStatus.PASS, DoctorCheckStatus.WARN}
        assert checks["backend-codex"].status in {DoctorCheckStatus.PASS, DoctorCheckStatus.WARN}

    def test_doctor_reports_missing_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_root = Path(temp_dir) / "missing-workspace"
            args = argparse.Namespace(
                workspace=str(missing_root),
                demo=False,
                timeout_seconds=5.0,
                json=False,
                jsonl=False,
            )
            result, exit_code = doctor.run_command(args)
        assert exit_code == 1
        assert result.status == DoctorStatus.NOT_READY
        assert result.workspace_root is None
        checks = {check.name: check for check in result.checks}
        assert checks["workspace-detection"].status == DoctorCheckStatus.FAIL
        assert checks["workspace-directory"].status == DoctorCheckStatus.FAIL
        assert "Could not detect a Lake workspace" in checks["workspace-detection"].summary

    def test_doctor_reports_specific_failure_for_missing_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir) / "workspace"
            workspace_root.mkdir(parents=True, exist_ok=True)
            (workspace_root / "lakefile.toml").write_text('name = "Broken"\n', encoding="utf-8")
            args = argparse.Namespace(
                workspace=str(workspace_root),
                demo=False,
                timeout_seconds=5.0,
                json=False,
                jsonl=False,
            )
            result, exit_code = doctor.run_command(args)
        assert exit_code == 1
        checks = {check.name: check for check in result.checks}
        assert checks["lean-toolchain"].status == DoctorCheckStatus.FAIL
        assert "Missing" in checks["lean-toolchain"].summary
        assert checks["lean-env"].status == DoctorCheckStatus.SKIP

    @pytest.mark.integration
    def test_doctor_supports_bundled_demo_workspace_explicitly(self) -> None:
        args = argparse.Namespace(
            workspace=None,
            demo=True,
            timeout_seconds=20.0,
            json=False,
            jsonl=False,
        )
        result, exit_code = doctor.run_command(args)
        assert exit_code == 0
        assert result.status == DoctorStatus.READY
        assert result.workspace_role == "bundled-demo"
        assert result.selected_via == "bundled-demo"
        checks = {check.name: check for check in result.checks}
        assert checks["workspace-role"].status == DoctorCheckStatus.PASS
        assert "Bundled demo workspace selected" in checks["workspace-role"].summary

    @pytest.mark.integration
    def test_doctor_fails_when_requested_hosted_backend_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = self._make_workspace_root(Path(temp_dir) / "workspace")
            args = argparse.Namespace(
                workspace=str(workspace_root),
                demo=False,
                timeout_seconds=20.0,
                json=False,
                jsonl=False,
                agent=["codex"],
            )
            real_which = shutil.which
            with patch(
                "kan_prover.engine.doctor.shutil.which",
                side_effect=lambda command: None if command == "codex" else real_which(command),
            ):
                result, exit_code = doctor.run_command(args)
        assert exit_code == 1
        assert result.status == DoctorStatus.NOT_READY
        checks = {check.name: check for check in result.checks}
        assert checks["backend-codex"].status == DoctorCheckStatus.FAIL
        assert "not available on PATH" in checks["backend-codex"].summary

    @pytest.mark.integration
    def test_doctor_fails_when_requested_hosted_backend_cli_is_broken(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = self._make_workspace_root(Path(temp_dir) / "workspace")
            args = argparse.Namespace(
                workspace=str(workspace_root),
                demo=False,
                timeout_seconds=20.0,
                json=False,
                jsonl=False,
                agent=["codex"],
            )
            real_which = shutil.which
            real_run = subprocess.run

            def fake_which(command: str) -> str | None:
                if command == "codex":
                    return "/usr/bin/codex"
                return real_which(command)

            def fake_run(
                argv: list[str],
                /,
                *,
                capture_output: bool,
                text: bool,
                check: bool,
                timeout: float | None = None,
                cwd: Path | None = None,
            ) -> subprocess.CompletedProcess[str]:
                if argv and argv[0] == "/usr/bin/codex":
                    return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="bad startup")
                return cast(
                    subprocess.CompletedProcess[str],
                    real_run(
                        argv,
                        capture_output=capture_output,
                        text=text,
                        check=check,
                        timeout=timeout,
                        cwd=cwd,
                    ),
                )

            with (
                patch("kan_prover.engine.doctor.shutil.which", side_effect=fake_which),
                patch("kan_prover.engine.doctor.subprocess.run", side_effect=fake_run),
            ):
                result, exit_code = doctor.run_command(args)
        assert exit_code == 1
        assert result.status == DoctorStatus.NOT_READY
        checks = {check.name: check for check in result.checks}
        assert checks["backend-codex"].status == DoctorCheckStatus.FAIL
        assert "failed its readiness probe" in checks["backend-codex"].summary

    def test_init_bootstraps_explicit_external_workspace_without_implying_demo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = self._make_workspace_root(Path(temp_dir) / "workspace")
            args = argparse.Namespace(
                workspace=str(workspace_root),
                demo=False,
                skip_build=True,
                timeout_seconds=20.0,
                json=False,
                jsonl=False,
            )
            result, exit_code = init.run_command(args)
        assert exit_code == 0
        assert result.workspace_root == str(workspace_root)
        assert result.workspace_role == "external-workspace"
        assert result.selected_via == "explicit-workspace"
        assert not result.build_ran

    def test_init_infers_workspace_from_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = self._make_workspace_root(Path(temp_dir) / "workspace")
            args = argparse.Namespace(
                workspace=None,
                demo=False,
                skip_build=True,
                timeout_seconds=20.0,
                json=False,
                jsonl=False,
            )
            with patch("kan_prover.cli.commands.init.Path.cwd", return_value=workspace_root):
                result, exit_code = init.run_command(args)
        assert exit_code == 0
        assert result.workspace_root == str(workspace_root)
        assert result.workspace_role == "external-workspace"
        assert result.selected_via == "cwd-inference"

    def test_init_reports_semantic_probe_build_failure_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = self._make_workspace_root(Path(temp_dir) / "workspace")
            args = argparse.Namespace(
                workspace=str(workspace_root),
                demo=False,
                skip_build=False,
                timeout_seconds=20.0,
                json=False,
                jsonl=False,
            )
            with (
                patch(
                    "kan_prover.cli.commands.init.build_semantic_probe_executable",
                    return_value=subprocess.CompletedProcess(
                        args=["lake", "build", "SemanticProbe"],
                        returncode=1,
                        stdout="probe stdout",
                        stderr="probe stderr",
                    ),
                ),
                patch("kan_prover.cli.commands.init.run_lake_build") as run_lake_build,
            ):
                result, exit_code = init.run_command(args)
        assert exit_code == 4
        assert result.build_ran
        assert "[semantic-probe]" in result.build_stdout
        assert "probe stdout" in result.build_stdout
        assert "[semantic-probe]" in result.build_stderr
        assert "probe stderr" in result.build_stderr
        run_lake_build.assert_not_called()

    def _make_workspace_root(self, workspace_root: Path) -> Path:
        workspace_root.mkdir(parents=True, exist_ok=True)
        for name in ("lakefile.toml", "lean-toolchain", "lake-manifest.json", "KanRuntime.lean"):
            (workspace_root / name).symlink_to(self.demo_workspace.root / name)
        (workspace_root / ".lake").symlink_to(self.demo_workspace.root / ".lake", target_is_directory=True)
        (workspace_root / "Problems").mkdir(parents=True, exist_ok=True)
        (workspace_root / "Tools").mkdir(parents=True, exist_ok=True)
        return workspace_root.resolve()
