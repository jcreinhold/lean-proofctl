"""Tests for graph-backed prove runs."""

import argparse
import json
import shutil
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from kan_prover.cli.commands import prove
from kan_prover.engine.config import ToolPaths, WorkspaceBinding, bind_workspace, discover_tool_paths
from kan_prover.engine.models import (
    AgentRequest,
    AgentResponse,
    BackendInvocation,
    BackendResultKind,
    PatchCandidate,
    ProveStatus,
)
from kan_prover.engine.prove import ProveOptions, run_prove

GOOD_BODY = """by
  exact True.intro
"""

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

RECURSIVE_PLAN = {
    "steps": [
        {
            "step_id": "helper-001",
            "title": "Introduce nested_truth",
            "goal": "Provide the nested helper used by helper_truth.",
            "layer": "missing-lemma",
            "helper_name": "nested_truth",
            "helper_statement": "theorem nested_truth : True :=",
            "helper_proof": "by\n  exact True.intro\n",
        }
    ]
}

pytestmark = pytest.mark.integration


class StubProveBackend:
    """Backend stub for graph-response classification tests."""

    def __init__(self, responses: dict[str, list[AgentResponse]]) -> None:
        self._responses = {key: list(value) for key, value in responses.items()}
        self._indices = dict.fromkeys(responses, 0)

    def name(self) -> str:
        return "stub"

    def propose(self, request: object) -> AgentResponse:
        typed_request = cast(AgentRequest, request)
        task_kind = typed_request.task_kind.value
        index = self._indices.get(task_kind, 0)
        responses = self._responses.get(task_kind, [])
        if index >= len(responses):
            return AgentResponse(
                backend="stub",
                result_kind=BackendResultKind.NO_PROGRESS,
                invocation=BackendInvocation(argv=["stub"], cwd="."),
                prompt_path="",
                prompt_text="",
                schema_path="",
                raw_stdout="",
                raw_stderr="",
                raw_output="",
                parsed_payload={"result_kind": "no_progress"},
                message=f"no stub response for {task_kind}",
            )
        self._indices[task_kind] = index + 1
        return responses[index]


