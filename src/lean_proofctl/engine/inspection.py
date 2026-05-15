"""Shared theorem inspection and planning helpers."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from lean_proofctl.adapters.base import Backend
from lean_proofctl.engine.cache import ensure_store, store_paths
from lean_proofctl.engine.config import ToolPaths, WorkspaceBinding
from lean_proofctl.engine.lean_runner import run_lean_check
from lean_proofctl.engine.ledger import append_index_row, write_inspect_artifacts, write_plan_artifacts
from lean_proofctl.engine.models import (
    AgentRequest,
    AgentResponse,
    BackendResultKind,
    Diagnostic,
    ExitCode,
    FileSpan,
    FileStateCheck,
    FileStateCheckStatus,
    HelperRef,
    InsertionPointSummary,
    InspectionSnapshot,
    InspectionStatus,
    InspectResult,
    JsonValue,
    PatchAttempt,
    PlanResult,
    PlanStatus,
    Position,
    PriorAttemptSummary,
    RepairFailure,
    RepairStrategy,
    RetrievalHit,
    RetrieveResult,
    RetrieveSnapshot,
    RunManifest,
    RunRequest,
    SandboxMetadata,
    SourceWindow,
    TaskKind,
    WorkspaceMetadata,
    dataclass_to_jsonable,
)
from lean_proofctl.engine.patching import apply_helper_imports
from lean_proofctl.engine.proofctl import (
    ProofctlContextBundle,
    ProofctlDeclarationTarget,
    SemanticContextProbes,
)
from lean_proofctl.engine.retrieval import EmbeddingRetrievalConfig, collect_retrieval_explanation
from lean_proofctl.engine.run_id import new_run_id
from lean_proofctl.engine.sandbox import SandboxWorkspace, create_sandbox_workspace
from lean_proofctl.engine.theorem_locator import (
    TheoremSpan,
    locate_theorem_span_from_target,
)


@dataclass(frozen=True)
class InspectionState:
    """
    Workspace-aware theorem target and probe context.

    `target_text` is the sandbox copy that Lean verified, not the untouched real
    workspace file. That keeps helper imports, declaration spans, and retrieval
    requests aligned with the exact text seen by the verifier.
    """

    workspace: WorkspaceBinding
    sandbox: SandboxWorkspace | None
    target_file: Path
    sandbox_target_file: Path
    target_module_path: Path
    target_text: str
    theorem: str
    target: ProofctlDeclarationTarget
    span: TheoremSpan
    theorem_statement: str | None
    editable_span: FileSpan
    proofctl_context: ProofctlContextBundle

    @property
    def helper_module_root(self) -> str | None:
        """Return the managed helper root when this inspection uses a sandbox."""
        return None if self.sandbox is None else self.sandbox.helper_module_root


@dataclass(frozen=True)
class InspectionPolicy:
    """Centralized read-command policy for sandboxing and full-file verification."""

    read_only: bool = False
    verify_file_state: bool = False

    def build_state(
        self,
        *,
        tool_paths: ToolPaths,
        workspace: WorkspaceBinding,
        run_dir: Path,
        target_file: Path,
        theorem: str,
        timeout_seconds: float,
        command_name: str,
    ) -> tuple[InspectionState, SandboxMetadata | None]:
        """Build inspection state under the read command's sandbox policy."""
        if self.read_only:
            state = build_read_only_inspection_state(
                tool_paths=tool_paths,
                workspace=workspace,
                target_file=target_file,
                theorem=theorem,
                timeout_seconds=timeout_seconds,
                command_name=command_name,
            )
            return state, None

        sandbox_workspace = create_sandbox_workspace(workspace=workspace, run_dir=run_dir)
        state = build_inspection_state(
            tool_paths=tool_paths,
            workspace=workspace,
            sandbox=sandbox_workspace,
            target_file=target_file,
            theorem=theorem,
            timeout_seconds=timeout_seconds,
            command_name=command_name,
        )
        return state, sandbox_workspace.metadata()

    def collect_file_state_check(
        self,
        *,
        state: InspectionState,
        timeout_seconds: float,
    ) -> tuple[list[Diagnostic], FileStateCheck]:
        """Run or skip the optional full-file Lean check for one read command."""
        if not self.verify_file_state:
            return [], FileStateCheck(
                status=FileStateCheckStatus.SKIPPED,
                requested=False,
                diagnostic_count=0,
            )
        if state.sandbox is None:
            msg = "File-state verification requires a sandbox-backed inspection state."
            raise ValueError(msg)
        diagnostics = _baseline_diagnostics(
            workspace=state.sandbox.sandbox_workspace,
            target_file=state.sandbox_target_file,
            timeout_seconds=timeout_seconds,
        )
        return diagnostics, FileStateCheck(
            status=FileStateCheckStatus.RAN,
            requested=True,
            diagnostic_count=len(diagnostics),
        )


