"""Tests for `proofctl resume` and `proofctl apply`."""

import argparse
import json
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from lean_proofctl.cli.commands import apply, prove, resume
from lean_proofctl.engine.config import ToolPaths, WorkspaceBinding, bind_workspace, discover_tool_paths
from lean_proofctl.engine.models import ApplyErrorCode, ApplyStatus, JsonValue, ProveResult, ProveStatus, ResumeStatus
from lean_proofctl.engine.proofctl import stored_proof_target_contract_error
from lean_proofctl.engine.replay import RunBundle
from lean_proofctl.engine.sandbox import helper_module_relative_path

GOOD_BODY = """by
  exact True.intro
"""

pytestmark = pytest.mark.integration

BAD_STEP = {
    "actions": [
        {
            "action_id": "action-001",
            "kind": "append_tactic_text",
            "tactic_text": "  exact False.elim ?h2",
            "rationale": "Stay in the failing branch so resume has frontier work to continue.",
        }
    ]
}

HELPER_ACTION = {
    "actions": [
        {
            "action_id": "action-001",
            "kind": "propose_helper_lemma",
            "helper": {
                "helper_name": "helper_truth",
                "helper_statement": "theorem helper_truth : True :=",
                "proof_stub": "by\n  exact True.intro\n",
            },
            "rationale": "Prove the missing helper first.",
        }
    ]
}


