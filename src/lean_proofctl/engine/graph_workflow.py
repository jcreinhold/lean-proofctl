"""Resume and apply workflows over stored proof graphs."""

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from lean_proofctl.adapters.base import Backend
from lean_proofctl.engine.config import ToolPaths, WorkspaceBinding
from lean_proofctl.engine.ledger import append_index_row, write_apply_artifacts
from lean_proofctl.engine.models import (
    ApplyErrorCode,
    ApplyResult,
    ApplyStatus,
    Diagnostic,
    DiagnosticClass,
    ExitCode,
    HelperRef,
    HelperState,
    HelperStatus,
    JsonValue,
    ProofGraphNode,
    Range,
    ResumeResult,
    RunManifest,
    Severity,
    StrategyFamily,
    TaskKind,
    TheoremPlan,
    TheoremPlanStep,
)
from lean_proofctl.engine.patching import apply_verified_transplant
from lean_proofctl.engine.proofctl import stored_proof_target_contract_error
from lean_proofctl.engine.prove import ProveOptions, ResumeSeed, run_resume_prove
from lean_proofctl.engine.replay import (
    RunBundle,
    _resolve_replay_workspace,
    _resolve_stored_target_file,
    _stored_target_module_path,
    _string,
    load_run_bundle,
)
from lean_proofctl.engine.run_id import new_run_id
from lean_proofctl.engine.sandbox import create_sandbox_workspace


@dataclass(frozen=True)
class GraphNodeSelector:
    """Optional stored graph node selector."""

    node_id: str | None = None
    graph_index: int | None = None


def resume_graph_run(
    *,
    tool_paths: ToolPaths,
    workspace_override: WorkspaceBinding | None,
    source_run_id: str,
    selector: GraphNodeSelector,
    backend: Backend,
    max_nodes: int,
    timeout_seconds: float,
) -> tuple[ResumeResult, int]:
    """Resume one stored prove graph from its frontier or a selected node."""
    bundle = load_run_bundle(tool_paths=tool_paths, run_id=source_run_id)
    if _string(bundle.request.get("command")) != "prove":
        raise ValueError(f"`proofctl resume` only supports `prove` runs, got `{bundle.request.get('command')}`.")
    target_file = _resolve_stored_target_file(
        tool_paths=tool_paths,
        workspace_override=workspace_override,
        bundle=bundle,
    )
    workspace = _resolve_replay_workspace(
        tool_paths=tool_paths,
        workspace_override=workspace_override,
        bundle=bundle,
        target_file=target_file,
    )
    theorem = _required_string(bundle.request.get("theorem"))
    seed = _resume_seed(bundle=bundle, selector=selector)
    options = ProveOptions(
        target_file=target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        max_nodes=max_nodes,
        max_decomposition_depth=_required_int(bundle.request.get("max_decomposition_depth") or 2),
    )
    return run_resume_prove(
        tool_paths=tool_paths,
        workspace=workspace,
        options=options,
        backend=backend,
        seed=seed,
    )


