"""Flat-file artifact ledger for verifier runs."""

import json
from pathlib import Path
from typing import cast

from kan_prover.engine.models import (
    ApplyResult,
    BenchResult,
    CheckResult,
    DecomposeResult,
    ExportResult,
    InspectResult,
    LoopResult,
    PlanResult,
    ProveResult,
    ReplayResult,
    ResumeResult,
    RetrieveResult,
    RunManifest,
    RunRequest,
    SessionActionResult,
    ShowRunResult,
    ShowSessionResult,
    TraceResult,
    dataclass_to_jsonable,
)


def write_check_artifacts(*, run_dir: Path, manifest: RunManifest, request: RunRequest, result: CheckResult) -> None:
    """Write the durable artifact bundle for one check run."""
    env_dir = run_dir / "env"
    context_dir = run_dir / "context"
    env_dir.mkdir(parents=True, exist_ok=True)
    context_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "manifest.json", dataclass_to_jsonable(manifest))
    _write_json(run_dir / "request.json", dataclass_to_jsonable(request))
    _write_json(run_dir / "result.json", result.to_json())
    (run_dir / "summary.md").write_text(_summary_markdown(result), encoding="utf-8")

    _write_json(
        env_dir / "invocation.json",
        {
            "argv": result.lean.invocation.argv,
            "cwd": result.lean.invocation.cwd,
            "timeout_seconds": result.lean.invocation.timeout_seconds,
            "returncode": result.lean.returncode,
            "timed_out": result.lean.timed_out,
            "wall_time_seconds": result.lean.wall_time_seconds,
        },
    )
    (env_dir / "stdout.txt").write_text(result.lean.stdout, encoding="utf-8")
    (env_dir / "stderr.txt").write_text(result.lean.stderr, encoding="utf-8")
    _write_json(context_dir / "diagnostic-parse-errors.json", result.lean.parse_errors)


def write_loop_artifacts(*, run_dir: Path, manifest: RunManifest, request: RunRequest, result: LoopResult) -> None:
    """Write the durable artifact bundle for one repair or solve run."""
    env_dir = run_dir / "env"
    context_dir = run_dir / "context"
    env_dir.mkdir(parents=True, exist_ok=True)
    context_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "manifest.json", dataclass_to_jsonable(manifest))
    _write_json(run_dir / "request.json", dataclass_to_jsonable(request))
    _write_json(run_dir / "result.json", result.to_json())
    (run_dir / "summary.md").write_text(_loop_summary_markdown(result), encoding="utf-8")
    _write_json(context_dir / "baseline-result.json", result.baseline.to_json())
    _write_json(context_dir / "retrieval.json", dataclass_to_jsonable(result.retrieval_hits))
    _write_json(context_dir / "proved-helpers.json", dataclass_to_jsonable(result.proved_helpers))
    _write_json(context_dir / "sandbox.json", dataclass_to_jsonable(result.sandbox))


def write_decompose_artifacts(
    *, run_dir: Path, manifest: RunManifest, request: RunRequest, result: DecomposeResult
) -> None:
    """Write the durable artifact bundle for one decompose run."""
    context_dir = run_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "manifest.json", dataclass_to_jsonable(manifest))
    _write_json(run_dir / "request.json", dataclass_to_jsonable(request))
    _write_json(run_dir / "result.json", result.to_json())
    (run_dir / "summary.md").write_text(_decompose_summary_markdown(result), encoding="utf-8")
    _write_json(context_dir / "retrieval.json", dataclass_to_jsonable(result.retrieval_hits))
    _write_json(context_dir / "sandbox.json", dataclass_to_jsonable(result.sandbox))


def write_inspect_artifacts(
    *, run_dir: Path, manifest: RunManifest, request: RunRequest, result: InspectResult | RetrieveResult
) -> None:
    """Write the durable artifact bundle for one inspect run."""
    context_dir = run_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "manifest.json", dataclass_to_jsonable(manifest))
    _write_json(run_dir / "request.json", dataclass_to_jsonable(request))
    _write_json(run_dir / "result.json", result.to_json())
    _write_json(context_dir / "snapshot.json", result.snapshot.to_json())
    if result.sandbox is not None:
        _write_json(context_dir / "sandbox.json", dataclass_to_jsonable(result.sandbox))
    (run_dir / "summary.md").write_text(_inspect_summary_markdown(result), encoding="utf-8")


def write_plan_artifacts(*, run_dir: Path, manifest: RunManifest, request: RunRequest, result: PlanResult) -> None:
    """Write the durable artifact bundle for one plan run."""
    context_dir = run_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "manifest.json", dataclass_to_jsonable(manifest))
    _write_json(run_dir / "request.json", dataclass_to_jsonable(request))
    _write_json(run_dir / "result.json", result.to_json())
    _write_json(context_dir / "inspection.json", result.inspection.to_json())
    _write_json(context_dir / "sandbox.json", dataclass_to_jsonable(result.sandbox))
    if result.plan is not None:
        _write_json(context_dir / "plan.json", dataclass_to_jsonable(result.plan))
    (run_dir / "summary.md").write_text(_plan_summary_markdown(result), encoding="utf-8")


