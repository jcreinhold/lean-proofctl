"""Tests for sandbox-backed theorem sessions."""

import argparse
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kan_prover.cli.commands import session, show
from kan_prover.cli.main import _render_show_session_report
from kan_prover.engine.config import ToolPaths, discover_tool_paths
from kan_prover.engine.models import SessionStatus

GOOD_BODY = """by
  exact True.intro
"""

pytestmark = pytest.mark.integration


class TestSessionCommand:
    """Integration tests for theorem-session lifecycle commands."""

    @classmethod
    def setup_class(cls) -> None:
        cls.tool_paths = discover_tool_paths()
        cls.fixtures = cls.tool_paths.root / "tests" / "fixtures"

    def test_session_start_creates_sandbox_backed_state_without_touching_real_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            original_text = target_file.read_text(encoding="utf-8")
            args = argparse.Namespace(
                file=str(target_file),
                workspace=str(workspace_root),
                theorem="fix_me",
                source_run_id=None,
                timeout_seconds=60.0,
            )
            with patch(
                "kan_prover.cli.commands.session.discover_tool_paths",
                return_value=self._temp_paths(temp_artifacts),
            ):
                result, exit_code = session.run_start(args)
            assert exit_code == 0
            assert result.status == SessionStatus.ACTIVE
            assert result.session_id
            assert Path(result.session_artifact_dir).is_dir()
            assert target_file.read_text(encoding="utf-8") == original_text
            session_payload = json.loads(
                (Path(result.session_artifact_dir) / "session.json").read_text(encoding="utf-8")
            )
            assert session_payload["current_node_id"] == result.current_node_id
            assert session_payload["semantic_session"]["mode"] == "one-shot"
            assert session_payload["current_state"]["semantic_session"]["mode"] == "one-shot"
            assert session_payload["current_state"]["semantic_probe_results"]["context-bundle"]["status"] == "ok"
            assert "proof_state" in session_payload["current_state"]
            assert session_payload["current_state"]["semantic_probe_results"]["proof-state"]["status"] == "ok"
            assert session_payload["current_state"]["pretty_proof_state"]
            assert len(session_payload["nodes"]) == 1

    def test_session_step_goals_checkpoint_and_finish_persist_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            original_text = target_file.read_text(encoding="utf-8")
            body_file = temp_root / "good.body.lean"
            body_file.write_text(GOOD_BODY, encoding="utf-8")

            with patch(
                "kan_prover.cli.commands.session.discover_tool_paths",
                return_value=self._temp_paths(temp_artifacts),
            ):
                start_result, start_exit = session.run_start(
                    argparse.Namespace(
                        file=str(target_file),
                        workspace=str(workspace_root),
                        theorem="fix_me",
                        source_run_id="run-123",
                        timeout_seconds=60.0,
                    )
                )
                assert start_exit == 0
                step_result, step_exit = session.run_step(
                    argparse.Namespace(
                        session_id=start_result.session_id,
                        body_file=str(body_file),
                        timeout_seconds=60.0,
                    )
                )
                goals_result, goals_exit = session.run_goals(
                    argparse.Namespace(session_id=start_result.session_id, timeout_seconds=60.0)
                )
                _checkpoint_result, checkpoint_exit = session.run_checkpoint(
                    argparse.Namespace(session_id=start_result.session_id, label="after-good-step")
                )
                finish_result, finish_exit = session.run_finish(
                    argparse.Namespace(session_id=start_result.session_id, timeout_seconds=60.0)
                )

            assert step_exit == 0
            assert goals_exit == 0
            assert checkpoint_exit == 0
            assert finish_exit == 0
            assert step_result.diagnostic_count == 0
            assert goals_result.diagnostic_count == 0
            assert finish_result.status == SessionStatus.FINISHED_VERIFIED
            assert target_file.read_text(encoding="utf-8") == original_text

            session_dir = Path(finish_result.session_artifact_dir)
            session_payload = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
            assert session_payload["source_run_id"] == "run-123"
            assert session_payload["status"] == SessionStatus.FINISHED_VERIFIED.value
            assert len(session_payload["nodes"]) == 4
            assert session_payload["nodes"][0]["snapshot"]["semantic_session"]["mode"] == "one-shot"
            assert session_payload["nodes"][1]["snapshot"]["semantic_session"]["mode"] == "live"
            assert session_payload["nodes"][2]["snapshot"]["semantic_session"]["mode"] == "live"
            assert session_payload["nodes"][3]["snapshot"]["semantic_session"]["mode"] == "live"
            assert session_payload["nodes"][1]["parent_node_id"] == session_payload["nodes"][0]["node_id"]
            assert session_payload["nodes"][1]["check"]["diagnostic_count"] == 0
            assert "proof_state" in session_payload["nodes"][1]["snapshot"]
            assert session_payload["checkpoints"][0]["label"] == "after-good-step"
            assert [entry["action"] for entry in session_payload["action_history"]] == [
                "start",
                "step",
                "goals",
                "checkpoint",
                "finish",
            ]
            assert (session_dir / "nodes" / session_payload["nodes"][1]["node_id"] / "theorem-body.lean").is_file()

            index_rows = [
                json.loads(line)
                for line in (temp_artifacts / "index.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            session_rows = [row for row in index_rows if row.get("session_id") == start_result.session_id]
            assert [row["action"] for row in session_rows] == [
                "start",
                "step",
                "goals",
                "checkpoint",
                "finish",
            ]

    def test_show_session_renders_current_or_selected_stored_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            body_file = temp_root / "good.body.lean"
            body_file.write_text(GOOD_BODY, encoding="utf-8")

            with patch(
                "kan_prover.cli.commands.session.discover_tool_paths",
                return_value=self._temp_paths(temp_artifacts),
            ):
                start_result, _start_exit = session.run_start(
                    argparse.Namespace(
                        file=str(target_file),
                        workspace=str(workspace_root),
                        theorem="fix_me",
                        source_run_id=None,
                        timeout_seconds=60.0,
                    )
                )
                _step_result, _step_exit = session.run_step(
                    argparse.Namespace(
                        session_id=start_result.session_id,
                        body_file=str(body_file),
                        timeout_seconds=60.0,
                    )
                )
                _finish_result, _finish_exit = session.run_finish(
                    argparse.Namespace(session_id=start_result.session_id, timeout_seconds=60.0)
                )

            with patch(
                "kan_prover.cli.commands.show.discover_tool_paths",
                return_value=self._temp_paths(temp_artifacts),
            ):
                result, exit_code = show.run_show_session(
                    argparse.Namespace(session_id=start_result.session_id, node_id=None, node_index=2)
                )

            assert exit_code == 0
            assert result.source_session_id == start_result.session_id
            assert result.session_status == SessionStatus.FINISHED_VERIFIED.value
            assert result.selected.index == 2
            assert result.selected.action == "step"
            assert result.selected.goal_count >= 1
            probe_names = {probe.probe for probe in result.selected.semantic_probes}
            assert "proof-state" in probe_names
            rendered = _render_show_session_report(result)
            assert "Nodes:" in rendered
            assert "Selected node:" in rendered

    def _workspace_with_fixture(self, temp_root: Path, fixture_name: str) -> tuple[Path, Path]:
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
        target_file = workspace_root / "Problems" / fixture_name
        target_file.write_text((self.fixtures / fixture_name).read_text(encoding="utf-8"), encoding="utf-8")
        return workspace_root.resolve(), target_file.resolve()

    def _temp_paths(self, artifacts_root: Path) -> ToolPaths:
        return ToolPaths(
            root=self.tool_paths.root,
            prompts=self.tool_paths.prompts,
            artifacts=artifacts_root,
            artifact_runs=artifacts_root / "runs",
            artifact_index=artifacts_root / "index.jsonl",
            artifact_sessions=artifacts_root / "sessions",
            bundled_demo_runtime=self.tool_paths.bundled_demo_runtime,
            packaged_probe_sources=self.tool_paths.packaged_probe_sources,
        )
