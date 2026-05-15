"""Tests for theorem-body repair, solve, decompose, and helper reuse."""

import argparse
import json
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from kan_prover.cli.commands import decompose, repair, solve
from kan_prover.engine.config import ToolPaths, WorkspaceBinding, bind_workspace, discover_tool_paths
from kan_prover.engine.models import (
    AgentRequest,
    AgentResponse,
    BackendInvocation,
    BackendResultKind,
    LoopStatus,
    PatchCandidate,
    SandboxStrategy,
)
from kan_prover.engine.source_probing import theorem_span_from_source
from kan_prover.engine.theorem_locator import TheoremLocatorError, locate_theorem_span_from_probe

GOOD_BODY = """by
  exact True.intro
"""

BAD_BODY = """by
  exact False.elim ?missing
"""

HELPER_BODY = """by
  exact helper_truth
"""

RECURSIVE_PLAN = {
    "steps": [
        {
            "step_id": "helper-001",
            "title": "Introduce helper_truth",
            "goal": "Provide the missing lemma used by helper_target.",
            "layer": "missing-lemma",
            "helper_name": "helper_truth",
            "helper_statement": "theorem helper_truth : True :=",
            "helper_proof": "by\n  exact nested_truth\n",
        }
    ]
}

NESTED_PLAN = {
    "steps": [
        {
            "step_id": "helper-002",
            "title": "Introduce nested_truth",
            "goal": "Provide the nested helper used by helper_truth.",
            "layer": "missing-lemma",
            "helper_name": "nested_truth",
            "helper_statement": "theorem nested_truth : True :=",
            "helper_proof": "by\n  exact True.intro\n",
        }
    ]
}

GOOD_PLAN = {
    "steps": [
        {
            "step_id": "helper-001",
            "title": "Introduce helper_truth",
            "goal": "Provide the missing lemma used by helper_target.",
            "layer": "missing-lemma",
            "helper_name": "helper_truth",
            "helper_statement": "theorem helper_truth : True :=",
            "helper_proof": "by\n  exact True.intro\n",
        }
    ]
}

BAD_PLAN = {
    "steps": [
        {
            "step_id": "helper-001",
            "title": "Broken helper",
            "goal": "This proof should fail.",
            "layer": "missing-lemma",
            "helper_name": "helper_truth",
            "helper_statement": "theorem helper_truth : True :=",
            "helper_proof": "by\n  exact False.elim ?missing\n",
        }
    ]
}

REPLACEMENT_PLAN = {
    "steps": [
        {
            "step_id": "helper-002",
            "title": "Replacement helper",
            "goal": "Replace the failed helper without touching the proved one.",
            "layer": "missing-lemma",
            "helper_name": "replacement_truth",
            "helper_statement": "theorem replacement_truth : True :=",
            "helper_proof": "by\n  exact True.intro\n",
        }
    ]
}

pytestmark = pytest.mark.integration


class StubBackend:
    """Small backend stub for explicit response-shape tests."""

    def __init__(self, responses: list[AgentResponse]) -> None:
        self._responses = responses

    def name(self) -> str:
        return "stub"

    def propose(self, request: object) -> AgentResponse:
        attempt_index = cast(AgentRequest, request).attempt_index
        return self._responses[attempt_index - 1]