def write_prove_artifacts(*, run_dir: Path, manifest: RunManifest, request: object, result: ProveResult) -> None:
    """Write the durable artifact bundle for one graph-backed prove run."""
    context_dir = run_dir / "context"
    graph_dir = run_dir / "graph"
    context_dir.mkdir(parents=True, exist_ok=True)
    graph_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "manifest.json", dataclass_to_jsonable(manifest))
    _write_json(run_dir / "request.json", dataclass_to_jsonable(request))
    _write_json(run_dir / "result.json", result.to_json())
    _write_json(graph_dir / "summary.json", dataclass_to_jsonable(result.graph))
    _write_json(graph_dir / "helpers.json", [helper.to_json() for helper in result.helpers])
    _write_json(graph_dir / "nodes.json", [node.to_json() for node in result.nodes])
    if isinstance(request, dict):
        request_payload = cast(dict[str, object], request)
        _write_json(
            context_dir / "retrieval.json",
            {
                "retrieval_hits": request_payload.get("retrieval_hits") or [],
                "active_helpers": request_payload.get("active_helpers") or [],
            },
        )
    _write_json(context_dir / "helpers.json", [helper.to_json() for helper in result.helpers])
    if result.plan is not None:
        _write_json(context_dir / "plan.json", dataclass_to_jsonable(result.plan))
    (run_dir / "summary.md").write_text(_prove_summary_markdown(result), encoding="utf-8")


def write_resume_artifacts(*, run_dir: Path, manifest: RunManifest, request: object, result: ResumeResult) -> None:
    """Write the durable artifact bundle for one graph-backed resume run."""
    context_dir = run_dir / "context"
    graph_dir = run_dir / "graph"
    context_dir.mkdir(parents=True, exist_ok=True)
    graph_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "manifest.json", dataclass_to_jsonable(manifest))
    _write_json(run_dir / "request.json", dataclass_to_jsonable(request))
    _write_json(run_dir / "result.json", result.to_json())
    _write_json(graph_dir / "summary.json", dataclass_to_jsonable(result.graph))
    _write_json(graph_dir / "helpers.json", [helper.to_json() for helper in result.helpers])
    _write_json(graph_dir / "nodes.json", [node.to_json() for node in result.nodes])
    _write_json(context_dir / "helpers.json", [helper.to_json() for helper in result.helpers])
    if result.plan is not None:
        _write_json(context_dir / "plan.json", dataclass_to_jsonable(result.plan))
    (run_dir / "summary.md").write_text(_resume_summary_markdown(result), encoding="utf-8")


def write_trace_artifacts(*, run_dir: Path, manifest: RunManifest, request: object, result: TraceResult) -> None:
    """Write the durable artifact bundle for one trace run."""
    context_dir = run_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "manifest.json", dataclass_to_jsonable(manifest))
    _write_json(run_dir / "request.json", dataclass_to_jsonable(request))
    _write_json(run_dir / "result.json", result.to_json())
    (run_dir / "summary.md").write_text(_trace_summary_markdown(result), encoding="utf-8")


def write_export_artifacts(*, run_dir: Path, manifest: RunManifest, request: object, result: ExportResult) -> None:
    """Write the durable artifact bundle for one export run."""
    context_dir = run_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "manifest.json", dataclass_to_jsonable(manifest))
    _write_json(run_dir / "request.json", dataclass_to_jsonable(request))
    _write_json(run_dir / "result.json", result.to_json())
    _write_json(context_dir / "bundle.json", result.bundle)
    (run_dir / "summary.md").write_text(_export_summary_markdown(result), encoding="utf-8")


def write_apply_artifacts(*, run_dir: Path, manifest: RunManifest, request: object, result: ApplyResult) -> None:
    """Write the durable artifact bundle for one apply run."""
    context_dir = run_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "manifest.json", dataclass_to_jsonable(manifest))
    _write_json(run_dir / "request.json", dataclass_to_jsonable(request))
    _write_json(run_dir / "result.json", result.to_json())
    (run_dir / "summary.md").write_text(_apply_summary_markdown(result), encoding="utf-8")


def write_replay_artifacts(*, run_dir: Path, manifest: RunManifest, request: object, result: ReplayResult) -> None:
    """Write the durable artifact bundle for one replay run."""
    context_dir = run_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "manifest.json", dataclass_to_jsonable(manifest))
    _write_json(run_dir / "request.json", dataclass_to_jsonable(request))
    _write_json(run_dir / "result.json", result.to_json())
    _write_json(context_dir / "child-result.json", result.child_result)
    (run_dir / "summary.md").write_text(_replay_summary_markdown(result), encoding="utf-8")


