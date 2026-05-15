"""Tests for deterministic theorem planning."""

import json
import shutil
import tempfile
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from lean_proofctl.cli.commands import plan
from lean_proofctl.engine.config import ToolPaths, bind_workspace, discover_tool_paths
from lean_proofctl.engine.inspection import run_plan
from lean_proofctl.engine.models import (
    AgentRequest,
    AgentResponse,
    BackendInvocation,
    BackendResultKind,
    PlanStatus,
    TheoremPlan,
    TheoremPlanStep,
)


class TestPlan:
    """Deterministic planner coverage for `--agent none`."""

    @classmethod
    def setup_class(cls) -> None:
        cls.tool_paths = discover_tool_paths()
        cls.sample_workspace_fixture = cls.tool_paths.root / "tests" / "acceptance" / "sample_workspace"
        cls.plan_fixture = cls.tool_paths.root / "tests" / "fixtures" / "sample_plan.json"

    @pytest.mark.integration
    def test_none_backend_plan_fixture_writes_artifacts_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)
            target_file = workspace_root / "DownstreamSample" / "Green.lean"
            args = Namespace(
                file=str(target_file),
                workspace=str(workspace_root),
                theorem="DownstreamSample.smoke_true",
                agent="none",
                plan_file=[str(self.plan_fixture)],
                timeout_seconds=60.0,
                json=False,
                jsonl=False,
            )
            with patch(
                "lean_proofctl.cli.commands.plan.discover_tool_paths", return_value=self._temp_paths(temp_artifacts)
            ):
                result, exit_code = plan.run_command(args)
            assert exit_code == 0
            assert result.status == PlanStatus.PLANNED
            assert result.plan is not None
            assert result.plan is not None
            assert result.plan.steps[0].step_id == "step-1"
            run_dir = Path(result.artifact_dir)
            assert (run_dir / "context" / "inspection.json").is_file()
            assert (run_dir / "context" / "plan.json").is_file()
            assert (run_dir / "plan" / "backend" / "request.json").is_file()
            assert (run_dir / "plan" / "backend" / "response.json").is_file()
            inspection_payload = json.loads((run_dir / "context" / "inspection.json").read_text(encoding="utf-8"))
            retrieval_hit = inspection_payload["retrieval_hits"][0]
            assert "match_reasons" in retrieval_hit
            assert "score_breakdown" in retrieval_hit
            index_rows = self._read_index_rows(temp_artifacts / "index.jsonl")
            assert index_rows[-1]["command"] == "plan"
            assert index_rows[-1]["status"] == PlanStatus.PLANNED.value
            assert index_rows[-1]["plan_step_count"] == 2

    @pytest.mark.integration
    def test_hosted_planner_request_uses_dedicated_plan_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root = temp_root / "sample-workspace"
            shutil.copytree(self.sample_workspace_fixture, workspace_root)
            tool_paths = self._temp_paths(temp_artifacts)
            workspace = bind_workspace(workspace_root)
            target_file = workspace_root / "DownstreamSample" / "Green.lean"
            backend = _RecordingBackend()

            result, exit_code = run_plan(
                tool_paths=tool_paths,
                workspace=workspace,
                target_file=target_file,
                theorem="DownstreamSample.smoke_true",
                timeout_seconds=60.0,
                backend=backend,
            )

            assert exit_code == 0
            assert result.status == PlanStatus.PLANNED
            assert backend.request is not None
            assert Path(backend.request.schema_path).name == "plan.schema.json"
            assert Path(result.backend_response.schema_path).name == "plan.schema.json"

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

    def _read_index_rows(self, index_path: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _RecordingBackend:
    def __init__(self) -> None:
        self.request: AgentRequest | None = None

    def name(self) -> str:
        return "recording"

    def propose(self, request: AgentRequest) -> AgentResponse:
        self.request = request
        theorem_plan = TheoremPlan(
            summary="Direct proof.",
            strategy="Use the trivial constructor.",
            steps=[
                TheoremPlanStep(
                    step_id="step-1",
                    title="Close the goal",
                    goal="Prove `True`.",
                    rationale="The theorem is propositionally trivial.",
                )
            ],
        )
        return AgentResponse(
            backend=self.name(),
            result_kind=BackendResultKind.THEOREM_PLAN,
            invocation=BackendInvocation(argv=["recording"], cwd="."),
            prompt_path="",
            prompt_text="",
            schema_path=request.schema_path,
            raw_stdout="",
            raw_stderr="",
            raw_output="",
            parsed_payload=None,
            theorem_plan=theorem_plan,
        )
