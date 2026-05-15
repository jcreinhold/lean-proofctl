"""Tests for trace, replay, export, and bench surfaces."""

import argparse
import json
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from kan_prover.cli.commands import bench, export, prove, repair, replay, show, solve, trace
from kan_prover.cli.main import _render_show_run_report
from kan_prover.engine import bench as bench_engine
from kan_prover.engine.config import ToolPaths, bind_workspace, discover_tool_paths
from kan_prover.engine.models import BatchStatus, InspectionStatus, JsonValue, ReplayMode
from tests.workspaces import downstream_kan_workspace

GOOD_BODY = """by
  exact True.intro
"""

BAD_BODY = """by
  exact False.elim ?missing
"""

BAD_STEP = {
    "actions": [
        {
            "action_id": "action-001",
            "kind": "append_tactic_text",
            "tactic_text": "  exact False.elim ?h2",
            "rationale": "Keep the theorem unsolved so resume still has frontier work.",
        }
    ]
}

HELPER_BODY = """by
  exact helper_truth
"""

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

RECURSIVE_PLAN = {
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


class TestInspectionCommand:
    """Integration tests for artifact-backed inspection and batch commands."""

    @classmethod
    def setup_class(cls) -> None:
        cls.tool_paths = discover_tool_paths()
        cls.demo_workspace = bind_workspace(cls.tool_paths.bundled_demo_runtime)
        cls.fixtures = cls.tool_paths.root / "tests" / "fixtures"
        cls.repo_root = downstream_kan_workspace(cls.tool_paths.root)
        cls.repo_mutual_target = cls.repo_root / "KanProofs" / "Structural" / "TypedSubstitution.lean"

    @pytest.mark.integration
    def test_trace_summarizes_existing_run_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            candidate_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            source_run_id = self._run_repair(
                temp_artifacts=temp_artifacts,
                workspace_root=workspace_root,
                target_file=target_file,
                candidate_path=candidate_path,
            )
            args = argparse.Namespace(run_id=source_run_id)
            with patch(
                "kan_prover.cli.commands.trace.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = trace.run_command(args)
            assert exit_code == 0
            assert result.status == InspectionStatus.OK
            assert result.source_run_id == source_run_id
            assert result.attempt_count == 1
            assert result.artifact_paths

    @pytest.mark.integration
    def test_show_run_renders_stored_repair_summary_without_raw_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            candidate_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            source_run_id = self._run_repair(
                temp_artifacts=temp_artifacts,
                workspace_root=workspace_root,
                target_file=target_file,
                candidate_path=candidate_path,
            )
            with patch(
                "kan_prover.cli.commands.show.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = show.run_show_run(argparse.Namespace(run_id=source_run_id, attempt=None))
            assert exit_code == 0
            assert result.source_run_id == source_run_id
            assert result.stored_command == "repair"
            assert result.selected is not None
            assert result.selected.selection_kind == "attempt"
            assert result.selected.candidate_id == "candidate-001"
            assert result.retrieval_hits
            probe_names = {probe.probe for probe in result.selected.semantic_probes}
            assert "proof-state" in probe_names
            rendered = _render_show_run_report(result)
            assert "Retrieval hits" in rendered
            assert "Semantic probes:" in rendered
            assert "Selected attempt:" in rendered

    @pytest.mark.integration
    def test_prove_runs_support_trace_export_and_recheck_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            candidate_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            step_path = self._write_json(
                temp_root / "theorem-step.json",
                {
                    "actions": [
                        {
                            "action_id": "action-001",
                            "kind": "append_tactic_text",
                            "tactic_text": "  exact True.intro",
                        }
                    ]
                },
            )
            source_run_id = self._run_prove(
                temp_artifacts=temp_artifacts,
                workspace_root=workspace_root,
                target_file=target_file,
                candidate_path=candidate_path,
                theorem_step_path=step_path,
            )

            with patch(
                "kan_prover.cli.commands.trace.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                trace_result, trace_exit = trace.run_command(argparse.Namespace(run_id=source_run_id))
            assert trace_exit == 0
            assert trace_result.attempt_count >= 2

            output_path = temp_root / "prove-bundle.json"
            with patch(
                "kan_prover.cli.commands.export.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                _export_result, export_exit = export.run_command(
                    argparse.Namespace(
                        run_id=source_run_id,
                        workspace=str(workspace_root),
                        attempt=None,
                        output=str(output_path),
                    )
                )
            assert export_exit == 0
            bundle = json.loads(output_path.read_text(encoding="utf-8"))
            assert bundle["source_run_id"] == source_run_id
            assert "current_draft" in bundle
            assert "graph" in bundle
            assert bundle["graph"]["branch_node_ids"]

            with patch(
                "kan_prover.cli.commands.replay.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                replay_result, replay_exit = replay.run_command(
                    argparse.Namespace(
                        run_id=source_run_id,
                        workspace=None,
                        attempt=None,
                        recheck_only=True,
                        reuse_agent_output=False,
                        requery_agent=False,
                    )
                )
            assert replay_exit == 0
            assert replay_result.mode == ReplayMode.RECHECK_ONLY
            assert replay_result.source_run_id == source_run_id
            assert replay_result.replayed_command == "session-finish"
            assert replay_result.replayed_status == "finished-verified"
            replay_session = cast(dict[str, JsonValue], replay_result.child_result["session"])
            semantic_session = cast(dict[str, JsonValue], replay_session["semantic_session"])
            assert semantic_session["mode"] == "live"

    @pytest.mark.integration
    def test_replay_recheck_only_reapplies_stored_patch_from_stored_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            candidate_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            source_run_id = self._run_repair(
                temp_artifacts=temp_artifacts,
                workspace_root=workspace_root,
                target_file=target_file,
                candidate_path=candidate_path,
            )
            args = argparse.Namespace(
                run_id=source_run_id,
                workspace=None,
                attempt=None,
                recheck_only=True,
                reuse_agent_output=False,
                requery_agent=False,
            )
            with patch(
                "kan_prover.cli.commands.replay.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = replay.run_command(args)
            assert exit_code == 0
            assert result.mode == ReplayMode.RECHECK_ONLY
            assert result.source_run_id == source_run_id
            assert result.replayed_status == "ok"
            assert result.workspace_root == str(workspace_root)
            assert result.target_module_path == "Problems/repair_target.lean"
            assert result.sandbox_strategy == "copied-workspace"
            child_result_path = Path(result.artifact_dir) / "context" / "child-result.json"
            assert child_result_path.is_file()

    @pytest.mark.integration
    def test_export_writes_self_contained_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            candidate_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            source_run_id = self._run_repair(
                temp_artifacts=temp_artifacts,
                workspace_root=workspace_root,
                target_file=target_file,
                candidate_path=candidate_path,
            )
            output_path = temp_root / "bundle.json"
            args = argparse.Namespace(
                run_id=source_run_id, workspace=str(workspace_root), attempt=None, output=str(output_path)
            )
            with patch(
                "kan_prover.cli.commands.export.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = export.run_command(args)
            assert exit_code == 0
            assert result.status == InspectionStatus.OK
            bundle = json.loads(output_path.read_text(encoding="utf-8"))
            assert bundle["source_run_id"] == source_run_id
            assert bundle["workspace_root"] == str(workspace_root)
            assert bundle["target_module_path"] == "Problems/repair_target.lean"
            assert bundle["theorem"] == "fix_me"
            assert bundle["helper_module_root"] == "KanProverGenerated.Helpers"
            assert bundle["proof_target"]["declaration_name"] == "fix_me"
            assert bundle["proof_target"]["safe_edit"]["kind"] == "replace-body"
            assert bundle["proof_target"]["proof_state_available"] is True
            assert bundle["proof_state"]["declaration_name"] == "fix_me"
            assert not bundle["syntax_probe_results"]
            assert "theorem-span" in bundle["semantic_probe_results"]
            assert "proof-state" in bundle["semantic_probe_results"]
            assert "source_window" in bundle
            assert "diagnostics" in bundle

    @pytest.mark.integration
    def test_export_preserves_lean_backed_metadata_for_repo_root_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            candidate_path = self._write_text(
                temp_root / "repo-root.body.lean",
                (self.tool_paths.root / "tests" / "acceptance" / "repo_root_cfgrade_le_refl.body.lean").read_text(
                    encoding="utf-8"
                ),
            )
            source_run_id = self._run_repair_for_theorem(
                temp_artifacts=temp_artifacts,
                workspace_root=self.repo_root,
                target_file=self.repo_root / "KanProofs" / "Algebra" / "ControlFlowGrades.lean",
                theorem="CFGrade.le_refl",
                candidate_path=candidate_path,
                timeout_seconds=120.0,
            )
            output_path = temp_root / "repo-root-bundle.json"
            args = argparse.Namespace(
                run_id=source_run_id, workspace=str(self.repo_root), attempt=None, output=str(output_path)
            )
            with patch(
                "kan_prover.cli.commands.export.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = export.run_command(args)
            assert exit_code == 0
            assert result.status == InspectionStatus.OK
            bundle = json.loads(output_path.read_text(encoding="utf-8"))
            assert bundle["workspace_root"] == str(self.repo_root)
            assert bundle["target_module_path"] == "KanProofs/Algebra/ControlFlowGrades.lean"
            assert bundle["theorem"] == "CFGrade.le_refl"
            assert bundle["proof_target"]["declaration_name"] == "CFGrade.le_refl"
            assert bundle["proof_target"]["safe_edit"]["kind"] == "replace-body"
            assert bundle["proof_target"]["proof_state_available"] is True
            declaration_group = cast(dict[str, JsonValue], bundle["proof_target"]["group"])
            assert declaration_group["kind"] == "standalone"
            assert not bundle["syntax_probe_results"]
            assert "theorem-span" in bundle["semantic_probe_results"]
            assert "proof-state" in bundle["semantic_probe_results"]
            assert bundle["retrieval_hits"]

    @pytest.mark.integration
    def test_export_includes_helper_aware_context_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
            helper_body = self._write_text(temp_root / "helper.body.lean", HELPER_BODY)
            plan_path = self._write_json(temp_root / "plan.json", GOOD_PLAN)
            source_run_id = self._run_solve(
                temp_artifacts=temp_artifacts,
                workspace_root=workspace_root,
                target_file=target_file,
                candidate_path=helper_body,
                plan_path=plan_path,
            )
            output_path = temp_root / "helper-bundle.json"
            args = argparse.Namespace(run_id=source_run_id, workspace=None, attempt=None, output=str(output_path))
            with patch(
                "kan_prover.cli.commands.export.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = export.run_command(args)
            assert exit_code == 0
            assert result.status == InspectionStatus.OK
            bundle = json.loads(output_path.read_text(encoding="utf-8"))
            assert bundle["workspace_root"] == str(workspace_root)
            assert bundle["target_module_path"] == "Problems/helper_target.lean"
            assert bundle["helper_module_root"] == "KanProverGenerated.Helpers"
            assert bundle["proof_target"]["declaration_name"] == "helper_target"
            assert bundle["proof_target"]["safe_edit"]["kind"] == "replace-body"
            assert bundle["retrieval_hits"]
            assert bundle["helpers"]
            assert "proof-state" in bundle["semantic_probe_results"]

    @pytest.mark.integration
    def test_export_and_replay_preserve_recursive_helper_depth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
            theorem_step_path = self._write_json(
                temp_root / "recursive-step.json",
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
            )
            decomposition_path = self._write_json(temp_root / "recursive-plan.json", RECURSIVE_PLAN)
            source_run_id = self._run_prove(
                temp_artifacts=temp_artifacts,
                workspace_root=workspace_root,
                target_file=target_file,
                candidate_path=self._write_text(temp_root / "unused.body.lean", GOOD_BODY),
                theorem_step_path=theorem_step_path,
                theorem="helper_target",
                decomposition_path=decomposition_path,
            )

            output_path = temp_root / "recursive-helper-bundle.json"
            with patch(
                "kan_prover.cli.commands.export.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, export_exit = export.run_command(
                    argparse.Namespace(
                        run_id=source_run_id,
                        workspace=str(workspace_root),
                        attempt=None,
                        output=str(output_path),
                    )
                )
            assert export_exit == 0
            assert result.status == InspectionStatus.OK
            bundle = json.loads(output_path.read_text(encoding="utf-8"))
            helper_depths = {helper["helper_name"]: helper["decomposition_depth"] for helper in bundle["helper_states"]}
            assert helper_depths["helper_truth"] == 1
            assert helper_depths["nested_truth"] == 2
            graph_nodes = json.loads(
                (temp_artifacts / "runs" / source_run_id / "graph" / "nodes.json").read_text(encoding="utf-8")
            )
            graph_helper_depths = sorted(
                node["decomposition_depth"] for node in graph_nodes if node["node_kind"] == "helper-obligation"
            )
            assert graph_helper_depths == [1, 2]

            with patch(
                "kan_prover.cli.commands.replay.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                replay_result, replay_exit = replay.run_command(
                    argparse.Namespace(
                        run_id=source_run_id,
                        workspace=None,
                        attempt=None,
                        recheck_only=True,
                        reuse_agent_output=False,
                        requery_agent=False,
                    )
                )
            assert replay_exit == 0
            assert replay_result.mode == ReplayMode.RECHECK_ONLY

    @pytest.mark.integration
    def test_bench_runs_targets_and_writes_aggregate_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root, _repair_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            good_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            bad_path = self._write_text(temp_root / "bad.body.lean", BAD_BODY)
            spec_path = temp_root / "bench.yaml"
            spec_path.write_text(
                "\n".join(
                    [
                        f"workspace: {workspace_root}",
                        "defaults:",
                        "  agent: none",
                        "  command: solve",
                        "  max_rounds: 1",
                        "  timeout_sec: 60",
                        "targets:",
                        "  - file: Problems/repair_target.lean",
                        "    theorem: fix_me",
                        "    candidate_files:",
                        f"      - {good_path}",
                        "  - file: Problems/repair_target.lean",
                        "    theorem: fix_me",
                        "    candidate_files:",
                        f"      - {bad_path}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            output_path = temp_root / "aggregate.json"
            args = argparse.Namespace(spec=str(spec_path), workspace=str(workspace_root), output=str(output_path))
            with patch(
                "kan_prover.cli.commands.bench.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = bench.run_command(args)
            assert exit_code == 1
            assert result.status == BatchStatus.FAILED
            assert result.target_count == 2
            assert result.success_count == 1
            assert result.failure_count == 1
            assert result.shared_workspace_root == str(workspace_root)
            aggregate = json.loads(output_path.read_text(encoding="utf-8"))
            assert len(aggregate["rows"]) == 2
            for row in aggregate["rows"]:
                assert row["workspace_root"] == str(workspace_root)
                assert row["target_module_path"] == "Problems/repair_target.lean"
                assert row["run_id"]
                assert Path(row["artifact_dir"]).is_dir()

    def test_bench_parse_supports_shared_workspace_and_target_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shared_workspace, _shared_target = self._workspace_with_fixture(temp_root / "shared", "repair_target.lean")
            override_workspace, _override_target = self._workspace_with_fixture(
                temp_root / "override", "repair_target.lean"
            )
            spec_path = temp_root / "bench.yaml"
            payload = cast(
                dict[str, JsonValue],
                {
                    "workspace": str(shared_workspace),
                    "defaults": {"command": "solve", "agent": "none", "timeout_sec": 60, "max_rounds": 1},
                    "targets": [
                        {"file": "Problems/repair_target.lean", "theorem": "fix_me"},
                        {
                            "workspace": str(override_workspace),
                            "file": "Problems/repair_target.lean",
                            "theorem": "fix_me",
                            "plan_files": ["plan.json"],
                            "theorem_step_files": ["step.json"],
                            "max_nodes": 9,
                        },
                    ],
                },
            )
            parsed = bench_engine._parse_targets(
                payload=payload,
                spec_path=spec_path,
                shared_workspace=shared_workspace,
            )
            assert len(parsed) == 2
            assert parsed[0].workspace is None
            assert parsed[0].file == (shared_workspace / "Problems" / "repair_target.lean").resolve()
            assert parsed[1].workspace == override_workspace.resolve()
            assert parsed[1].file == (override_workspace / "Problems" / "repair_target.lean").resolve()
            assert parsed[1].plan_files == [(spec_path.parent / "plan.json").resolve()]
            assert parsed[1].theorem_step_files == [(spec_path.parent / "step.json").resolve()]

    @pytest.mark.integration
    def test_bench_reports_named_benchmark_summary_and_pass_k(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root, _repair_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            good_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            bad_path = self._write_text(temp_root / "bad.body.lean", BAD_BODY)
            spec_path = temp_root / "bench.yaml"
            spec_path.write_text(
                "\n".join(
                    [
                        "benchmark:",
                        "  family: miniF2F",
                        "  suite: miniF2F-test",
                        "  split: test",
                        "  subset: subset-v1",
                        "  source_repo: https://github.com/facebookresearch/miniF2F",
                        "  source_commit: deadbeef",
                        "  pass_k: 2",
                        f"workspace: {workspace_root}",
                        "defaults:",
                        "  agent: none",
                        "  command: solve",
                        "  max_rounds: 1",
                        "  timeout_sec: 60",
                        "targets:",
                        "  - id: fix-me",
                        "    file: Problems/repair_target.lean",
                        "    theorem: fix_me",
                        "    candidate_files:",
                        f"      - {bad_path}",
                        "  - id: fix-me",
                        "    file: Problems/repair_target.lean",
                        "    theorem: fix_me",
                        "    candidate_files:",
                        f"      - {good_path}",
                        "  - id: untouched",
                        "    file: Problems/repair_target.lean",
                        "    theorem: fix_me",
                        "    candidate_files:",
                        f"      - {bad_path}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            output_path = temp_root / "aggregate.json"
            args = argparse.Namespace(spec=str(spec_path), workspace=str(workspace_root), output=str(output_path))
            with patch(
                "kan_prover.cli.commands.bench.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = bench.run_command(args)
            assert exit_code == 1
            assert result.benchmark_summary is not None
            assert result.benchmark_summary.family == "miniF2F"
            assert result.benchmark_summary.theorem_count == 2
            assert result.benchmark_summary.solved_count == 1
            assert result.benchmark_summary.pass_k == 2
            assert result.benchmark_summary.pass_count == 1
            assert result.benchmark_summary.pass_rate == pytest.approx(0.5)
            aggregate = json.loads(output_path.read_text(encoding="utf-8"))
            benchmark_summary = cast(dict[str, JsonValue], aggregate["benchmark_summary"])
            assert benchmark_summary["family"] == "miniF2F"
            assert benchmark_summary["pass_k"] == 2
            assert benchmark_summary["pass_count"] == 1
            assert {row["target_id"] for row in aggregate["rows"]} == {"fix-me", "untouched"}

    def test_checked_in_standard_manifests_parse_expected_benchmark_metadata(self) -> None:
        manifest_specs = [
            (
                self.tool_paths.root / "benchmarks" / "manifests" / "minif2f-test-subset.yaml",
                "miniF2F",
                "miniF2F-test",
                "subset-v1",
                6,
            ),
            (
                self.tool_paths.root / "benchmarks" / "manifests" / "putnambench-subset.yaml",
                "PutnamBench",
                "PutnamBench",
                "subset-v1",
                4,
            ),
        ]

        for manifest_path, family, suite, subset, theorem_count in manifest_specs:
            payload = bench_engine._load_spec(manifest_path)
            benchmark = bench_engine._parse_benchmark_spec(payload)
            assert benchmark is not None
            assert benchmark.family == family
            assert benchmark.suite == suite
            assert benchmark.subset == subset
            assert benchmark.pass_k == 1

            shared_workspace = bench_engine._resolve_workspace_path(
                spec_path=manifest_path,
                value=cast(str, payload["workspace"]),
            )
            assert shared_workspace is not None
            assert shared_workspace.is_dir()

            targets = bench_engine._parse_targets(
                payload=payload,
                spec_path=manifest_path,
                shared_workspace=shared_workspace,
            )
            assert len(targets) == theorem_count
            assert all(target.target_id for target in targets)
            assert all(target.command == "prove" for target in targets)
            assert all(target.workspace is None for target in targets)
            assert all(target.file.is_file() for target in targets)

    @pytest.mark.integration
    def test_bench_runs_resume_targets_with_graph_metrics_and_deterministic_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root, target_file = self._workspace_with_fixture(temp_root, "repair_target.lean")
            bad_step_path = self._write_json(temp_root / "bad-step.json", BAD_STEP)
            source_run_id, source_exit = self._run_prove_attempt(
                temp_artifacts=temp_artifacts,
                workspace_root=workspace_root,
                target_file=target_file,
                candidate_path=None,
                theorem_step_path=bad_step_path,
                max_nodes=2,
            )
            assert source_exit == 1

            good_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            spec_path = temp_root / "resume-bench.yaml"
            spec_path.write_text(
                "\n".join(
                    [
                        f"workspace: {workspace_root}",
                        "defaults:",
                        "  agent: none",
                        "  command: resume",
                        "  timeout_sec: 60",
                        "  max_nodes: 6",
                        "targets:",
                        "  - file: Problems/repair_target.lean",
                        "    theorem: fix_me",
                        f"    source_run_id: {source_run_id}",
                        "    candidate_files:",
                        f"      - {good_path}",
                        "  - file: Problems/repair_target.lean",
                        "    theorem: fix_me",
                        f"    source_run_id: {source_run_id}",
                        "    candidate_files:",
                        f"      - {good_path}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(spec=str(spec_path), workspace=str(workspace_root), output=None)
            with patch(
                "kan_prover.cli.commands.bench.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = bench.run_command(args)

            assert exit_code == 0
            assert result.status == BatchStatus.COMPLETED
            assert result.target_count == 2
            first_row, second_row = result.rows
            assert first_row.command == "resume"
            assert first_row.source_run_id == source_run_id
            assert first_row.node_count is not None
            assert first_row.edge_count is not None
            assert first_row.frontier_count is not None
            assert first_row.verified_node_id
            assert first_row.node_count == second_row.node_count
            assert first_row.edge_count == second_row.edge_count
            assert first_row.helper_count == second_row.helper_count
            assert first_row.verified_node_id == second_row.verified_node_id

    def _run_repair(
        self,
        *,
        temp_artifacts: Path,
        workspace_root: Path,
        target_file: Path,
        candidate_path: Path,
    ) -> str:
        return self._run_repair_for_theorem(
            temp_artifacts=temp_artifacts,
            workspace_root=workspace_root,
            target_file=target_file,
            theorem="fix_me",
            candidate_path=candidate_path,
            timeout_seconds=60.0,
        )

    def _run_repair_for_theorem(
        self,
        *,
        temp_artifacts: Path,
        workspace_root: Path,
        target_file: Path,
        theorem: str,
        candidate_path: Path,
        timeout_seconds: float,
    ) -> str:
        args = argparse.Namespace(
            file=str(target_file),
            workspace=str(workspace_root),
            theorem=theorem,
            agent="none",
            candidate_file=[str(candidate_path)],
            replay_attempt=[],
            decomposition_file=[],
            timeout_seconds=timeout_seconds,
            apply=False,
        )
        with patch("kan_prover.cli.commands.repair.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)):
            result, exit_code = repair.run_command(args)
        assert exit_code == 0
        return result.run_id

    def _run_solve(
        self,
        *,
        temp_artifacts: Path,
        workspace_root: Path,
        target_file: Path,
        candidate_path: Path,
        plan_path: Path,
    ) -> str:
        args = argparse.Namespace(
            file=str(target_file),
            workspace=str(workspace_root),
            theorem="helper_target",
            agent="none",
            candidate_file=[str(candidate_path)],
            replay_attempt=[],
            decomposition_file=[str(plan_path)],
            timeout_seconds=60.0,
            max_attempts=1,
            apply=False,
        )
        with patch("kan_prover.cli.commands.solve.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)):
            result, exit_code = solve.run_command(args)
        assert exit_code == 0
        return result.run_id

    def _run_prove(
        self,
        *,
        temp_artifacts: Path,
        workspace_root: Path,
        target_file: Path,
        candidate_path: Path,
        theorem_step_path: Path,
        theorem: str = "fix_me",
        decomposition_path: Path | None = None,
    ) -> str:
        args = argparse.Namespace(
            file=str(target_file),
            workspace=str(workspace_root),
            theorem=theorem,
            agent="none",
            candidate_file=[str(candidate_path)],
            replay_attempt=[],
            plan_file=[str(self.tool_paths.root / "tests" / "fixtures" / "sample_plan.json")],
            theorem_step_file=[str(theorem_step_path)],
            decomposition_file=[] if decomposition_path is None else [str(decomposition_path)],
            timeout_seconds=60.0,
            max_nodes=6,
        )
        with patch("kan_prover.cli.commands.prove.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)):
            result, exit_code = prove.run_command(args)
        assert exit_code == 0
        return result.run_id

    def _run_prove_attempt(
        self,
        *,
        temp_artifacts: Path,
        workspace_root: Path,
        target_file: Path,
        candidate_path: Path | None,
        theorem_step_path: Path,
        max_nodes: int,
    ) -> tuple[str, int]:
        args = argparse.Namespace(
            file=str(target_file),
            workspace=str(workspace_root),
            theorem="fix_me",
            agent="none",
            candidate_file=[] if candidate_path is None else [str(candidate_path)],
            replay_attempt=[],
            plan_file=[str(self.tool_paths.root / "tests" / "fixtures" / "sample_plan.json")],
            theorem_step_file=[str(theorem_step_path)],
            timeout_seconds=60.0,
            max_nodes=max_nodes,
        )
        with patch("kan_prover.cli.commands.prove.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)):
            result, exit_code = prove.run_command(args)
        return result.run_id, exit_code

    def _copy_fixture(self, name: str, destination: Path) -> Path:
        destination.write_text((self.fixtures / name).read_text(encoding="utf-8"), encoding="utf-8")
        return destination

    def _workspace_with_fixture(self, temp_root: Path, fixture_name: str) -> tuple[Path, Path]:
        workspace_root = self._make_workspace_root(temp_root / "workspace").resolve()
        target_file = self._copy_fixture(fixture_name, workspace_root / "Problems" / fixture_name).resolve()
        return workspace_root, target_file

    def _make_workspace_root(self, workspace_root: Path) -> Path:
        workspace_root.mkdir(parents=True, exist_ok=True)
        for name in ["lakefile.toml", "lake-manifest.json", "lean-toolchain", "KanRuntime.lean"]:
            (workspace_root / name).symlink_to(self.demo_workspace.root / name)
        for name in [".lake", "Tools"]:
            (workspace_root / name).symlink_to(self.demo_workspace.root / name, target_is_directory=True)
        (workspace_root / "Problems").mkdir(parents=True, exist_ok=True)
        return workspace_root

    def _write_text(self, path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        return path

    def _write_json(self, path: Path, payload: object) -> Path:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def _repo_root_theorem_body(self, *, theorem: str) -> str:
        source = self.repo_mutual_target.read_text(encoding="utf-8")
        marker = f"private theorem {theorem}"
        theorem_start = source.index(marker)
        body_start = source.index(":= by", theorem_start) + 3
        body_end = source.find("\n\nprivate theorem ", body_start)
        if body_end == -1:
            body_end = source.find("\n\nprivate def ", body_start)
        if body_end == -1:
            body_end = len(source)
        return source[body_start:body_end].rstrip() + "\n"

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