def write_bench_artifacts(*, run_dir: Path, manifest: RunManifest, request: object, result: BenchResult) -> None:
    """Write the durable artifact bundle for one bench run."""
    context_dir = run_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "manifest.json", dataclass_to_jsonable(manifest))
    _write_json(run_dir / "request.json", dataclass_to_jsonable(request))
    _write_json(run_dir / "result.json", result.to_json())
    _write_json(context_dir / "aggregate.json", result.to_json())
    (run_dir / "summary.md").write_text(_bench_summary_markdown(result), encoding="utf-8")


def write_session_artifacts(
    *, run_dir: Path, manifest: RunManifest, request: object, result: SessionActionResult
) -> None:
    """Write the durable artifact bundle for one theorem session command."""
    context_dir = run_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "manifest.json", dataclass_to_jsonable(manifest))
    _write_json(run_dir / "request.json", dataclass_to_jsonable(request))
    _write_json(run_dir / "result.json", result.to_json())
    _write_json(context_dir / "session.json", result.session)
    (run_dir / "summary.md").write_text(_session_summary_markdown(result), encoding="utf-8")


def write_show_artifacts(
    *, run_dir: Path, manifest: RunManifest, request: object, result: ShowRunResult | ShowSessionResult
) -> None:
    """Write the durable artifact bundle for one stored-artifact inspection run."""
    context_dir = run_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)

    _write_json(run_dir / "manifest.json", dataclass_to_jsonable(manifest))
    _write_json(run_dir / "request.json", dataclass_to_jsonable(request))
    _write_json(run_dir / "result.json", result.to_json())
    summary = (
        _show_run_summary_markdown(result)
        if isinstance(result, ShowRunResult)
        else _show_session_summary_markdown(result)
    )
    (run_dir / "summary.md").write_text(summary, encoding="utf-8")