class TestRepairLoop:
    """Integration tests for the repair and helper-aware solve loop."""

    @classmethod
    def setup_class(cls) -> None:
        cls.tool_paths = discover_tool_paths()
        cls.demo_workspace = bind_workspace(cls.tool_paths.bundled_demo_runtime)
        cls.fixtures = cls.tool_paths.root / "tests" / "fixtures"

    def test_successful_sandboxed_theorem_body_patching(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            original_text = target_file.read_text(encoding="utf-8")
            candidate_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            args = self._repair_args(
                file=target_file,
                workspace=workspace.root,
                theorem="fix_me",
                candidate_files=[candidate_path],
                apply=False,
            )
            with patch(
                "kan_prover.cli.commands.repair.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = repair.run_command(args)
            assert exit_code == 0
            assert result.status == LoopStatus.VERIFIED
            assert result.verified_attempt == 1
            assert not result.applied
            assert result.target_module_path == "Problems/repair_target.lean"
            assert result.helper_module_root == "KanProverGenerated.Helpers"
            assert target_file.read_text(encoding="utf-8") == original_text
            assert result.sandbox.strategy == SandboxStrategy.COPIED_WORKSPACE
            attempt_dir = Path(result.work_dir) / "attempt-001"
            assert (attempt_dir / "candidate.json").is_file()
            assert (attempt_dir / "candidate.body.lean").is_file()
            assert (attempt_dir / "patch.diff").is_file()
            assert (attempt_dir / "backend" / "request.json").is_file()
            assert (attempt_dir / "backend" / "response.json").is_file()
            assert (attempt_dir / "backend" / "retrieval.json").is_file()
            assert (attempt_dir / "backend" / "command.json").is_file()
            assert Path(result.attempts[0].sandbox_file).is_file()
            assert result.attempts[0].target_module_path == "Problems/repair_target.lean"
            assert result.attempts[0].helper_module_root == "KanProverGenerated.Helpers"
            assert Path(result.attempts[0].sandbox_file).is_relative_to(Path(result.sandbox.sandbox_workspace_root))

    def test_repair_request_records_strategy_and_selected_failure_without_breaking_agent_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
            candidate_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            args = self._repair_args(
                file=target_file,
                workspace=workspace.root,
                theorem="helper_target",
                candidate_files=[candidate_path],
                apply=False,
            )
            with patch(
                "kan_prover.cli.commands.repair.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = repair.run_command(args)
            assert exit_code == 0
            assert result.status == LoopStatus.VERIFIED
            request_payload = json.loads(
                (Path(result.work_dir) / "attempt-001" / "backend" / "request.json").read_text(encoding="utf-8")
            )
            assert request_payload["repair_strategy"] == "targeted-correction"
            selected_failure = cast(dict[str, object], request_payload["selected_failure"])
            assert selected_failure["diagnostic_class"] == "unknown-identifier"
            assert selected_failure["identifier"] == "helper_truth"

    def test_ambiguous_theorem_span_rejection(self) -> None:
        source = """theorem dup : True := by
  exact True.intro

theorem dup : True := by
  exact True.intro
"""
        with pytest.raises(TheoremLocatorError):
            probe = theorem_span_from_source(file_path=Path("<memory>"), source=source, theorem="dup")
            locate_theorem_span_from_probe(source=source, theorem_name="dup", probe=probe)

    def test_failed_attempts_do_not_corrupt_working_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            original_text = target_file.read_text(encoding="utf-8")
            candidate_path = self._write_text(temp_root / "bad.body.lean", BAD_BODY)
            args = self._repair_args(
                file=target_file,
                workspace=workspace.root,
                theorem="fix_me",
                candidate_files=[candidate_path],
                apply=True,
            )
            with patch(
                "kan_prover.cli.commands.repair.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = repair.run_command(args)
            assert exit_code == 1
            assert result.status == LoopStatus.FAILED
            assert not result.applied
            assert target_file.read_text(encoding="utf-8") == original_text

    def test_repair_classifies_empty_candidate_response_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            args = self._repair_args(
                file=target_file,
                workspace=workspace.root,
                theorem="fix_me",
                candidate_files=[],
                apply=False,
            )
            backend = StubBackend(
                responses=[
                    self._agent_response(
                        result_kind=BackendResultKind.THEOREM_BODY_REPLACEMENTS,
                        candidates=[],
                        message="backend returned nothing usable",
                    )
                ]
            )
            with (
                patch(
                    "kan_prover.cli.commands.repair.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
                ),
                patch("kan_prover.cli.commands.repair.create_backend", return_value=backend),
            ):
                result, exit_code = repair.run_command(args)
            assert exit_code == 1
            assert result.status == LoopStatus.NO_CANDIDATE
            assert result.attempt_count == 0
            response_payload = json.loads(
                (Path(result.work_dir) / "attempt-001" / "backend" / "response.json").read_text(encoding="utf-8")
            )
            classification = response_payload["theorem_body_classification"]
            assert classification["response_class"] == "no-usable-candidate"
            assert classification["usable_candidate_count"] == 0

    def test_repair_classifies_multiple_usable_candidates_without_picking_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            args = self._repair_args(
                file=target_file,
                workspace=workspace.root,
                theorem="fix_me",
                candidate_files=[],
                apply=False,
            )
            backend = StubBackend(
                responses=[
                    self._agent_response(
                        result_kind=BackendResultKind.THEOREM_BODY_REPLACEMENTS,
                        candidates=[
                            PatchCandidate(
                                candidate_id="candidate-001",
                                theorem="fix_me",
                                body=GOOD_BODY,
                                source="stub",
                            ),
                            PatchCandidate(
                                candidate_id="candidate-002",
                                theorem="fix_me",
                                body=GOOD_BODY,
                                source="stub",
                            ),
                        ],
                    )
                ]
            )
            with (
                patch(
                    "kan_prover.cli.commands.repair.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
                ),
                patch("kan_prover.cli.commands.repair.create_backend", return_value=backend),
            ):
                result, exit_code = repair.run_command(args)
            assert exit_code == 1
            assert result.status == LoopStatus.NO_CANDIDATE
            assert result.attempt_count == 0
            response_payload = json.loads(
                (Path(result.work_dir) / "attempt-001" / "backend" / "response.json").read_text(encoding="utf-8")
            )
            classification = response_payload["theorem_body_classification"]
            assert classification["response_class"] == "multiple-usable-candidates"
            assert classification["usable_candidate_count"] == 2

    def test_repair_recovers_from_partial_candidate_malformed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            args = self._repair_args(
                file=target_file,
                workspace=workspace.root,
                theorem="fix_me",
                candidate_files=[],
                apply=False,
            )
            backend = StubBackend(
                responses=[
                    self._agent_response(
                        result_kind=BackendResultKind.THEOREM_BODY_REPLACEMENTS,
                        candidates=[
                            PatchCandidate(
                                candidate_id="candidate-001",
                                theorem="fix_me",
                                body=GOOD_BODY,
                                source="stub",
                            )
                        ],
                        candidate_parse_errors=["candidate 2: stub candidate is missing `body`"],
                    )
                ]
            )
            with (
                patch(
                    "kan_prover.cli.commands.repair.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
                ),
                patch("kan_prover.cli.commands.repair.create_backend", return_value=backend),
            ):
                result, exit_code = repair.run_command(args)
            assert exit_code == 0
            assert result.status == LoopStatus.VERIFIED
            response_payload = json.loads(
                (Path(result.work_dir) / "attempt-001" / "backend" / "response.json").read_text(encoding="utf-8")
            )
            classification = response_payload["theorem_body_classification"]
            assert classification["response_class"] == "single-usable-candidate"
            assert classification["invalid_candidate_count"] == 1

    def test_apply_only_writes_verified_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            candidate_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            args = self._repair_args(
                file=target_file,
                workspace=workspace.root,
                theorem="fix_me",
                candidate_files=[candidate_path],
                apply=True,
            )
            with patch(
                "kan_prover.cli.commands.repair.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = repair.run_command(args)
            assert exit_code == 0
            assert result.applied
            updated = target_file.read_text(encoding="utf-8")
            assert "exact True.intro" in updated
            assert "exact False.elim ?h" not in updated

    def test_replay_of_stored_patch_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            candidate_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            first_args = self._repair_args(
                file=target_file,
                workspace=workspace.root,
                theorem="fix_me",
                candidate_files=[candidate_path],
                apply=False,
            )
            paths = self._temp_paths(temp_artifacts)
            with patch("kan_prover.cli.commands.repair.discover_tool_paths", return_value=paths):
                first_result, first_exit = repair.run_command(first_args)
            assert first_exit == 0
            replay_candidate = Path(first_result.work_dir) / "attempt-001" / "candidate.json"
            replay_args = self._repair_args(
                file=target_file,
                workspace=workspace.root,
                theorem="fix_me",
                replay_attempts=[replay_candidate],
                apply=False,
            )
            with patch("kan_prover.cli.commands.repair.discover_tool_paths", return_value=paths):
                replay_result, replay_exit = repair.run_command(replay_args)
            assert replay_exit == 0
            assert replay_result.status == LoopStatus.VERIFIED
            assert replay_result.verified_attempt == 1

    def test_solve_reuses_same_loop_with_bounded_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            bad_path = self._write_text(temp_root / "bad.body.lean", BAD_BODY)
            good_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            args = self._solve_args(
                file=target_file,
                workspace=workspace.root,
                theorem="fix_me",
                candidate_files=[bad_path, good_path],
                max_attempts=2,
                apply=False,
            )
            with patch(
                "kan_prover.cli.commands.solve.discover_tool_paths",
                return_value=self._temp_paths(temp_artifacts),
            ):
                result, exit_code = solve.run_command(args)
            assert exit_code == 0
            assert result.status == LoopStatus.VERIFIED
            assert result.attempt_count == 2
            assert result.verified_attempt == 2
            index_rows = [
                json.loads(line)
                for line in (temp_artifacts / "index.jsonl").read_text(encoding="utf-8").splitlines()
                if line
            ]
            assert len(index_rows) == 1
            assert index_rows[0]["command"] == "solve"

    def test_decompose_emits_structured_helper_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
            plan_path = self._write_json(temp_root / "plan.json", GOOD_PLAN)
            args = argparse.Namespace(
                file=str(target_file),
                workspace=str(workspace.root),
                theorem="helper_target",
                agent="none",
                decomposition_file=[str(plan_path)],
                timeout_seconds=60.0,
            )
            with patch(
                "kan_prover.cli.commands.decompose.discover_tool_paths",
                return_value=self._temp_paths(temp_artifacts),
            ):
                result, exit_code = decompose.run_command(args)
            assert exit_code == 0
            assert result.status == LoopStatus.VERIFIED
            assert len(result.steps) == 1
            assert result.target_module_path == "Problems/helper_target.lean"
            assert result.helper_module_root == "KanProverGenerated.Helpers"
            assert result.steps[0].helper_name == "helper_truth"

    def test_helper_sidecar_file_is_created_under_generated_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
            helper_body = self._write_text(temp_root / "helper.body.lean", HELPER_BODY)
            plan_path = self._write_json(temp_root / "plan.json", GOOD_PLAN)
            args = self._solve_args(
                file=target_file,
                workspace=workspace.root,
                theorem="helper_target",
                candidate_files=[helper_body],
                decomposition_files=[plan_path],
                max_attempts=1,
                apply=False,
            )
            paths = self._temp_paths(temp_artifacts)
            with patch("kan_prover.cli.commands.solve.discover_tool_paths", return_value=paths):
                result, exit_code = solve.run_command(args)
            assert exit_code == 0
            assert result.status == LoopStatus.VERIFIED
            assert len(result.proved_helpers) == 1
            helper_path = Path(result.proved_helpers[0].file_path)
            assert helper_path.is_file()
            assert "KanProverGenerated/Helpers" in str(helper_path)
            assert helper_path.is_relative_to(Path(result.sandbox.sandbox_workspace_root))
            assert Path(result.work_dir).parent.joinpath("decompose-001").is_dir()

    def test_proved_helpers_persist_across_iterations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
            helper_body = self._write_text(temp_root / "helper.body.lean", HELPER_BODY)
            plan_path = self._write_json(temp_root / "plan.json", GOOD_PLAN)
            paths = self._temp_paths(temp_artifacts)
            first_args = self._solve_args(
                file=target_file,
                workspace=workspace.root,
                theorem="helper_target",
                candidate_files=[helper_body],
                decomposition_files=[plan_path],
                max_attempts=1,
                apply=False,
            )
            with patch("kan_prover.cli.commands.solve.discover_tool_paths", return_value=paths):
                first_result, first_exit = solve.run_command(first_args)
            assert first_exit == 0
            second_args = self._solve_args(
                file=target_file,
                workspace=workspace.root,
                theorem="helper_target",
                candidate_files=[helper_body],
                max_attempts=1,
                apply=False,
            )
            with patch("kan_prover.cli.commands.solve.discover_tool_paths", return_value=paths):
                second_result, second_exit = solve.run_command(second_args)
            assert second_exit == 0
            assert second_result.status == LoopStatus.VERIFIED
            assert len(second_result.retrieval_hits) >= 1
            assert any(hit.source_kind.value == "proved-helper" for hit in second_result.retrieval_hits)
            assert not any(
                "KanProverGenerated.Helpers" in attempt.verification.target_file for attempt in first_result.attempts
            )

    def test_failed_helpers_can_be_replaced_without_deleting_proved_ones(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
            helper_body = self._write_text(temp_root / "helper.body.lean", HELPER_BODY)
            good_plan_path = self._write_json(temp_root / "good-plan.json", GOOD_PLAN)
            bad_plan_path = self._write_json(temp_root / "bad-plan.json", BAD_PLAN)
            replacement_plan_path = self._write_json(temp_root / "replacement-plan.json", REPLACEMENT_PLAN)
            paths = self._temp_paths(temp_artifacts)
            good_args = self._solve_args(
                file=target_file,
                workspace=workspace.root,
                theorem="helper_target",
                candidate_files=[helper_body],
                decomposition_files=[good_plan_path],
                max_attempts=1,
                apply=False,
            )
            with patch("kan_prover.cli.commands.solve.discover_tool_paths", return_value=paths):
                _good_result, good_exit = solve.run_command(good_args)
            assert good_exit == 0
            failed_args = self._solve_args(
                file=target_file,
                workspace=workspace.root,
                theorem="helper_target",
                candidate_files=[helper_body],
                decomposition_files=[bad_plan_path],
                max_attempts=1,
                apply=False,
            )
            with patch("kan_prover.cli.commands.solve.discover_tool_paths", return_value=paths):
                failed_result, failed_exit = solve.run_command(failed_args)
            assert failed_exit == 0
            assert failed_result.status == LoopStatus.VERIFIED
            replacement_args = self._solve_args(
                file=target_file,
                workspace=workspace.root,
                theorem="helper_target",
                candidate_files=[helper_body],
                decomposition_files=[replacement_plan_path],
                max_attempts=1,
                apply=False,
            )
            with patch("kan_prover.cli.commands.solve.discover_tool_paths", return_value=paths):
                replacement_result, replacement_exit = solve.run_command(replacement_args)
            assert replacement_exit == 0
            helper_names = {
                hit.title for hit in replacement_result.retrieval_hits if hit.source_kind.value == "proved-helper"
            }
            assert "helper_truth" in helper_names

    def test_helper_aware_solve_succeeds_where_direct_repair_stalls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
            helper_body = self._write_text(temp_root / "helper.body.lean", HELPER_BODY)
            plan_path = self._write_json(temp_root / "plan.json", GOOD_PLAN)
            direct_args = self._repair_args(
                file=target_file,
                workspace=workspace.root,
                theorem="helper_target",
                candidate_files=[helper_body],
                apply=False,
            )
            helper_args = self._solve_args(
                file=target_file,
                workspace=workspace.root,
                theorem="helper_target",
                candidate_files=[helper_body],
                decomposition_files=[plan_path],
                max_attempts=1,
                apply=False,
            )
            paths = self._temp_paths(temp_artifacts)
            with patch("kan_prover.cli.commands.repair.discover_tool_paths", return_value=paths):
                direct_result, direct_exit = repair.run_command(direct_args)
            with patch("kan_prover.cli.commands.solve.discover_tool_paths", return_value=paths):
                helper_result, helper_exit = solve.run_command(helper_args)
            assert direct_exit == 1
            assert direct_result.status == LoopStatus.FAILED
            assert helper_exit == 0
            assert helper_result.status == LoopStatus.VERIFIED

    def test_recursive_helper_decomposition_is_bounded_and_records_depth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
            helper_body = self._write_text(temp_root / "helper.body.lean", HELPER_BODY)
            recursive_plan = self._write_json(temp_root / "recursive-plan.json", RECURSIVE_PLAN)
            nested_plan = self._write_json(temp_root / "nested-plan.json", NESTED_PLAN)
            recursive_args = self._solve_args(
                file=target_file,
                workspace=workspace.root,
                theorem="helper_target",
                candidate_files=[helper_body],
                decomposition_files=[recursive_plan, nested_plan],
                max_attempts=1,
                max_decomposition_depth=2,
                apply=False,
            )
            single_level_args = self._solve_args(
                file=target_file,
                workspace=workspace.root,
                theorem="helper_target",
                candidate_files=[helper_body],
                decomposition_files=[recursive_plan, nested_plan],
                max_attempts=1,
                max_decomposition_depth=1,
                apply=False,
            )
            paths = self._temp_paths(temp_artifacts)
            with patch("kan_prover.cli.commands.solve.discover_tool_paths", return_value=paths):
                recursive_result, recursive_exit = solve.run_command(recursive_args)
            with patch("kan_prover.cli.commands.solve.discover_tool_paths", return_value=paths):
                single_level_result, single_level_exit = solve.run_command(single_level_args)
            assert recursive_exit == 0
            assert recursive_result.status == LoopStatus.VERIFIED
            assert recursive_result.max_decomposition_depth == 2
            assert recursive_result.decomposition_count == 2
            depths = {helper.helper_name: helper.decomposition_depth for helper in recursive_result.proved_helpers}
            assert depths["helper_truth"] == 1
            assert depths["nested_truth"] == 2
            result_payload = json.loads(
                (Path(recursive_result.artifact_dir) / "result.json").read_text(encoding="utf-8")
            )
            assert result_payload["max_decomposition_depth"] == 2
            stored_helpers = json.loads(
                (Path(recursive_result.artifact_dir) / "context" / "proved-helpers.json").read_text(encoding="utf-8")
            )
            assert {helper["helper_name"]: helper["decomposition_depth"] for helper in stored_helpers} == {
                "nested_truth": 2,
                "helper_truth": 1,
            }
            assert single_level_exit == 1
            assert single_level_result.status == LoopStatus.FAILED

    def test_apply_only_writes_target_and_required_helper_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
            before = self._workspace_snapshot(workspace.root)
            helper_body = self._write_text(temp_root / "helper.body.lean", HELPER_BODY)
            plan_path = self._write_json(temp_root / "plan.json", GOOD_PLAN)
            args = self._solve_args(
                file=target_file,
                workspace=workspace.root,
                theorem="helper_target",
                candidate_files=[helper_body],
                decomposition_files=[plan_path],
                max_attempts=1,
                apply=True,
            )
            with patch(
                "kan_prover.cli.commands.solve.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = solve.run_command(args)
            assert exit_code == 0
            assert result.applied
            helper_file = (
                workspace.root
                / "KanProverGenerated"
                / "Helpers"
                / "Problems"
                / "HelperTarget"
                / "HelperTarget"
                / "HelperTruth.lean"
            )
            assert helper_file.is_file()
            assert (
                "import KanProverGenerated.Helpers.Problems.HelperTarget.HelperTarget.HelperTruth"
                in target_file.read_text(encoding="utf-8")
            )
            after = self._workspace_snapshot(workspace.root)
            changed = {path for path, content in after.items() if before.get(path) != content}
            assert changed == {
                target_file.resolve().relative_to(workspace.root.resolve()),
                helper_file.resolve().relative_to(workspace.root.resolve()),
            }

    def test_helper_imports_resolve_from_sandbox_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
            helper_body = self._write_text(temp_root / "helper.body.lean", HELPER_BODY)
            plan_path = self._write_json(temp_root / "plan.json", GOOD_PLAN)
            args = self._solve_args(
                file=target_file,
                workspace=workspace.root,
                theorem="helper_target",
                candidate_files=[helper_body],
                decomposition_files=[plan_path],
                max_attempts=1,
                apply=False,
            )
            with patch(
                "kan_prover.cli.commands.solve.discover_tool_paths",
                return_value=self._temp_paths(temp_artifacts),
            ):
                result, exit_code = solve.run_command(args)
            assert exit_code == 0
            sandbox_target = Path(result.attempts[0].sandbox_file)
            assert "import KanProverGenerated.Helpers." in sandbox_target.read_text(encoding="utf-8")
            assert Path(result.proved_helpers[0].file_path).is_relative_to(Path(result.sandbox.sandbox_workspace_root))
            assert str(self.tool_paths.bundled_demo_runtime) not in result.proved_helpers[0].file_path

    def _repair_args(
        self,
        *,
        file: Path,
        workspace: Path,
        theorem: str,
        candidate_files: list[Path] | None = None,
        replay_attempts: list[Path] | None = None,
        decomposition_files: list[Path] | None = None,
        max_decomposition_depth: int = 2,
        apply: bool,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            file=str(file),
            workspace=str(workspace),
            theorem=theorem,
            agent="none",
            candidate_file=[str(path) for path in candidate_files or []],
            replay_attempt=[str(path) for path in replay_attempts or []],
            decomposition_file=[str(path) for path in decomposition_files or []],
            timeout_seconds=60.0,
            max_decomposition_depth=max_decomposition_depth,
            apply=apply,
        )

    def _solve_args(
        self,
        *,
        file: Path,
        workspace: Path,
        theorem: str,
        candidate_files: list[Path] | None = None,
        replay_attempts: list[Path] | None = None,
        decomposition_files: list[Path] | None = None,
        max_attempts: int,
        max_decomposition_depth: int = 2,
        apply: bool,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            file=str(file),
            workspace=str(workspace),
            theorem=theorem,
            agent="none",
            candidate_file=[str(path) for path in candidate_files or []],
            replay_attempt=[str(path) for path in replay_attempts or []],
            decomposition_file=[str(path) for path in decomposition_files or []],
            timeout_seconds=60.0,
            max_attempts=max_attempts,
            max_decomposition_depth=max_decomposition_depth,
            apply=apply,
        )

    def _copy_fixture(self, name: str, destination: Path) -> Path:
        source = self.fixtures / name
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return destination

    def _workspace_with_fixture(self, temp_root: Path, fixture_name: str) -> tuple[WorkspaceBinding, Path]:
        workspace_root = self._make_workspace_root(temp_root / "workspace")
        target_file = self._copy_fixture(fixture_name, workspace_root / "Problems" / fixture_name)
        return bind_workspace(workspace_root), target_file

    def _workspace_snapshot(self, workspace_root: Path) -> dict[Path, str]:
        snapshot: dict[Path, str] = {}
        for path in sorted(workspace_root.rglob("*")):
            if path.is_file() and path.suffix == ".lean":
                snapshot[path.relative_to(workspace_root)] = path.read_text(encoding="utf-8")
        return snapshot

    def _make_workspace_root(self, workspace_root: Path) -> Path:
        workspace_root.mkdir(parents=True, exist_ok=True)
        for name in ["lakefile.toml", "lake-manifest.json", "lean-toolchain", "KanRuntime.lean"]:
            (workspace_root / name).symlink_to(self.demo_workspace.root / name)
        for name in [".lake", "Tools"]:
            (workspace_root / name).symlink_to(self.demo_workspace.root / name, target_is_directory=True)
        (workspace_root / "Problems").mkdir(parents=True, exist_ok=True)
        return workspace_root

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

    def _agent_response(
        self,
        *,
        result_kind: BackendResultKind,
        candidates: list[PatchCandidate],
        message: str | None = None,
        candidate_parse_errors: list[str] | None = None,
    ) -> AgentResponse:
        return AgentResponse(
            backend="stub",
            result_kind=result_kind,
            invocation=BackendInvocation(argv=["stub"], cwd="."),
            prompt_path="",
            prompt_text="",
            schema_path="",
            raw_stdout="",
            raw_stderr="",
            raw_output="",
            parsed_payload={"result_kind": result_kind.value},
            candidates=candidates,
            candidate_parse_errors=candidate_parse_errors or [],
            message=message,
        )

    def _write_text(self, path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        return path

    def _write_json(self, path: Path, payload: object) -> Path:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path