def apply_graph_run(
    *,
    tool_paths: ToolPaths,
    workspace_override: WorkspaceBinding | None,
    source_run_id: str,
    selector: GraphNodeSelector,
) -> tuple[ApplyResult, int]:
    """Apply one verified theorem node plus managed helpers to the real workspace."""
    started_at = datetime.now(UTC)
    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    bundle = load_run_bundle(tool_paths=tool_paths, run_id=source_run_id)
    source_command = _string(bundle.request.get("command")) or _string(bundle.manifest.get("command"))
    if source_command != "prove":
        return _apply_error(
            run_id=run_id,
            run_dir=run_dir,
            source_run_id=source_run_id,
            target_file=_string(bundle.request.get("target_file")) or "",
            target_module_path=_stored_target_module_path(bundle),
            theorem=_string(bundle.request.get("theorem")),
            workspace_root=_string(bundle.request.get("workspace_root")),
            workspace_package=_string(bundle.request.get("workspace_package")),
            started_at=started_at,
            error_code=ApplyErrorCode.INVALID_SOURCE_RUN,
            error_message=f"`proofctl apply` only supports `prove` runs, got `{source_command}`.",
            source_node_id=selector.node_id,
        )
    contract_error = stored_proof_target_contract_error(
        command="proofctl apply",
        proof_target=cast(JsonValue | None, bundle.request.get("proof_target")),
        payload=_stored_probe_payload(bundle, "declaration-target"),
    )
    if contract_error is not None:
        return _apply_error(
            run_id=run_id,
            run_dir=run_dir,
            source_run_id=source_run_id,
            target_file=_string(bundle.request.get("target_file")) or "",
            target_module_path=_stored_target_module_path(bundle),
            theorem=_string(bundle.request.get("theorem")),
            workspace_root=_string(bundle.request.get("workspace_root")),
            workspace_package=_string(bundle.request.get("workspace_package")),
            started_at=started_at,
            error_code=ApplyErrorCode.INVALID_SOURCE_RUN,
            error_message=contract_error,
            source_node_id=selector.node_id,
        )

    target_file = _resolve_stored_target_file(
        tool_paths=tool_paths,
        workspace_override=workspace_override,
        bundle=bundle,
    )
    workspace = _resolve_replay_workspace(
        tool_paths=tool_paths,
        workspace_override=workspace_override,
        bundle=bundle,
        target_file=target_file,
    )
    theorem = _string(bundle.request.get("theorem"))
    nodes = _load_graph_nodes(bundle)
    helpers = _load_helper_states(bundle)
    selected_node, error_code, error_message = _select_verified_node(
        nodes=nodes, selector=selector, run_id=source_run_id
    )
    if selected_node is None:
        return _apply_error(
            run_id=run_id,
            run_dir=run_dir,
            source_run_id=source_run_id,
            target_file=_project_relative(target_file, workspace.root),
            target_module_path=_stored_target_module_path(bundle),
            theorem=theorem,
            workspace_root=str(workspace.root),
            workspace_package=workspace.package_name,
            started_at=started_at,
            error_code=cast(ApplyErrorCode, error_code),
            error_message=cast(str, error_message),
            source_node_id=selector.node_id or _graph_index_node_id(nodes=nodes, graph_index=selector.graph_index),
        )
    helper_by_id = {helper.helper_id: helper for helper in helpers}
    helper_refs = [
        HelperRef(
            helper_name=helper.helper_name or helper.helper_id,
            module_name=helper.module_name,
            file_path=helper.file_path,
            theorem=helper.source_theorem,
            statement=helper.statement,
        )
        for helper_id in selected_node.active_helper_ids
        for helper in [helper_by_id.get(helper_id)]
        if helper is not None and helper.status is HelperStatus.VERIFIED
    ]
    sandbox = create_sandbox_workspace(workspace=workspace, run_dir=run_dir)
    shutil.rmtree(sandbox.helper_root_dir, ignore_errors=True)
    sandbox.helper_root_dir.mkdir(parents=True, exist_ok=True)
    for helper_ref in helper_refs:
        sandbox.materialize_helper(helper_ref)
    helper_modules = [helper.module_name for helper in helper_refs]
    try:
        apply_verified_transplant(
            sandbox=sandbox,
            target_file=target_file,
            theorem=_required_string(bundle.request.get("theorem")),
            candidate_body=selected_node.theorem_draft,
            helper_modules=helper_modules,
        )
    except FileNotFoundError as error:
        return _apply_error(
            run_id=run_id,
            run_dir=run_dir,
            source_run_id=source_run_id,
            target_file=_project_relative(target_file, workspace.root),
            target_module_path=_stored_target_module_path(bundle),
            theorem=theorem,
            workspace_root=str(workspace.root),
            workspace_package=workspace.package_name,
            started_at=started_at,
            error_code=ApplyErrorCode.HELPER_SOURCE_MISSING,
            error_message=str(error),
            source_node_id=selected_node.node_id,
            applied_helper_modules=helper_modules,
        )
    except ValueError as error:
        error_text = str(error)
        code = (
            ApplyErrorCode.HELPER_OUTSIDE_MANAGED_ROOT
            if "outside managed root" in error_text
            else ApplyErrorCode.HELPER_CONFLICT
        )
        return _apply_error(
            run_id=run_id,
            run_dir=run_dir,
            source_run_id=source_run_id,
            target_file=_project_relative(target_file, workspace.root),
            target_module_path=_stored_target_module_path(bundle),
            theorem=theorem,
            workspace_root=str(workspace.root),
            workspace_package=workspace.package_name,
            started_at=started_at,
            error_code=code,
            error_message=error_text,
            source_node_id=selected_node.node_id,
            applied_helper_modules=helper_modules,
        )

    finished_at = datetime.now(UTC)
    result = ApplyResult(
        run_id=run_id,
        command="apply",
        status=ApplyStatus.APPLIED,
        exit_code=ExitCode.SUCCESS,
        source_run_id=source_run_id,
        source_node_id=selected_node.node_id,
        target_file=_project_relative(target_file, workspace.root),
        target_module_path=_stored_target_module_path(bundle),
        theorem=theorem,
        workspace_root=str(workspace.root),
        workspace_package=workspace.package_name,
        artifact_dir=str(run_dir),
        applied_helper_modules=helper_modules,
        started_at=started_at,
        finished_at=finished_at,
    )
    manifest = RunManifest(
        run_id=run_id,
        command="apply",
        status=result.status,
        exit_code=result.exit_code,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    request = {
        "source_run_id": source_run_id,
        "source_node_id": selected_node.node_id,
        "target_file": result.target_file,
        "target_module_path": result.target_module_path,
        "workspace_root": result.workspace_root,
    }
    write_apply_artifacts(run_dir=run_dir, manifest=manifest, request=request, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, 0


def _resume_seed(*, bundle: RunBundle, selector: GraphNodeSelector) -> ResumeSeed:
    nodes = _load_graph_nodes(bundle)
    if not nodes:
        raise ValueError(f"Run `{bundle.run_id}` has no stored proof graph.")
    summary = cast(dict[str, JsonValue], bundle.result.get("graph") or {})
    selected_node_id: str | None = None
    frontier_node_ids: list[str] = []
    if selector.node_id is not None or selector.graph_index is not None:
        node = _select_node(nodes=nodes, selector=selector)
        if node is None:
            raise ValueError(f"Run `{bundle.run_id}` does not contain the selected node.")
        if node.node_kind != "theorem-state":
            raise ValueError(f"Selected node `{node.node_id}` is not a theorem state.")
        if node.status == "verified":
            raise ValueError(f"Selected node `{node.node_id}` is already verified.")
        selected_node_id = node.node_id
        frontier_node_ids = [node.node_id]
    else:
        node_ids = {node.node_id for node in nodes}
        stored_frontier = [node_id for node_id in _string_list(summary.get("frontier_node_ids")) if node_id in node_ids]
        frontier_node_ids = [
            node_id
            for node_id in stored_frontier
            if next(node for node in nodes if node.node_id == node_id).node_kind == "theorem-state"
        ]
        if not frontier_node_ids:
            raise ValueError(f"Run `{bundle.run_id}` has no saved frontier to resume.")
    plan = _load_plan(bundle)
    return ResumeSeed(
        source_run_id=bundle.run_id,
        source_node_id=selected_node_id,
        plan=plan,
        nodes=nodes,
        helpers=_load_helper_states(bundle),
        frontier_node_ids=frontier_node_ids,
    )


def _apply_error(
    *,
    run_id: str,
    run_dir: Path,
    source_run_id: str,
    target_file: str,
    target_module_path: str | None,
    theorem: str | None,
    workspace_root: str | None,
    workspace_package: str | None,
    started_at: datetime,
    error_code: ApplyErrorCode,
    error_message: str,
    source_node_id: str | None,
    applied_helper_modules: list[str] | None = None,
) -> tuple[ApplyResult, int]:
    finished_at = datetime.now(UTC)
    result = ApplyResult(
        run_id=run_id,
        command="apply",
        status=ApplyStatus.ERROR,
        exit_code=ExitCode.BAD_USAGE
        if error_code
        in {
            ApplyErrorCode.NO_VERIFIED_NODE,
            ApplyErrorCode.AMBIGUOUS_VERIFIED_NODE,
            ApplyErrorCode.NODE_NOT_FOUND,
            ApplyErrorCode.NODE_NOT_THEOREM_STATE,
            ApplyErrorCode.NODE_NOT_VERIFIED,
            ApplyErrorCode.INVALID_SOURCE_RUN,
        }
        else ExitCode.CHECK_FAILED,
        source_run_id=source_run_id,
        source_node_id=source_node_id,
        target_file=target_file,
        target_module_path=target_module_path,
        theorem=theorem,
        workspace_root=workspace_root,
        workspace_package=workspace_package,
        artifact_dir=str(run_dir),
        applied_helper_modules=list(applied_helper_modules or []),
        started_at=started_at,
        finished_at=finished_at,
        error_code=error_code,
        error_message=error_message,
    )
    manifest = RunManifest(
        run_id=run_id,
        command="apply",
        status=result.status,
        exit_code=result.exit_code,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    request = {"source_run_id": source_run_id, "source_node_id": source_node_id, "target_file": target_file}
    write_apply_artifacts(run_dir=run_dir, manifest=manifest, request=request, result=result)
    return result, 2 if result.exit_code is ExitCode.BAD_USAGE else 1


def _stored_probe_payload(bundle: RunBundle, probe_name: str) -> dict[str, JsonValue] | None:
    for container_name in ("semantic_probe_results", "probe_results"):
        container = bundle.request.get(container_name)
        if not isinstance(container, dict):
            continue
        payload = container.get(probe_name)
        if isinstance(payload, dict):
            return cast(dict[str, JsonValue], payload)
    return None


def _select_verified_node(
    *, nodes: list[ProofGraphNode], selector: GraphNodeSelector, run_id: str
) -> tuple[ProofGraphNode | None, ApplyErrorCode | None, str | None]:
    if selector.node_id is not None or selector.graph_index is not None:
        node = _select_node(nodes=nodes, selector=selector)
        if node is None:
            return None, ApplyErrorCode.NODE_NOT_FOUND, f"Run `{run_id}` does not contain the selected node."
        if node.node_kind != "theorem-state":
            return None, ApplyErrorCode.NODE_NOT_THEOREM_STATE, f"Node `{node.node_id}` is not a theorem state."
        if node.status != "verified":
            return None, ApplyErrorCode.NODE_NOT_VERIFIED, f"Node `{node.node_id}` is not verified."
        return node, None, None
    verified_nodes = [node for node in nodes if node.node_kind == "theorem-state" and node.status == "verified"]
    if not verified_nodes:
        return None, ApplyErrorCode.NO_VERIFIED_NODE, f"Run `{run_id}` has no verified theorem node to apply."
    if len(verified_nodes) > 1:
        node_ids = ", ".join(node.node_id for node in verified_nodes)
        return (
            None,
            ApplyErrorCode.AMBIGUOUS_VERIFIED_NODE,
            f"Run `{run_id}` has multiple verified theorem nodes: {node_ids}.",
        )
    return verified_nodes[0], None, None


def _select_node(*, nodes: list[ProofGraphNode], selector: GraphNodeSelector) -> ProofGraphNode | None:
    if selector.node_id is not None:
        return next((node for node in nodes if node.node_id == selector.node_id), None)
    if selector.graph_index is not None:
        return next((node for node in nodes if node.graph_index == selector.graph_index), None)
    return None


def _graph_index_node_id(*, nodes: list[ProofGraphNode], graph_index: int | None) -> str | None:
    if graph_index is None:
        return None
    node = next((item for item in nodes if item.graph_index == graph_index), None)
    return None if node is None else node.node_id


def _load_graph_nodes(bundle: RunBundle) -> list[ProofGraphNode]:
    payload = cast(list[dict[str, JsonValue]], bundle.result.get("nodes") or [])
    return [_proof_graph_node_from_json(item) for item in payload if isinstance(item, dict)]


def _load_helper_states(bundle: RunBundle) -> list[HelperState]:
    payload = cast(list[dict[str, JsonValue]], bundle.result.get("helpers") or [])
    return [_helper_state_from_json(item) for item in payload if isinstance(item, dict)]


def _proof_graph_node_from_json(payload: dict[str, JsonValue]) -> ProofGraphNode:
    return ProofGraphNode(
        graph_index=_required_int(payload.get("graph_index")),
        node_id=_required_string(payload.get("node_id")),
        node_kind=_required_string(payload.get("node_kind")),
        parent_node_id=_string(payload.get("parent_node_id")),
        strategy_family=None
        if _string(payload.get("strategy_family")) is None
        else StrategyFamily(_required_string(payload.get("strategy_family"))),
        task_kind=None
        if _string(payload.get("task_kind")) is None
        else TaskKind(_required_string(payload.get("task_kind"))),
        session_id=_string(payload.get("session_id")),
        session_artifact_dir=_string(payload.get("session_artifact_dir")),
        session_node_id=_string(payload.get("session_node_id")),
        action_payload=cast(dict[str, JsonValue], payload.get("action_payload") or {}),
        theorem_draft=_required_string(payload.get("theorem_draft")),
        diagnostics=_diagnostics_from_json(payload.get("diagnostics")),
        goals=_string_list(payload.get("goals")),
        probe_snapshot=cast(dict[str, JsonValue], payload.get("probe_snapshot") or {}),
        score=_required_int(payload.get("score")),
        status=_required_string(payload.get("status")),
        dedup_signature=_required_string(payload.get("dedup_signature")),
        decomposition_depth=_required_int(payload.get("decomposition_depth") or 0),
        active_helper_ids=_string_list(payload.get("active_helper_ids")),
        helper_id=_string(payload.get("helper_id")),
        backend_request_path=_string(payload.get("backend_request_path")),
        backend_response_path=_string(payload.get("backend_response_path")),
    )


def _helper_state_from_json(payload: dict[str, JsonValue]) -> HelperState:
    return HelperState(
        helper_id=_required_string(payload.get("helper_id")),
        source_theorem=_required_string(payload.get("source_theorem")),
        statement=_required_string(payload.get("statement")),
        module_name=_required_string(payload.get("module_name")),
        file_path=_required_string(payload.get("file_path")),
        dependencies=_string_list(payload.get("dependencies")),
        status=HelperStatus(_required_string(payload.get("status"))),
        consumer_theorem_states=_string_list(payload.get("consumer_theorem_states")),
        decomposition_depth=_required_int(payload.get("decomposition_depth") or 0),
        helper_name=_string(payload.get("helper_name")),
        source_node_id=_string(payload.get("source_node_id")),
        verification_artifact_dir=_string(payload.get("verification_artifact_dir")),
        proof_stub=_string(payload.get("proof_stub")),
        diagnostics=_diagnostics_from_json(payload.get("diagnostics")),
    )


def _load_plan(bundle: RunBundle) -> TheoremPlan | None:
    payload = cast(dict[str, JsonValue] | None, bundle.result.get("plan"))
    if payload is None:
        return None
    return TheoremPlan(
        summary=_required_string(payload.get("summary")),
        strategy=_required_string(payload.get("strategy")),
        steps=[
            TheoremPlanStep(
                step_id=_required_string(step.get("step_id")),
                title=_required_string(step.get("title")),
                goal=_required_string(step.get("goal")),
                rationale=_required_string(step.get("rationale")),
                depends_on=_string_list(step.get("depends_on")),
                helper_suggestions=_string_list(step.get("helper_suggestions")),
                retrieval_refs=_string_list(step.get("retrieval_refs")),
                file_actions=_string_list(step.get("file_actions")),
            )
            for step in cast(list[dict[str, JsonValue]], payload.get("steps") or [])
            if isinstance(step, dict)
        ],
        helper_gaps=_string_list(payload.get("helper_gaps")),
        risks=_string_list(payload.get("risks")),
    )


def _diagnostics_from_json(payload: object) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for item in cast(list[JsonValue], payload or []):
        if not isinstance(item, dict):
            continue
        range_payload = cast(dict[str, JsonValue], item.get("range") or {})
        diagnostics.append(
            Diagnostic(
                file=_required_string(item.get("file")),
                range=Range(
                    startLine=_required_int(range_payload.get("startLine")),
                    startCol=_required_int(range_payload.get("startCol")),
                    endLine=_required_int(range_payload.get("endLine")),
                    endCol=_required_int(range_payload.get("endCol")),
                ),
                severity=Severity(_required_string(item.get("severity"))),
                message=_required_string(item.get("message")),
                class_name=DiagnosticClass(_string(item.get("class")) or _required_string(item.get("class_name"))),
            )
        )
    return diagnostics


def _project_relative(path: Path, workspace_root: Path) -> str:
    return str(path.resolve().relative_to(workspace_root.resolve()))


def _required_string(value: JsonValue | None) -> str:
    if isinstance(value, str):
        return value
    raise ValueError(f"Expected string, got {value!r}")


def _required_int(value: JsonValue | None) -> int:
    if isinstance(value, int):
        return value
    raise ValueError(f"Expected int, got {value!r}")


def _string_list(value: object) -> list[str]:
    return [item for item in cast(list[object], value or []) if isinstance(item, str)]