def append_index_row(
    index_path: Path,
    result: CheckResult
    | LoopResult
    | DecomposeResult
    | InspectResult
    | RetrieveResult
    | PlanResult
    | ProveResult
    | ResumeResult
    | TraceResult
    | ExportResult
    | ApplyResult
    | ReplayResult
    | SessionActionResult
    | ShowRunResult
    | ShowSessionResult
    | BenchResult,
) -> None:
    """Append one compact JSONL row for a run."""
    row = {
        "run_id": result.run_id,
        "command": result.command,
        "status": result.status.value,
        "exit_code": result.exit_code.value,
        "artifact_dir": result.artifact_dir,
        "started_at": dataclass_to_jsonable(result.started_at),
        "finished_at": dataclass_to_jsonable(result.finished_at),
    }
    if isinstance(result, CheckResult):
        row["target_file"] = result.target_file
        row["target_module_path"] = result.target_file
        row["workspace_root"] = result.workspace.root
        row["workspace_package"] = result.workspace.package
        row["wall_time_seconds"] = result.wall_time_seconds
        row["diagnostic_count"] = result.diagnostic_count
    if isinstance(result, LoopResult):
        row["target_file"] = result.target_file
        row["target_module_path"] = result.target_module_path
        row["workspace_root"] = result.workspace.root
        row["workspace_package"] = result.workspace.package
        row["wall_time_seconds"] = result.wall_time_seconds
        row["theorem"] = result.theorem
        row["backend"] = result.backend
        row["attempt_count"] = result.attempt_count
        row["verified_attempt"] = result.verified_attempt
        row["applied"] = result.applied
        row["max_decomposition_depth"] = result.max_decomposition_depth
        row["sandbox_strategy"] = result.sandbox.strategy.value
        row["sandbox_workspace_root"] = result.sandbox.sandbox_workspace_root
        row["helper_module_root"] = result.helper_module_root
        row["diagnostic_count"] = (
            result.attempts[-1].verification.diagnostic_count if result.attempts else result.baseline.diagnostic_count
        )
    if isinstance(result, DecomposeResult):
        row["target_file"] = result.target_file
        row["target_module_path"] = result.target_module_path
        row["workspace_root"] = result.workspace.root
        row["workspace_package"] = result.workspace.package
        row["wall_time_seconds"] = result.wall_time_seconds
        row["theorem"] = result.theorem
        row["backend"] = result.backend
        row["max_decomposition_depth"] = result.max_decomposition_depth
        row["step_count"] = len(result.steps)
        row["retrieval_count"] = len(result.retrieval_hits)
        row["sandbox_strategy"] = result.sandbox.strategy.value
        row["sandbox_workspace_root"] = result.sandbox.sandbox_workspace_root
        row["helper_module_root"] = result.helper_module_root
    if isinstance(result, InspectResult):
        row["target_file"] = result.target_file
        row["target_module_path"] = result.target_module_path
        row["workspace_root"] = result.workspace.root
        row["workspace_package"] = result.workspace.package
        row["wall_time_seconds"] = result.wall_time_seconds
        row["theorem"] = result.theorem
        row["read_only"] = result.read_only
        row["retrieval_count"] = len(result.snapshot.retrieval_hits)
        row["helper_count"] = len(result.snapshot.active_helpers)
        row["diagnostic_count"] = result.snapshot.diagnostic_count
        if result.sandbox is not None:
            row["sandbox_strategy"] = result.sandbox.strategy.value
            row["sandbox_workspace_root"] = result.sandbox.sandbox_workspace_root
        if result.helper_module_root is not None:
            row["helper_module_root"] = result.helper_module_root
    if isinstance(result, RetrieveResult):
        row["target_file"] = result.target_file
        row["target_module_path"] = result.target_module_path
        row["workspace_root"] = result.workspace.root
        row["workspace_package"] = result.workspace.package
        row["wall_time_seconds"] = result.wall_time_seconds
        row["theorem"] = result.theorem
        row["read_only"] = result.read_only
        row["retrieval_count"] = len(result.snapshot.retrieval.hits)
        row["helper_count"] = len(result.snapshot.retrieval.active_helpers)
        row["diagnostic_count"] = result.snapshot.diagnostic_count
        if result.sandbox is not None:
            row["sandbox_strategy"] = result.sandbox.strategy.value
            row["sandbox_workspace_root"] = result.sandbox.sandbox_workspace_root
        if result.helper_module_root is not None:
            row["helper_module_root"] = result.helper_module_root
    if isinstance(result, PlanResult):
        row["target_file"] = result.target_file
        row["target_module_path"] = result.target_module_path
        row["workspace_root"] = result.workspace.root
        row["workspace_package"] = result.workspace.package
        row["wall_time_seconds"] = result.wall_time_seconds
        row["theorem"] = result.theorem
        row["backend"] = result.backend
        row["has_plan"] = result.plan is not None
        row["plan_step_count"] = 0 if result.plan is None else len(result.plan.steps)
        row["retrieval_count"] = len(result.inspection.retrieval_hits)
        row["helper_count"] = len(result.inspection.active_helpers)
        row["diagnostic_count"] = result.inspection.diagnostic_count
        row["sandbox_strategy"] = result.sandbox.strategy.value
        row["sandbox_workspace_root"] = result.sandbox.sandbox_workspace_root
        row["helper_module_root"] = result.helper_module_root
    if isinstance(result, ProveResult):
        row["target_file"] = result.target_file
        row["target_module_path"] = result.target_module_path
        row["workspace_root"] = result.workspace.root
        row["workspace_package"] = result.workspace.package
        row["wall_time_seconds"] = result.wall_time_seconds
        row["theorem"] = result.theorem
        row["backend"] = result.backend
        row["max_decomposition_depth"] = result.max_decomposition_depth
        row["node_count"] = result.graph.node_count
        row["edge_count"] = result.graph.edge_count
        row["verified_node_id"] = result.graph.verified_node_id
        row["frontier_count"] = len(result.graph.frontier_node_ids)
        row["helper_count"] = len(result.helpers)
    if isinstance(result, ResumeResult):
        row["target_file"] = result.target_file
        row["target_module_path"] = result.target_module_path
        row["workspace_root"] = result.workspace.root
        row["workspace_package"] = result.workspace.package
        row["wall_time_seconds"] = result.wall_time_seconds
        row["theorem"] = result.theorem
        row["backend"] = result.backend
        row["max_decomposition_depth"] = result.max_decomposition_depth
        row["node_count"] = result.graph.node_count
        row["edge_count"] = result.graph.edge_count
        row["verified_node_id"] = result.graph.verified_node_id
        row["frontier_count"] = len(result.graph.frontier_node_ids)
        row["helper_count"] = len(result.helpers)
        row["source_run_id"] = result.source_run_id
        row["source_node_id"] = result.source_node_id
    if isinstance(result, TraceResult):
        row["source_run_id"] = result.source_run_id
        row["source_command"] = result.source_command
        row["target_file"] = result.target_file
        row["theorem"] = result.theorem
        row["backend"] = result.backend
        row["attempt_count"] = result.attempt_count
    if isinstance(result, ExportResult):
        row["source_run_id"] = result.source_run_id
        row["source_command"] = result.source_command
        row["target_file"] = result.target_file
        row["target_module_path"] = result.target_module_path
        row["workspace_root"] = result.workspace_root
        row["workspace_package"] = result.workspace_package
        row["theorem"] = result.theorem
        row["bundle_path"] = result.bundle_path
    if isinstance(result, ApplyResult):
        row["source_run_id"] = result.source_run_id
        row["source_node_id"] = result.source_node_id
        row["target_file"] = result.target_file
        row["target_module_path"] = result.target_module_path
        row["workspace_root"] = result.workspace_root
        row["workspace_package"] = result.workspace_package
        row["theorem"] = result.theorem
        row["helper_count"] = len(result.applied_helper_modules)
        row["error_code"] = result.error_code.value if result.error_code is not None else None
    if isinstance(result, ReplayResult):
        row["source_run_id"] = result.source_run_id
        row["source_command"] = result.source_command
        row["mode"] = result.mode.value
        row["target_file"] = result.target_file
        row["target_module_path"] = result.target_module_path
        row["workspace_root"] = result.workspace_root
        row["workspace_package"] = result.workspace_package
        row["sandbox_strategy"] = result.sandbox_strategy
        row["theorem"] = result.theorem
        row["backend"] = result.backend
        row["replayed_run_id"] = result.replayed_run_id
    if isinstance(result, ShowRunResult):
        row["source_run_id"] = result.source_run_id
        row["source_artifact_dir"] = result.source_artifact_dir
        row["source_command"] = result.stored_command
        row["target_file"] = result.target_file
        row["target_module_path"] = result.target_module_path
        row["workspace_root"] = result.workspace_root
        row["workspace_package"] = result.workspace_package
        row["theorem"] = result.theorem
        row["backend"] = result.backend
        row["retrieval_count"] = len(result.retrieval_hits)
        row["probe_count"] = len(result.semantic_probes)
        row["selection_kind"] = result.selected.selection_kind if result.selected is not None else None
    if isinstance(result, ShowSessionResult):
        row["source_session_id"] = result.source_session_id
        row["session_artifact_dir"] = result.session_artifact_dir
        row["session_status"] = result.session_status
        row["target_file"] = result.target_file
        row["target_module_path"] = result.target_module_path
        row["theorem"] = result.theorem
        row["current_node_id"] = result.current_node_id
        row["selected_node_id"] = result.selected.node_id
        row["checkpoint_count"] = len(result.checkpoints)
        row["node_count"] = len(result.nodes)
    if isinstance(result, BenchResult):
        row["spec_path"] = result.spec_path
        row["output_path"] = result.output_path
        row["shared_workspace_root"] = result.shared_workspace_root
        row["target_count"] = result.target_count
        row["success_count"] = result.success_count
        row["failure_count"] = result.failure_count
        row["wall_time_seconds"] = result.total_wall_time_seconds
    if isinstance(result, SessionActionResult):
        row["session_id"] = result.session_id
        row["action"] = result.action.value
        row["target_file"] = result.target_file
        row["target_module_path"] = result.target_module_path
        row["theorem"] = result.theorem
        row["session_artifact_dir"] = result.session_artifact_dir
        row["current_node_id"] = result.current_node_id
        row["diagnostic_count"] = result.diagnostic_count
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True))
        handle.write("\n")


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _summary_markdown(result: CheckResult) -> str:
    lines = [
        "# kan-prover check",
        "",
        f"- run id: `{result.run_id}`",
        f"- status: `{result.status.value}`",
        f"- exit code: `{result.exit_code.value}`",
        f"- target file: `{result.target_file}`",
        f"- workspace: `{result.workspace.package}` at `{result.workspace.root}`",
        f"- wall time: `{result.wall_time_seconds:.3f}s`",
        f"- diagnostics: `{result.diagnostic_count}`",
        "",
    ]
    if result.diagnostics:
        lines.append("## Diagnostics")
        lines.append("")
        for diagnostic in result.diagnostics:
            lines.append(
                f"- `{diagnostic.severity.value}` `{diagnostic.class_name.value}` "
                f"{diagnostic.file}:{diagnostic.range.startLine}:{diagnostic.range.startCol} {diagnostic.message}"
            )
    else:
        lines.extend(["## Diagnostics", "", "- none"])
    return "\n".join(lines) + "\n"