class TestProve:
    """Deterministic coverage for `kan-prover prove`."""

    @classmethod
    def setup_class(cls) -> None:
        cls.tool_paths = discover_tool_paths()
        cls.sample_workspace_fixture = cls.tool_paths.root / "tests" / "acceptance" / "sample_workspace"
        cls.fixtures = cls.tool_paths.root / "tests" / "fixtures"
        cls.plan_fixture = cls.tool_paths.root / "tests" / "fixtures" / "sample_plan.json"

    def test_prove_persists_graph_and_mixes_state_and_repair_strategies(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)
            target_file = workspace_root / "DownstreamSample" / "RepairTarget.lean"
            candidate_path = temp_root / "good.body.lean"
            candidate_path.write_text(GOOD_BODY, encoding="utf-8")
            theorem_step_path = temp_root / "theorem-step.json"
            theorem_step_path.write_text(
                json.dumps(
                    {
                        "actions": [
                            {
                                "action_id": "action-001",
                                "kind": "append_tactic_text",
                                "tactic_text": "  exact True.intro",
                                "rationale": "Try a direct constructor first.",
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
                theorem="DownstreamSample.fix_me",
                agent="none",
                candidate_file=[str(candidate_path)],
                replay_attempt=[],
                plan_file=[str(self.plan_fixture)],
                theorem_step_file=[str(theorem_step_path)],
                timeout_seconds=60.0,
                max_nodes=6,
                json=False,
                jsonl=False,
            )
            with patch(
                "kan_prover.cli.commands.prove.discover_tool_paths",
                return_value=self._temp_paths(temp_artifacts),
            ):
                result, exit_code = prove.run_command(args)

            assert exit_code == 0
            assert result.status == ProveStatus.VERIFIED
            assert result.graph.verified_node_id is not None
            assert result.graph.node_count >= 3
            assert result.graph.root_node_id == "prove-node-000-root"
            strategy_names = {node.strategy_family.value for node in result.nodes if node.strategy_family is not None}
            assert "state-search" in strategy_names
            assert "repair-from-diagnostic" in strategy_names

            run_dir = Path(result.artifact_dir)
            assert (run_dir / "graph" / "summary.json").is_file()
            assert (run_dir / "graph" / "nodes.json").is_file()
            assert (run_dir / "context" / "retrieval.json").is_file()
            retrieval_payload = json.loads((run_dir / "context" / "retrieval.json").read_text(encoding="utf-8"))
            retrieval_hit = retrieval_payload["retrieval_hits"][0]
            assert "match_reasons" in retrieval_hit
            assert "score_breakdown" in retrieval_hit
            stored_nodes = json.loads((run_dir / "graph" / "nodes.json").read_text(encoding="utf-8"))
            assert len(stored_nodes) == result.graph.node_count
            child = next(node for node in stored_nodes if node["parent_node_id"] == "prove-node-000-root")
            root = next(node for node in stored_nodes if node["node_id"] == "prove-node-000-root")
            theorem_states = [node for node in stored_nodes if node["node_kind"] == "theorem-state"]
            assert root["probe_snapshot"]["semantic_session"]["mode"] == "one-shot"
            assert any(
                node["node_id"] != "prove-node-000-root"
                and node["probe_snapshot"]["semantic_session"]["mode"] == "live"
                for node in theorem_states
            )
            assert any(
                node["status"] == "verified" and node["probe_snapshot"]["semantic_session"].get("reuse") == "reused"
                for node in theorem_states
                if node["node_id"] != "prove-node-000-root"
            )
            assert "action_payload" in child
            assert "theorem_draft" in child
            assert "diagnostics" in child
            assert "score" in child
            assert "status" in child
            assert "dedup_signature" in child

            index_rows = self._read_index_rows(temp_artifacts / "index.jsonl")
            assert index_rows[-1]["command"] == "prove"
            assert index_rows[-1]["status"] == ProveStatus.VERIFIED.value
            node_count = index_rows[-1]["node_count"]
            assert isinstance(node_count, int)
            assert node_count >= 3

    def test_prove_verifies_helper_before_parent_theorem(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
            theorem_step_path = temp_root / "helper-step.json"
            theorem_step_path.write_text(json.dumps(HELPER_ACTION, indent=2) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                file=str(target_file),
                workspace=str(workspace.root),
                theorem="helper_target",
                agent="none",
                candidate_file=[],
                replay_attempt=[],
                plan_file=[],
                theorem_step_file=[str(theorem_step_path)],
                timeout_seconds=60.0,
                max_nodes=6,
                json=False,
                jsonl=False,
            )
            with patch(
                "kan_prover.cli.commands.prove.discover_tool_paths",
                return_value=self._temp_paths(temp_artifacts),
            ):
                result, exit_code = prove.run_command(args)

            assert exit_code == 0
            assert result.status == ProveStatus.VERIFIED
            assert len(result.helpers) == 1
            assert result.helpers[0].status.value == "verified"
            assert all(helper.module_name.startswith("KanProverGenerated.Helpers.") for helper in result.helpers)
            assert result.helpers[0].consumer_theorem_states == [result.graph.verified_node_id]
            helper_node = next(node for node in result.nodes if node.node_kind == "helper-obligation")
            verified_node = next(node for node in result.nodes if node.node_id == result.graph.verified_node_id)
            assert helper_node.status == "verified"
            assert result.helpers[0].helper_id in verified_node.active_helper_ids
            assert result.graph.branch_node_ids == [
                "prove-node-000-root",
                helper_node.node_id,
                verified_node.node_id,
            ]
            helpers_payload = json.loads(
                (Path(result.artifact_dir) / "context" / "helpers.json").read_text(encoding="utf-8")
            )
            assert len(helpers_payload) == 1
            assert helpers_payload[0]["status"] == "verified"
            helper_artifact_dir = result.helpers[0].verification_artifact_dir
            assert helper_artifact_dir is not None
            helper_history_dir = Path(helper_artifact_dir) / "history"
            history_files = [path.name for path in sorted(helper_history_dir.glob("*.json"))]
            assert history_files[:3] == ["001-proposed.json", "002-active.json", "003-verified.json"]
            assert history_files[-1] == "004-verified.json"

    def test_prove_recurses_into_helper_decomposition_and_records_depth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace, target_file = self._workspace_with_fixture(temp_root, "helper_target.lean")
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
            decomposition_path = temp_root / "recursive-plan.json"
            decomposition_path.write_text(json.dumps(RECURSIVE_PLAN, indent=2) + "\n", encoding="utf-8")
            args = argparse.Namespace(
                file=str(target_file),
                workspace=str(workspace.root),
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
            with patch(
                "kan_prover.cli.commands.prove.discover_tool_paths",
                return_value=self._temp_paths(temp_artifacts),
            ):
                result, exit_code = prove.run_command(args)

            assert exit_code == 0
            assert result.status == ProveStatus.VERIFIED
            assert result.max_decomposition_depth == 2
            helper_depths = {helper.helper_name: helper.decomposition_depth for helper in result.helpers}
            assert helper_depths["helper_truth"] == 1
            assert helper_depths["nested_truth"] == 2
            helper_nodes = [node for node in result.nodes if node.node_kind == "helper-obligation"]
            assert {node.graph_index: node.decomposition_depth for node in helper_nodes}
            assert sorted(node.decomposition_depth for node in helper_nodes) == [1, 2]
            request_payload = json.loads((Path(result.artifact_dir) / "request.json").read_text(encoding="utf-8"))
            assert request_payload["max_decomposition_depth"] == 2
            stored_nodes = json.loads((Path(result.artifact_dir) / "graph" / "nodes.json").read_text(encoding="utf-8"))
            assert sorted(
                node["decomposition_depth"] for node in stored_nodes if node["node_kind"] == "helper-obligation"
            ) == [
                1,
                2,
            ]

    def test_prove_keeps_verified_child_distinct_from_already_solved_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)
            target_file = workspace_root / "DownstreamSample" / "Green.lean"
            candidate_path = temp_root / "green.body.lean"
            candidate_path.write_text(GOOD_BODY, encoding="utf-8")
            args = argparse.Namespace(
                file=str(target_file),
                workspace=str(workspace_root),
                theorem="DownstreamSample.smoke_true",
                agent="none",
                candidate_file=[str(candidate_path)],
                replay_attempt=[],
                plan_file=[str(self.plan_fixture)],
                theorem_step_file=[],
                timeout_seconds=60.0,
                max_nodes=4,
                json=False,
                jsonl=False,
            )
            with patch(
                "kan_prover.cli.commands.prove.discover_tool_paths",
                return_value=self._temp_paths(temp_artifacts),
            ):
                result, exit_code = prove.run_command(args)

            assert exit_code == 0
            assert result.status == ProveStatus.VERIFIED
            verified_node = next(node for node in result.nodes if node.node_id == result.graph.verified_node_id)
            root_node = next(node for node in result.nodes if node.node_id == result.graph.root_node_id)
            assert root_node.dedup_signature != verified_node.dedup_signature
            assert verified_node.status == "verified"
            assert verified_node.goals == []

    def test_prove_rejects_multiple_usable_candidates_without_silent_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)
            target_file = workspace_root / "DownstreamSample" / "RepairTarget.lean"
            backend = StubProveBackend(
                responses={
                    "plan": [
                        AgentResponse(
                            backend="stub",
                            result_kind=BackendResultKind.NO_PROGRESS,
                            invocation=BackendInvocation(argv=["stub", "plan"], cwd="."),
                            prompt_path="",
                            prompt_text="",
                            schema_path="",
                            raw_stdout="",
                            raw_stderr="",
                            raw_output="",
                            parsed_payload={"result_kind": "no_progress"},
                            message="skip plan",
                        )
                    ],
                    "solve": [
                        self._agent_response(
                            candidates=[
                                {
                                    "candidate_id": "candidate-001",
                                    "theorem_name": "DownstreamSample.fix_me",
                                    "body": GOOD_BODY,
                                },
                                {
                                    "candidate_id": "candidate-002",
                                    "theorem_name": "DownstreamSample.fix_me",
                                    "body": GOOD_BODY,
                                },
                            ]
                        )
                    ],
                    "repair": [
                        AgentResponse(
                            backend="stub",
                            result_kind=BackendResultKind.NO_PROGRESS,
                            invocation=BackendInvocation(argv=["stub", "repair"], cwd="."),
                            prompt_path="",
                            prompt_text="",
                            schema_path="",
                            raw_stdout="",
                            raw_stderr="",
                            raw_output="",
                            parsed_payload={"result_kind": "no_progress"},
                            message="skip repair",
                        )
                    ],
                }
            )
            result, exit_code = run_prove(
                tool_paths=self._temp_paths(temp_artifacts),
                workspace=bind_workspace(workspace_root),
                options=ProveOptions(
                    target_file=target_file,
                    theorem="DownstreamSample.fix_me",
                    timeout_seconds=60.0,
                    max_nodes=4,
                ),
                backend=backend,
            )

            assert exit_code == 1
            assert result.status == ProveStatus.UNSOLVED
            assert result.graph.verified_node_id is None
            assert result.graph.node_count == 1
            response_payload = json.loads(
                (
                    Path(result.artifact_dir)
                    / "graph"
                    / "requests"
                    / "prove-node-000-root-whole-body-001.response.json"
                ).read_text(encoding="utf-8")
            )
            classification = response_payload["theorem_body_classification"]
            assert classification["response_class"] == "multiple-usable-candidates"
            assert classification["usable_candidate_count"] == 2

    def test_prove_root_only_path_keeps_one_shot_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)
            target_file = workspace_root / "DownstreamSample" / "Green.lean"
            backend = StubProveBackend(responses={})

            with patch("kan_prover.engine.session.LiveSemanticSession", side_effect=AssertionError("unexpected live")):
                result, exit_code = run_prove(
                    tool_paths=self._temp_paths(temp_artifacts),
                    workspace=bind_workspace(workspace_root),
                    options=ProveOptions(
                        target_file=target_file,
                        theorem="DownstreamSample.smoke_true",
                        timeout_seconds=60.0,
                        max_nodes=4,
                        max_backend_calls=0,
                    ),
                    backend=backend,
                )

            assert exit_code == 1
            assert result.status == ProveStatus.UNSOLVED
            assert result.graph.node_count == 1
            semantic_session = cast(dict[str, object], result.nodes[0].probe_snapshot["semantic_session"])
            assert semantic_session["mode"] == "one-shot"

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

    def _agent_response(self, *, candidates: list[dict[str, str]]) -> AgentResponse:
        return AgentResponse(
            backend="stub",
            result_kind=BackendResultKind.THEOREM_BODY_REPLACEMENTS,
            invocation=BackendInvocation(argv=["stub", "solve"], cwd="."),
            prompt_path="",
            prompt_text="",
            schema_path="",
            raw_stdout="",
            raw_stderr="",
            raw_output="",
            parsed_payload={"result_kind": "theorem_body_replacements"},
            candidates=[
                PatchCandidate(
                    candidate_id=item["candidate_id"],
                    theorem=item["theorem_name"],
                    body=item["body"],
                    source="stub",
                )
                for item in candidates
            ],
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

    def _read_index_rows(self, index_path: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines() if line.strip()]