def build_inspection_state(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    sandbox: SandboxWorkspace,
    target_file: Path,
    theorem: str,
    timeout_seconds: float,
    command_name: str,
    active_helpers: list[HelperRef] | None = None,
) -> InspectionState:
    """
    Build the shared theorem inspection state inside a workspace sandbox.

    Inspection is the point where `proofctl` binds sandboxed source text,
    Lean's declaration targeting, and theorem-local proof state. Later engine
    steps treat this object as the authoritative join of those three views.
    """
    helper_refs = active_helpers or []
    source_text = target_file.read_text(encoding="utf-8")
    for helper in helper_refs:
        sandbox.materialize_helper(helper)
    target_text = apply_helper_imports(
        source_text,
        helper_modules=[helper.module_name for helper in helper_refs],
    )
    sandbox_target_file = sandbox.target_file(target_file)
    sandbox_target_file.write_text(target_text, encoding="utf-8")
    probes, target = _resolve_semantic_target(
        tool_paths=tool_paths,
        workspace=sandbox.sandbox_workspace,
        file_path=sandbox_target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        command_name=command_name,
    )
    span = locate_theorem_span_from_target(source=target_text, theorem_name=theorem, target=target)
    proofctl_context = probes.context_bundle(require_semantic_probes=True)
    body_start_line = _line_for_offset(target_text, span.body_start)
    body_end_line = _line_for_offset(target_text, span.body_end)
    theorem_statement = target_text[span.declaration_start : span.body_start].strip() or None
    return InspectionState(
        workspace=workspace,
        sandbox=sandbox,
        target_file=target_file,
        sandbox_target_file=sandbox_target_file,
        target_module_path=sandbox.target_relative_path(target_file),
        target_text=target_text,
        theorem=theorem,
        target=target,
        span=span,
        theorem_statement=theorem_statement,
        editable_span=FileSpan(start_line=body_start_line, end_line=body_end_line),
        proofctl_context=proofctl_context,
    )


def build_read_only_inspection_state(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    target_file: Path,
    theorem: str,
    timeout_seconds: float,
    command_name: str,
) -> InspectionState:
    """
    Build theorem inspection state directly from the bound workspace.

    This path is semantic-only and read-only: it asks Lean about the existing
    theorem surface without copying the workspace, patching source text, or
    materializing helpers.
    """
    target_file = target_file.expanduser().resolve()
    source_text = target_file.read_text(encoding="utf-8")
    probes, target = _resolve_semantic_target(
        tool_paths=tool_paths,
        workspace=workspace,
        file_path=target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        command_name=command_name,
    )
    span = locate_theorem_span_from_target(source=source_text, theorem_name=theorem, target=target)
    proofctl_context = probes.context_bundle(require_semantic_probes=True)
    body_start_line = _line_for_offset(source_text, span.body_start)
    body_end_line = _line_for_offset(source_text, span.body_end)
    theorem_statement = source_text[span.declaration_start : span.body_start].strip() or None
    return InspectionState(
        workspace=workspace,
        sandbox=None,
        target_file=target_file,
        sandbox_target_file=target_file,
        target_module_path=target_file.relative_to(workspace.root),
        target_text=source_text,
        theorem=theorem,
        target=target,
        span=span,
        theorem_statement=theorem_statement,
        editable_span=FileSpan(start_line=body_start_line, end_line=body_end_line),
        proofctl_context=proofctl_context,
    )