def _loop_summary_markdown(result: LoopResult) -> str:
    lines = [
        f"# kan-prover {result.command}",
        "",
        f"- run id: `{result.run_id}`",
        f"- status: `{result.status.value}`",
        f"- exit code: `{result.exit_code.value}`",
        f"- target file: `{result.target_file}`",
        f"- workspace: `{result.workspace.package}` at `{result.workspace.root}`",
        f"- theorem: `{result.theorem}`",
        f"- backend: `{result.backend}`",
        f"- attempts: `{result.attempt_count}`",
        f"- verified attempt: `{result.verified_attempt}`",
        f"- applied: `{str(result.applied).lower()}`",
        f"- proved helpers: `{len(result.proved_helpers)}`",
        f"- retrieval hits: `{len(result.retrieval_hits)}`",
        f"- wall time: `{result.wall_time_seconds:.3f}s`",
        "",
        "## Baseline",
        "",
        f"- status: `{result.baseline.status.value}`",
        f"- diagnostics: `{result.baseline.diagnostic_count}`",
        "",
    ]
    if result.attempts:
        lines.extend(["## Attempts", ""])
        for attempt in result.attempts:
            lines.append(
                f"- `#{attempt.attempt_index}` verified=`{str(attempt.verified).lower()}` "
                f"candidate=`{attempt.candidate.candidate_id}` diff=`{attempt.diff_path}`"
            )
    else:
        lines.extend(["## Attempts", "", "- none"])
    return "\n".join(lines) + "\n"