class TestResumeApply:
    """Regression coverage for stored proof-graph workflows."""

    @classmethod
    def setup_class(cls) -> None:
        cls.tool_paths = discover_tool_paths()
        cls.fixtures = cls.tool_paths.root / "tests" / "fixtures"

    def test_resume_continues_incomplete_prove_from_saved_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            step_path = self._write_json(temp_root / "bad-step.json", BAD_STEP)
            source_run_id, source_result = self._run_prove(
                temp_artifacts=temp_artifacts,
                workspace=workspace,
                target_file=target_file,
                theorem="fix_me",
                candidate_files=[],
                theorem_step_files=[step_path],
                max_nodes=2,
            )
            assert source_result.status == ProveStatus.UNSOLVED
            assert source_result.graph.frontier_node_ids == ["prove-node-001-state-search"]

            candidate_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            args = argparse.Namespace(
                run_id=source_run_id,
                workspace=str(workspace.root),
                agent="none",
                candidate_file=[str(candidate_path)],
                replay_attempt=[],
                plan_file=[],
                theorem_step_file=[],
                node_id=None,
                graph_index=None,
                timeout_seconds=60.0,
                max_nodes=6,
            )
            with patch(
                "lean_proofctl.cli.commands.resume.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = resume.run_command(args)

            assert exit_code == 0
            assert result.status == ResumeStatus.VERIFIED
            assert result.source_run_id == source_run_id
            assert result.source_node_id is None
            assert result.graph.verified_node_id is not None
            assert result.graph.node_count > source_result.graph.node_count

    def test_resume_selected_node_continues_incomplete_prove(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            step_path = self._write_json(temp_root / "bad-step.json", BAD_STEP)
            source_run_id, source_result = self._run_prove(
                temp_artifacts=temp_artifacts,
                workspace=workspace,
                target_file=target_file,
                theorem="fix_me",
                candidate_files=[],
                theorem_step_files=[step_path],
                max_nodes=2,
            )
            candidate_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            selected_node_id = source_result.graph.frontier_node_ids[0]
            args = argparse.Namespace(
                run_id=source_run_id,
                workspace=str(workspace.root),
                agent="none",
                candidate_file=[str(candidate_path)],
                replay_attempt=[],
                plan_file=[],
                theorem_step_file=[],
                node_id=selected_node_id,
                graph_index=None,
                timeout_seconds=60.0,
                max_nodes=6,
            )
            with patch(
                "lean_proofctl.cli.commands.resume.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = resume.run_command(args)

            assert exit_code == 0
            assert result.status == ResumeStatus.VERIFIED
            assert result.source_node_id == selected_node_id
            assert result.graph.verified_node_id is not None

    def test_apply_transplants_verified_theorem_and_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
            step_path = self._write_json(temp_root / "helper-step.json", HELPER_ACTION)
            source_run_id, source_result = self._run_prove(
                temp_artifacts=temp_artifacts,
                workspace=workspace,
                target_file=target_file,
                theorem="helper_target",
                candidate_files=[],
                theorem_step_files=[step_path],
                max_nodes=6,
            )
            helper = source_result.helpers[-1]
            args = argparse.Namespace(
                run_id=source_run_id,
                workspace=str(workspace.root),
                node_id=source_result.graph.verified_node_id,
                graph_index=None,
            )
            with patch(
                "lean_proofctl.cli.commands.apply.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = apply.run_command(args)

            assert exit_code == 0
            assert result.status == ApplyStatus.APPLIED
            assert result.source_node_id == source_result.graph.verified_node_id
            updated = target_file.read_text(encoding="utf-8")
            assert f"import {helper.module_name}" in updated
            assert "exact helper_truth" in updated
            helper_path = (
                workspace.root
                / "ProofctlGenerated"
                / "Helpers"
                / helper_module_relative_path(module_name=helper.module_name)
            )
            assert helper_path.is_file()
            assert "theorem helper_truth : True :=" in helper_path.read_text(encoding="utf-8")

    def test_apply_rejects_ambiguous_verified_targets_with_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            candidate_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            source_run_id, source_result = self._run_prove(
                temp_artifacts=temp_artifacts,
                workspace=workspace,
                target_file=target_file,
                theorem="fix_me",
                candidate_files=[candidate_path],
                theorem_step_files=[],
                max_nodes=6,
            )
            run_dir = temp_artifacts / "runs" / source_run_id
            payload = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
            verified_node = next(
                node for node in payload["nodes"] if node["node_id"] == source_result.graph.verified_node_id
            )
            duplicate = dict(verified_node)
            duplicate["node_id"] = "prove-node-999-duplicate"
            duplicate["graph_index"] = 999
            payload["nodes"].append(duplicate)
            (run_dir / "result.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            args = argparse.Namespace(
                run_id=source_run_id, workspace=str(workspace.root), node_id=None, graph_index=None
            )
            with patch(
                "lean_proofctl.cli.commands.apply.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = apply.run_command(args)

            assert exit_code == 2
            assert result.status == ApplyStatus.ERROR
            assert result.error_code == ApplyErrorCode.AMBIGUOUS_VERIFIED_NODE

    def test_apply_rejects_conflicting_helper_target_with_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
            step_path = self._write_json(temp_root / "helper-step.json", HELPER_ACTION)
            source_run_id, source_result = self._run_prove(
                temp_artifacts=temp_artifacts,
                workspace=workspace,
                target_file=target_file,
                theorem="helper_target",
                candidate_files=[],
                theorem_step_files=[step_path],
                max_nodes=6,
            )
            helper = source_result.helpers[-1]
            helper_path = (
                workspace.root
                / "ProofctlGenerated"
                / "Helpers"
                / helper_module_relative_path(module_name=helper.module_name)
            )
            helper_path.parent.mkdir(parents=True, exist_ok=True)
            helper_path.write_text("theorem helper_truth : True := by\n  exact False.elim ?boom\n", encoding="utf-8")

            args = argparse.Namespace(
                run_id=source_run_id,
                workspace=str(workspace.root),
                node_id=source_result.graph.verified_node_id,
                graph_index=None,
            )
            with patch(
                "lean_proofctl.cli.commands.apply.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = apply.run_command(args)

            assert exit_code == 1
            assert result.status == ApplyStatus.ERROR
            assert result.error_code == ApplyErrorCode.HELPER_CONFLICT
            assert result.error_message is not None
            assert "different contents" in result.error_message

    def test_apply_checks_stored_proof_target_contract_from_request_payload(self) -> None:
        bundle = RunBundle(
            run_id="run-123",
            run_dir=Path("/tmp/run-123"),
            manifest={"command": "prove"},
            request={
                "command": "prove",
                "proof_target": {
                    "module_path": "Problems.repair_target",
                    "short_name": "fix_me",
                    "declaration_name": "Problems.fix_me",
                    "namespace": None,
                    "namespace_stack": [],
                    "imports": [],
                    "keyword": "theorem",
                    "modifiers": [],
                    "group": {"kind": "standalone"},
                    "declaration_span": {
                        "start": {"line": 1, "column": 1},
                        "end": {"line": 3, "column": 1},
                    },
                    "name_span": {
                        "start": {"line": 1, "column": 9},
                        "end": {"line": 1, "column": 15},
                    },
                    "body_span": {
                        "start": {"line": 2, "column": 1},
                        "end": {"line": 3, "column": 1},
                    },
                    "safe_edit": None,
                    "proof_state_available": False,
                },
            },
            result={},
        )
        error = stored_proof_target_contract_error(
            command="proofctl apply",
            proof_target=cast(JsonValue, bundle.request["proof_target"]),
            payload=None,
        )
        assert error is not None
        assert "safe-edit boundary" in error

    def _run_prove(
        self,
        *,
        temp_artifacts: Path,
        workspace: WorkspaceBinding,
        target_file: Path,
        theorem: str,
        candidate_files: list[Path],
        theorem_step_files: list[Path],
        max_nodes: int,
    ) -> tuple[str, ProveResult]:
        args = argparse.Namespace(
            file=str(target_file),
            workspace=str(workspace.root),
            theorem=theorem,
            agent="none",
            candidate_file=[str(path) for path in candidate_files],
            replay_attempt=[],
            plan_file=[],
            theorem_step_file=[str(path) for path in theorem_step_files],
            timeout_seconds=60.0,
            max_nodes=max_nodes,
            json=False,
            jsonl=False,
        )
        with patch(
            "lean_proofctl.cli.commands.prove.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
        ):
            result, exit_code = prove.run_command(args)
        assert exit_code == 0 if result.status is ProveStatus.VERIFIED else 1
        return result.run_id, result

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

    def _workspace_with_fixture(self, temp_root: Path, fixture_name: str) -> tuple[WorkspaceBinding, Path]:
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
        return bind_workspace(workspace_root), target_file

    def _write_text(self, path: Path, contents: str) -> Path:
        path.write_text(contents, encoding="utf-8")
        return path

    def _write_json(self, path: Path, payload: object) -> Path:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path