def build_inspection_snapshot(
    *,
    state: InspectionState,
    tool_paths: ToolPaths,
    diagnostics: list[Diagnostic],
    embedding_config: EmbeddingRetrievalConfig | None = None,
) -> InspectionSnapshot:
    """Build one theorem-local snapshot with retrieval and helper inventory."""
    store = store_paths(tool_paths.artifacts)
    ensure_store(store)
    proof_state_payload = (
        cast(dict[str, JsonValue], dataclass_to_jsonable(state.proofctl_context.proof_state))
        if state.proofctl_context.proof_state is not None
        else None
    )
    retrieval_explanation = collect_retrieval_explanation(
        workspace_root=state.workspace.root,
        target_module_path=state.target_module_path,
        target_file=state.target_file,
        theorem=state.theorem,
        source_text=state.target_text,
        store=store,
        failure_classes=[diagnostic.class_name.value for diagnostic in diagnostics],
        theorem_statement=state.theorem_statement,
        goal_snapshot=state.proofctl_context.goal_snapshot,
        imports=state.proofctl_context.imports,
        namespace=state.proofctl_context.namespace,
        tool_paths=tool_paths,
        workspace=state.workspace,
        embedding_config=embedding_config,
    )
    retrieval_hits = retrieval_explanation.hits
    active_helpers = retrieval_explanation.active_helpers
    insertion_point = None
    if state.proofctl_context.insertion_point is not None:
        insertion_point = InsertionPointSummary(
            kind=state.proofctl_context.insertion_point.kind,
            position=Position(
                line=state.proofctl_context.insertion_point.position.line,
                column=state.proofctl_context.insertion_point.position.column,
            ),
        )
    return InspectionSnapshot(
        theorem_statement=state.theorem_statement,
        editable_span=state.editable_span,
        imports=state.proofctl_context.imports,
        namespace=state.proofctl_context.namespace,
        proof_target=cast(
            dict[str, JsonValue] | None,
            None
            if state.proofctl_context.proof_target is None
            else dataclass_to_jsonable(state.proofctl_context.proof_target),
        ),
        proof_state=proof_state_payload,
        local_context=state.proofctl_context.local_context,
        goal_snapshot=state.proofctl_context.goal_snapshot,
        insertion_point=insertion_point,
        diagnostic_count=len(diagnostics),
        diagnostics=diagnostics,
        retrieval_hits=retrieval_hits,
        active_helpers=active_helpers,
        retrieval_explanation=retrieval_explanation,
        syntax_probe_results=state.proofctl_context.syntax_probe_results,
        semantic_probe_results=state.proofctl_context.semantic_probe_results,
        probe_results=state.proofctl_context.probe_results,
    )


def _resolve_semantic_target(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    file_path: Path,
    theorem: str,
    timeout_seconds: float,
    command_name: str,
) -> tuple[SemanticContextProbes, ProofctlDeclarationTarget]:
    probes = SemanticContextProbes.from_workspace(
        tool_paths=tool_paths,
        workspace=workspace,
        file_path=file_path,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        include_declaration_target=True,
    )
    declaration_target_result = probes.declaration_target_result
    assert declaration_target_result is not None
    target = probes.require_proof_target(command=command_name)
    return probes, target