def _decompose_summary_markdown(result: DecomposeResult) -> str:
    lines = [
        "# kan-prover decompose",
        "",
        f"- run id: `{result.run_id}`",
        f"- status: `{result.status.value}`",
        f"- exit code: `{result.exit_code.value}`",
        f"- target file: `{result.target_file}`",
        f"- workspace: `{result.workspace.package}` at `{result.workspace.root}`",
        f"- theorem: `{result.theorem}`",
        f"- backend: `{result.backend}`",
        f"- retrieval hits: `{len(result.retrieval_hits)}`",
        f"- steps: `{len(result.steps)}`",
        f"- wall time: `{result.wall_time_seconds:.3f}s`",
        "",
    ]
    if result.steps:
        lines.extend(["## Steps", ""])
        for step in result.steps:
            lines.append(f"- `{step.step_id}` `{step.layer.value}` {step.title}")
    else:
        lines.extend(["## Steps", "", "- none"])
    return "\n".join(lines) + "\n"


def _inspect_summary_markdown(result: InspectResult | RetrieveResult) -> str:
    retrieval_hits = (
        len(result.snapshot.retrieval.hits)
        if isinstance(result, RetrieveResult)
        else len(result.snapshot.retrieval_hits)
    )
    active_helpers = (
        len(result.snapshot.retrieval.active_helpers)
        if isinstance(result, RetrieveResult)
        else len(result.snapshot.active_helpers)
    )
    lines = [
        f"# kan-prover {result.command}",
        "",
        f"- run id: `{result.run_id}`",
        f"- status: `{result.status.value}`",
        f"- exit code: `{result.exit_code.value}`",
        f"- target file: `{result.target_file}`",
        f"- workspace: `{result.workspace.package}` at `{result.workspace.root}`",
        f"- theorem: `{result.theorem}`",
        f"- read only: `{str(result.read_only).lower()}`",
        f"- file state check: `{result.file_state_check.status.value}`",
        f"- file state diagnostics: `{result.file_state_check.diagnostic_count}`",
        f"- retrieval hits: `{retrieval_hits}`",
        f"- active helpers: `{active_helpers}`",
        f"- wall time: `{result.wall_time_seconds:.3f}s`",
        "",
    ]
    if result.snapshot.theorem_statement is not None:
        lines.extend(["## Statement", "", "```lean", result.snapshot.theorem_statement, "```", ""])
    return "\n".join(lines) + "\n"


def _plan_summary_markdown(result: PlanResult) -> str:
    lines = [
        "# kan-prover plan",
        "",
        f"- run id: `{result.run_id}`",
        f"- status: `{result.status.value}`",
        f"- exit code: `{result.exit_code.value}`",
        f"- target file: `{result.target_file}`",
        f"- workspace: `{result.workspace.package}` at `{result.workspace.root}`",
        f"- theorem: `{result.theorem}`",
        f"- backend: `{result.backend}`",
        f"- retrieval hits: `{len(result.inspection.retrieval_hits)}`",
        f"- active helpers: `{len(result.inspection.active_helpers)}`",
        f"- wall time: `{result.wall_time_seconds:.3f}s`",
        "",
    ]
    if result.plan is None:
        lines.extend(["## Plan", "", "- no plan"])
    else:
        lines.extend(["## Plan", "", f"- summary: {result.plan.summary}", f"- steps: `{len(result.plan.steps)}`"])
    return "\n".join(lines) + "\n"


def _trace_summary_markdown(result: TraceResult) -> str:
    lines = [
        "# kan-prover trace",
        "",
        f"- source run id: `{result.source_run_id}`",
        f"- source command: `{result.source_command}`",
        f"- source status: `{result.source_status}`",
        f"- target file: `{result.target_file}`",
        f"- theorem: `{result.theorem}`",
        f"- backend: `{result.backend}`",
        f"- attempts: `{result.attempt_count}`",
        "",
    ]
    if result.attempts:
        lines.extend(["## Attempts", ""])
        for attempt in result.attempts:
            lines.append(
                f"- `#{attempt.attempt_index}` verified=`{str(attempt.verified).lower()}` "
                f"diagnostics=`{attempt.diagnostic_count}` candidate=`{attempt.candidate_id}`"
            )
    else:
        lines.extend(["## Attempts", "", "- none"])
    return "\n".join(lines) + "\n"


