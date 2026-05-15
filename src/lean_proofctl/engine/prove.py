"""Graph-backed theorem proving loop built on theorem sessions."""

import json
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

from lean_proofctl.adapters.base import Backend
from lean_proofctl.engine.backend_protocol import classify_theorem_body_response
from lean_proofctl.engine.config import ToolPaths, WorkspaceBinding
from lean_proofctl.engine.inspection import (
    InspectionState,
    _workspace_metadata,
    build_agent_request,
    build_inspection_snapshot,
    build_inspection_state,
)
from lean_proofctl.engine.lean_runner import run_lean_check, run_lean_make_olean
from lean_proofctl.engine.ledger import append_index_row, write_prove_artifacts, write_resume_artifacts
from lean_proofctl.engine.models import (
    AgentRequest,
    AgentResponse,
    BackendResultKind,
    CheckResult,
    CheckStatus,
    DecompositionLayer,
    DecompositionStep,
    Diagnostic,
    DiagnosticClass,
    ExitCode,
    FileSpan,
    HelperRef,
    HelperState,
    HelperStatus,
    InspectionSnapshot,
    JsonValue,
    ProofGraphNode,
    ProofGraphSummary,
    ProveResult,
    ProveStatus,
    Range,
    ResumeResult,
    ResumeStatus,
    RetrievalHit,
    RunManifest,
    Severity,
    SourceWindow,
    StrategyFamily,
    TaskKind,
    TheoremPlan,
    TheoremSessionActionKind,
    dataclass_to_jsonable,
)
from lean_proofctl.engine.patching import helper_module_name, render_helper_file
from lean_proofctl.engine.run_id import new_run_id
from lean_proofctl.engine.sandbox import (
    MANAGED_HELPER_MODULE_ROOT,
    MANAGED_HELPER_ROOT,
    SandboxWorkspace,
    create_sandbox_workspace,
)
from lean_proofctl.engine.session import (
    SessionSemanticRuntime,
    finish_session,
    load_session_state,
    start_session,
    step_session,
)


@dataclass(frozen=True)
class ProveOptions:
    """Stable inputs for one graph-backed prove run."""

    target_file: Path
    theorem: str
    timeout_seconds: float
    max_nodes: int
    max_backend_calls: int | None = None
    max_decomposition_depth: int = 2


@dataclass(frozen=True)
class _Expansion:
    family: StrategyFamily
    request: AgentRequest
    response: AgentResponse
    action_payloads: list[dict[str, JsonValue]]


@dataclass(frozen=True)
class _MaterializedAction:
    nodes: list[ProofGraphNode]
    helper_states: list[HelperState]
    backend_call_count: int = 0


@dataclass(frozen=True)
class ResumeSeed:
    """Stored graph state used to continue one prove run."""

    source_run_id: str
    source_node_id: str | None
    plan: TheoremPlan | None
    nodes: list[ProofGraphNode]
    helpers: list[HelperState]
    frontier_node_ids: list[str]


@dataclass(frozen=True)
class _GraphSearchResult:
    plan: TheoremPlan | None
    graph: ProofGraphSummary
    helpers: list[HelperState]
    nodes: list[ProofGraphNode]
    backend_call_count: int