def build_retrieve_snapshot(
    *,
    state: InspectionState,
    tool_paths: ToolPaths,
    diagnostics: list[Diagnostic],
    embedding_config: EmbeddingRetrievalConfig | None = None,
) -> RetrieveSnapshot:
    """Build one retrieval-focused theorem snapshot."""
    inspection = build_inspection_snapshot(
        state=state,
        tool_paths=tool_paths,
        diagnostics=diagnostics,
        embedding_config=embedding_config,
    )
    assert inspection.retrieval_explanation is not None
    return RetrieveSnapshot(
        theorem_statement=inspection.theorem_statement,
        imports=inspection.imports,
        namespace=inspection.namespace,
        goal_snapshot=inspection.goal_snapshot,
        diagnostic_count=inspection.diagnostic_count,
        diagnostics=inspection.diagnostics,
        retrieval=inspection.retrieval_explanation,
    )


def build_agent_request(
    *,
    task_kind: TaskKind,
    attempt_index: int,
    state: InspectionState,
    diagnostics: list[Diagnostic],
    prior_attempts: list[PatchAttempt],
    retrieval_hits: list[RetrievalHit],
    active_helpers: list[HelperRef],
    schema_path: Path,
    allowed_output_files: list[str],
    candidate_limit: int,
    repair_strategy: RepairStrategy | None = None,
    selected_failure: RepairFailure | None = None,
) -> AgentRequest:
    """Build one strict backend request from the shared inspection state."""
    if state.sandbox is None or state.helper_module_root is None:
        msg = "Backend planning and proof workflows require a sandbox-backed inspection state."
        raise ValueError(msg)
    source_lines = state.target_text.splitlines()
    declaration_start_line = _line_for_offset(state.target_text, state.span.declaration_start)
    body_end_line = _line_for_offset(state.target_text, state.span.body_end)
    window_start = max(1, declaration_start_line - 8)
    window_end = min(len(source_lines), body_end_line + 8)
    proof_state_payload = (
        cast(dict[str, JsonValue], dataclass_to_jsonable(state.proofctl_context.proof_state))
        if state.proofctl_context.proof_state is not None
        else None
    )
    return AgentRequest(
        task_kind=task_kind,
        attempt_index=attempt_index,
        candidate_limit=candidate_limit,
        theorem_name=state.theorem,
        target_file=str(state.target_module_path),
        workspace_root=str(state.workspace.root),
        workspace_package=state.workspace.package_name,
        sandbox_workspace_root=str(state.sandbox.sandbox_workspace.root),
        target_module_path=str(state.target_module_path),
        helper_module_root=state.helper_module_root,
        helper_root_dir=str(state.sandbox.helper_root_dir),
        theorem_statement=state.theorem_statement,
        editable_span=state.editable_span,
        source_window=SourceWindow(
            file=str(state.target_module_path),
            start_line=window_start,
            end_line=window_end,
            text="\n".join(source_lines[window_start - 1 : window_end]),
        ),
        diagnostics=diagnostics,
        prior_attempts=[
            PriorAttemptSummary(
                attempt_index=attempt.attempt_index,
                candidate_id=attempt.candidate.candidate_id,
                source=attempt.candidate.source,
                verified=attempt.verified,
                diagnostic_count=attempt.verification.diagnostic_count,
                diagnostics=attempt.verification.diagnostics,
            )
            for attempt in prior_attempts
        ],
        retrieval_hits=retrieval_hits,
        active_helpers=active_helpers,
        allowed_output_files=allowed_output_files,
        schema_path=str(schema_path),
        repair_strategy=repair_strategy,
        selected_failure=selected_failure,
        imports=state.proofctl_context.imports,
        namespace=state.proofctl_context.namespace,
        proof_target=cast(
            dict[str, JsonValue] | None,
            None
            if state.proofctl_context.proof_target is None
            else dataclass_to_jsonable(state.proofctl_context.proof_target),
        ),
        proof_state=proof_state_payload,
        local_context=state.proofctl_context.local_context,
        goal_snapshot=state.proofctl_context.goal_snapshot,
        syntax_probe_results=state.proofctl_context.syntax_probe_results,
        semantic_probe_results=state.proofctl_context.semantic_probe_results,
        probe_results=state.proofctl_context.probe_results,
    )