def _prove_summary_markdown(result: ProveResult) -> str:
    lines = [
        "# kan-prover prove",
        "",
        f"- run id: `{result.run_id}`",
        f"- status: `{result.status.value}`",
        f"- exit code: `{result.exit_code.value}`",
        f"- target file: `{result.target_file}`",
        f"- workspace: `{result.workspace.package}` at `{result.workspace.root}`",
        f"- theorem: `{result.theorem}`",
        f"- backend: `{result.backend}`",
        f"- nodes: `{result.graph.node_count}`",
        f"- edges: `{result.graph.edge_count}`",
        f"- helpers: `{len(result.helpers)}`",
        f"- verified node: `{result.graph.verified_node_id}`",
        f"- frontier: `{len(result.graph.frontier_node_ids)}`",
        f"- wall time: `{result.wall_time_seconds:.3f}s`",
        "",
        "## Helpers",
        "",
    ]
    if result.helpers:
        for helper in result.helpers:
            lines.append(
                f"- `{helper.helper_id}` status=`{helper.status.value}` module=`{helper.module_name}` "
                f"consumers=`{len(helper.consumer_theorem_states)}`"
            )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Branch",
            "",
        ]
    )
    if result.graph.branch_node_ids:
        for node_id in result.graph.branch_node_ids:
            lines.append(f"- `{node_id}`")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def _resume_summary_markdown(result: ResumeResult) -> str:
    lines = [
        "# kan-prover resume",
        "",
        f"- run id: `{result.run_id}`",
        f"- status: `{result.status.value}`",
        f"- exit code: `{result.exit_code.value}`",
        f"- source run id: `{result.source_run_id}`",
        f"- source node id: `{result.source_node_id}`",
        f"- target file: `{result.target_file}`",
        f"- theorem: `{result.theorem}`",
        f"- backend: `{result.backend}`",
        f"- nodes: `{result.graph.node_count}`",
        f"- verified node: `{result.graph.verified_node_id}`",
        f"- frontier: `{len(result.graph.frontier_node_ids)}`",
    ]
    if result.error_message is not None:
        lines.append(f"- error: `{result.error_message}`")
    return "\n".join(lines) + "\n"


def _export_summary_markdown(result: ExportResult) -> str:
    return (
        "\n".join(
            [
                "# kan-prover export",
                "",
                f"- source run id: `{result.source_run_id}`",
                f"- source command: `{result.source_command}`",
                f"- source attempt: `{result.source_attempt_index}`",
                f"- target file: `{result.target_file}`",
                f"- theorem: `{result.theorem}`",
                f"- bundle path: `{result.bundle_path}`",
                f"- format: `{result.format}`",
                "",
            ]
        )
        + "\n"
    )


def _apply_summary_markdown(result: ApplyResult) -> str:
    lines = [
        "# kan-prover apply",
        "",
        f"- run id: `{result.run_id}`",
        f"- status: `{result.status.value}`",
        f"- exit code: `{result.exit_code.value}`",
        f"- source run id: `{result.source_run_id}`",
        f"- source node id: `{result.source_node_id}`",
        f"- target file: `{result.target_file}`",
        f"- theorem: `{result.theorem}`",
        f"- applied helpers: `{len(result.applied_helper_modules)}`",
    ]
    if result.error_code is not None:
        lines.append(f"- error code: `{result.error_code.value}`")
    if result.error_message is not None:
        lines.append(f"- error: `{result.error_message}`")
    return "\n".join(lines) + "\n"


def _replay_summary_markdown(result: ReplayResult) -> str:
    return (
        "\n".join(
            [
                "# kan-prover replay",
                "",
                f"- source run id: `{result.source_run_id}`",
                f"- source command: `{result.source_command}`",
                f"- mode: `{result.mode.value}`",
                f"- replay label: `{result.machine_label}`",
                f"- target file: `{result.target_file}`",
                f"- theorem: `{result.theorem}`",
                f"- backend: `{result.backend}`",
                f"- replayed run id: `{result.replayed_run_id}`",
                f"- replayed command: `{result.replayed_command}`",
                f"- replayed status: `{result.replayed_status}`",
                "",
            ]
        )
        + "\n"
    )


