"""Unattended local benchmark batches for `kan-prover`."""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import yaml

from kan_prover.adapters import create_backend
from kan_prover.engine.config import ToolPaths, WorkspaceBinding, resolve_workspace_binding
from kan_prover.engine.graph_workflow import GraphNodeSelector, resume_graph_run
from kan_prover.engine.ledger import append_index_row, write_bench_artifacts
from kan_prover.engine.loop import LoopOptions, run_decompose, run_loop
from kan_prover.engine.models import (
    BatchStatus,
    BenchBenchmarkSummary,
    BenchConditionSummary,
    BenchPairSummary,
    BenchResult,
    BenchTargetResult,
    DecomposeResult,
    EvaluationWorkflow,
    ExitCode,
    JsonValue,
    LoopResult,
    ProveResult,
    ResumeResult,
    RunManifest,
)
from kan_prover.engine.prove import ProveOptions, run_prove
from kan_prover.engine.replay import load_run_bundle
from kan_prover.engine.run_id import new_run_id


@dataclass(frozen=True)
class BenchTargetSpec:
    """One target row in a benchmark spec."""

    target_id: str | None
    workspace: Path | None
    file: Path
    theorem: str
    command: str
    agent: str
    timeout_seconds: float
    max_attempts: int
    max_nodes: int | None
    candidate_files: list[Path]
    replay_attempts: list[Path]
    decomposition_files: list[Path]
    plan_files: list[Path]
    theorem_step_files: list[Path]
    source_run_id: str | None
    source_node_id: str | None
    graph_index: int | None


@dataclass(frozen=True)
class PairedBudgetSpec:
    """Common budget applied to each paired condition."""

    timeout_seconds: float
    max_backend_calls: int
    token_budget: int | None


@dataclass(frozen=True)
class PairedTheoremSpec:
    """One theorem in the fixed paired theorem suite."""

    theorem_id: str
    difficulty: str
    workspace: Path | None
    file: Path
    theorem: str


@dataclass(frozen=True)
class PairedConditionSpec:
    """One evaluated condition in the paired benchmark harness."""

    condition_id: str
    label: str
    workflow: EvaluationWorkflow
    backend_label: str
    agent: str
    command: str
    prompt_template_name: str | None
    enable_retrieval: bool
    enable_decomposition: bool
    max_nodes: int | None
    timeout_seconds: float
    max_backend_calls: int
    token_budget: int | None
    theorem_overrides: dict[str, dict[str, JsonValue]]


@dataclass(frozen=True)
class BenchBenchmarkSpec:
    """Optional named benchmark metadata attached to a manifest."""

    family: str
    suite: str
    split: str | None
    subset: str | None
    source_repo: str | None
    source_commit: str | None
    pass_k: int | None