def run_prove(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    options: ProveOptions,
    backend: Backend,
) -> tuple[ProveResult, int]:
    """Run a bounded graph-backed prove loop over theorem sessions."""
    started_at = datetime.now(UTC)
    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    target_file = options.target_file.expanduser().resolve()

    plan, inspection_state, inspection_snapshot, plan_request, plan_backend_calls = _plan_phase(
        tool_paths=tool_paths,
        workspace=workspace,
        run_dir=run_dir,
        target_file=target_file,
        theorem=options.theorem,
        timeout_seconds=options.timeout_seconds,
        backend=backend,
        max_backend_calls=options.max_backend_calls,
        command_name="prove",
    )
    session_runtime = SessionSemanticRuntime(tool_paths=tool_paths)
    try:
        search = _run_graph_search(
            tool_paths=tool_paths,
            workspace=workspace,
            options=options,
            backend=backend,
            run_id=run_id,
            run_dir=run_dir,
            target_file=target_file,
            inspection_state=inspection_state,
            inspection_snapshot=inspection_snapshot,
            plan=plan,
            seed=None,
            initial_backend_call_count=plan_backend_calls,
            session_runtime=session_runtime,
        )
    finally:
        session_runtime.close()
    finished_at = datetime.now(UTC)
    status = ProveStatus.VERIFIED if search.graph.verified_node_id is not None else ProveStatus.UNSOLVED
    exit_code = ExitCode.SUCCESS if search.graph.verified_node_id is not None else ExitCode.CHECK_FAILED
    result = ProveResult(
        run_id=run_id,
        command="prove",
        status=status,
        exit_code=exit_code,
        target_file=_project_relative(target_file, workspace.root),
        target_module_path=str(inspection_state.target_module_path),
        theorem=options.theorem,
        tool_root=str(tool_paths.root),
        workspace=_workspace_metadata(workspace),
        artifact_dir=str(run_dir),
        backend=backend.name(),
        wall_time_seconds=(finished_at - started_at).total_seconds(),
        backend_call_count=search.backend_call_count,
        max_decomposition_depth=options.max_decomposition_depth,
        plan=search.plan,
        graph=search.graph,
        helpers=search.helpers,
        nodes=search.nodes,
        started_at=started_at,
        finished_at=finished_at,
    )
    request_payload = {
        "command": "prove",
        "target_file": _project_relative(target_file, workspace.root),
        "workspace_root": str(workspace.root),
        "workspace_package": workspace.package_name,
        "workspace_toolchain": workspace.toolchain,
        "target_module_path": str(inspection_state.target_module_path),
        "timeout_seconds": options.timeout_seconds,
        "theorem": options.theorem,
        "backend": backend.name(),
        "max_nodes": options.max_nodes,
        "max_decomposition_depth": options.max_decomposition_depth,
        "source_window": dataclass_to_jsonable(plan_request.source_window),
        "diagnostics": [diagnostic.to_json() for diagnostic in plan_request.diagnostics],
        "retrieval_hits": dataclass_to_jsonable(inspection_snapshot.retrieval_hits),
        "active_helpers": dataclass_to_jsonable(inspection_snapshot.active_helpers),
        "helpers": [helper.to_json() for helper in result.helpers],
        "proof_target": plan_request.proof_target,
        "proof_state": plan_request.proof_state,
        "syntax_probe_results": plan_request.syntax_probe_results,
        "semantic_probe_results": plan_request.semantic_probe_results,
        "probe_results": plan_request.probe_results,
    }
    manifest = RunManifest(
        run_id=run_id,
        command="prove",
        status=status,
        exit_code=exit_code,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    write_prove_artifacts(run_dir=run_dir, manifest=manifest, request=request_payload, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, _exit_code_number(exit_code)


def run_resume_prove(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    options: ProveOptions,
    backend: Backend,
    seed: ResumeSeed,
) -> tuple[ResumeResult, int]:
    """Continue one stored proof graph from its frontier or a selected node."""
    started_at = datetime.now(UTC)
    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    target_file = options.target_file.expanduser().resolve()
    inspection_state, inspection_snapshot = _inspection_phase(
        tool_paths=tool_paths,
        workspace=workspace,
        run_dir=run_dir,
        target_file=target_file,
        theorem=options.theorem,
        timeout_seconds=options.timeout_seconds,
        command_name="resume",
    )
    session_runtime = SessionSemanticRuntime(tool_paths=tool_paths)
    try:
        search = _run_graph_search(
            tool_paths=tool_paths,
            workspace=workspace,
            options=options,
            backend=backend,
            run_id=run_id,
            run_dir=run_dir,
            target_file=target_file,
            inspection_state=inspection_state,
            inspection_snapshot=inspection_snapshot,
            plan=seed.plan,
            seed=seed,
            initial_backend_call_count=0,
            session_runtime=session_runtime,
        )
    finally:
        session_runtime.close()
    finished_at = datetime.now(UTC)
    status = ResumeStatus.VERIFIED if search.graph.verified_node_id is not None else ResumeStatus.UNSOLVED
    exit_code = ExitCode.SUCCESS if search.graph.verified_node_id is not None else ExitCode.CHECK_FAILED
    result = ResumeResult(
        run_id=run_id,
        command="resume",
        status=status,
        exit_code=exit_code,
        source_run_id=seed.source_run_id,
        source_node_id=seed.source_node_id,
        target_file=_project_relative(target_file, workspace.root),
        target_module_path=str(inspection_state.target_module_path),
        theorem=options.theorem,
        tool_root=str(tool_paths.root),
        workspace=_workspace_metadata(workspace),
        artifact_dir=str(run_dir),
        backend=backend.name(),
        wall_time_seconds=(finished_at - started_at).total_seconds(),
        backend_call_count=search.backend_call_count,
        max_decomposition_depth=options.max_decomposition_depth,
        plan=search.plan,
        graph=search.graph,
        helpers=search.helpers,
        nodes=search.nodes,
        started_at=started_at,
        finished_at=finished_at,
    )
    request_payload = {
        "command": "resume",
        "source_run_id": seed.source_run_id,
        "source_node_id": seed.source_node_id,
        "target_file": _project_relative(target_file, workspace.root),
        "workspace_root": str(workspace.root),
        "workspace_package": workspace.package_name,
        "workspace_toolchain": workspace.toolchain,
        "target_module_path": str(inspection_state.target_module_path),
        "timeout_seconds": options.timeout_seconds,
        "theorem": options.theorem,
        "backend": backend.name(),
        "max_nodes": options.max_nodes,
        "max_decomposition_depth": options.max_decomposition_depth,
        "frontier_node_ids": seed.frontier_node_ids,
        "helpers": [helper.to_json() for helper in result.helpers],
        "proof_target": inspection_snapshot.proof_target,
        "proof_state": inspection_snapshot.proof_state,
        "syntax_probe_results": inspection_snapshot.syntax_probe_results,
        "semantic_probe_results": inspection_snapshot.semantic_probe_results,
        "probe_results": inspection_snapshot.probe_results,
    }
    manifest = RunManifest(
        run_id=run_id,
        command="resume",
        status=status,
        exit_code=exit_code,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    write_resume_artifacts(run_dir=run_dir, manifest=manifest, request=request_payload, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, _exit_code_number(exit_code)


def _inspection_phase(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    run_dir: Path,
    target_file: Path,
    theorem: str,
    timeout_seconds: float,
    command_name: str,
) -> tuple[InspectionState, InspectionSnapshot]:
    inspect_root = run_dir / f"plan-{run_dir.name}"
    sandbox = create_sandbox_workspace(workspace=workspace, run_dir=inspect_root)
    state = build_inspection_state(
        tool_paths=tool_paths,
        workspace=workspace,
        sandbox=sandbox,
        target_file=target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        command_name=command_name,
    )
    inspection = build_inspection_snapshot(state=state, tool_paths=tool_paths, diagnostics=[])
    return state, inspection


def _plan_phase(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    run_dir: Path,
    target_file: Path,
    theorem: str,
    timeout_seconds: float,
    backend: Backend,
    max_backend_calls: int | None,
    command_name: str,
) -> tuple[TheoremPlan | None, InspectionState, InspectionSnapshot, AgentRequest, int]:
    state, inspection = _inspection_phase(
        tool_paths=tool_paths,
        workspace=workspace,
        run_dir=run_dir,
        target_file=target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        command_name=command_name,
    )
    request = build_agent_request(
        task_kind=TaskKind.PLAN,
        attempt_index=1,
        state=state,
        diagnostics=[],
        prior_attempts=[],
        retrieval_hits=inspection.retrieval_hits,
        active_helpers=inspection.active_helpers,
        schema_path=tool_paths.prompts / "schemas" / "plan.schema.json",
        allowed_output_files=[],
        candidate_limit=0,
    )
    if max_backend_calls is not None and max_backend_calls <= 0:
        return None, state, inspection, request, 0
    response = backend.propose(request)
    _write_backend_exchange(
        request_path=run_dir / "plan" / "backend" / "request.json",
        response_path=run_dir / "plan" / "backend" / "response.json",
        request=request,
        response=response,
    )
    plan = response.theorem_plan if response.result_kind is BackendResultKind.THEOREM_PLAN else None
    return plan, state, inspection, request, 1


def _run_graph_search(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    options: ProveOptions,
    backend: Backend,
    run_id: str,
    run_dir: Path,
    target_file: Path,
    inspection_state: InspectionState,
    inspection_snapshot: InspectionSnapshot,
    plan: TheoremPlan | None,
    seed: ResumeSeed | None,
    initial_backend_call_count: int,
    session_runtime: SessionSemanticRuntime,
) -> _GraphSearchResult:
    if seed is None:
        root_node = _start_root_node(
            tool_paths=tool_paths,
            workspace=workspace,
            target_file=target_file,
            theorem=options.theorem,
            timeout_seconds=options.timeout_seconds,
            source_run_id=run_id,
            session_runtime=session_runtime,
        )
        nodes: list[ProofGraphNode] = [root_node]
        helper_states: list[HelperState] = []
        frontier_ids: list[str] = [root_node.node_id]
        _emit_progress(
            f"root={root_node.node_id} diagnostics={len(root_node.diagnostics)} goals={len(root_node.goals)}"
        )
    else:
        nodes = list(seed.nodes)
        helper_states = list(seed.helpers)
        frontier_ids = list(dict.fromkeys(seed.frontier_node_ids))
        _emit_progress(f"resume={seed.source_run_id} frontier={len(frontier_ids)} seed_nodes={len(nodes)}")
    helper_by_id = {helper.helper_id: helper for helper in helper_states}
    node_by_id = {node.node_id: node for node in nodes}
    seen_signatures = {
        node.dedup_signature: node.node_id
        for node in nodes
        if node.node_kind == "theorem-state" and node.status != "deduped"
    }
    family_attempts = _family_attempt_counts(nodes)
    verified_node_id = next(
        (node.node_id for node in nodes if node.node_kind == "theorem-state" and node.status == "verified"),
        None,
    )
    backend_call_count = initial_backend_call_count
    decomposition_attempt_count = 0

    while frontier_ids and len(nodes) < options.max_nodes and verified_node_id is None:
        current = _pick_frontier(frontier_ids=frontier_ids, node_by_id=node_by_id)
        frontier_ids = [node_id for node_id in frontier_ids if node_id != current.node_id]
        expanded_children: list[ProofGraphNode] = []
        for family in _strategy_order(node=current, plan=plan):
            if len(nodes) >= options.max_nodes or verified_node_id is not None:
                break
            if options.max_backend_calls is not None and backend_call_count >= options.max_backend_calls:
                break
            family_attempts[family] += 1
            expansion, backend_calls_used = _expand_node(
                tool_paths=tool_paths,
                workspace=workspace,
                run_dir=run_dir,
                target_file=target_file,
                theorem=options.theorem,
                timeout_seconds=options.timeout_seconds,
                inspection_state=inspection_state,
                inspection_retrieval=inspection_snapshot.retrieval_hits,
                backend=backend,
                parent=current,
                family=family,
                attempt_index=family_attempts[family],
                helper_by_id=helper_by_id,
            )
            backend_call_count += backend_calls_used
            if expansion is None:
                continue
            _emit_progress(f"expand={current.node_id} strategy={family.value} actions={len(expansion.action_payloads)}")
            request_path = (
                run_dir
                / "graph"
                / "requests"
                / f"{current.node_id}-{family.value}-{family_attempts[family]:03d}.request.json"
            )
            response_path = (
                run_dir
                / "graph"
                / "requests"
                / f"{current.node_id}-{family.value}-{family_attempts[family]:03d}.response.json"
            )
            if not expansion.action_payloads:
                _write_backend_exchange(
                    request_path=request_path,
                    response_path=response_path,
                    request=expansion.request,
                    response=expansion.response,
                )
                continue
            for action_offset, action_payload in enumerate(expansion.action_payloads, start=1):
                if len(nodes) >= options.max_nodes:
                    break
                materialized = _materialize_action(
                    tool_paths=tool_paths,
                    workspace=workspace,
                    parent=current,
                    family=family,
                    action_payload=action_payload,
                    theorem=options.theorem,
                    target_file=target_file,
                    timeout_seconds=options.timeout_seconds,
                    graph_index=len(nodes),
                    run_id=run_id,
                    run_dir=run_dir,
                    backend_request_path=str(request_path),
                    backend_response_path=str(response_path),
                    helper_by_id=helper_by_id,
                    backend=backend,
                    max_decomposition_depth=options.max_decomposition_depth,
                    decomposition_attempt_index=decomposition_attempt_count + 1,
                    max_backend_calls=(
                        None
                        if options.max_backend_calls is None
                        else max(0, options.max_backend_calls - backend_call_count)
                    ),
                    max_nodes=options.max_nodes - len(nodes),
                    session_runtime=session_runtime,
                )
                backend_call_count += materialized.backend_call_count
                decomposition_attempt_count += materialized.backend_call_count
                for state in materialized.helper_states:
                    helper_by_id[state.helper_id] = state
                    if not any(existing.helper_id == state.helper_id for existing in helper_states):
                        helper_states.append(state)
                first_child: ProofGraphNode | None = None
                for child_node in materialized.nodes:
                    if first_child is None:
                        first_child = child_node
                    dedup_target = seen_signatures.get(child_node.dedup_signature)
                    if dedup_target is not None and child_node.node_kind == "theorem-state":
                        child_node = replace(child_node, status="deduped")
                    else:
                        seen_signatures[child_node.dedup_signature] = child_node.node_id
                    nodes.append(child_node)
                    node_by_id[child_node.node_id] = child_node
                    expanded_children.append(child_node)
                    if child_node.status == "verified" and child_node.node_kind == "theorem-state":
                        verified_node_id = child_node.node_id
                    if child_node.status == "frontier":
                        frontier_ids.append(child_node.node_id)
                    _write_graph_node(run_dir=run_dir, node=child_node)
                    if verified_node_id is not None or len(nodes) >= options.max_nodes:
                        break
                if action_offset == 1 and first_child is not None:
                    _write_backend_exchange(
                        request_path=request_path,
                        response_path=response_path,
                        request=expansion.request,
                        response=expansion.response,
                    )
                if verified_node_id is not None:
                    break
        node_by_id[current.node_id] = replace(current, status="expanded" if expanded_children else "stalled")

    final_frontier = [node_id for node_id in frontier_ids if node_by_id[node_id].status == "frontier"]
    branch_node_ids = _branch_ids(
        node_by_id=node_by_id,
        leaf_id=verified_node_id or _best_leaf(final_frontier, node_by_id),
    )
    root_node_id = seed.nodes[0].node_id if seed is not None and seed.nodes else "prove-node-000-root"
    return _GraphSearchResult(
        plan=plan,
        graph=ProofGraphSummary(
            root_node_id=root_node_id,
            verified_node_id=verified_node_id,
            frontier_node_ids=final_frontier,
            node_count=len(nodes),
            edge_count=max(0, len(nodes) - 1),
            branch_node_ids=branch_node_ids,
        ),
        helpers=[helper_by_id.get(helper.helper_id, helper) for helper in helper_states],
        nodes=[node_by_id.get(node.node_id, node) for node in nodes],
        backend_call_count=backend_call_count,
    )


def _expand_node(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    run_dir: Path,
    target_file: Path,
    theorem: str,
    timeout_seconds: float,
    inspection_state: InspectionState,
    inspection_retrieval: list[RetrievalHit],
    backend: Backend,
    parent: ProofGraphNode,
    family: StrategyFamily,
    attempt_index: int,
    helper_by_id: dict[str, HelperState],
) -> tuple[_Expansion | None, int]:
    active_helpers = _helper_refs_for_ids(
        helper_ids=parent.active_helper_ids,
        helper_by_id=helper_by_id,
    )
    if family is StrategyFamily.STATE_SEARCH:
        if parent.session_id is None:
            return None, 0
        session = load_session_state(tool_paths=tool_paths, session_id=parent.session_id)
        request = build_agent_request(
            task_kind=TaskKind.THEOREM_STEP,
            attempt_index=attempt_index,
            state=inspection_state,
            diagnostics=parent.diagnostics,
            prior_attempts=[],
            retrieval_hits=inspection_retrieval,
            active_helpers=active_helpers,
            schema_path=tool_paths.prompts / "schemas" / "theorem-session-action.schema.json",
            allowed_output_files=[],
            candidate_limit=0,
        )
        request = replace(
            request,
            session_id=parent.session_id,
            current_draft=parent.theorem_draft,
            checkpoint_labels=[
                str(item.get("label"))
                for item in cast(list[dict[str, JsonValue]], session.get("checkpoints", []))
                if isinstance(item, dict) and item.get("label") is not None
            ],
        )
        response = backend.propose(request)
        if response.result_kind is not BackendResultKind.THEOREM_SESSION_ACTIONS:
            return _Expansion(family=family, request=request, response=response, action_payloads=[]), 1
        payloads = [
            _action_payload(action)
            for action in response.theorem_session_actions
            if action.kind
            in {
                TheoremSessionActionKind.APPEND_TACTIC_TEXT,
                TheoremSessionActionKind.PROPOSE_HELPER_LEMMA,
                TheoremSessionActionKind.REPLACE_THEOREM_DRAFT,
            }
        ]
        return _Expansion(family=family, request=request, response=response, action_payloads=payloads), 1
    task_kind = TaskKind.SOLVE if family is StrategyFamily.WHOLE_BODY else TaskKind.REPAIR
    request = build_agent_request(
        task_kind=task_kind,
        attempt_index=attempt_index,
        state=inspection_state,
        diagnostics=parent.diagnostics,
        prior_attempts=[],
        retrieval_hits=inspection_retrieval,
        active_helpers=active_helpers,
        schema_path=tool_paths.prompts / "schemas" / "result.schema.json",
        allowed_output_files=[str(inspection_state.target_module_path)],
        candidate_limit=1,
    )
    classified = classify_theorem_body_response(
        response=backend.propose(request),
        expected_theorem=theorem,
    )
    response = classified.response
    if len(classified.usable_candidates) != 1:
        return _Expansion(family=family, request=request, response=response, action_payloads=[]), 1
    payload: dict[str, JsonValue] = {
        "kind": "replace_theorem_draft",
        "candidate_id": classified.usable_candidates[0].candidate_id,
        "theorem_name": classified.usable_candidates[0].theorem,
        "body": classified.usable_candidates[0].body,
    }
    return _Expansion(family=family, request=request, response=response, action_payloads=[payload]), 1


def _materialize_action(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    parent: ProofGraphNode,
    family: StrategyFamily,
    action_payload: dict[str, JsonValue],
    theorem: str,
    target_file: Path,
    timeout_seconds: float,
    graph_index: int,
    run_id: str,
    run_dir: Path,
    backend_request_path: str,
    backend_response_path: str,
    helper_by_id: dict[str, HelperState],
    backend: Backend,
    max_decomposition_depth: int,
    decomposition_attempt_index: int,
    max_backend_calls: int | None,
    max_nodes: int,
    session_runtime: SessionSemanticRuntime,
) -> _MaterializedAction:
    if str(action_payload.get("kind")) == TheoremSessionActionKind.PROPOSE_HELPER_LEMMA.value:
        return _materialize_helper_action(
            tool_paths=tool_paths,
            workspace=workspace,
            parent=parent,
            family=family,
            action_payload=action_payload,
            theorem=theorem,
            target_file=target_file,
            timeout_seconds=timeout_seconds,
            graph_index=graph_index,
            run_dir=run_dir,
            backend_request_path=backend_request_path,
            backend_response_path=backend_response_path,
            helper_by_id=helper_by_id,
            backend=backend,
            max_decomposition_depth=max_decomposition_depth,
            decomposition_attempt_index=decomposition_attempt_index,
            max_backend_calls=max_backend_calls,
            max_nodes=max_nodes,
            session_runtime=session_runtime,
        )
    return _MaterializedAction(
        nodes=[
            _materialize_theorem_child(
                tool_paths=tool_paths,
                workspace=workspace,
                parent=parent,
                family=family,
                action_payload=action_payload,
                theorem=theorem,
                target_file=target_file,
                timeout_seconds=timeout_seconds,
                graph_index=graph_index,
                run_id=run_id,
                backend_request_path=backend_request_path,
                backend_response_path=backend_response_path,
                helper_by_id=helper_by_id,
                session_runtime=session_runtime,
            )
        ],
        helper_states=[],
        backend_call_count=0,
    )


def _materialize_theorem_child(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    parent: ProofGraphNode,
    family: StrategyFamily,
    action_payload: dict[str, JsonValue],
    theorem: str,
    target_file: Path,
    timeout_seconds: float,
    graph_index: int,
    run_id: str,
    backend_request_path: str,
    backend_response_path: str,
    helper_by_id: dict[str, HelperState],
    session_runtime: SessionSemanticRuntime,
) -> ProofGraphNode:
    theorem_draft = _draft_after_action(parent.theorem_draft, action_payload)
    active_helpers = _helper_refs_for_ids(helper_ids=parent.active_helper_ids, helper_by_id=helper_by_id)
    session_start, _start_exit = start_session(
        tool_paths=tool_paths,
        workspace=workspace,
        target_file=target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        source_run_id=run_id,
        active_helpers=active_helpers,
        semantic_runtime=session_runtime,
    )
    try:
        session = load_session_state(tool_paths=tool_paths, session_id=session_start.session_id)
        session_state = cast(dict[str, JsonValue], session["current_state"])
        session_node_id = session_start.current_node_id
        if not (str(action_payload.get("kind")) == "activate_helper" and theorem_draft == parent.theorem_draft):
            step_result, _step_exit = step_session(
                tool_paths=tool_paths,
                session_id=session_start.session_id,
                body=theorem_draft,
                timeout_seconds=timeout_seconds,
                semantic_runtime=session_runtime,
            )
            session = load_session_state(tool_paths=tool_paths, session_id=session_start.session_id)
            session_state = cast(dict[str, JsonValue], session["current_state"])
            session_node_id = step_result.current_node_id
        diagnostics = _diagnostics_from_payload(session_state.get("diagnostics"))
        goals = _string_list(session_state.get("goals"))
        status = "frontier"
        if not diagnostics:
            finish_result, _finish_exit = finish_session(
                tool_paths=tool_paths,
                session_id=session_start.session_id,
                timeout_seconds=timeout_seconds,
                semantic_runtime=session_runtime,
            )
            session = load_session_state(tool_paths=tool_paths, session_id=session_start.session_id)
            session_state = cast(dict[str, JsonValue], session["current_state"])
            diagnostics = _diagnostics_from_payload(session_state.get("diagnostics"))
            goals = _string_list(session_state.get("goals"))
            session_node_id = finish_result.current_node_id
            status = "verified" if finish_result.status.value == "finished-verified" else "frontier"
        return ProofGraphNode(
            graph_index=graph_index,
            node_id=f"prove-node-{graph_index:03d}-{family.value}",
            node_kind="theorem-state",
            parent_node_id=parent.node_id,
            strategy_family=family,
            task_kind=_task_kind_for_family(family),
            session_id=session_start.session_id,
            session_artifact_dir=session_start.session_artifact_dir,
            session_node_id=session_node_id,
            action_payload=action_payload,
            theorem_draft=theorem_draft,
            diagnostics=diagnostics,
            goals=goals,
            probe_snapshot=_probe_snapshot(session_state),
            score=_score_node(diagnostics=diagnostics, goals=goals, verified=status == "verified"),
            status=status,
            dedup_signature=_dedup_signature(
                theorem_draft=theorem_draft,
                diagnostics=session_state.get("diagnostics"),
                goals=session_state.get("goals"),
                verification_status=status,
                active_helper_ids=list(parent.active_helper_ids),
            ),
            decomposition_depth=parent.decomposition_depth,
            active_helper_ids=list(parent.active_helper_ids),
            backend_request_path=backend_request_path,
            backend_response_path=backend_response_path,
        )
    finally:
        session_runtime.close_session(session_start.session_id)


def _materialize_helper_action(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    parent: ProofGraphNode,
    family: StrategyFamily,
    action_payload: dict[str, JsonValue],
    theorem: str,
    target_file: Path,
    timeout_seconds: float,
    graph_index: int,
    run_dir: Path,
    backend_request_path: str,
    backend_response_path: str,
    helper_by_id: dict[str, HelperState],
    backend: Backend,
    max_decomposition_depth: int,
    decomposition_attempt_index: int,
    max_backend_calls: int | None,
    max_nodes: int,
    session_runtime: SessionSemanticRuntime,
) -> _MaterializedAction:
    helper = cast(dict[str, JsonValue], action_payload.get("helper") or {})
    helper_name = _json_string(helper.get("helper_name"), "GeneratedHelper")
    helper_statement = _json_string(helper.get("helper_statement"))
    proof_stub = _json_string(helper.get("proof_stub"))
    helper_id = f"helper-{graph_index:03d}-{helper_name}"
    helper_node_id = f"prove-node-{graph_index:03d}-helper"
    helper_root = run_dir / "helpers" / helper_id
    dependency_ids = list(parent.active_helper_ids)
    resolved = _verify_helper_obligation(
        tool_paths=tool_paths,
        workspace=workspace,
        backend=backend,
        helper_id=helper_id,
        helper_node_id=helper_node_id,
        source_theorem=theorem,
        source_node_id=parent.node_id,
        target_file=target_file,
        helper_name=helper_name,
        helper_statement=helper_statement,
        proof_stub=proof_stub,
        dependency_ids=dependency_ids,
        timeout_seconds=timeout_seconds,
        artifact_dir=helper_root,
        helper_by_id=helper_by_id,
        graph_index=graph_index + 1,
        current_depth=parent.decomposition_depth + 1,
        max_decomposition_depth=max_decomposition_depth,
        decomposition_attempt_index=decomposition_attempt_index,
        max_backend_calls=max_backend_calls,
        max_nodes=max(0, max_nodes - 1),
    )
    verified_state = resolved.final_state
    helper_node = ProofGraphNode(
        graph_index=graph_index,
        node_id=helper_node_id,
        node_kind="helper-obligation",
        parent_node_id=parent.node_id,
        strategy_family=family,
        task_kind=TaskKind.THEOREM_STEP,
        session_id=verified_state.helper_id,
        session_artifact_dir=verified_state.verification_artifact_dir,
        session_node_id=None,
        action_payload=action_payload,
        theorem_draft=proof_stub,
        diagnostics=verified_state.diagnostics,
        goals=[],
        probe_snapshot={},
        score=_score_node(
            diagnostics=verified_state.diagnostics,
            goals=[],
            verified=verified_state.status is HelperStatus.VERIFIED,
        ),
        status=verified_state.status.value,
        dedup_signature=sha256(
            json.dumps(
                {
                    "helper_id": verified_state.helper_id,
                    "status": verified_state.status.value,
                    "statement": verified_state.statement,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16],
        decomposition_depth=verified_state.decomposition_depth,
        active_helper_ids=list(verified_state.dependencies),
        helper_id=verified_state.helper_id,
        backend_request_path=backend_request_path,
        backend_response_path=backend_response_path,
    )
    nodes = [helper_node, *resolved.nodes]
    helper_states = list(resolved.helper_states)
    if verified_state.status is not HelperStatus.VERIFIED:
        return _MaterializedAction(
            nodes=nodes,
            helper_states=helper_states,
            backend_call_count=resolved.backend_call_count,
        )
    if len(nodes) >= max_nodes:
        return _MaterializedAction(
            nodes=nodes,
            helper_states=helper_states,
            backend_call_count=resolved.backend_call_count,
        )
    continuation_helper_ids = list(
        dict.fromkeys([*parent.active_helper_ids, *verified_state.dependencies, verified_state.helper_id])
    )
    continuation_helper_by_id = {**helper_by_id, **{state.helper_id: state for state in helper_states}}
    continuation = _materialize_theorem_child(
        tool_paths=tool_paths,
        workspace=workspace,
        parent=replace(parent, active_helper_ids=continuation_helper_ids),
        family=family,
        action_payload={
            "kind": "activate_helper",
            "helper_id": verified_state.helper_id,
            "body": parent.theorem_draft,
        },
        theorem=theorem,
        target_file=target_file,
        timeout_seconds=timeout_seconds,
        graph_index=graph_index + 1,
        run_id=run_dir.name,
        backend_request_path=backend_request_path,
        backend_response_path=backend_response_path,
        helper_by_id=continuation_helper_by_id,
        session_runtime=session_runtime,
    )
    consumed_state = _register_helper_consumer(helper=verified_state, consumer_node_id=continuation.node_id)
    continuation = replace(
        continuation,
        parent_node_id=helper_node.node_id,
        active_helper_ids=continuation_helper_ids,
    )
    _write_helper_state(state=consumed_state)
    return _MaterializedAction(
        nodes=[*nodes, continuation],
        helper_states=[*helper_states, consumed_state],
        backend_call_count=resolved.backend_call_count,
    )


@dataclass(frozen=True)
class _ResolvedHelperObligation:
    final_state: HelperState
    helper_states: list[HelperState]
    nodes: list[ProofGraphNode]
    backend_call_count: int


def _verify_helper_obligation(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    backend: Backend,
    helper_id: str,
    helper_node_id: str,
    source_theorem: str,
    source_node_id: str,
    target_file: Path,
    helper_name: str,
    helper_statement: str,
    proof_stub: str,
    dependency_ids: list[str],
    timeout_seconds: float,
    artifact_dir: Path,
    helper_by_id: dict[str, HelperState],
    graph_index: int,
    current_depth: int,
    max_decomposition_depth: int,
    decomposition_attempt_index: int,
    max_backend_calls: int | None,
    max_nodes: int,
    sandbox: SandboxWorkspace | None = None,
) -> _ResolvedHelperObligation:
    sandbox = sandbox or create_sandbox_workspace(workspace=workspace, run_dir=artifact_dir)
    module_name = _managed_helper_module_name(
        source_file=target_file,
        source_theorem=source_theorem,
        helper_name=helper_name,
        sandbox_root=sandbox.sandbox_workspace.root,
        workspace_root=workspace.root,
    )
    helper_file = sandbox.helper_file(module_name)
    proposed_state = HelperState(
        helper_id=helper_id,
        source_theorem=source_theorem,
        statement=helper_statement,
        module_name=module_name,
        file_path=str(helper_file),
        dependencies=dependency_ids,
        status=HelperStatus.PROPOSED,
        consumer_theorem_states=[],
        decomposition_depth=current_depth,
        helper_name=helper_name,
        source_node_id=source_node_id,
        verification_artifact_dir=str(artifact_dir),
        proof_stub=proof_stub,
        diagnostics=[],
    )
    _write_helper_state(state=proposed_state)
    active_state = replace(proposed_state, status=HelperStatus.ACTIVE)
    _write_helper_state(state=active_state)
    dependency_refs = _helper_refs_for_ids(helper_ids=dependency_ids, helper_by_id=helper_by_id)
    step = DecompositionStep(
        step_id=helper_id,
        title=helper_name,
        goal=f"Verify helper `{helper_name}`.",
        layer=DecompositionLayer.MISSING_LEMMA,
        helper_name=helper_name,
        helper_statement=helper_statement,
        helper_proof=proof_stub,
        helper_imports=[helper.module_name for helper in dependency_refs],
    )
    check_result = _verify_helper_step(
        sandbox=sandbox,
        helper_file=helper_file,
        artifact_dir=artifact_dir,
        step=step,
        timeout_seconds=timeout_seconds,
        dependency_refs=dependency_refs,
    )
    nodes: list[ProofGraphNode] = []
    helper_states: list[HelperState] = []
    backend_call_count = 0
    final_dependencies = list(dependency_ids)
    status = HelperStatus.VERIFIED if check_result is None else HelperStatus.DISCARDED
    diagnostics = [] if check_result is None else check_result.diagnostics
    if (
        check_result is not None
        and current_depth < max_decomposition_depth
        and max_nodes > 0
        and (max_backend_calls is None or max_backend_calls > 0)
    ):
        request = _build_helper_decomposition_request(
            sandbox=sandbox,
            helper_file=helper_file,
            helper_name=helper_name,
            helper_statement=helper_statement,
            helper_text=_render_helper_step(step=step, dependency_refs=dependency_refs),
            diagnostics=check_result.diagnostics,
            dependency_refs=dependency_refs,
            attempt_index=decomposition_attempt_index,
            schema_path=tool_paths.prompts / "schemas" / "result.schema.json",
        )
        response = backend.propose(request)
        _write_backend_exchange(
            request_path=artifact_dir / "decompose" / "backend" / "request.json",
            response_path=artifact_dir / "decompose" / "backend" / "response.json",
            request=request,
            response=response,
        )
        backend_call_count += 1
        if response.result_kind is BackendResultKind.DECOMPOSITION:
            next_graph_index = graph_index
            resolved_helper_by_id = dict(helper_by_id)
            resolved_helper_by_id[helper_id] = active_state
            for step_index, nested_step in enumerate(response.decomposition, start=1):
                if nested_step.layer is not DecompositionLayer.MISSING_LEMMA or max_nodes - len(nodes) <= 0:
                    continue
                child_helper_name = _json_string(nested_step.helper_name, nested_step.step_id)
                child_helper_id = f"helper-{next_graph_index:03d}-{child_helper_name}"
                child_node_id = f"prove-node-{next_graph_index:03d}-helper"
                nested_resolved = _verify_helper_obligation(
                    tool_paths=tool_paths,
                    workspace=workspace,
                    backend=backend,
                    helper_id=child_helper_id,
                    helper_node_id=child_node_id,
                    source_theorem=helper_name,
                    source_node_id=helper_node_id,
                    target_file=helper_file,
                    helper_name=child_helper_name,
                    helper_statement=_json_string(nested_step.helper_statement),
                    proof_stub=_json_string(nested_step.helper_proof),
                    dependency_ids=final_dependencies,
                    timeout_seconds=timeout_seconds,
                    artifact_dir=artifact_dir / "decompose" / f"helper-{step_index:03d}",
                    helper_by_id=resolved_helper_by_id,
                    graph_index=next_graph_index + 1,
                    current_depth=current_depth + 1,
                    max_decomposition_depth=max_decomposition_depth,
                    decomposition_attempt_index=decomposition_attempt_index + backend_call_count,
                    max_backend_calls=(
                        None if max_backend_calls is None else max(0, max_backend_calls - backend_call_count)
                    ),
                    max_nodes=max(0, max_nodes - len(nodes) - 1),
                    sandbox=sandbox,
                )
                child_node = _helper_node_from_state(
                    graph_index=next_graph_index,
                    node_id=child_node_id,
                    parent_node_id=helper_node_id,
                    family=StrategyFamily.STATE_SEARCH,
                    action_payload={
                        "kind": TheoremSessionActionKind.PROPOSE_HELPER_LEMMA.value,
                        "helper": {
                            "helper_name": child_helper_name,
                            "helper_statement": _json_string(nested_step.helper_statement),
                            "proof_stub": _json_string(nested_step.helper_proof),
                        },
                    },
                    state=nested_resolved.final_state,
                    backend_request_path=str(artifact_dir / "decompose" / "backend" / "request.json"),
                    backend_response_path=str(artifact_dir / "decompose" / "backend" / "response.json"),
                )
                nodes.extend([child_node, *nested_resolved.nodes])
                helper_states.extend(nested_resolved.helper_states)
                backend_call_count += nested_resolved.backend_call_count
                next_graph_index += 1 + len(nested_resolved.nodes)
                resolved_helper_by_id[nested_resolved.final_state.helper_id] = nested_resolved.final_state
                if nested_resolved.final_state.status is not HelperStatus.VERIFIED:
                    break
                final_dependencies.append(nested_resolved.final_state.helper_id)
            dependency_refs = _helper_refs_for_ids(helper_ids=final_dependencies, helper_by_id=resolved_helper_by_id)
            retry_result = _verify_helper_step(
                sandbox=sandbox,
                helper_file=helper_file,
                artifact_dir=artifact_dir / "retry",
                step=step,
                timeout_seconds=timeout_seconds,
                dependency_refs=dependency_refs,
            )
            if retry_result is None:
                status = HelperStatus.VERIFIED
                diagnostics = []
            else:
                diagnostics = retry_result.diagnostics
    if status is HelperStatus.DISCARDED:
        helper_file.unlink(missing_ok=True)
        helper_file.with_suffix(".olean").unlink(missing_ok=True)
    final_state = HelperState(
        helper_id=helper_id,
        source_theorem=source_theorem,
        statement=helper_statement,
        module_name=module_name,
        file_path=str(helper_file),
        dependencies=final_dependencies,
        status=status,
        consumer_theorem_states=[],
        decomposition_depth=current_depth,
        helper_name=helper_name,
        source_node_id=source_node_id,
        verification_artifact_dir=str(artifact_dir),
        proof_stub=proof_stub,
        diagnostics=diagnostics,
    )
    _write_helper_state(state=final_state)
    helper_states.append(final_state)
    return _ResolvedHelperObligation(
        final_state=final_state,
        helper_states=helper_states,
        nodes=nodes,
        backend_call_count=backend_call_count,
    )


def _verify_helper_step(
    *,
    sandbox: SandboxWorkspace,
    helper_file: Path,
    artifact_dir: Path,
    step: DecompositionStep,
    timeout_seconds: float,
    dependency_refs: list[HelperRef],
) -> CheckResult | None:
    for helper_ref in dependency_refs:
        sandbox.materialize_helper(helper_ref)
    helper_file.parent.mkdir(parents=True, exist_ok=True)
    helper_file.write_text(_render_helper_step(step=step, dependency_refs=dependency_refs), encoding="utf-8")
    check_result = _run_check(
        workspace=sandbox.sandbox_workspace,
        target_file=helper_file,
        timeout_seconds=timeout_seconds,
    )
    _write_backend_exchange_json(path=artifact_dir / "proposal.json", payload=dataclass_to_jsonable(step))
    _write_backend_exchange_json(path=artifact_dir / "verification.json", payload=check_result.to_json())
    if check_result.status is not CheckStatus.OK:
        return check_result
    build_result = run_lean_make_olean(sandbox.sandbox_workspace.root, helper_file, timeout_seconds)
    _write_backend_exchange_json(
        path=artifact_dir / "build.json",
        payload={
            "argv": build_result.args,
            "returncode": build_result.returncode,
            "stdout": build_result.stdout,
            "stderr": build_result.stderr,
        },
    )
    return None


def _render_helper_step(*, step: DecompositionStep, dependency_refs: list[HelperRef]) -> str:
    helper_imports = list(dict.fromkeys([*step.helper_imports, *(helper.module_name for helper in dependency_refs)]))
    return render_helper_file(replace(step, helper_imports=helper_imports))


def _build_helper_decomposition_request(
    *,
    sandbox: SandboxWorkspace,
    helper_file: Path,
    helper_name: str,
    helper_statement: str,
    helper_text: str,
    diagnostics: list[Diagnostic],
    dependency_refs: list[HelperRef],
    attempt_index: int,
    schema_path: Path,
) -> AgentRequest:
    module_path = helper_file.relative_to(sandbox.sandbox_workspace.root)
    lines = helper_text.splitlines()
    line_count = max(1, len(lines))
    body_line = next((index for index, line in enumerate(lines, start=1) if ":= by" in line), line_count)
    return AgentRequest(
        task_kind=TaskKind.DECOMPOSE,
        attempt_index=attempt_index,
        candidate_limit=1,
        theorem_name=helper_name,
        target_file=str(module_path),
        workspace_root=str(sandbox.sandbox_workspace.root),
        workspace_package=sandbox.sandbox_workspace.package_name,
        sandbox_workspace_root=str(sandbox.sandbox_workspace.root),
        target_module_path=str(module_path),
        helper_module_root=sandbox.helper_module_root,
        helper_root_dir=str(sandbox.helper_root_dir),
        theorem_statement=helper_statement,
        editable_span=FileSpan(start_line=body_line, end_line=line_count),
        source_window=SourceWindow(file=str(module_path), start_line=1, end_line=line_count, text=helper_text),
        diagnostics=diagnostics,
        prior_attempts=[],
        retrieval_hits=[],
        active_helpers=dependency_refs,
        allowed_output_files=[str(module_path)],
        schema_path=str(schema_path),
    )


def _helper_node_from_state(
    *,
    graph_index: int,
    node_id: str,
    parent_node_id: str,
    family: StrategyFamily,
    action_payload: dict[str, JsonValue],
    state: HelperState,
    backend_request_path: str,
    backend_response_path: str,
) -> ProofGraphNode:
    return ProofGraphNode(
        graph_index=graph_index,
        node_id=node_id,
        node_kind="helper-obligation",
        parent_node_id=parent_node_id,
        strategy_family=family,
        task_kind=TaskKind.THEOREM_STEP,
        session_id=state.helper_id,
        session_artifact_dir=state.verification_artifact_dir,
        session_node_id=None,
        action_payload=action_payload,
        theorem_draft=state.proof_stub or "",
        diagnostics=state.diagnostics,
        goals=[],
        probe_snapshot={},
        score=_score_node(diagnostics=state.diagnostics, goals=[], verified=state.status is HelperStatus.VERIFIED),
        status=state.status.value,
        dedup_signature=sha256(
            json.dumps(
                {
                    "helper_id": state.helper_id,
                    "status": state.status.value,
                    "statement": state.statement,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16],
        decomposition_depth=state.decomposition_depth,
        active_helper_ids=list(state.dependencies),
        helper_id=state.helper_id,
        backend_request_path=backend_request_path,
        backend_response_path=backend_response_path,
    )


def _helper_project_root(*, source_file: Path, sandbox_root: Path, workspace_root: Path) -> Path:
    try:
        source_file.resolve().relative_to(workspace_root)
        return workspace_root
    except ValueError:
        return sandbox_root


def _managed_helper_module_name(
    *,
    source_file: Path,
    source_theorem: str,
    helper_name: str,
    sandbox_root: Path,
    workspace_root: Path,
) -> str:
    helper_root = sandbox_root / MANAGED_HELPER_ROOT
    try:
        relative = source_file.resolve().relative_to(helper_root)
        parts = [*relative.with_suffix("").parts, _helper_module_segment(helper_name)]
        return ".".join([*MANAGED_HELPER_MODULE_ROOT.split("."), *parts])
    except ValueError:
        return helper_module_name(
            target_file=source_file,
            theorem=source_theorem,
            helper_name=helper_name,
            project_root=_helper_project_root(
                source_file=source_file,
                sandbox_root=sandbox_root,
                workspace_root=workspace_root,
            ),
        )


def _helper_module_segment(text: str) -> str:
    words = [word for word in "".join(char if char.isalnum() else " " for char in text).split() if word]
    if not words:
        return "Generated"
    segment = "".join(word[:1].upper() + word[1:] for word in words)
    if segment[:1].isdigit():
        return f"N{segment}"
    return segment


def _register_helper_consumer(*, helper: HelperState, consumer_node_id: str) -> HelperState:
    consumers = list(helper.consumer_theorem_states)
    if consumer_node_id not in consumers:
        consumers.append(consumer_node_id)
    return replace(helper, consumer_theorem_states=consumers)


def _task_kind_for_family(family: StrategyFamily) -> TaskKind:
    if family is StrategyFamily.STATE_SEARCH:
        return TaskKind.THEOREM_STEP
    if family is StrategyFamily.WHOLE_BODY:
        return TaskKind.SOLVE
    return TaskKind.REPAIR


def _strategy_order(*, node: ProofGraphNode, plan: TheoremPlan | None) -> list[StrategyFamily]:
    if node.node_kind != "theorem-state":
        return []
    if node.parent_node_id is None:
        preferred = [
            StrategyFamily.STATE_SEARCH,
            StrategyFamily.REPAIR_FROM_DIAGNOSTIC,
            StrategyFamily.WHOLE_BODY,
        ]
    elif node.diagnostics:
        preferred = [
            StrategyFamily.REPAIR_FROM_DIAGNOSTIC,
            StrategyFamily.STATE_SEARCH,
            StrategyFamily.WHOLE_BODY,
        ]
    else:
        preferred = [StrategyFamily.STATE_SEARCH, StrategyFamily.WHOLE_BODY]
    if plan is not None and "whole" in plan.strategy.lower():
        return [StrategyFamily.WHOLE_BODY, *[item for item in preferred if item is not StrategyFamily.WHOLE_BODY]]
    return preferred


def _helper_refs_for_ids(*, helper_ids: list[str], helper_by_id: dict[str, HelperState]) -> list[HelperRef]:
    refs: list[HelperRef] = []
    for helper_id in helper_ids:
        helper = helper_by_id.get(helper_id)
        if helper is None or helper.status is not HelperStatus.VERIFIED:
            continue
        refs.append(
            HelperRef(
                helper_name=helper.helper_name or helper.helper_id,
                module_name=helper.module_name,
                file_path=helper.file_path,
                theorem=helper.source_theorem,
                statement=helper.statement,
            )
        )
    return refs


def _draft_after_action(parent_draft: str, action_payload: dict[str, JsonValue]) -> str:
    kind = str(action_payload.get("kind"))
    if kind == "append_tactic_text":
        suffix = str(action_payload.get("tactic_text", ""))
        joiner = "" if parent_draft.endswith("\n") or suffix.startswith("\n") else "\n"
        return f"{parent_draft}{joiner}{suffix}"
    return str(action_payload.get("body", parent_draft))


def _action_payload(action: object) -> dict[str, JsonValue]:
    payload = cast(dict[str, JsonValue], dataclass_to_jsonable(action))
    payload["kind"] = payload.get("kind")
    return payload


def _pick_frontier(*, frontier_ids: list[str], node_by_id: dict[str, ProofGraphNode]) -> ProofGraphNode:
    ranked = sorted((node_by_id[node_id] for node_id in frontier_ids), key=lambda node: (-node.score, node.node_id))
    return ranked[0]


def _best_leaf(frontier_ids: list[str], node_by_id: dict[str, ProofGraphNode]) -> str | None:
    if not frontier_ids:
        return None
    return _pick_frontier(frontier_ids=frontier_ids, node_by_id=node_by_id).node_id


def _branch_ids(*, node_by_id: dict[str, ProofGraphNode], leaf_id: str | None) -> list[str]:
    if leaf_id is None:
        return []
    branch: list[str] = []
    current_id: str | None = leaf_id
    while current_id is not None:
        branch.append(current_id)
        current_id = node_by_id[current_id].parent_node_id
    branch.reverse()
    return branch


def _family_attempt_counts(nodes: list[ProofGraphNode]) -> dict[StrategyFamily, int]:
    counts = {
        StrategyFamily.WHOLE_BODY: 0,
        StrategyFamily.STATE_SEARCH: 0,
        StrategyFamily.REPAIR_FROM_DIAGNOSTIC: 0,
    }
    seen_requests: dict[StrategyFamily, set[str]] = {family: set() for family in counts}
    for node in nodes:
        if node.strategy_family is None or node.backend_request_path is None:
            continue
        if node.backend_request_path in seen_requests[node.strategy_family]:
            continue
        seen_requests[node.strategy_family].add(node.backend_request_path)
        counts[node.strategy_family] += 1
    return counts


def _start_root_node(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    target_file: Path,
    theorem: str,
    timeout_seconds: float,
    source_run_id: str,
    session_runtime: SessionSemanticRuntime,
) -> ProofGraphNode:
    root_result, _root_exit = start_session(
        tool_paths=tool_paths,
        workspace=workspace,
        target_file=target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        source_run_id=source_run_id,
        semantic_runtime=session_runtime,
    )
    root_session = load_session_state(tool_paths=tool_paths, session_id=root_result.session_id)
    root_payload = cast(dict[str, JsonValue], root_session["current_state"])
    return ProofGraphNode(
        graph_index=0,
        node_id="prove-node-000-root",
        node_kind="theorem-state",
        parent_node_id=None,
        strategy_family=None,
        task_kind=None,
        session_id=root_result.session_id,
        session_artifact_dir=root_result.session_artifact_dir,
        session_node_id=root_result.current_node_id,
        action_payload={"kind": "root", "session_action": "start"},
        theorem_draft=cast(str, root_session["current_draft"]),
        diagnostics=_diagnostics_from_payload(root_payload.get("diagnostics")),
        goals=_string_list(root_payload.get("goals")),
        probe_snapshot=_probe_snapshot(root_payload),
        score=_score_node(
            diagnostics=_diagnostics_from_payload(root_payload.get("diagnostics")),
            goals=_string_list(root_payload.get("goals")),
            verified=False,
        ),
        status="frontier",
        dedup_signature=_dedup_signature(
            theorem_draft=cast(str, root_session["current_draft"]),
            diagnostics=root_payload.get("diagnostics"),
            goals=root_payload.get("goals"),
            verification_status="frontier",
            active_helper_ids=[],
        ),
        active_helper_ids=[],
        decomposition_depth=0,
    )


def _dedup_signature(
    *,
    theorem_draft: str,
    diagnostics: object,
    goals: object,
    verification_status: str,
    active_helper_ids: list[str],
) -> str:
    payload = {
        "draft": " ".join(theorem_draft.split()),
        "diagnostics": diagnostics or [],
        "goals": goals or [],
        "verification_status": verification_status,
        "active_helper_ids": sorted(active_helper_ids),
    }
    return sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _score_node(*, diagnostics: list[Diagnostic], goals: list[str], verified: bool) -> int:
    if verified:
        return 10_000
    return max(0, 1_000 - len(diagnostics) * 100 - len(goals) * 10)


def _probe_snapshot(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "semantic_session": cast(dict[str, JsonValue], payload.get("semantic_session") or {}),
        "proof_target": cast(dict[str, JsonValue] | None, payload.get("proof_target")),
        "proof_state": cast(dict[str, JsonValue] | None, payload.get("proof_state")),
        "syntax_probe_results": cast(dict[str, JsonValue], payload.get("syntax_probe_results") or {}),
        "semantic_probe_results": cast(dict[str, JsonValue], payload.get("semantic_probe_results") or {}),
        "probe_results": cast(dict[str, JsonValue], payload.get("probe_results") or {}),
    }


def _diagnostics_from_payload(payload: object) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for item in cast(list[JsonValue], payload or []):
        if not isinstance(item, dict):
            continue
        range_payload = cast(dict[str, JsonValue], item.get("range") or {})
        diagnostics.append(
            Diagnostic(
                file=_json_string(item.get("file")),
                range=Range(
                    startLine=_json_int(range_payload.get("startLine"), 1),
                    startCol=_json_int(range_payload.get("startCol"), 0),
                    endLine=_json_int(range_payload.get("endLine"), 1),
                    endCol=_json_int(range_payload.get("endCol"), 0),
                ),
                severity=Severity(_json_string(item.get("severity"), Severity.ERROR.value)),
                message=_json_string(item.get("message")),
                class_name=DiagnosticClass(
                    _json_string(item.get("class"), _json_string(item.get("class_name"), DiagnosticClass.OTHER.value))
                ),
            )
        )
    return diagnostics


def _string_list(payload: object) -> list[str]:
    return [str(item) for item in cast(list[object], payload or []) if isinstance(item, str)]


def _json_string(value: JsonValue | None, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _json_int(value: JsonValue | None, default: int) -> int:
    return value if isinstance(value, int) else default


def _run_check(*, workspace: WorkspaceBinding, target_file: Path, timeout_seconds: float) -> CheckResult:
    started_at = datetime.now(UTC)
    lean_result = run_lean_check(
        workspace_root=workspace.root,
        target_file=target_file,
        timeout_seconds=timeout_seconds,
        workspace_root_for_diagnostics=workspace.root,
        use_lake_env=True,
    )
    finished_at = datetime.now(UTC)
    if lean_result.timed_out:
        status = CheckStatus.TIMEOUT
        exit_code = ExitCode.TIMEOUT
    elif lean_result.returncode == 0:
        status = CheckStatus.OK
        exit_code = ExitCode.SUCCESS
    elif lean_result.diagnostics:
        status = CheckStatus.FAILED
        exit_code = ExitCode.CHECK_FAILED
    else:
        status = CheckStatus.ERROR
        exit_code = ExitCode.INTERNAL_ERROR
    return CheckResult(
        run_id=new_run_id(started_at),
        command="prove-helper-check",
        status=status,
        exit_code=exit_code,
        target_file=_project_relative(target_file, workspace.root),
        tool_root="",
        workspace=_workspace_metadata(workspace),
        artifact_dir="",
        wall_time_seconds=lean_result.wall_time_seconds,
        diagnostic_count=len(lean_result.diagnostics),
        diagnostics=lean_result.diagnostics,
        lean=lean_result,
        started_at=started_at,
        finished_at=finished_at,
    )


def _write_backend_exchange_json(*, path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_helper_state(*, state: HelperState) -> None:
    artifact_dir = state.verification_artifact_dir
    if artifact_dir is None:
        return
    artifact_path = Path(artifact_dir)
    history_dir = artifact_path / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    payload = state.to_json()
    (artifact_path / "state.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    snapshot_count = len(list(history_dir.glob("*.json"))) + 1
    (history_dir / f"{snapshot_count:03d}-{state.status.value}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_backend_exchange(
    *,
    request_path: Path,
    response_path: Path,
    request: AgentRequest,
    response: AgentResponse,
) -> None:
    request_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(request.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    response_path.write_text(json.dumps(response.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_graph_node(*, run_dir: Path, node: ProofGraphNode) -> None:
    node_dir = run_dir / "graph" / "nodes"
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / f"{node.node_id}.json").write_text(
        json.dumps(node.to_json(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _emit_progress(message: str) -> None:
    print(f"[proofctl prove] {message}", file=sys.stderr)


def _exit_code_number(exit_code: ExitCode) -> int:
    if exit_code is ExitCode.SUCCESS:
        return 0
    if exit_code is ExitCode.CHECK_FAILED:
        return 1
    if exit_code is ExitCode.BAD_USAGE:
        return 2
    if exit_code is ExitCode.TIMEOUT:
        return 3
    return 4


def _project_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