def _bench_summary_markdown(result: BenchResult) -> str:
    lines = [
        "# kan-prover bench",
        "",
        f"- spec path: `{result.spec_path}`",
        f"- output path: `{result.output_path}`",
        f"- targets: `{result.target_count}`",
        f"- success: `{result.success_count}`",
        f"- failure: `{result.failure_count}`",
        f"- wall time: `{result.total_wall_time_seconds:.3f}s`",
        "",
    ]
    if result.benchmark_summary is not None:
        summary = result.benchmark_summary
        lines.extend(
            [
                "## Benchmark Summary",
                "",
                f"- family: `{summary.family}`",
                f"- suite: `{summary.suite}`",
                f"- split: `{summary.split or '-'}`",
                f"- subset: `{summary.subset or '-'}`",
                f"- solved: `{summary.solved_count}/{summary.theorem_count}`",
                f"- solve_rate: `{summary.solve_rate:.3f}`",
            ]
        )
        if summary.pass_k is not None and summary.pass_count is not None and summary.pass_rate is not None:
            lines.append(
                f"- pass@{summary.pass_k}: `{summary.pass_count}/{summary.theorem_count}` (`{summary.pass_rate:.3f}`)"
            )
        if summary.source_repo is not None:
            lines.append(f"- source_repo: `{summary.source_repo}`")
        if summary.source_commit is not None:
            lines.append(f"- source_commit: `{summary.source_commit}`")
        lines.extend(["", "## Rows", ""])
    else:
        lines.extend(["## Rows", ""])
    if result.rows:
        for row in result.rows:
            details = [
                f"status=`{row.status}`",
                f"backend=`{row.backend}`",
                f"run=`{row.run_id}`",
            ]
            if row.target_id is not None:
                details.append(f"id=`{row.target_id}`")
            if row.workflow is not None:
                details.append(f"workflow=`{row.workflow}`")
            if row.condition_id is not None:
                details.append(f"condition=`{row.condition_id}`")
            if row.difficulty is not None:
                details.append(f"difficulty=`{row.difficulty}`")
            if row.benchmark_family is not None:
                details.append(f"benchmark=`{row.benchmark_family}`")
            if row.backend_call_count is not None:
                details.append(f"backend_calls=`{row.backend_call_count}`")
            if row.plan_step_count is not None:
                details.append(f"plan_steps=`{row.plan_step_count}`")
            if row.node_count is not None:
                details.append(f"nodes=`{row.node_count}`")
            if row.edge_count is not None:
                details.append(f"edges=`{row.edge_count}`")
            if row.helper_count is not None:
                details.append(f"helpers=`{row.helper_count}`")
            if row.source_run_id is not None:
                details.append(f"source_run=`{row.source_run_id}`")
            lines.append(f"- `{row.target_file}` `{row.theorem}` " + " ".join(details))
    else:
        lines.append("- none")
    if result.condition_summaries:
        lines.extend(["", "## Condition Summaries", ""])
        for summary in result.condition_summaries:
            details = [
                f"workflow=`{summary.workflow.value}`",
                f"backend=`{summary.backend}`",
                f"solve_rate=`{summary.solve_rate:.3f}`",
                f"solved=`{summary.solved_count}/{summary.theorem_count}`",
            ]
            if summary.average_time_to_first_verified_seconds is not None:
                details.append(f"avg_first_verified=`{summary.average_time_to_first_verified_seconds:.3f}s`")
            if summary.average_backend_calls_per_solved_theorem is not None:
                details.append(f"avg_backend_calls=`{summary.average_backend_calls_per_solved_theorem:.3f}`")
            lines.append(f"- `{summary.condition_id}` " + " ".join(details))
    if result.pair_summaries:
        lines.extend(["", "## Pair Summaries", ""])
        for pair in result.pair_summaries:
            details = [
                f"difficulty=`{pair.difficulty}`",
                f"plain_solved=`{str(pair.plain_solved).lower()}`",
                f"tool_solved=`{str(pair.tool_solved).lower()}`",
            ]
            if pair.hard_win:
                details.append("hard_win=`true`")
            if not pair.easy_guardrail_passed:
                details.append("easy_guardrail_passed=`false`")
            lines.append(f"- `{pair.backend}` `{pair.theorem}` " + " ".join(details))
    return "\n".join(lines) + "\n"


def _session_summary_markdown(result: SessionActionResult) -> str:
    lines = [
        "# kan-prover session",
        "",
        f"- run id: `{result.run_id}`",
        f"- action: `{result.action.value}`",
        f"- status: `{result.status.value}`",
        f"- exit code: `{result.exit_code.value}`",
        f"- session id: `{result.session_id}`",
        f"- target file: `{result.target_file}`",
        f"- theorem: `{result.theorem}`",
        f"- current node id: `{result.current_node_id}`",
        f"- diagnostics: `{result.diagnostic_count}`",
    ]
    return "\n".join(lines) + "\n"


def _show_run_summary_markdown(result: ShowRunResult) -> str:
    lines = [
        "# kan-prover show run",
        "",
        f"- source run id: `{result.source_run_id}`",
        f"- stored command: `{result.stored_command}`",
        f"- stored status: `{result.stored_status}`",
        f"- target file: `{result.target_file}`",
        f"- theorem: `{result.theorem}`",
        f"- backend: `{result.backend}`",
        f"- retrieval hits: `{len(result.retrieval_hits)}`",
        f"- semantic probes: `{len(result.semantic_probes)}`",
    ]
    if result.graph is not None:
        lines.extend(
            [
                f"- graph nodes: `{result.graph.node_count}`",
                f"- graph verified node: `{result.graph.verified_node_id}`",
            ]
        )
    if result.selected is not None:
        lines.extend(
            [
                f"- selected kind: `{result.selected.selection_kind}`",
                f"- selected label: `{result.selected.label}`",
                f"- selected diagnostics: `{result.selected.diagnostic_count}`",
            ]
        )
    return "\n".join(lines) + "\n"


def _show_session_summary_markdown(result: ShowSessionResult) -> str:
    lines = [
        "# kan-prover show session",
        "",
        f"- source session id: `{result.source_session_id}`",
        f"- session status: `{result.session_status}`",
        f"- target file: `{result.target_file}`",
        f"- theorem: `{result.theorem}`",
        f"- current node id: `{result.current_node_id}`",
        f"- checkpoints: `{len(result.checkpoints)}`",
        f"- nodes: `{len(result.nodes)}`",
        f"- selected node: `{result.selected.node_id}`",
    ]
    return "\n".join(lines) + "\n"