def run_bench(
    *,
    tool_paths: ToolPaths,
    explicit_workspace: WorkspaceBinding | None,
    spec_path: Path,
    output_path: Path | None = None,
) -> tuple[BenchResult, int]:
    """Run one simple unattended benchmark spec."""
    started_at = datetime.now(UTC)
    payload = _load_spec(spec_path)
    benchmark = _parse_benchmark_spec(payload)
    if _is_paired_spec(payload):
        return _run_paired_bench(
            tool_paths=tool_paths,
            explicit_workspace=explicit_workspace,
            spec_path=spec_path,
            output_path=output_path,
            payload=payload,
            started_at=started_at,
            benchmark=benchmark,
        )
    shared_workspace = _resolve_workspace_path(spec_path=spec_path, value=_string(payload.get("workspace")))
    rows_spec = _parse_targets(
        payload=payload,
        spec_path=spec_path,
        shared_workspace=explicit_workspace.root if explicit_workspace is not None else shared_workspace,
    )
    if not rows_spec:
        msg = f"Benchmark spec has no targets: {spec_path}"
        raise ValueError(msg)

    rows: list[BenchTargetResult] = []
    for target in rows_spec:
        backend = create_backend(
            agent=target.agent,
            candidate_files=target.candidate_files,
            replay_attempts=target.replay_attempts,
            decomposition_files=target.decomposition_files,
            plan_files=target.plan_files,
            theorem_step_files=target.theorem_step_files,
        )
        workspace = (
            explicit_workspace
            if explicit_workspace is not None and target.workspace is None
            else resolve_workspace_binding(
                tool_paths=tool_paths,
                workspace=str(target.workspace) if target.workspace is not None else None,
                target_file=target.file,
            )
        )
        if target.command == "decompose":
            child_result, _child_exit = run_decompose(
                tool_paths=tool_paths,
                workspace=workspace,
                target_file=target.file,
                theorem=target.theorem,
                timeout_seconds=target.timeout_seconds,
                max_decomposition_depth=2,
                backend=backend,
            )
            rows.append(_bench_row_from_decompose(child_result))
            rows[-1] = _annotate_standard_bench_row(rows[-1], target=target, benchmark=benchmark)
            continue
        if target.command == "prove":
            child_result, _child_exit = run_prove(
                tool_paths=tool_paths,
                workspace=workspace,
                options=ProveOptions(
                    target_file=target.file,
                    theorem=target.theorem,
                    timeout_seconds=target.timeout_seconds,
                    max_nodes=target.max_nodes or 8,
                ),
                backend=backend,
            )
            rows.append(
                _annotate_standard_bench_row(
                    _bench_row_from_graph_result(child_result), target=target, benchmark=benchmark
                )
            )
            continue
        if target.command == "resume":
            if target.source_run_id is None:
                msg = f"Resume bench target is missing `source_run_id`: {target.file}"
                raise ValueError(msg)
            child_result, _child_exit = resume_graph_run(
                tool_paths=tool_paths,
                workspace_override=workspace,
                source_run_id=target.source_run_id,
                selector=GraphNodeSelector(node_id=target.source_node_id, graph_index=target.graph_index),
                backend=backend,
                max_nodes=_resume_max_nodes(
                    tool_paths=tool_paths,
                    source_run_id=target.source_run_id,
                    max_nodes=target.max_nodes,
                ),
                timeout_seconds=target.timeout_seconds,
            )
            rows.append(
                _annotate_standard_bench_row(
                    _bench_row_from_graph_result(
                        child_result,
                        source_run_id=child_result.source_run_id,
                        source_node_id=child_result.source_node_id,
                    ),
                    target=target,
                    benchmark=benchmark,
                )
            )
            continue
        options = LoopOptions(
            command=target.command,
            target_file=target.file,
            theorem=target.theorem,
            timeout_seconds=target.timeout_seconds,
            apply_patch=False,
            max_attempts=target.max_attempts,
            max_decomposition_depth=2,
        )
        child_result, _child_exit = run_loop(
            tool_paths=tool_paths,
            workspace=workspace,
            options=options,
            backend=backend,
        )
        rows.append(
            _annotate_standard_bench_row(_bench_row_from_loop(child_result), target=target, benchmark=benchmark)
        )

    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    final_output_path = output_path or (run_dir / "context" / "aggregate.json")
    final_output_path.parent.mkdir(parents=True, exist_ok=True)
    target_count = len(rows)
    success_count = sum(1 for row in rows if row.exit_code == ExitCode.SUCCESS.value)
    failure_count = target_count - success_count
    finished_at = datetime.now(UTC)
    benchmark_summary = _benchmark_summary(rows=rows, benchmark=benchmark)
    result = BenchResult(
        run_id=run_id,
        command="bench",
        status=BatchStatus.COMPLETED if failure_count == 0 else BatchStatus.FAILED,
        exit_code=ExitCode.SUCCESS if failure_count == 0 else ExitCode.CHECK_FAILED,
        spec_path=str(spec_path),
        artifact_dir=str(run_dir),
        output_path=str(final_output_path),
        shared_workspace_root=(
            str(explicit_workspace.root)
            if explicit_workspace is not None
            else str(shared_workspace)
            if shared_workspace is not None
            else None
        ),
        target_count=target_count,
        success_count=success_count,
        failure_count=failure_count,
        total_wall_time_seconds=sum(row.wall_time_seconds for row in rows),
        rows=rows,
        started_at=started_at,
        finished_at=finished_at,
        benchmark_summary=benchmark_summary,
    )
    final_output_path.write_text(json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = RunManifest(
        run_id=run_id,
        command="bench",
        status=result.status,
        exit_code=result.exit_code,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    request = {
        "spec_path": str(spec_path),
        "output_path": str(final_output_path),
        "target_count": len(rows_spec),
        "shared_workspace_root": result.shared_workspace_root,
        "benchmark": None if benchmark is None else _benchmark_request_payload(benchmark),
        "targets": [
            {
                "id": target.target_id,
                "file": str(target.file),
                "theorem": target.theorem,
                "command": target.command,
                "workspace_root": (
                    str(target.workspace) if target.workspace is not None else result.shared_workspace_root
                ),
                "source_run_id": target.source_run_id,
                "source_node_id": target.source_node_id,
            }
            for target in rows_spec
        ],
    }
    write_bench_artifacts(run_dir=run_dir, manifest=manifest, request=request, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, 0 if result.exit_code is ExitCode.SUCCESS else 1


def _is_paired_spec(payload: dict[str, JsonValue]) -> bool:
    return isinstance(payload.get("theorem_suite"), list) and isinstance(payload.get("conditions"), list)


def _run_paired_bench(
    *,
    tool_paths: ToolPaths,
    explicit_workspace: WorkspaceBinding | None,
    spec_path: Path,
    output_path: Path | None,
    payload: dict[str, JsonValue],
    started_at: datetime,
    benchmark: BenchBenchmarkSpec | None,
) -> tuple[BenchResult, int]:
    shared_workspace = _resolve_workspace_path(spec_path=spec_path, value=_string(payload.get("workspace")))
    budget = _parse_paired_budget(payload=payload)
    theorem_suite = _parse_paired_theorem_suite(
        payload=payload,
        spec_path=spec_path,
        shared_workspace=explicit_workspace.root if explicit_workspace is not None else shared_workspace,
    )
    conditions = _parse_paired_conditions(payload=payload, budget=budget, spec_path=spec_path)
    rows: list[BenchTargetResult] = []
    for theorem in theorem_suite:
        for condition in conditions:
            rows.append(
                _run_paired_condition(
                    tool_paths=tool_paths,
                    explicit_workspace=explicit_workspace,
                    theorem=theorem,
                    condition=condition,
                )
            )

    condition_summaries = _condition_summaries(rows)
    pair_summaries = _pair_summaries(rows)
    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    final_output_path = output_path or (run_dir / "context" / "aggregate.json")
    final_output_path.parent.mkdir(parents=True, exist_ok=True)
    success_count = sum(1 for row in rows if row.exit_code == ExitCode.SUCCESS.value)
    failure_count = len(rows) - success_count
    finished_at = datetime.now(UTC)
    benchmark_summary = _benchmark_summary(rows=rows, benchmark=benchmark)
    result = BenchResult(
        run_id=run_id,
        command="bench",
        status=BatchStatus.COMPLETED,
        exit_code=ExitCode.SUCCESS,
        spec_path=str(spec_path),
        artifact_dir=str(run_dir),
        output_path=str(final_output_path),
        shared_workspace_root=(
            str(explicit_workspace.root)
            if explicit_workspace is not None
            else str(shared_workspace)
            if shared_workspace is not None
            else None
        ),
        target_count=len(rows),
        success_count=success_count,
        failure_count=failure_count,
        total_wall_time_seconds=sum(row.wall_time_seconds for row in rows),
        rows=rows,
        started_at=started_at,
        finished_at=finished_at,
        benchmark_summary=benchmark_summary,
        condition_summaries=condition_summaries,
        pair_summaries=pair_summaries,
    )
    final_output_path.write_text(json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = RunManifest(
        run_id=run_id,
        command="bench",
        status=result.status,
        exit_code=result.exit_code,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    request = {
        "spec_path": str(spec_path),
        "output_path": str(final_output_path),
        "shared_workspace_root": result.shared_workspace_root,
        "benchmark": None if benchmark is None else _benchmark_request_payload(benchmark),
        "budget": {
            "timeout_seconds": budget.timeout_seconds,
            "max_backend_calls": budget.max_backend_calls,
            "token_budget": budget.token_budget,
        },
        "theorem_suite": [
            {
                "theorem_id": theorem.theorem_id,
                "difficulty": theorem.difficulty,
                "file": str(theorem.file),
                "theorem": theorem.theorem,
                "workspace_root": (
                    str(theorem.workspace) if theorem.workspace is not None else result.shared_workspace_root
                ),
            }
            for theorem in theorem_suite
        ],
        "conditions": [
            {
                "condition_id": condition.condition_id,
                "label": condition.label,
                "workflow": condition.workflow.value,
                "agent": condition.agent,
                "command": condition.command,
                "timeout_seconds": condition.timeout_seconds,
                "max_backend_calls": condition.max_backend_calls,
                "token_budget": condition.token_budget,
            }
            for condition in conditions
        ],
    }
    write_bench_artifacts(run_dir=run_dir, manifest=manifest, request=request, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, 0


def _parse_paired_budget(payload: dict[str, JsonValue]) -> PairedBudgetSpec:
    protocol = cast(dict[str, JsonValue], payload.get("protocol") or {})
    timeout_seconds = _number(protocol.get("timeout_seconds")) or _number(protocol.get("timeout_sec")) or 120.0
    max_backend_calls = _int(protocol.get("max_backend_calls")) or 3
    token_budget = _int(protocol.get("token_budget"))
    return PairedBudgetSpec(
        timeout_seconds=timeout_seconds,
        max_backend_calls=max_backend_calls,
        token_budget=token_budget,
    )


def _parse_benchmark_spec(payload: dict[str, JsonValue]) -> BenchBenchmarkSpec | None:
    raw = payload.get("benchmark")
    if not isinstance(raw, dict):
        return None
    pass_k = _int(raw.get("pass_k"))
    return BenchBenchmarkSpec(
        family=_required_string(raw.get("family")),
        suite=_string(raw.get("suite")) or _required_string(raw.get("family")),
        split=_string(raw.get("split")),
        subset=_string(raw.get("subset")),
        source_repo=_string(raw.get("source_repo")),
        source_commit=_string(raw.get("source_commit")),
        pass_k=pass_k if pass_k is not None and pass_k > 0 else None,
    )


def _parse_paired_theorem_suite(
    *,
    payload: dict[str, JsonValue],
    spec_path: Path,
    shared_workspace: Path | None,
) -> list[PairedTheoremSpec]:
    suite_rows = payload.get("theorem_suite")
    if not isinstance(suite_rows, list):
        msg = f"Paired benchmark spec is missing `theorem_suite`: {spec_path}"
        raise ValueError(msg)
    spec_dir = spec_path.parent
    parsed: list[PairedTheoremSpec] = []
    for row in suite_rows:
        if not isinstance(row, dict):
            continue
        target_workspace = _resolve_workspace_path(spec_path=spec_path, value=_string(row.get("workspace")))
        resolved_workspace = target_workspace or shared_workspace
        parsed.append(
            PairedTheoremSpec(
                theorem_id=_required_string(row.get("id") or row.get("theorem_id")),
                difficulty=_required_string(row.get("difficulty")),
                workspace=target_workspace,
                file=_resolve_target_path(
                    spec_dir=spec_dir,
                    workspace_root=resolved_workspace,
                    value=_required_string(row.get("file")),
                ),
                theorem=_required_string(row.get("theorem")),
            )
        )
    if not parsed:
        msg = f"Paired benchmark spec has no theorem rows: {spec_path}"
        raise ValueError(msg)
    return parsed


def _parse_paired_conditions(
    *, payload: dict[str, JsonValue], budget: PairedBudgetSpec, spec_path: Path
) -> list[PairedConditionSpec]:
    condition_rows = payload.get("conditions")
    if not isinstance(condition_rows, list):
        msg = "Paired benchmark spec is missing `conditions`."
        raise ValueError(msg)
    parsed: list[PairedConditionSpec] = []
    for row in condition_rows:
        if not isinstance(row, dict):
            continue
        workflow = EvaluationWorkflow(_required_string(row.get("workflow")))
        theorem_overrides = _resolve_theorem_overrides(spec_path=spec_path, value=row.get("theorems"))
        condition_id = _required_string(row.get("id") or row.get("condition_id"))
        backend_label = _required_string(row.get("backend"))
        parsed.append(
            PairedConditionSpec(
                condition_id=condition_id,
                label=_string(row.get("label")) or condition_id,
                workflow=workflow,
                backend_label=backend_label,
                agent=_string(row.get("agent")) or backend_label,
                command=_string(row.get("command")) or ("prove" if workflow is EvaluationWorkflow.TOOL else "solve"),
                prompt_template_name=_string(row.get("prompt_template_name")) or _string(row.get("prompt_template")),
                enable_retrieval=_bool(
                    row.get("enable_retrieval"),
                    default=workflow is EvaluationWorkflow.TOOL,
                ),
                enable_decomposition=_bool(
                    row.get("enable_decomposition"),
                    default=workflow is EvaluationWorkflow.TOOL,
                ),
                max_nodes=_int(row.get("max_nodes")),
                timeout_seconds=_number(row.get("timeout_seconds")) or budget.timeout_seconds,
                max_backend_calls=_int(row.get("max_backend_calls")) or budget.max_backend_calls,
                token_budget=(
                    _int(row.get("token_budget")) if row.get("token_budget") is not None else budget.token_budget
                ),
                theorem_overrides=theorem_overrides,
            )
        )
    if not parsed:
        msg = "Paired benchmark spec has no condition rows."
        raise ValueError(msg)
    return parsed


def _resolve_theorem_overrides(*, spec_path: Path, value: object) -> dict[str, dict[str, JsonValue]]:
    if not isinstance(value, dict):
        return {}
    resolved: dict[str, dict[str, JsonValue]] = {}
    for theorem_id, override in value.items():
        if not isinstance(theorem_id, str) or not isinstance(override, dict):
            continue
        payload = cast(dict[str, JsonValue], dict(override))
        for key in (
            "candidate_files",
            "replay_attempts",
            "decomposition_files",
            "plan_files",
            "theorem_step_files",
        ):
            payload[key] = [str(path) for path in _path_list(spec_path.parent, payload.get(key))]
        resolved[theorem_id] = payload
    return resolved


def _run_paired_condition(
    *,
    tool_paths: ToolPaths,
    explicit_workspace: WorkspaceBinding | None,
    theorem: PairedTheoremSpec,
    condition: PairedConditionSpec,
) -> BenchTargetResult:
    override = condition.theorem_overrides.get(theorem.theorem_id, {})
    backend = create_backend(
        agent=condition.agent,
        candidate_files=_path_list(Path.cwd(), override.get("candidate_files")),
        replay_attempts=_path_list(Path.cwd(), override.get("replay_attempts")),
        decomposition_files=_path_list(Path.cwd(), override.get("decomposition_files")),
        plan_files=_path_list(Path.cwd(), override.get("plan_files")),
        theorem_step_files=_path_list(Path.cwd(), override.get("theorem_step_files")),
    )
    workspace = (
        explicit_workspace
        if explicit_workspace is not None and theorem.workspace is None
        else resolve_workspace_binding(
            tool_paths=tool_paths,
            workspace=str(theorem.workspace) if theorem.workspace is not None else None,
            target_file=theorem.file,
        )
    )
    if condition.workflow is EvaluationWorkflow.TOOL:
        child_result, _child_exit = run_prove(
            tool_paths=tool_paths,
            workspace=workspace,
            options=ProveOptions(
                target_file=theorem.file,
                theorem=theorem.theorem,
                timeout_seconds=condition.timeout_seconds,
                max_nodes=condition.max_nodes or 8,
                max_backend_calls=condition.max_backend_calls,
            ),
            backend=backend,
        )
        return _annotate_bench_row(
            _bench_row_from_graph_result(child_result),
            theorem=theorem,
            condition=condition,
            backend_label=condition.backend_label,
        )
    child_result, _child_exit = run_loop(
        tool_paths=tool_paths,
        workspace=workspace,
        options=LoopOptions(
            command=condition.command,
            target_file=theorem.file,
            theorem=theorem.theorem,
            timeout_seconds=condition.timeout_seconds,
            apply_patch=False,
            max_attempts=condition.max_backend_calls,
            max_backend_calls=condition.max_backend_calls,
            enable_retrieval=condition.enable_retrieval,
            enable_decomposition=condition.enable_decomposition,
            prompt_template_name=condition.prompt_template_name,
        ),
        backend=backend,
    )
    return _annotate_bench_row(
        _bench_row_from_loop(child_result),
        theorem=theorem,
        condition=condition,
        backend_label=condition.backend_label,
    )


def _annotate_bench_row(
    row: BenchTargetResult,
    *,
    theorem: PairedTheoremSpec,
    condition: PairedConditionSpec,
    backend_label: str,
) -> BenchTargetResult:
    return replace(
        row,
        workflow=condition.workflow.value,
        condition_id=condition.condition_id,
        condition_label=condition.label,
        difficulty=theorem.difficulty,
        theorem_id=theorem.theorem_id,
        token_budget=condition.token_budget,
        backend=backend_label,
    )


def _annotate_standard_bench_row(
    row: BenchTargetResult,
    *,
    target: BenchTargetSpec,
    benchmark: BenchBenchmarkSpec | None,
) -> BenchTargetResult:
    if benchmark is None:
        return replace(row, target_id=target.target_id)
    return replace(
        row,
        target_id=target.target_id,
        benchmark_family=benchmark.family,
        benchmark_suite=benchmark.suite,
        benchmark_split=benchmark.split,
        benchmark_subset=benchmark.subset,
    )


def _benchmark_summary(
    *, rows: list[BenchTargetResult], benchmark: BenchBenchmarkSpec | None
) -> BenchBenchmarkSummary | None:
    if benchmark is None or not rows:
        return None
    grouped: dict[str, list[BenchTargetResult]] = {}
    for row in rows:
        key = row.target_id or row.theorem
        grouped.setdefault(key, []).append(row)
    theorem_count = len(grouped)
    solved_count = sum(1 for theorem_rows in grouped.values() if any(_row_solved(row) for row in theorem_rows))
    failed_count = theorem_count - solved_count
    pass_count = None
    pass_rate = None
    if benchmark.pass_k is not None:
        pass_count = sum(
            1 for theorem_rows in grouped.values() if any(_row_solved(row) for row in theorem_rows[: benchmark.pass_k])
        )
        pass_rate = (pass_count / theorem_count) if theorem_count else 0.0
    return BenchBenchmarkSummary(
        family=benchmark.family,
        suite=benchmark.suite,
        split=benchmark.split,
        subset=benchmark.subset,
        theorem_count=theorem_count,
        solved_count=solved_count,
        failed_count=failed_count,
        solve_rate=(solved_count / theorem_count) if theorem_count else 0.0,
        source_repo=benchmark.source_repo,
        source_commit=benchmark.source_commit,
        pass_k=benchmark.pass_k,
        pass_count=pass_count,
        pass_rate=pass_rate,
    )


def _benchmark_request_payload(benchmark: BenchBenchmarkSpec) -> dict[str, JsonValue]:
    return {
        "family": benchmark.family,
        "suite": benchmark.suite,
        "split": benchmark.split,
        "subset": benchmark.subset,
        "source_repo": benchmark.source_repo,
        "source_commit": benchmark.source_commit,
        "pass_k": benchmark.pass_k,
    }


def _row_solved(row: BenchTargetResult) -> bool:
    return row.exit_code == ExitCode.SUCCESS.value


def _condition_summaries(rows: list[BenchTargetResult]) -> list[BenchConditionSummary]:
    condition_ids = list(dict.fromkeys(row.condition_id for row in rows if row.condition_id is not None))
    summaries: list[BenchConditionSummary] = []
    for condition_id in condition_ids:
        scoped = [row for row in rows if row.condition_id == condition_id]
        solved = [row for row in scoped if row.exit_code == ExitCode.SUCCESS.value]
        solved_count = len(solved)
        summaries.append(
            BenchConditionSummary(
                condition_id=condition_id,
                condition_label=cast(str, scoped[0].condition_label),
                workflow=EvaluationWorkflow(cast(str, scoped[0].workflow)),
                backend=scoped[0].backend,
                theorem_count=len(scoped),
                solved_count=solved_count,
                failed_count=len(scoped) - solved_count,
                solve_rate=(solved_count / len(scoped)) if scoped else 0.0,
                average_time_to_first_verified_seconds=(
                    sum(cast(float, row.time_to_first_verified_seconds) for row in solved) / solved_count
                    if solved_count
                    else None
                ),
                average_backend_calls_per_solved_theorem=(
                    sum(cast(int, row.backend_call_count) for row in solved) / solved_count if solved_count else None
                ),
                token_budget=scoped[0].token_budget,
            )
        )
    return summaries


def _pair_summaries(rows: list[BenchTargetResult]) -> list[BenchPairSummary]:
    grouped: dict[tuple[str, str], dict[str, BenchTargetResult]] = {}
    for row in rows:
        if row.theorem_id is None or row.workflow is None or row.backend is None:
            continue
        key = (row.backend, row.theorem_id)
        bucket = grouped.setdefault(key, {})
        bucket[row.workflow] = row
    pairs: list[BenchPairSummary] = []
    for (backend, theorem_id), bucket in grouped.items():
        plain = bucket.get(EvaluationWorkflow.PLAIN.value)
        tool = bucket.get(EvaluationWorkflow.TOOL.value)
        if plain is None or tool is None:
            continue
        plain_solved = plain.exit_code == ExitCode.SUCCESS.value
        tool_solved = tool.exit_code == ExitCode.SUCCESS.value
        pairs.append(
            BenchPairSummary(
                backend=backend,
                theorem_id=theorem_id,
                theorem=tool.theorem,
                difficulty=tool.difficulty or "unknown",
                plain_condition_id=cast(str, plain.condition_id),
                tool_condition_id=cast(str, tool.condition_id),
                plain_solved=plain_solved,
                tool_solved=tool_solved,
                hard_win=tool.difficulty == "hard" and tool_solved and not plain_solved,
                easy_guardrail_passed=tool.difficulty != "easy" or (not plain_solved or tool_solved),
            )
        )
    return pairs


def _load_spec(spec_path: Path) -> dict[str, JsonValue]:
    text = spec_path.read_text(encoding="utf-8")
    loaded: object = json.loads(text) if spec_path.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(loaded, dict):
        msg = f"Benchmark spec must be a mapping: {spec_path}"
        raise ValueError(msg)
    return cast(dict[str, JsonValue], loaded)


def _parse_targets(
    *,
    payload: dict[str, JsonValue],
    spec_path: Path,
    shared_workspace: Path | None,
) -> list[BenchTargetSpec]:
    defaults = cast(dict[str, JsonValue], payload.get("defaults") or {})
    target_rows = payload.get("targets")
    if not isinstance(target_rows, list):
        msg = f"Benchmark spec is missing `targets`: {spec_path}"
        raise ValueError(msg)
    spec_dir = spec_path.parent
    parsed: list[BenchTargetSpec] = []
    for row in target_rows:
        if not isinstance(row, dict):
            continue
        command = _string(row.get("command")) or _string(defaults.get("command")) or "solve"
        if command not in {"solve", "repair", "decompose", "prove", "resume"}:
            msg = f"Unsupported bench command `{command}` in {spec_path}."
            raise ValueError(msg)
        target_workspace = _resolve_workspace_path(
            spec_path=spec_path,
            value=_string(row.get("workspace")) or _string(defaults.get("workspace")),
        )
        resolved_workspace = target_workspace or shared_workspace
        max_nodes = _int(row.get("max_nodes"))
        if max_nodes is None:
            max_nodes = _int(defaults.get("max_nodes"))
        graph_index = _int(row.get("graph_index"))
        if graph_index is None:
            graph_index = _int(defaults.get("graph_index"))
        parsed.append(
            BenchTargetSpec(
                target_id=_string(row.get("id"))
                or _string(row.get("target_id"))
                or _required_string(row.get("theorem")),
                workspace=target_workspace,
                file=_resolve_target_path(
                    spec_dir=spec_dir,
                    workspace_root=resolved_workspace,
                    value=_required_string(row.get("file")),
                ),
                theorem=_required_string(row.get("theorem")),
                command=command,
                agent=_string(row.get("agent")) or _string(defaults.get("agent")) or "none",
                timeout_seconds=_number(row.get("timeout_sec"))
                or _number(row.get("timeout_seconds"))
                or _number(defaults.get("timeout_sec"))
                or _number(defaults.get("timeout_seconds"))
                or 60.0,
                max_attempts=_int(row.get("max_attempts"))
                or _int(row.get("max_rounds"))
                or _int(defaults.get("max_attempts"))
                or _int(defaults.get("max_rounds"))
                or 5,
                max_nodes=max_nodes,
                candidate_files=_path_list(spec_dir, row.get("candidate_files") or defaults.get("candidate_files")),
                replay_attempts=_path_list(spec_dir, row.get("replay_attempts") or defaults.get("replay_attempts")),
                decomposition_files=_path_list(
                    spec_dir, row.get("decomposition_files") or defaults.get("decomposition_files")
                ),
                plan_files=_path_list(spec_dir, row.get("plan_files") or defaults.get("plan_files")),
                theorem_step_files=_path_list(
                    spec_dir, row.get("theorem_step_files") or defaults.get("theorem_step_files")
                ),
                source_run_id=_string(row.get("source_run_id")) or _string(defaults.get("source_run_id")),
                source_node_id=_string(row.get("source_node_id")) or _string(defaults.get("source_node_id")),
                graph_index=graph_index,
            )
        )
    return parsed


def _bench_row_from_decompose(result: DecomposeResult) -> BenchTargetResult:
    return BenchTargetResult(
        target_file=result.target_file,
        target_module_path=result.target_module_path,
        theorem=result.theorem,
        command=result.command,
        backend=result.backend,
        status=result.status.value,
        exit_code=result.exit_code.value,
        run_id=result.run_id,
        artifact_dir=result.artifact_dir,
        workspace_root=result.workspace.root,
        workspace_package=result.workspace.package,
        wall_time_seconds=result.wall_time_seconds,
        backend_call_count=result.backend_call_count,
        time_to_first_verified_seconds=result.wall_time_seconds if result.exit_code is ExitCode.SUCCESS else None,
        retrieval_hits=len(result.retrieval_hits),
    )


def _bench_row_from_loop(result: LoopResult) -> BenchTargetResult:
    return BenchTargetResult(
        target_file=result.target_file,
        target_module_path=result.target_module_path,
        theorem=result.theorem,
        command=result.command,
        backend=result.backend,
        status=result.status.value,
        exit_code=result.exit_code.value,
        run_id=result.run_id,
        artifact_dir=result.artifact_dir,
        workspace_root=result.workspace.root,
        workspace_package=result.workspace.package,
        wall_time_seconds=result.wall_time_seconds,
        backend_call_count=result.backend_call_count,
        time_to_first_verified_seconds=result.wall_time_seconds if result.exit_code is ExitCode.SUCCESS else None,
        attempt_count=result.attempt_count,
        verified_attempt=result.verified_attempt,
        retrieval_hits=len(result.retrieval_hits),
        helper_count=len(result.proved_helpers),
    )


def _bench_row_from_graph_result(
    result: ProveResult | ResumeResult,
    *,
    source_run_id: str | None = None,
    source_node_id: str | None = None,
) -> BenchTargetResult:
    plan = result.plan
    graph = result.graph
    helpers = result.helpers
    return BenchTargetResult(
        target_file=result.target_file,
        target_module_path=result.target_module_path,
        theorem=result.theorem,
        command=result.command,
        backend=result.backend,
        status=result.status.value,
        exit_code=result.exit_code.value,
        run_id=result.run_id,
        artifact_dir=result.artifact_dir,
        workspace_root=result.workspace.root,
        workspace_package=result.workspace.package,
        wall_time_seconds=result.wall_time_seconds,
        backend_call_count=result.backend_call_count,
        time_to_first_verified_seconds=result.wall_time_seconds if result.exit_code is ExitCode.SUCCESS else None,
        plan_step_count=0 if plan is None else len(plan.steps),
        node_count=graph.node_count,
        edge_count=graph.edge_count,
        branch_count=len(graph.branch_node_ids),
        frontier_count=len(graph.frontier_node_ids),
        verified_node_id=graph.verified_node_id,
        helper_count=len(helpers),
        source_run_id=source_run_id,
        source_node_id=source_node_id,
    )


def _resume_max_nodes(*, tool_paths: ToolPaths, source_run_id: str, max_nodes: int | None) -> int:
    if max_nodes is not None:
        return max_nodes
    bundle = load_run_bundle(tool_paths=tool_paths, run_id=source_run_id)
    graph_payload = cast(dict[str, JsonValue], bundle.result.get("graph") or {})
    node_payload = cast(list[JsonValue], bundle.result.get("nodes") or [])
    stored_node_count = graph_payload.get("node_count")
    stored_count = stored_node_count if isinstance(stored_node_count, int) else len(node_payload)
    return stored_count + 8


def _resolve_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _resolve_workspace_path(*, spec_path: Path, value: str | None) -> Path | None:
    return _resolve_path(spec_path.parent, value) if value is not None else None


def _resolve_target_path(*, spec_dir: Path, workspace_root: Path | None, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    if workspace_root is not None:
        return (workspace_root / path).resolve()
    return (spec_dir / path).resolve()


def _path_list(base: Path, value: JsonValue | None) -> list[Path]:
    if not isinstance(value, list):
        return []
    return [_resolve_path(base, item) for item in value if isinstance(item, str)]


def _required_string(value: object) -> str:
    if isinstance(value, str):
        return value
    msg = f"Expected string, got {type(value).__name__}."
    raise ValueError(msg)


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _bool(value: object, *, default: bool) -> bool:
    return value if isinstance(value, bool) else default
