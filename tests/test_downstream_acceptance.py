"""Real downstream acceptance coverage for workspace-aware `kan-prover`."""

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from kan_prover.cli.commands import (
    apply,
    bench,
    check,
    inspect,
    plan,
    prove,
    repair,
    replay,
    resume,
    retrieve,
    session,
    show,
    trace,
)
from kan_prover.engine.config import ToolPaths, discover_tool_paths
from kan_prover.engine.models import (
    ApplyResult,
    ApplyStatus,
    BatchStatus,
    BenchResult,
    CheckResult,
    CheckStatus,
    InspectionStatus,
    InspectResult,
    JsonValue,
    LoopResult,
    LoopStatus,
    PlanResult,
    PlanStatus,
    ProveResult,
    ProveStatus,
    ReplayMode,
    ReplayResult,
    ResumeResult,
    ResumeStatus,
    RetrieveResult,
    SandboxStrategy,
    SessionActionResult,
    SessionStatus,
    ShowRunResult,
    ShowSessionResult,
    TraceResult,
)
from kan_prover.engine.proofctl import (
    ProbeStatus,
    UnsupportedTargetError,
    imports_and_namespace,
    theorem_span,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.acceptance]


class TestDownstreamAcceptance:
    """Explicit acceptance tests for real downstream workspaces."""

    @classmethod
    def setup_class(cls) -> None:
        cls.tool_paths = discover_tool_paths()
        cls.repo_root = cls.tool_paths.root.parent.parent
        cls.acceptance_dir = cls.tool_paths.root / "tests" / "acceptance"
        cls.sample_workspace_fixture = cls.acceptance_dir / "sample_workspace"
        cls.repo_target = cls.repo_root / "KanProofs" / "Algebra" / "ControlFlowGrades.lean"
        cls.repo_private_target = cls.repo_root / "KanProofs" / "Algebra" / "Types.lean"
        cls.repo_candidate = cls.acceptance_dir / "repo_root_cfgrade_le_refl.body.lean"
        cls.repo_plan = cls.acceptance_dir / "repo_root_cfgrade_le_refl.plan.json"
        cls.sample_candidate = cls.acceptance_dir / "sample_fix_me.body.lean"
        cls.sample_helper_candidate = cls.acceptance_dir / "sample_helper_target.body.lean"
        cls.sample_plan = cls.tool_paths.root / "tests" / "fixtures" / "sample_plan.json"
        cls.sample_helper_plan = cls.acceptance_dir / "sample_helper_target.plan.json"
        cls.sample_helper_step = cls.acceptance_dir / "sample_helper_target.step.json"
        cls.sample_resume_bad_step = cls.acceptance_dir / "sample_resume_bad.step.json"
        cls.repo_bench_spec = cls.acceptance_dir / "repo_root_bench.yaml"
        cls.workflow_bench_spec = cls.acceptance_dir / "downstream_workflow_bench.yaml"
        cls.paired_bench_spec = cls.acceptance_dir / "paired_baseline_bench.yaml"
        cls.minif2f_smoke_bench_spec = cls.acceptance_dir / "minif2f_standard_bench.yaml"

    def test_repo_root_acceptance_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)

            check_result, check_exit = self._run_check(
                temp_paths=temp_paths,
                target_file=self.repo_target,
                workspace_root=self.repo_root,
                timeout_seconds=120.0,
            )
            assert check_exit == 0
            assert check_result.status == CheckStatus.OK
            assert check_result.workspace.root == str(self.repo_root)

            span_result = theorem_span(file_path=self.repo_target, theorem="CFGrade.le_refl")
            assert span_result.status == ProbeStatus.OK
            imports_result = imports_and_namespace(file_path=self.repo_target, theorem="CFGrade.le_refl")
            assert imports_result.status == ProbeStatus.OK

            inspect_result, inspect_exit = self._run_inspect(
                temp_paths=temp_paths,
                target_file=self.repo_target,
                workspace_root=self.repo_root,
                theorem="CFGrade.le_refl",
                timeout_seconds=120.0,
            )
            assert inspect_exit == 0
            assert inspect_result.status == InspectionStatus.OK
            assert inspect_result.snapshot.namespace == "CFGrade"
            assert inspect_result.file_state_check.status.value == "skipped"
            assert inspect_result.snapshot.goal_snapshot
            assert inspect_result.snapshot.retrieval_hits

            plan_result, plan_exit = self._run_plan(
                temp_paths=temp_paths,
                target_file=self.repo_target,
                workspace_root=self.repo_root,
                theorem="CFGrade.le_refl",
                plan_file=self.repo_plan,
                timeout_seconds=120.0,
            )
            assert plan_exit == 0
            assert plan_result.status == PlanStatus.PLANNED
            assert plan_result.plan is not None
            assert plan_result.plan is not None
            assert plan_result.plan.summary == "Case split on `cf` and simplify the finite order table."
            assert len(plan_result.plan.steps) == 2

            repair_result, repair_exit = self._run_repair(
                temp_paths=temp_paths,
                target_file=self.repo_target,
                workspace_root=self.repo_root,
                theorem="CFGrade.le_refl",
                candidate_file=self.repo_candidate,
                timeout_seconds=120.0,
            )
            assert repair_exit == 0
            assert repair_result.status == LoopStatus.VERIFIED
            assert repair_result.target_module_path == "KanProofs/Algebra/ControlFlowGrades.lean"
            assert repair_result.sandbox.strategy == SandboxStrategy.GIT_WORKTREE

            trace_result, trace_exit = self._run_trace(temp_paths=temp_paths, source_run_id=repair_result.run_id)
            assert trace_exit == 0
            assert trace_result.status == InspectionStatus.OK
            assert trace_result.source_run_id == repair_result.run_id

            replay_result, replay_exit = self._run_replay(temp_paths=temp_paths, source_run_id=repair_result.run_id)
            assert replay_exit == 0
            assert replay_result.mode == ReplayMode.RECHECK_ONLY
            assert replay_result.status == InspectionStatus.OK
            assert replay_result.replayed_status == "ok"
            repo_replay_workspace = replay_result.workspace_root
            assert repo_replay_workspace is not None
            assert Path(repo_replay_workspace).resolve() == self.repo_root.resolve()

            self._cleanup_recorded_worktrees(temp_artifacts=temp_artifacts, git_root=self.repo_root)

    def test_repo_root_retrieve_acceptance_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)

            retrieve_result, retrieve_exit = self._run_retrieve(
                temp_paths=temp_paths,
                target_file=self.repo_target,
                workspace_root=self.repo_root,
                theorem="CFGrade.le_refl",
                timeout_seconds=120.0,
            )
            assert retrieve_exit == 0
            assert retrieve_result.status == InspectionStatus.OK
            assert retrieve_result.file_state_check.status.value == "skipped"
            assert retrieve_result.snapshot.retrieval.hits
            assert retrieve_result.snapshot.retrieval.hits[0].score_breakdown
            assert retrieve_result.snapshot.retrieval.hits[0].source_kind is not None

    def test_sample_workspace_retrieve_acceptance_path_with_embedding_scoring(self) -> None:
        class DeterministicAcceptanceEmbeddingRetriever:
            def provider_name(self) -> str:
                return "test-double"

            def model_name(self) -> str | None:
                return "deterministic"

            def embed(self, texts: list[str]) -> list[list[float]]:
                vectors: list[list[float]] = []
                for text in texts:
                    if "smoke_true" in text:
                        vectors.append([1.0, 0.0])
                    else:
                        vectors.append([0.5, 0.2])
                return vectors

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)
            target_file = workspace_root / "DownstreamSample" / "Green.lean"

            with patch(
                "kan_prover.engine.retrieval._build_embedding_retriever",
                return_value=DeterministicAcceptanceEmbeddingRetriever(),
            ):
                retrieve_result, retrieve_exit = self._run_retrieve(
                    temp_paths=temp_paths,
                    target_file=target_file,
                    workspace_root=workspace_root,
                    theorem="DownstreamSample.smoke_true",
                    timeout_seconds=60.0,
                    read_only=True,
                    enable_embedding_retrieval=True,
                )
            assert retrieve_exit == 0
            assert retrieve_result.status == InspectionStatus.OK
            assert retrieve_result.snapshot.retrieval.hits
            assert any(
                component.label == "embedding-similarity"
                for component in retrieve_result.snapshot.retrieval.hits[0].score_breakdown
            )

    def test_sample_workspace_read_only_inspect_acceptance_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)
            green_file = workspace_root / "DownstreamSample" / "Green.lean"

            inspect_result, inspect_exit = self._run_inspect(
                temp_paths=temp_paths,
                target_file=green_file,
                workspace_root=workspace_root,
                theorem="DownstreamSample.smoke_true",
                timeout_seconds=60.0,
                read_only=True,
            )
            assert inspect_exit == 0
            assert inspect_result.status == InspectionStatus.OK
            assert inspect_result.read_only is True
            assert inspect_result.sandbox is None
            assert inspect_result.helper_module_root is None
            assert inspect_result.file_state_check.status.value == "skipped"
            assert inspect_result.snapshot.diagnostic_count == 0
            context_bundle = cast(
                dict[str, JsonValue], inspect_result.snapshot.semantic_probe_results["context-bundle"]
            )
            assert context_bundle["status"] == "ok"
            assert inspect_result.snapshot.proof_state is not None
            assert inspect_result.snapshot.goal_snapshot
            assert inspect_result.snapshot.retrieval_hits
            assert not (Path(inspect_result.artifact_dir) / "context" / "sandbox.json").exists()

    def test_sample_workspace_show_run_acceptance_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)
            target_file = workspace_root / "DownstreamSample" / "RepairTarget.lean"

            repair_result, repair_exit = self._run_repair(
                temp_paths=temp_paths,
                target_file=target_file,
                workspace_root=workspace_root,
                theorem="DownstreamSample.fix_me",
                candidate_file=self.sample_candidate,
                timeout_seconds=60.0,
            )
            assert repair_exit == 0
            assert repair_result.status == LoopStatus.VERIFIED

            show_run_result, show_run_exit = self._run_show_run(
                temp_paths=temp_paths,
                source_run_id=repair_result.run_id,
            )
            assert show_run_exit == 0
            assert show_run_result.stored_command == "repair"
            assert show_run_result.selected is not None
            assert show_run_result.selected.selection_kind == "attempt"
            assert show_run_result.retrieval_hits
            assert any(probe.probe == "proof-state" for probe in show_run_result.selected.semantic_probes)

    def test_sample_workspace_recursive_helper_prove_acceptance_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)
            target_file = workspace_root / "DownstreamSample" / "HelperTarget.lean"
            theorem_step_path = temp_root / "recursive-helper-step.json"
            theorem_step_path.write_text(
                json.dumps(
                    {
                        "actions": [
                            {
                                "action_id": "action-001",
                                "kind": "propose_helper_lemma",
                                "helper": {
                                    "helper_name": "helper_truth",
                                    "helper_statement": "theorem helper_truth : True :=",
                                    "proof_stub": "by\n  exact nested_truth\n",
                                },
                            }
                        ]
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            decomposition_path = temp_root / "recursive-helper-plan.json"
            decomposition_path.write_text(
                json.dumps(
                    {
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
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                file=str(target_file),
                workspace=str(workspace_root),
                theorem="helper_target",
                agent="none",
                candidate_file=[],
                replay_attempt=[],
                plan_file=[],
                theorem_step_file=[str(theorem_step_path)],
                decomposition_file=[str(decomposition_path)],
                timeout_seconds=60.0,
                max_nodes=8,
                max_decomposition_depth=2,
                json=False,
                jsonl=False,
            )
            with patch("kan_prover.cli.commands.prove.discover_tool_paths", return_value=temp_paths):
                result, exit_code = prove.run_command(args)
            assert exit_code == 0
            assert result.status == ProveStatus.VERIFIED
            assert {helper.helper_name: helper.decomposition_depth for helper in result.helpers} == {
                "nested_truth": 2,
                "helper_truth": 1,
            }

    def test_repo_root_theorem_session_acceptance_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            original_text = self.repo_target.read_text(encoding="utf-8")

            start_result, start_exit = self._run_session_start(
                temp_paths=temp_paths,
                target_file=self.repo_target,
                workspace_root=self.repo_root,
                theorem="CFGrade.le_refl",
                timeout_seconds=120.0,
            )
            assert start_exit == 0
            assert start_result.status == SessionStatus.ACTIVE

            step_result, step_exit = self._run_session_step(
                temp_paths=temp_paths,
                session_id=start_result.session_id,
                body_file=self.repo_candidate,
                timeout_seconds=120.0,
            )
            assert step_exit == 0
            assert step_result.status == SessionStatus.ACTIVE
            assert step_result.diagnostic_count == 0

            goals_result, goals_exit = self._run_session_goals(
                temp_paths=temp_paths,
                session_id=start_result.session_id,
                timeout_seconds=120.0,
            )
            assert goals_exit == 0
            assert goals_result.status == SessionStatus.ACTIVE

            checkpoint_result, checkpoint_exit = self._run_session_checkpoint(
                temp_paths=temp_paths,
                session_id=start_result.session_id,
                label="after-case-split",
            )
            assert checkpoint_exit == 0
            assert checkpoint_result.status == SessionStatus.ACTIVE

            finish_result, finish_exit = self._run_session_finish(
                temp_paths=temp_paths,
                session_id=start_result.session_id,
                timeout_seconds=120.0,
            )
            assert finish_exit == 0
            assert finish_result.status == SessionStatus.FINISHED_VERIFIED
            assert self.repo_target.read_text(encoding="utf-8") == original_text

            session_payload = json.loads(
                (Path(finish_result.session_artifact_dir) / "session.json").read_text(encoding="utf-8")
            )
            assert session_payload["status"] == SessionStatus.FINISHED_VERIFIED.value
            assert "proof_state" in session_payload["current_state"]
            current_proof_state = session_payload["current_state"]["proof_state"]
            assert isinstance(current_proof_state, dict)
            assert current_proof_state["source"] == "verification-finished"
            assert current_proof_state["local_context"] == []
            assert current_proof_state["goals"] == []
            assert [entry["action"] for entry in session_payload["action_history"]] == [
                "start",
                "step",
                "goals",
                "checkpoint",
                "finish",
            ]
            assert session_payload["checkpoints"][0]["label"] == "after-case-split"

            self._cleanup_recorded_worktrees(temp_artifacts=temp_artifacts, git_root=self.repo_root)

    def test_sample_workspace_show_session_acceptance_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)
            target_file = workspace_root / "DownstreamSample" / "RepairTarget.lean"

            start_result, start_exit = self._run_session_start(
                temp_paths=temp_paths,
                target_file=target_file,
                workspace_root=workspace_root,
                theorem="DownstreamSample.fix_me",
                timeout_seconds=60.0,
            )
            assert start_exit == 0
            assert start_result.status == SessionStatus.ACTIVE

            step_result, step_exit = self._run_session_step(
                temp_paths=temp_paths,
                session_id=start_result.session_id,
                body_file=self.sample_candidate,
                timeout_seconds=60.0,
            )
            assert step_exit == 0
            assert step_result.status == SessionStatus.ACTIVE

            finish_result, finish_exit = self._run_session_finish(
                temp_paths=temp_paths,
                session_id=start_result.session_id,
                timeout_seconds=60.0,
            )
            assert finish_exit == 0
            assert finish_result.status == SessionStatus.FINISHED_VERIFIED

            show_session_result, show_session_exit = self._run_show_session(
                temp_paths=temp_paths,
                source_session_id=start_result.session_id,
                node_index=2,
            )
            assert show_session_exit == 0
            assert show_session_result.selected.action == "step"
            assert show_session_result.selected.goal_count >= 1
            assert any(probe.probe == "proof-state" for probe in show_session_result.selected.semantic_probes)

    def test_repo_root_private_theorem_inspect_rejects_catalog_only_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)

            with pytest.raises(UnsupportedTargetError) as error:
                self._run_inspect(
                    temp_paths=temp_paths,
                    target_file=self.repo_private_target,
                    workspace_root=self.repo_root,
                    theorem="target_length_pos_of_renaming",
                    timeout_seconds=120.0,
                )
            assert error.value.command == "inspect"
            assert error.value.declaration_name == "target_length_pos_of_renaming"
            assert error.value.detail is not None
            assert "editable proof bodies" in error.value.detail

            self._cleanup_recorded_worktrees(temp_artifacts=temp_artifacts, git_root=self.repo_root)

    def test_repo_root_private_theorem_repair_rejects_catalog_only_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)

            with pytest.raises(UnsupportedTargetError) as error:
                self._run_repair(
                    temp_paths=temp_paths,
                    target_file=self.repo_private_target,
                    workspace_root=self.repo_root,
                    theorem="target_length_pos_of_renaming",
                    candidate_file=self.repo_candidate,
                    timeout_seconds=120.0,
                )
            assert error.value.command == "repair"
            assert error.value.declaration_name == "target_length_pos_of_renaming"
            assert error.value.detail is not None
            assert "editable proof bodies" in error.value.detail

            self._cleanup_recorded_worktrees(temp_artifacts=temp_artifacts, git_root=self.repo_root)

    def test_repo_root_prove_acceptance_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)

            prove_result, prove_exit = self._run_prove(
                temp_paths=temp_paths,
                target_file=self.repo_target,
                workspace_root=self.repo_root,
                theorem="CFGrade.le_refl",
                candidate_file=self.repo_candidate,
                timeout_seconds=120.0,
            )
            assert prove_exit == 0
            assert prove_result.status == ProveStatus.VERIFIED
            assert prove_result.graph.verified_node_id is not None
            assert prove_result.graph.node_count >= 2
            assert prove_result.target_module_path == "KanProofs/Algebra/ControlFlowGrades.lean"
            prove_retrieval = json.loads(
                (Path(prove_result.artifact_dir) / "context" / "retrieval.json").read_text(encoding="utf-8")
            )
            assert "match_reasons" in prove_retrieval["retrieval_hits"][0]
            assert "score_breakdown" in prove_retrieval["retrieval_hits"][0]

            self._cleanup_recorded_worktrees(temp_artifacts=temp_artifacts, git_root=self.repo_root)

    def test_generic_sample_workspace_acceptance_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)

            green_file = workspace_root / "DownstreamSample" / "Green.lean"
            repair_file = workspace_root / "DownstreamSample" / "RepairTarget.lean"

            check_result, check_exit = self._run_check(
                temp_paths=temp_paths,
                target_file=green_file,
                workspace_root=workspace_root,
                timeout_seconds=60.0,
            )
            assert check_exit == 0
            assert check_result.status == CheckStatus.OK
            assert check_result.workspace.package == "DownstreamSample"

            span_result = theorem_span(file_path=green_file, theorem="DownstreamSample.smoke_true")
            assert span_result.status == ProbeStatus.OK
            imports_result = imports_and_namespace(file_path=green_file, theorem="DownstreamSample.smoke_true")
            assert imports_result.status == ProbeStatus.OK

            inspect_result, inspect_exit = self._run_inspect(
                temp_paths=temp_paths,
                target_file=green_file,
                workspace_root=workspace_root,
                theorem="DownstreamSample.smoke_true",
                timeout_seconds=60.0,
            )
            assert inspect_exit == 0
            assert inspect_result.status == InspectionStatus.OK
            assert inspect_result.snapshot.namespace == "DownstreamSample"
            assert not inspect_result.snapshot.syntax_probe_results
            inspect_snapshot = json.loads(
                (Path(inspect_result.artifact_dir) / "context" / "snapshot.json").read_text(encoding="utf-8")
            )
            assert "match_reasons" in inspect_snapshot["retrieval_hits"][0]
            assert "score_breakdown" in inspect_snapshot["retrieval_hits"][0]
            proof_state_probe = cast(
                dict[str, JsonValue], inspect_result.snapshot.semantic_probe_results["proof-state"]
            )
            assert proof_state_probe["status"] == ProbeStatus.OK.value
            assert inspect_result.snapshot.proof_state is not None
            assert inspect_result.snapshot.goal_snapshot
            assert inspect_result.snapshot.retrieval_hits

            plan_result, plan_exit = self._run_plan(
                temp_paths=temp_paths,
                target_file=green_file,
                workspace_root=workspace_root,
                theorem="DownstreamSample.smoke_true",
                plan_file=self.sample_plan,
                timeout_seconds=60.0,
            )
            assert plan_exit == 0
            assert plan_result.status == PlanStatus.PLANNED
            assert plan_result.plan is not None
            assert plan_result.plan is not None
            assert plan_result.plan.summary == "Prove the theorem directly from `True.intro`."
            assert len(plan_result.plan.steps) == 2

            repair_result, repair_exit = self._run_repair(
                temp_paths=temp_paths,
                target_file=repair_file,
                workspace_root=workspace_root,
                theorem="DownstreamSample.fix_me",
                candidate_file=self.sample_candidate,
                timeout_seconds=60.0,
            )
            assert repair_exit == 0
            assert repair_result.status == LoopStatus.VERIFIED
            assert repair_result.sandbox.strategy == SandboxStrategy.COPIED_WORKSPACE
            assert repair_result.target_module_path == "DownstreamSample/RepairTarget.lean"
            request_payload = json.loads(
                (Path(repair_result.work_dir) / "attempt-001" / "backend" / "request.json").read_text(encoding="utf-8")
            )
            assert request_payload["repair_strategy"] == "targeted-correction"
            selected_failure = cast(dict[str, JsonValue], request_payload["selected_failure"])
            assert selected_failure["diagnostic_class"] == "unsolved-goals"

            trace_result, trace_exit = self._run_trace(temp_paths=temp_paths, source_run_id=repair_result.run_id)
            assert trace_exit == 0
            assert trace_result.status == InspectionStatus.OK

            replay_result, replay_exit = self._run_replay(temp_paths=temp_paths, source_run_id=repair_result.run_id)
            assert replay_exit == 0
            assert replay_result.mode == ReplayMode.RECHECK_ONLY
            assert replay_result.replayed_status == "ok"
            sample_replay_workspace = replay_result.workspace_root
            assert sample_replay_workspace is not None
            assert Path(sample_replay_workspace).resolve() == workspace_root.resolve()

    def test_sample_workspace_helper_first_prove_beats_one_shot_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)
            target_file = workspace_root / "DownstreamSample" / "HelperTarget.lean"

            repair_result, repair_exit = self._run_repair(
                temp_paths=temp_paths,
                target_file=target_file,
                workspace_root=workspace_root,
                theorem="helper_target",
                candidate_file=self.sample_helper_candidate,
                timeout_seconds=60.0,
            )
            assert repair_exit == 1
            assert repair_result.status == LoopStatus.FAILED

            prove_result, prove_exit = self._run_prove(
                temp_paths=temp_paths,
                target_file=target_file,
                workspace_root=workspace_root,
                theorem="helper_target",
                candidate_file=None,
                timeout_seconds=60.0,
                plan_file=self.sample_helper_plan,
                theorem_step_file=self.sample_helper_step,
            )
            assert prove_exit == 0
            assert prove_result.status == ProveStatus.VERIFIED
            assert prove_result.helpers
            assert prove_result.helpers[-1].status.value == "verified"
            assert "KanProverGenerated.Helpers." in prove_result.helpers[-1].module_name

    def test_sample_workspace_resume_acceptance_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)
            target_file = workspace_root / "DownstreamSample" / "RepairTarget.lean"

            source_result, source_exit = self._run_prove(
                temp_paths=temp_paths,
                target_file=target_file,
                workspace_root=workspace_root,
                theorem="DownstreamSample.fix_me",
                candidate_file=None,
                timeout_seconds=60.0,
                plan_file=self.sample_plan,
                theorem_step_file=self.sample_resume_bad_step,
                max_nodes=2,
            )
            assert source_exit == 1
            assert source_result.status == ProveStatus.UNSOLVED
            assert source_result.graph.frontier_node_ids

            resume_result, resume_exit = self._run_resume(
                temp_paths=temp_paths,
                source_run_id=source_result.run_id,
                workspace_root=workspace_root,
                candidate_file=self.sample_candidate,
                timeout_seconds=60.0,
                max_nodes=6,
            )
            assert resume_exit == 0
            assert resume_result.status == ResumeStatus.VERIFIED
            assert resume_result.source_run_id == source_result.run_id
            assert resume_result.graph.verified_node_id is not None
            assert resume_result.graph.node_count > source_result.graph.node_count

    def test_sample_workspace_apply_acceptance_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)
            target_file = workspace_root / "DownstreamSample" / "HelperTarget.lean"

            prove_result, prove_exit = self._run_prove(
                temp_paths=temp_paths,
                target_file=target_file,
                workspace_root=workspace_root,
                theorem="helper_target",
                candidate_file=None,
                timeout_seconds=60.0,
                plan_file=self.sample_helper_plan,
                theorem_step_file=self.sample_helper_step,
            )
            assert prove_exit == 0
            assert prove_result.status == ProveStatus.VERIFIED
            verified_node_id = prove_result.graph.verified_node_id
            assert verified_node_id is not None

            apply_result, apply_exit = self._run_apply(
                temp_paths=temp_paths,
                source_run_id=prove_result.run_id,
                workspace_root=workspace_root,
                node_id=verified_node_id,
            )
            assert apply_exit == 0
            assert apply_result.status == ApplyStatus.APPLIED
            assert apply_result.source_node_id == verified_node_id
            updated_text = target_file.read_text(encoding="utf-8")
            helper_module = prove_result.helpers[-1].module_name
            assert f"import {helper_module}" in updated_text
            assert "exact helper_truth" in updated_text

    def test_downstream_workflow_benchmark_manifest_captures_graph_and_helper_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            output_path = temp_root / "downstream-workflow-bench.json"
            result, exit_code = self._run_bench(
                temp_paths=temp_paths,
                spec_path=self.workflow_bench_spec,
                output_path=output_path,
                workspace_root=None,
            )
            assert exit_code == 0
            assert result.status == BatchStatus.COMPLETED
            assert result.target_count == 2

            aggregate = json.loads(output_path.read_text(encoding="utf-8"))
            assert len(aggregate["rows"]) == 2
            rows_by_theorem = {row["theorem"]: row for row in aggregate["rows"]}

            repo_row = rows_by_theorem["CFGrade.le_refl"]
            assert repo_row["command"] == "prove"
            assert repo_row["workspace_root"] == str(self.repo_root)
            assert repo_row["target_module_path"] == "KanProofs/Algebra/ControlFlowGrades.lean"
            assert repo_row["plan_step_count"] == 2
            assert repo_row["node_count"] >= 2
            assert repo_row["edge_count"] >= 1
            assert repo_row["helper_count"] == 0
            assert repo_row["verified_node_id"]

            helper_row = rows_by_theorem["helper_target"]
            assert helper_row["command"] == "prove"
            assert helper_row["workspace_root"] == str(self.sample_workspace_fixture.resolve())
            assert helper_row["target_module_path"] == "DownstreamSample/HelperTarget.lean"
            assert helper_row["plan_step_count"] == 2
            assert helper_row["node_count"] >= 3
            assert helper_row["edge_count"] >= 2
            assert helper_row["helper_count"] == 1
            assert helper_row["verified_node_id"]

            self._cleanup_recorded_worktrees(temp_artifacts=temp_artifacts, git_root=self.repo_root)

    def test_repo_root_benchmark_manifest_links_rows_to_real_run_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            output_path = temp_root / "repo-root-bench.json"
            result, exit_code = self._run_bench(
                temp_paths=temp_paths,
                spec_path=self.repo_bench_spec,
                output_path=output_path,
                workspace_root=self.repo_root,
            )
            assert exit_code == 0
            assert result.status == BatchStatus.COMPLETED
            assert result.target_count == 1
            assert result.shared_workspace_root == str(self.repo_root)
            aggregate = json.loads(output_path.read_text(encoding="utf-8"))
            assert len(aggregate["rows"]) == 1
            row = aggregate["rows"][0]
            assert row["run_id"]
            assert Path(row["artifact_dir"]).is_dir()
            assert row["target_module_path"] == "KanProofs/Algebra/ControlFlowGrades.lean"
            assert row["theorem"] == "CFGrade.le_refl"
            self._cleanup_recorded_worktrees(temp_artifacts=temp_artifacts, git_root=self.repo_root)

    def test_paired_baseline_harness_reports_hard_win_and_easy_guardrails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            output_path = temp_root / "paired-baseline-bench.json"
            result, exit_code = self._run_bench(
                temp_paths=temp_paths,
                spec_path=self.paired_bench_spec,
                output_path=output_path,
                workspace_root=self.repo_root,
            )
            assert exit_code == 0
            assert result.status == BatchStatus.COMPLETED
            assert result.target_count == 12
            assert len(result.condition_summaries) == 4
            assert any(summary.solve_rate < 1.0 for summary in result.condition_summaries)
            summaries_by_condition = {summary.condition_id: summary for summary in result.condition_summaries}
            assert summaries_by_condition["tool-codex"].solve_rate > summaries_by_condition["baseline-codex"].solve_rate
            assert (
                summaries_by_condition["tool-claude"].solve_rate > summaries_by_condition["baseline-claude"].solve_rate
            )

            aggregate = json.loads(output_path.read_text(encoding="utf-8"))
            assert len(aggregate["rows"]) == 12
            assert len(aggregate["condition_summaries"]) == 4
            assert len(aggregate["pair_summaries"]) == 6

            hard_pairs = [
                pair for pair in aggregate["pair_summaries"] if pair["difficulty"] == "hard" and pair["hard_win"]
            ]
            assert {pair["backend"] for pair in hard_pairs} == {"codex", "claude"}

            easy_pairs = [pair for pair in aggregate["pair_summaries"] if pair["difficulty"] == "easy"]
            assert all(pair["easy_guardrail_passed"] for pair in easy_pairs)

            rows_by_condition = {}
            for row in aggregate["rows"]:
                rows_by_condition.setdefault(row["condition_id"], []).append(row)
                assert row["token_budget"] == 4096
                assert row["workflow"] in {"plain", "tool"}
                assert Path(row["artifact_dir"]).is_dir()

            for row in rows_by_condition["baseline-codex"]:
                assert row["backend"] == "codex"
                assert row["workflow"] == "plain"
            for row in rows_by_condition["tool-claude"]:
                assert row["backend"] == "claude"
                assert row["workflow"] == "tool"

            self._cleanup_recorded_worktrees(temp_artifacts=temp_artifacts, git_root=self.repo_root)

    def test_standard_minif2f_manifest_reports_benchmark_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            output_path = temp_root / "minif2f-standard-bench.json"
            result, exit_code = self._run_bench(
                temp_paths=temp_paths,
                spec_path=self.minif2f_smoke_bench_spec,
                output_path=output_path,
                workspace_root=None,
            )
            assert exit_code == 0
            assert result.status == BatchStatus.COMPLETED
            assert result.target_count == 1
            assert result.benchmark_summary is not None
            assert result.benchmark_summary.family == "miniF2F"
            assert result.benchmark_summary.suite == "miniF2F-test"
            assert result.benchmark_summary.theorem_count == 1
            assert result.benchmark_summary.solved_count == 1
            assert result.benchmark_summary.pass_k == 1
            assert result.benchmark_summary.pass_count == 1
            assert result.benchmark_summary.pass_rate == 1.0

            aggregate = json.loads(output_path.read_text(encoding="utf-8"))
            benchmark_summary = cast(dict[str, JsonValue], aggregate["benchmark_summary"])
            assert benchmark_summary["family"] == "miniF2F"
            assert benchmark_summary["suite"] == "miniF2F-test"
            assert benchmark_summary["solved_count"] == 1
            assert benchmark_summary["pass_k"] == 1
            assert benchmark_summary["pass_count"] == 1
            assert len(aggregate["rows"]) == 1
            assert all(row["benchmark_family"] == "miniF2F" for row in aggregate["rows"])
            assert all(row["target_id"] for row in aggregate["rows"])

    def test_repo_root_bench_repeats_healthy_target_with_lean_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            output_path = temp_root / "repo-root-bench.json"
            spec_path = temp_root / "repo-root-bench.yaml"
            spec_path.write_text(
                "\n".join(
                    [
                        "defaults:",
                        "  agent: none",
                        "  command: repair",
                        "  max_rounds: 1",
                        "  timeout_sec: 120",
                        "targets:",
                        "  - file: KanProofs/Algebra/ControlFlowGrades.lean",
                        "    theorem: CFGrade.le_refl",
                        "    candidate_files:",
                        f"          - {self.acceptance_dir / 'repo_root_cfgrade_le_refl.body.lean'}",
                        "  - file: KanProofs/Algebra/ControlFlowGrades.lean",
                        "    theorem: CFGrade.le_refl",
                        "    candidate_files:",
                        f"          - {self.acceptance_dir / 'repo_root_cfgrade_le_refl.body.lean'}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result, exit_code = self._run_bench(
                temp_paths=temp_paths,
                spec_path=spec_path,
                output_path=output_path,
                workspace_root=self.repo_root,
            )
            assert exit_code == 0
            assert result.status == BatchStatus.COMPLETED
            assert result.target_count == 2

            aggregate = json.loads(output_path.read_text(encoding="utf-8"))
            assert len(aggregate["rows"]) == 2
            for row in aggregate["rows"]:
                assert row["theorem"] == "CFGrade.le_refl"
                assert row["target_module_path"] == "KanProofs/Algebra/ControlFlowGrades.lean"
                assert row["workspace_root"] == str(self.repo_root)
                request_payload = json.loads((Path(row["artifact_dir"]) / "request.json").read_text(encoding="utf-8"))
                assert not request_payload["syntax_probe_results"]
                assert request_payload["semantic_probe_results"]["declaration-target"]["status"] == "ok"
                assert "proof-state" in request_payload["semantic_probe_results"]
                declaration_target = request_payload["semantic_probe_results"]["declaration-target"]["result"]
                assert isinstance(declaration_target, dict)
                assert declaration_target["declaration_name"] == "CFGrade.le_refl"
                assert declaration_target["group"]["kind"] == "standalone"

            self._cleanup_recorded_worktrees(temp_artifacts=temp_artifacts, git_root=self.repo_root)

    def _run_check(
        self,
        *,
        temp_paths: ToolPaths,
        target_file: Path,
        workspace_root: Path,
        timeout_seconds: float,
    ) -> tuple[CheckResult, int]:
        args = argparse.Namespace(
            file=str(target_file),
            workspace=str(workspace_root),
            timeout_seconds=timeout_seconds,
            json=False,
            jsonl=False,
        )
        with patch("kan_prover.cli.commands.check.discover_tool_paths", return_value=temp_paths):
            return check.run_command(args)

    def _run_repair(
        self,
        *,
        temp_paths: ToolPaths,
        target_file: Path,
        workspace_root: Path,
        theorem: str,
        candidate_file: Path,
        timeout_seconds: float,
    ) -> tuple[LoopResult, int]:
        args = argparse.Namespace(
            file=str(target_file),
            workspace=str(workspace_root),
            theorem=theorem,
            agent="none",
            candidate_file=[str(candidate_file)],
            replay_attempt=[],
            decomposition_file=[],
            timeout_seconds=timeout_seconds,
            apply=False,
        )
        with patch("kan_prover.cli.commands.repair.discover_tool_paths", return_value=temp_paths):
            return repair.run_command(args)

    def _run_inspect(
        self,
        *,
        temp_paths: ToolPaths,
        target_file: Path,
        workspace_root: Path,
        theorem: str,
        timeout_seconds: float,
        read_only: bool = False,
        verify_file_state: bool = False,
    ) -> tuple[InspectResult, int]:
        args = argparse.Namespace(
            file=str(target_file),
            workspace=str(workspace_root),
            theorem=theorem,
            timeout_seconds=timeout_seconds,
            read_only=read_only,
            verify_file_state=verify_file_state,
            json=False,
            jsonl=False,
        )
        with patch("kan_prover.cli.commands.inspect.discover_tool_paths", return_value=temp_paths):
            return inspect.run_command(args)

    def _run_plan(
        self,
        *,
        temp_paths: ToolPaths,
        target_file: Path,
        workspace_root: Path,
        theorem: str,
        plan_file: Path,
        timeout_seconds: float,
    ) -> tuple[PlanResult, int]:
        args = argparse.Namespace(
            file=str(target_file),
            workspace=str(workspace_root),
            theorem=theorem,
            agent="none",
            plan_file=[str(plan_file)],
            timeout_seconds=timeout_seconds,
            json=False,
            jsonl=False,
        )
        with patch("kan_prover.cli.commands.plan.discover_tool_paths", return_value=temp_paths):
            return plan.run_command(args)

    def _run_retrieve(
        self,
        *,
        temp_paths: ToolPaths,
        target_file: Path,
        workspace_root: Path,
        theorem: str,
        timeout_seconds: float,
        read_only: bool = False,
        verify_file_state: bool = False,
        enable_embedding_retrieval: bool = False,
    ) -> tuple[RetrieveResult, int]:
        args = argparse.Namespace(
            file=str(target_file),
            workspace=str(workspace_root),
            theorem=theorem,
            timeout_seconds=timeout_seconds,
            read_only=read_only,
            verify_file_state=verify_file_state,
            enable_embedding_retrieval=enable_embedding_retrieval,
            embedding_provider=None,
            embedding_model=None,
            embedding_endpoint=None,
            embedding_api_key_env=None,
            embedding_weight=None,
            json=False,
            jsonl=False,
        )
        with patch("kan_prover.cli.commands.retrieve.discover_tool_paths", return_value=temp_paths):
            return retrieve.run_command(args)

    def _run_trace(self, *, temp_paths: ToolPaths, source_run_id: str) -> tuple[TraceResult, int]:
        args = argparse.Namespace(run_id=source_run_id)
        with patch("kan_prover.cli.commands.trace.discover_tool_paths", return_value=temp_paths):
            return trace.run_command(args)

    def _run_show_run(self, *, temp_paths: ToolPaths, source_run_id: str) -> tuple[ShowRunResult, int]:
        args = argparse.Namespace(run_id=source_run_id, attempt=None)
        with patch("kan_prover.cli.commands.show.discover_tool_paths", return_value=temp_paths):
            return show.run_show_run(args)

    def _run_show_session(
        self,
        *,
        temp_paths: ToolPaths,
        source_session_id: str,
        node_index: int | None = None,
    ) -> tuple[ShowSessionResult, int]:
        args = argparse.Namespace(session_id=source_session_id, node_id=None, node_index=node_index)
        with patch("kan_prover.cli.commands.show.discover_tool_paths", return_value=temp_paths):
            return show.run_show_session(args)

    def _run_replay(self, *, temp_paths: ToolPaths, source_run_id: str) -> tuple[ReplayResult, int]:
        args = argparse.Namespace(
            run_id=source_run_id,
            workspace=None,
            attempt=None,
            recheck_only=True,
            reuse_agent_output=False,
            requery_agent=False,
        )
        with patch("kan_prover.cli.commands.replay.discover_tool_paths", return_value=temp_paths):
            return replay.run_command(args)

    def _run_prove(
        self,
        *,
        temp_paths: ToolPaths,
        target_file: Path,
        workspace_root: Path,
        theorem: str,
        candidate_file: Path | None,
        timeout_seconds: float,
        plan_file: Path | None = None,
        theorem_step_file: Path | None = None,
        max_nodes: int = 4,
    ) -> tuple[ProveResult, int]:
        args = argparse.Namespace(
            file=str(target_file),
            workspace=str(workspace_root),
            theorem=theorem,
            agent="none",
            candidate_file=[] if candidate_file is None else [str(candidate_file)],
            replay_attempt=[],
            plan_file=[str(plan_file or self.sample_plan)],
            theorem_step_file=[] if theorem_step_file is None else [str(theorem_step_file)],
            timeout_seconds=timeout_seconds,
            max_nodes=max_nodes,
            json=False,
            jsonl=False,
        )
        with patch("kan_prover.cli.commands.prove.discover_tool_paths", return_value=temp_paths):
            return prove.run_command(args)

    def _run_resume(
        self,
        *,
        temp_paths: ToolPaths,
        source_run_id: str,
        workspace_root: Path,
        candidate_file: Path | None,
        timeout_seconds: float,
        max_nodes: int,
    ) -> tuple[ResumeResult, int]:
        args = argparse.Namespace(
            run_id=source_run_id,
            workspace=str(workspace_root),
            agent="none",
            candidate_file=[] if candidate_file is None else [str(candidate_file)],
            replay_attempt=[],
            plan_file=[],
            theorem_step_file=[],
            node_id=None,
            graph_index=None,
            timeout_seconds=timeout_seconds,
            max_nodes=max_nodes,
        )
        with patch("kan_prover.cli.commands.resume.discover_tool_paths", return_value=temp_paths):
            return resume.run_command(args)

    def _run_apply(
        self,
        *,
        temp_paths: ToolPaths,
        source_run_id: str,
        workspace_root: Path,
        node_id: str | None,
    ) -> tuple[ApplyResult, int]:
        args = argparse.Namespace(
            run_id=source_run_id,
            workspace=str(workspace_root),
            node_id=node_id,
            graph_index=None,
        )
        with patch("kan_prover.cli.commands.apply.discover_tool_paths", return_value=temp_paths):
            return apply.run_command(args)

    def _run_session_start(
        self,
        *,
        temp_paths: ToolPaths,
        target_file: Path,
        workspace_root: Path,
        theorem: str,
        timeout_seconds: float,
    ) -> tuple[SessionActionResult, int]:
        args = argparse.Namespace(
            file=str(target_file),
            workspace=str(workspace_root),
            theorem=theorem,
            source_run_id=None,
            timeout_seconds=timeout_seconds,
        )
        with patch("kan_prover.cli.commands.session.discover_tool_paths", return_value=temp_paths):
            return session.run_start(args)

    def _run_session_step(
        self,
        *,
        temp_paths: ToolPaths,
        session_id: str,
        body_file: Path,
        timeout_seconds: float,
    ) -> tuple[SessionActionResult, int]:
        args = argparse.Namespace(
            session_id=session_id,
            body_file=str(body_file),
            timeout_seconds=timeout_seconds,
        )
        with patch("kan_prover.cli.commands.session.discover_tool_paths", return_value=temp_paths):
            return session.run_step(args)

    def _run_session_goals(
        self,
        *,
        temp_paths: ToolPaths,
        session_id: str,
        timeout_seconds: float,
    ) -> tuple[SessionActionResult, int]:
        args = argparse.Namespace(session_id=session_id, timeout_seconds=timeout_seconds)
        with patch("kan_prover.cli.commands.session.discover_tool_paths", return_value=temp_paths):
            return session.run_goals(args)

    def _run_session_checkpoint(
        self,
        *,
        temp_paths: ToolPaths,
        session_id: str,
        label: str,
    ) -> tuple[SessionActionResult, int]:
        args = argparse.Namespace(session_id=session_id, label=label)
        with patch("kan_prover.cli.commands.session.discover_tool_paths", return_value=temp_paths):
            return session.run_checkpoint(args)

    def _run_session_finish(
        self,
        *,
        temp_paths: ToolPaths,
        session_id: str,
        timeout_seconds: float,
    ) -> tuple[SessionActionResult, int]:
        args = argparse.Namespace(session_id=session_id, timeout_seconds=timeout_seconds)
        with patch("kan_prover.cli.commands.session.discover_tool_paths", return_value=temp_paths):
            return session.run_finish(args)

    def _run_bench(
        self,
        *,
        temp_paths: ToolPaths,
        spec_path: Path,
        output_path: Path,
        workspace_root: Path | None,
    ) -> tuple[BenchResult, int]:
        args = argparse.Namespace(
            spec=str(spec_path),
            workspace=None if workspace_root is None else str(workspace_root),
            output=str(output_path),
        )
        with patch("kan_prover.cli.commands.bench.discover_tool_paths", return_value=temp_paths):
            return bench.run_command(args)

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

    def _cleanup_recorded_worktrees(self, *, temp_artifacts: Path, git_root: Path) -> None:
        sandboxes: set[Path] = set()
        for request_path in temp_artifacts.glob("runs/**/request.json"):
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            sandbox_root = payload.get("sandbox_root")
            if isinstance(sandbox_root, str):
                sandboxes.add(Path(sandbox_root))
        for session_path in temp_artifacts.glob("sessions/**/session.json"):
            payload = json.loads(session_path.read_text(encoding="utf-8"))
            sandbox_payload = payload.get("sandbox")
            if isinstance(sandbox_payload, dict):
                sandbox_root = sandbox_payload.get("sandbox_root")
                if isinstance(sandbox_root, str):
                    sandboxes.add(Path(sandbox_root))
        for sandbox_path in sandboxes:
            if not sandbox_path.exists():
                continue
            subprocess.run(
                ["git", "-C", str(git_root), "worktree", "remove", "--force", str(sandbox_path)],
                capture_output=True,
                text=True,
                check=False,
            )