def run_inspect(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    target_file: Path,
    theorem: str,
    timeout_seconds: float,
    read_only: bool = False,
    verify_file_state: bool = False,
    embedding_config: EmbeddingRetrievalConfig | None = None,
) -> tuple[InspectResult, int]:
    """Collect one theorem inspection snapshot without mutating the workspace."""
    started_at = datetime.now(UTC)
    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    target_file = target_file.expanduser().resolve()
    policy = InspectionPolicy(read_only=read_only, verify_file_state=verify_file_state)
    state, sandbox_metadata = policy.build_state(
        tool_paths=tool_paths,
        workspace=workspace,
        run_dir=run_dir,
        target_file=target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        command_name="inspect",
    )
    diagnostics, file_state_check = policy.collect_file_state_check(
        state=state,
        timeout_seconds=timeout_seconds,
    )
    snapshot = build_inspection_snapshot(
        state=state,
        tool_paths=tool_paths,
        diagnostics=diagnostics,
        embedding_config=embedding_config,
    )
    finished_at = datetime.now(UTC)
    workspace_metadata = _workspace_metadata(workspace)
    result = InspectResult(
        run_id=run_id,
        command="inspect",
        status=InspectionStatus.OK,
        exit_code=ExitCode.SUCCESS,
        target_file=_project_relative(target_file, workspace.root),
        target_module_path=str(state.target_module_path),
        theorem=theorem,
        tool_root=str(tool_paths.root),
        workspace=workspace_metadata,
        sandbox=sandbox_metadata,
        helper_module_root=state.helper_module_root,
        read_only=read_only,
        file_state_check=file_state_check,
        artifact_dir=str(run_dir),
        wall_time_seconds=(finished_at - started_at).total_seconds(),
        snapshot=snapshot,
        started_at=started_at,
        finished_at=finished_at,
    )
    request = _request_payload(
        command="inspect",
        tool_paths=tool_paths,
        workspace=workspace_metadata,
        sandbox=sandbox_metadata,
        state=state,
        started_at=started_at,
        timeout_seconds=timeout_seconds,
        read_only=read_only,
        verify_file_state=verify_file_state,
    )
    manifest = RunManifest(
        run_id=run_id,
        command="inspect",
        status=result.status,
        exit_code=result.exit_code,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    write_inspect_artifacts(run_dir=run_dir, manifest=manifest, request=request, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, 0


def run_retrieve(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    target_file: Path,
    theorem: str,
    timeout_seconds: float,
    read_only: bool = False,
    verify_file_state: bool = False,
    embedding_config: EmbeddingRetrievalConfig | None = None,
) -> tuple[RetrieveResult, int]:
    """Produce one retrieval-focused theorem inspection run."""
    started_at = datetime.now(UTC)
    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    target_file = target_file.expanduser().resolve()
    policy = InspectionPolicy(read_only=read_only, verify_file_state=verify_file_state)
    state, sandbox_metadata = policy.build_state(
        tool_paths=tool_paths,
        workspace=workspace,
        run_dir=run_dir,
        target_file=target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        command_name="retrieve",
    )
    diagnostics, file_state_check = policy.collect_file_state_check(
        state=state,
        timeout_seconds=timeout_seconds,
    )
    snapshot = build_retrieve_snapshot(
        state=state,
        tool_paths=tool_paths,
        diagnostics=diagnostics,
        embedding_config=embedding_config,
    )
    finished_at = datetime.now(UTC)
    workspace_metadata = _workspace_metadata(workspace)
    result = RetrieveResult(
        run_id=run_id,
        command="retrieve",
        status=InspectionStatus.OK,
        exit_code=ExitCode.SUCCESS,
        target_file=_project_relative(target_file, workspace.root),
        target_module_path=str(state.target_module_path),
        theorem=theorem,
        tool_root=str(tool_paths.root),
        workspace=workspace_metadata,
        sandbox=sandbox_metadata,
        helper_module_root=state.helper_module_root,
        read_only=read_only,
        file_state_check=file_state_check,
        artifact_dir=str(run_dir),
        wall_time_seconds=(finished_at - started_at).total_seconds(),
        snapshot=snapshot,
        started_at=started_at,
        finished_at=finished_at,
    )
    request = _request_payload(
        command="retrieve",
        tool_paths=tool_paths,
        workspace=workspace_metadata,
        sandbox=sandbox_metadata,
        state=state,
        started_at=started_at,
        timeout_seconds=timeout_seconds,
        read_only=read_only,
        verify_file_state=verify_file_state,
    )
    manifest = RunManifest(
        run_id=run_id,
        command="retrieve",
        status=result.status,
        exit_code=result.exit_code,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    write_inspect_artifacts(run_dir=run_dir, manifest=manifest, request=request, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, 0


def run_plan(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    target_file: Path,
    theorem: str,
    timeout_seconds: float,
    backend: Backend,
) -> tuple[PlanResult, int]:
    """Produce one structured theorem plan from the bounded backend interface."""
    started_at = datetime.now(UTC)
    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    backend_dir = run_dir / "plan" / "backend"
    sandbox_workspace = create_sandbox_workspace(workspace=workspace, run_dir=run_dir)
    sandbox_metadata = sandbox_workspace.metadata()
    target_file = target_file.expanduser().resolve()
    state = build_inspection_state(
        tool_paths=tool_paths,
        workspace=workspace,
        sandbox=sandbox_workspace,
        target_file=target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        command_name="plan",
    )
    helper_module_root = state.helper_module_root
    assert helper_module_root is not None
    diagnostics = _baseline_diagnostics(
        workspace=sandbox_workspace.sandbox_workspace,
        target_file=state.sandbox_target_file,
        timeout_seconds=timeout_seconds,
    )
    inspection = build_inspection_snapshot(state=state, tool_paths=tool_paths, diagnostics=diagnostics)
    request = build_agent_request(
        task_kind=TaskKind.PLAN,
        attempt_index=1,
        state=state,
        diagnostics=diagnostics,
        prior_attempts=[],
        retrieval_hits=inspection.retrieval_hits,
        active_helpers=inspection.active_helpers,
        schema_path=tool_paths.prompts / "schemas" / "plan.schema.json",
        allowed_output_files=[],
        candidate_limit=0,
    )
    response = backend.propose(request)
    _write_backend_artifacts(backend_dir=backend_dir, request=request, response=response, inspection=inspection)
    finished_at = datetime.now(UTC)
    if response.result_kind is BackendResultKind.THEOREM_PLAN:
        status = PlanStatus.PLANNED
        exit_code = ExitCode.SUCCESS
        plan = response.theorem_plan
    elif response.result_kind is BackendResultKind.NO_PROGRESS:
        status = PlanStatus.NO_PLAN
        exit_code = ExitCode.CHECK_FAILED
        plan = None
    else:
        msg = f"Backend `{backend.name()}` returned `{response.result_kind.value}` for plan."
        raise ValueError(msg)
    workspace_metadata = _workspace_metadata(workspace)
    result = PlanResult(
        run_id=run_id,
        command="plan",
        status=status,
        exit_code=exit_code,
        target_file=_project_relative(target_file, workspace.root),
        target_module_path=str(state.target_module_path),
        theorem=theorem,
        tool_root=str(tool_paths.root),
        workspace=workspace_metadata,
        sandbox=sandbox_metadata,
        helper_module_root=helper_module_root,
        artifact_dir=str(run_dir),
        backend=backend.name(),
        wall_time_seconds=(finished_at - started_at).total_seconds(),
        backend_call_count=1,
        inspection=inspection,
        plan=plan,
        backend_response=response,
        started_at=started_at,
        finished_at=finished_at,
    )
    request_payload = _request_payload(
        command="plan",
        tool_paths=tool_paths,
        workspace=workspace_metadata,
        sandbox=sandbox_metadata,
        state=state,
        started_at=started_at,
        timeout_seconds=timeout_seconds,
        backend=backend.name(),
    )
    manifest = RunManifest(
        run_id=run_id,
        command="plan",
        status=result.status,
        exit_code=result.exit_code,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    write_plan_artifacts(run_dir=run_dir, manifest=manifest, request=request_payload, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, _exit_code_number(exit_code)


def _baseline_diagnostics(
    *, workspace: WorkspaceBinding, target_file: Path, timeout_seconds: float
) -> list[Diagnostic]:
    lean_result = run_lean_check(
        workspace_root=workspace.root,
        target_file=target_file,
        timeout_seconds=timeout_seconds,
        workspace_root_for_diagnostics=workspace.root,
    )
    return lean_result.diagnostics


def _request_payload(
    *,
    command: str,
    tool_paths: ToolPaths,
    workspace: WorkspaceMetadata,
    sandbox: SandboxMetadata | None,
    state: InspectionState,
    started_at: datetime,
    timeout_seconds: float,
    backend: str | None = None,
    read_only: bool = False,
    verify_file_state: bool = False,
) -> RunRequest:
    return RunRequest(
        command=command,
        target_file=_project_relative(state.target_file, state.workspace.root),
        cwd=str(state.workspace.root),
        tool_root=str(tool_paths.root),
        workspace_root=workspace.root,
        workspace_package=workspace.package,
        workspace_toolchain=workspace.toolchain,
        target_module_path=str(state.target_module_path),
        timeout_seconds=timeout_seconds,
        json_output=False,
        jsonl_output=False,
        started_at=started_at,
        sandbox_strategy=None if sandbox is None else sandbox.strategy,
        sandbox_root=None if sandbox is None else sandbox.sandbox_root,
        sandbox_workspace_root=None if sandbox is None else sandbox.sandbox_workspace_root,
        helper_module_root=state.helper_module_root,
        helper_root_dir=None if sandbox is None else sandbox.helper_root_dir,
        read_only=read_only,
        verify_file_state=verify_file_state,
        proof_target=cast(
            dict[str, JsonValue] | None,
            None
            if state.proofctl_context.proof_target is None
            else dataclass_to_jsonable(state.proofctl_context.proof_target),
        ),
        syntax_probe_results=state.proofctl_context.syntax_probe_results,
        semantic_probe_results=state.proofctl_context.semantic_probe_results,
        probe_results=state.proofctl_context.probe_results,
        theorem=state.theorem,
        backend=backend,
    )


def _write_backend_artifacts(
    *,
    backend_dir: Path,
    request: AgentRequest,
    response: AgentResponse,
    inspection: InspectionSnapshot,
) -> None:
    backend_dir.mkdir(parents=True, exist_ok=True)
    _write_json(backend_dir / "request.json", request.to_json())
    _write_json(backend_dir / "response.json", response.to_json())
    _write_json(
        backend_dir / "command.json",
        {
            "argv": response.invocation.argv,
            "cwd": response.invocation.cwd,
            "prompt_path": response.prompt_path,
            "schema_path": response.schema_path,
        },
    )
    _write_json(
        backend_dir / "retrieval.json",
        {
            "retrieval_hits": dataclass_to_jsonable(inspection.retrieval_hits),
            "active_helpers": dataclass_to_jsonable(inspection.active_helpers),
        },
    )
    (backend_dir / "stdout.txt").write_text(response.raw_stdout, encoding="utf-8")
    (backend_dir / "stderr.txt").write_text(response.raw_stderr, encoding="utf-8")


def _exit_code_number(exit_code: ExitCode) -> int:
    if exit_code == ExitCode.SUCCESS:
        return 0
    if exit_code == ExitCode.CHECK_FAILED:
        return 1
    if exit_code == ExitCode.BAD_USAGE:
        return 2
    if exit_code == ExitCode.TIMEOUT:
        return 3
    return 4


def _line_for_offset(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _project_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _workspace_metadata(workspace: WorkspaceBinding) -> WorkspaceMetadata:
    return WorkspaceMetadata(
        root=str(workspace.root),
        package=workspace.package_name,
        toolchain=workspace.toolchain,
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
