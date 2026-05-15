"""Sandboxed theorem-body repair, solve, and decomposition loop."""

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from lean_proofctl.adapters.base import Backend
from lean_proofctl.engine.backend_protocol import classify_theorem_body_response
from lean_proofctl.engine.cache import StorePaths, append_jsonl, ensure_store, store_paths
from lean_proofctl.engine.config import ToolPaths, WorkspaceBinding
from lean_proofctl.engine.inspection import InspectionState, build_agent_request, build_inspection_state
from lean_proofctl.engine.lean_runner import run_lean_check, run_lean_make_olean
from lean_proofctl.engine.ledger import (
    append_index_row,
    write_decompose_artifacts,
    write_loop_artifacts,
)
from lean_proofctl.engine.models import (
    AgentRequest,
    AgentResponse,
    BackendResultKind,
    CheckResult,
    CheckStatus,
    DecomposeResult,
    DecompositionLayer,
    DecompositionStep,
    Diagnostic,
    DiagnosticClass,
    ExitCode,
    FileSpan,
    HelperRef,
    JsonValue,
    LeanRunResult,
    LoopResult,
    LoopStatus,
    PatchAttempt,
    RepairFailure,
    RepairStrategy,
    RetrievalHit,
    RunManifest,
    RunRequest,
    SourceWindow,
    TaskKind,
    TheoremBodyResponseClass,
    VerifiedHelper,
    WorkspaceMetadata,
    dataclass_to_jsonable,
)
from lean_proofctl.engine.patching import (
    apply_verified_transplant,
    create_sandbox_patch,
    helper_module_name,
    render_helper_file,
)
from lean_proofctl.engine.retrieval import collect_retrieval_hits
from lean_proofctl.engine.run_id import new_run_id
from lean_proofctl.engine.sandbox import (
    MANAGED_HELPER_MODULE_ROOT,
    MANAGED_HELPER_ROOT,
    SandboxWorkspace,
    create_sandbox_workspace,
)

IDENT_RE = re.compile(r"[A-Za-z_][\w.']*")
STOPWORDS = {
    "by",
    "exact",
    "have",
    "lemma",
    "namespace",
    "show",
    "theorem",
}


@dataclass(frozen=True)
class LoopOptions:
    """Stable inputs for one repair or solve run."""

    command: str
    target_file: Path
    theorem: str
    timeout_seconds: float
    apply_patch: bool
    max_attempts: int
    max_backend_calls: int | None = None
    enable_retrieval: bool = True
    enable_decomposition: bool = True
    max_decomposition_depth: int = 2
    prompt_template_name: str | None = None


@dataclass(frozen=True)
class _DecompositionOutcome:
    """Verified helpers plus the backend-call cost of one decomposition pass."""

    verified_helpers: list[VerifiedHelper]
    backend_call_count: int
    decomposition_count: int


type AttemptState = InspectionState


def run_loop(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    options: LoopOptions,
    backend: Backend,
) -> tuple[LoopResult, int]:
    """Run the bounded theorem-body repair loop and persist one run bundle."""
    started_at = datetime.now(UTC)
    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    work_dir = run_dir / "work"
    store = store_paths(tool_paths.artifacts)
    ensure_store(store)
    workspace_metadata = _workspace_metadata(workspace)
    sandbox_workspace = create_sandbox_workspace(workspace=workspace, run_dir=run_dir)
    sandbox_metadata = sandbox_workspace.metadata()

    target_file = options.target_file.expanduser().resolve()
    attempt_state = _build_attempt_state(
        tool_paths=tool_paths,
        workspace=workspace,
        sandbox=sandbox_workspace,
        target_file=target_file,
        theorem=options.theorem,
        timeout_seconds=options.timeout_seconds,
        command_name=options.command,
    )
    helper_module_root = attempt_state.helper_module_root
    assert helper_module_root is not None
    task_kind = TaskKind(options.command)

    baseline = _perform_check(
        tool_paths=tool_paths,
        workspace=sandbox_workspace.sandbox_workspace,
        target_file=attempt_state.sandbox_target_file,
        timeout_seconds=options.timeout_seconds,
        command="check",
        run_id=f"{run_id}-baseline",
        artifact_dir=run_dir / "baseline",
        started_at=started_at,
    )
    attempts: list[PatchAttempt] = []
    retrieval_hits: list[RetrievalHit] = []
    active_helpers: list[HelperRef] = []
    proved_helpers: list[VerifiedHelper] = []
    verified_attempt: int | None = None
    applied = False
    decomposition_count = 0
    decomposition_attempted = False
    backend_call_count = 0

    attempt_index = 1
    while attempt_index <= options.max_attempts:
        if options.max_backend_calls is not None and backend_call_count >= options.max_backend_calls:
            break
        diagnostics = baseline.diagnostics if not attempts else attempts[-1].verification.diagnostics
        if options.enable_retrieval:
            retrieval_hits, active_helpers = collect_retrieval_hits(
                workspace_root=workspace.root,
                target_module_path=attempt_state.target_module_path,
                target_file=target_file,
                theorem=options.theorem,
                source_text=attempt_state.target_text,
                store=store,
                failure_classes=[diagnostic.class_name.value for diagnostic in diagnostics],
                theorem_statement=attempt_state.theorem_statement,
                goal_snapshot=attempt_state.proofctl_context.goal_snapshot,
                imports=attempt_state.proofctl_context.imports,
                namespace=attempt_state.proofctl_context.namespace,
                tool_paths=tool_paths,
                workspace=workspace,
            )
        else:
            retrieval_hits, active_helpers = [], []
        attempt_name = f"attempt-{attempt_index:03d}"
        attempt_root = work_dir / attempt_name
        attempt_root.mkdir(parents=True, exist_ok=True)
        _write_retrieval_artifact(
            path=attempt_root / "backend" / "retrieval.json",
            retrieval_hits=retrieval_hits,
            active_helpers=active_helpers,
        )
        repair_strategy, selected_failure = _select_repair_strategy(diagnostics=diagnostics)
        request = _build_agent_request(
            task_kind=task_kind,
            attempt_index=attempt_index,
            attempt_state=attempt_state,
            diagnostics=diagnostics,
            prior_attempts=attempts,
            retrieval_hits=retrieval_hits,
            active_helpers=active_helpers,
            schema_path=tool_paths.prompts / "schemas" / "result.schema.json",
            helper_output_limit=2,
            prompt_template_name=options.prompt_template_name,
            repair_strategy=repair_strategy,
            selected_failure=selected_failure,
        )
        classified = classify_theorem_body_response(
            response=backend.propose(request),
            expected_theorem=options.theorem,
        )
        response = classified.response
        backend_call_count += 1
        _write_backend_artifacts(attempt_root=attempt_root, request=request, response=response)
        response_class = (
            response.theorem_body_classification.response_class
            if response.theorem_body_classification is not None
            else None
        )
        if len(classified.usable_candidates) != 1:
            if (
                options.enable_decomposition
                and not decomposition_attempted
                and response_class
                in {
                    TheoremBodyResponseClass.NO_PROGRESS,
                    TheoremBodyResponseClass.NO_USABLE_CANDIDATE,
                }
            ):
                new_helpers = _run_decomposition(
                    tool_paths=tool_paths,
                    workspace=workspace,
                    sandbox=sandbox_workspace,
                    backend=backend,
                    run_dir=run_dir,
                    attempt_index=attempt_index,
                    attempt_state=attempt_state,
                    diagnostics=diagnostics,
                    attempts=attempts,
                    retrieval_hits=retrieval_hits,
                    helper_output_limit=2,
                    timeout_seconds=options.timeout_seconds,
                    current_depth=0,
                    max_decomposition_depth=options.max_decomposition_depth,
                    decomposition_attempt_index=1,
                    max_backend_calls=(
                        None
                        if options.max_backend_calls is None
                        else max(0, options.max_backend_calls - backend_call_count)
                    ),
                )
                if new_helpers.verified_helpers:
                    decomposition_attempted = True
                    decomposition_count += new_helpers.decomposition_count
                    proved_helpers.extend(new_helpers.verified_helpers)
                    backend_call_count += new_helpers.backend_call_count
                    continue
            break
        candidate = classified.usable_candidates[0]
        _materialize_active_helpers(sandbox=sandbox_workspace, active_helpers=active_helpers)
        patch = create_sandbox_patch(
            sandbox=sandbox_workspace,
            sandbox_root=attempt_root,
            target_file=target_file,
            source_text=attempt_state.target_text,
            span=attempt_state.span,
            candidate_body=candidate.body,
            active_helpers=active_helpers,
        )
        verification = _perform_check(
            tool_paths=tool_paths,
            workspace=sandbox_workspace.sandbox_workspace,
            target_file=patch.verification_file,
            timeout_seconds=options.timeout_seconds,
            command="check",
            run_id=f"{run_id}-{attempt_name}",
            artifact_dir=patch.sandbox_root / "check",
            started_at=datetime.now(UTC),
            use_lake_env=bool(active_helpers),
        )
        _write_attempt_artifacts(
            sandbox_root=patch.sandbox_root,
            candidate=dataclass_to_jsonable(candidate),
            candidate_body=candidate.body,
            diff_text=patch.diff_text,
        )
        attempt = PatchAttempt(
            attempt_index=attempt_index,
            candidate=candidate,
            workspace_root=str(workspace.root),
            workspace_package=workspace.package_name,
            target_module_path=str(attempt_state.target_module_path),
            sandbox_root=str(patch.sandbox_root),
            sandbox_workspace_root=str(patch.sandbox_workspace_root),
            helper_module_root=helper_module_root,
            sandbox_file=str(patch.sandbox_file),
            verification_file=str(patch.verification_file),
            candidate_body_path=str(patch.sandbox_root / "candidate.body.lean"),
            diff_path=str(patch.sandbox_root / "patch.diff"),
            backend_request_path=str(attempt_root / "backend" / "request.json"),
            backend_response_path=str(attempt_root / "backend" / "response.json"),
            backend_stdout_path=str(attempt_root / "backend" / "stdout.txt"),
            backend_stderr_path=str(attempt_root / "backend" / "stderr.txt"),
            backend_command_path=str(attempt_root / "backend" / "command.json"),
            verified=verification.status is CheckStatus.OK,
            proof_target=cast(
                dict[str, JsonValue] | None,
                None
                if attempt_state.proofctl_context.proof_target is None
                else dataclass_to_jsonable(attempt_state.proofctl_context.proof_target),
            ),
            syntax_probe_results=attempt_state.proofctl_context.syntax_probe_results,
            semantic_probe_results=attempt_state.proofctl_context.semantic_probe_results,
            backend_response=response,
            verification=verification,
            repair_strategy=repair_strategy,
            selected_failure=selected_failure,
        )
        attempts.append(attempt)
        if attempt.verified:
            verified_attempt = attempt_index
            if options.apply_patch:
                _apply_verified_transplant(
                    workspace=workspace,
                    sandbox=sandbox_workspace,
                    target_file=target_file,
                    theorem=options.theorem,
                    candidate_body=candidate.body,
                    helper_modules=patch.helper_modules,
                    timeout_seconds=options.timeout_seconds,
                )
                applied = True
            _record_accepted_patch(
                store=store,
                run_id=run_id,
                workspace_root=workspace.root,
                target_module_path=attempt_state.target_module_path,
                theorem=options.theorem,
                theorem_statement=attempt_state.theorem_statement,
                namespace=attempt_state.proofctl_context.namespace,
                imports=attempt_state.proofctl_context.imports,
                goal_snapshot=attempt_state.proofctl_context.goal_snapshot,
                body=candidate.body,
                candidate_body_path=patch.sandbox_root / "candidate.body.lean",
                diagnostics=verification.diagnostics,
                helper_modules=patch.helper_modules,
                helper_module_root=helper_module_root,
            )
            break
        if options.enable_decomposition and not decomposition_attempted:
            new_helpers = _run_decomposition(
                tool_paths=tool_paths,
                workspace=workspace,
                sandbox=sandbox_workspace,
                backend=backend,
                run_dir=run_dir,
                attempt_index=attempt_index,
                attempt_state=attempt_state,
                diagnostics=verification.diagnostics,
                attempts=attempts,
                retrieval_hits=retrieval_hits,
                helper_output_limit=2,
                timeout_seconds=options.timeout_seconds,
                current_depth=0,
                max_decomposition_depth=options.max_decomposition_depth,
                decomposition_attempt_index=1,
                max_backend_calls=(
                    None
                    if options.max_backend_calls is None
                    else max(0, options.max_backend_calls - backend_call_count)
                ),
            )
            if new_helpers.verified_helpers:
                decomposition_attempted = True
                decomposition_count += new_helpers.decomposition_count
                proved_helpers.extend(new_helpers.verified_helpers)
                backend_call_count += new_helpers.backend_call_count
                continue
        attempt_index += 1

    finished_at = datetime.now(UTC)
    status, exit_code = _loop_status(attempts=attempts, verified_attempt=verified_attempt)
    result = LoopResult(
        run_id=run_id,
        command=options.command,
        status=status,
        exit_code=exit_code,
        target_file=_project_relative(target_file, workspace.root),
        target_module_path=str(attempt_state.target_module_path),
        theorem=options.theorem,
        tool_root=str(tool_paths.root),
        workspace=workspace_metadata,
        sandbox=sandbox_metadata,
        helper_module_root=helper_module_root,
        artifact_dir=str(run_dir),
        work_dir=str(work_dir),
        backend=backend.name(),
        wall_time_seconds=(finished_at - started_at).total_seconds(),
        backend_call_count=backend_call_count,
        attempt_count=len(attempts),
        verified_attempt=verified_attempt,
        applied=applied,
        max_decomposition_depth=options.max_decomposition_depth,
        retrieval_hits=retrieval_hits,
        proved_helpers=proved_helpers,
        decomposition_count=decomposition_count,
        baseline=baseline,
        attempts=attempts,
        started_at=started_at,
        finished_at=finished_at,
    )
    request = RunRequest(
        command=options.command,
        target_file=_project_relative(target_file, workspace.root),
        cwd=str(workspace.root),
        tool_root=str(tool_paths.root),
        workspace_root=workspace_metadata.root,
        workspace_package=workspace_metadata.package,
        workspace_toolchain=workspace_metadata.toolchain,
        target_module_path=str(attempt_state.target_module_path),
        timeout_seconds=options.timeout_seconds,
        json_output=False,
        jsonl_output=False,
        started_at=started_at,
        sandbox_strategy=sandbox_metadata.strategy,
        sandbox_root=sandbox_metadata.sandbox_root,
        sandbox_workspace_root=sandbox_metadata.sandbox_workspace_root,
        helper_module_root=attempt_state.helper_module_root,
        helper_root_dir=sandbox_metadata.helper_root_dir,
        syntax_probe_results=attempt_state.proofctl_context.syntax_probe_results,
        semantic_probe_results=attempt_state.proofctl_context.semantic_probe_results,
        probe_results=attempt_state.proofctl_context.probe_results,
        theorem=options.theorem,
        backend=backend.name(),
        apply=options.apply_patch,
        max_attempts=options.max_attempts,
        max_decomposition_depth=options.max_decomposition_depth,
    )
    manifest = RunManifest(
        run_id=run_id,
        command=options.command,
        status=status,
        exit_code=exit_code,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    write_loop_artifacts(run_dir=run_dir, manifest=manifest, request=request, result=result)
    append_index_row(tool_paths.artifact_index, result)
    _record_failure_summary(
        store=store,
        run_id=run_id,
        workspace_root=workspace.root,
        target_module_path=attempt_state.target_module_path,
        theorem=options.theorem,
        theorem_statement=attempt_state.theorem_statement,
        namespace=attempt_state.proofctl_context.namespace,
        imports=attempt_state.proofctl_context.imports,
        goal_snapshot=attempt_state.proofctl_context.goal_snapshot,
        diagnostics=result.baseline.diagnostics if not attempts else attempts[-1].verification.diagnostics,
        helper_modules=[helper.module_name for helper in active_helpers],
        helper_module_root=helper_module_root,
        success=verified_attempt is not None,
    )
    return result, _exit_code_number(exit_code)


def run_decompose(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    target_file: Path,
    theorem: str,
    timeout_seconds: float,
    max_decomposition_depth: int,
    backend: Backend,
) -> tuple[DecomposeResult, int]:
    """Produce one structured helper plan without patching the target theorem."""
    started_at = datetime.now(UTC)
    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    store = store_paths(tool_paths.artifacts)
    ensure_store(store)
    workspace_metadata = _workspace_metadata(workspace)
    sandbox_workspace = create_sandbox_workspace(workspace=workspace, run_dir=run_dir)
    sandbox_metadata = sandbox_workspace.metadata()

    target_file = target_file.expanduser().resolve()
    attempt_state = _build_attempt_state(
        tool_paths=tool_paths,
        workspace=workspace,
        sandbox=sandbox_workspace,
        target_file=target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        command_name="decompose",
    )
    helper_module_root = attempt_state.helper_module_root
    assert helper_module_root is not None
    baseline = _perform_check(
        tool_paths=tool_paths,
        workspace=sandbox_workspace.sandbox_workspace,
        target_file=attempt_state.sandbox_target_file,
        timeout_seconds=timeout_seconds,
        command="check",
        run_id=f"{run_id}-baseline",
        artifact_dir=run_dir / "baseline",
        started_at=started_at,
    )
    retrieval_hits, active_helpers = collect_retrieval_hits(
        workspace_root=workspace.root,
        target_module_path=attempt_state.target_module_path,
        target_file=target_file,
        theorem=theorem,
        source_text=attempt_state.target_text,
        store=store,
        failure_classes=[diagnostic.class_name.value for diagnostic in baseline.diagnostics],
        theorem_statement=attempt_state.theorem_statement,
        goal_snapshot=attempt_state.proofctl_context.goal_snapshot,
        imports=attempt_state.proofctl_context.imports,
        namespace=attempt_state.proofctl_context.namespace,
        tool_paths=tool_paths,
        workspace=workspace,
    )
    request = _build_agent_request(
        task_kind=TaskKind.DECOMPOSE,
        attempt_index=1,
        attempt_state=attempt_state,
        diagnostics=baseline.diagnostics,
        prior_attempts=[],
        retrieval_hits=retrieval_hits,
        active_helpers=active_helpers,
        schema_path=tool_paths.prompts / "schemas" / "result.schema.json",
        helper_output_limit=2,
        prompt_template_name=None,
    )
    response = backend.propose(request)
    _write_backend_artifacts(attempt_root=run_dir / "decompose", request=request, response=response)
    _write_retrieval_artifact(
        path=run_dir / "decompose" / "backend" / "retrieval.json",
        retrieval_hits=retrieval_hits,
        active_helpers=active_helpers,
    )
    finished_at = datetime.now(UTC)
    if response.result_kind is BackendResultKind.DECOMPOSITION:
        status = LoopStatus.VERIFIED
        exit_code = ExitCode.SUCCESS
        steps = response.decomposition
    elif response.result_kind is BackendResultKind.NO_PROGRESS:
        status = LoopStatus.NO_CANDIDATE
        exit_code = ExitCode.CHECK_FAILED
        steps = []
    else:
        msg = f"Backend `{backend.name()}` returned `{response.result_kind.value}` for decompose."
        raise ValueError(msg)
    result = DecomposeResult(
        run_id=run_id,
        command="decompose",
        status=status,
        exit_code=exit_code,
        target_file=_project_relative(target_file, workspace.root),
        target_module_path=str(attempt_state.target_module_path),
        theorem=theorem,
        tool_root=str(tool_paths.root),
        workspace=workspace_metadata,
        sandbox=sandbox_metadata,
        helper_module_root=helper_module_root,
        artifact_dir=str(run_dir),
        backend=backend.name(),
        wall_time_seconds=(finished_at - started_at).total_seconds(),
        backend_call_count=1,
        max_decomposition_depth=max_decomposition_depth,
        retrieval_hits=retrieval_hits,
        steps=steps,
        started_at=started_at,
        finished_at=finished_at,
    )
    request_payload = RunRequest(
        command="decompose",
        target_file=_project_relative(target_file, workspace.root),
        cwd=str(workspace.root),
        tool_root=str(tool_paths.root),
        workspace_root=workspace_metadata.root,
        workspace_package=workspace_metadata.package,
        workspace_toolchain=workspace_metadata.toolchain,
        target_module_path=str(attempt_state.target_module_path),
        timeout_seconds=timeout_seconds,
        json_output=False,
        jsonl_output=False,
        started_at=started_at,
        sandbox_strategy=sandbox_metadata.strategy,
        sandbox_root=sandbox_metadata.sandbox_root,
        sandbox_workspace_root=sandbox_metadata.sandbox_workspace_root,
        helper_module_root=attempt_state.helper_module_root,
        helper_root_dir=sandbox_metadata.helper_root_dir,
        syntax_probe_results=attempt_state.proofctl_context.syntax_probe_results,
        semantic_probe_results=attempt_state.proofctl_context.semantic_probe_results,
        probe_results=attempt_state.proofctl_context.probe_results,
        theorem=theorem,
        backend=backend.name(),
        max_decomposition_depth=max_decomposition_depth,
    )
    manifest = RunManifest(
        run_id=run_id,
        command="decompose",
        status=status,
        exit_code=exit_code,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    write_decompose_artifacts(run_dir=run_dir, manifest=manifest, request=request_payload, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, _exit_code_number(exit_code)


def _run_decomposition(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    sandbox: SandboxWorkspace,
    backend: Backend,
    run_dir: Path,
    attempt_index: int,
    attempt_state: AttemptState,
    diagnostics: list[Diagnostic],
    attempts: list[PatchAttempt],
    retrieval_hits: list[RetrievalHit],
    helper_output_limit: int,
    timeout_seconds: float,
    current_depth: int,
    max_decomposition_depth: int,
    decomposition_attempt_index: int,
    max_backend_calls: int | None,
) -> _DecompositionOutcome:
    if max_backend_calls is not None and max_backend_calls <= 0:
        return _DecompositionOutcome(verified_helpers=[], backend_call_count=0, decomposition_count=0)
    request = _build_agent_request(
        task_kind=TaskKind.DECOMPOSE,
        attempt_index=decomposition_attempt_index,
        attempt_state=attempt_state,
        diagnostics=diagnostics,
        prior_attempts=attempts,
        retrieval_hits=retrieval_hits,
        active_helpers=[],
        schema_path=tool_paths.prompts / "schemas" / "result.schema.json",
        helper_output_limit=helper_output_limit,
        prompt_template_name=None,
    )
    decompose_root = run_dir / f"decompose-{attempt_index:03d}"
    response = backend.propose(request)
    _write_backend_artifacts(attempt_root=decompose_root, request=request, response=response)
    _write_retrieval_artifact(
        path=decompose_root / "backend" / "retrieval.json",
        retrieval_hits=retrieval_hits,
        active_helpers=[],
    )
    if response.result_kind is not BackendResultKind.DECOMPOSITION:
        return _DecompositionOutcome(verified_helpers=[], backend_call_count=1, decomposition_count=0)
    verified: list[VerifiedHelper] = []
    store = store_paths(tool_paths.artifacts)
    backend_call_count = 1
    decomposition_count = 1
    for step_index, step in enumerate(response.decomposition[:helper_output_limit], start=1):
        if step.layer is not DecompositionLayer.MISSING_LEMMA:
            continue
        existing = _existing_helper(
            store=store,
            workspace_root=workspace.root,
            target_module_path=attempt_state.target_module_path,
            theorem=attempt_state.theorem,
            helper_name=step.helper_name,
        )
        if existing is not None:
            sandbox.materialize_helper(
                HelperRef(
                    helper_name=existing.helper_name,
                    module_name=existing.module_name,
                    file_path=existing.file_path,
                    theorem=existing.theorem,
                    statement=existing.statement,
                    workspace_root=str(workspace.root),
                    target_module_path=str(attempt_state.target_module_path),
                    helper_module_root=attempt_state.helper_module_root,
                )
            )
            verified.append(existing)
            continue
        helper_outcome = _resolve_recursive_helper(
            tool_paths=tool_paths,
            sandbox=sandbox,
            backend=backend,
            source_file=attempt_state.target_file,
            source_theorem=attempt_state.theorem,
            step=step,
            helper_root=decompose_root / f"helper-{step_index:03d}",
            timeout_seconds=timeout_seconds,
            current_depth=current_depth + 1,
            max_decomposition_depth=max_decomposition_depth,
            decomposition_attempt_index=decomposition_attempt_index + backend_call_count,
            max_backend_calls=(None if max_backend_calls is None else max(0, max_backend_calls - backend_call_count)),
            dependency_helpers=[],
        )
        backend_call_count += helper_outcome.backend_call_count
        decomposition_count += helper_outcome.decomposition_count
        if helper_outcome.resolved_helper is None:
            break
        verified.extend(helper_outcome.nested_helpers)
        verified.append(helper_outcome.resolved_helper)
        verified_helper = helper_outcome.resolved_helper
        append_jsonl(
            store.proved_helpers,
            {
                "run_id": run_dir.name,
                "workspace_root": str(workspace.root),
                "target_module_path": str(attempt_state.target_module_path),
                "theorem": attempt_state.theorem,
                "helper_name": verified_helper.helper_name,
                "module_name": verified_helper.module_name,
                "file_path": verified_helper.file_path,
                "statement": verified_helper.statement,
                "source_step_id": verified_helper.source_step_id,
                "decomposition_depth": verified_helper.decomposition_depth,
                "helper_module_root": attempt_state.helper_module_root,
                "namespace": attempt_state.proofctl_context.namespace,
                "imports": attempt_state.proofctl_context.imports,
                "goal_snapshot": attempt_state.proofctl_context.goal_snapshot,
                "goal_constants": _goal_constants(
                    theorem_statement=step.helper_statement,
                    goal_snapshot=attempt_state.proofctl_context.goal_snapshot,
                ),
                "type_heads": _type_heads(
                    theorem_statement=step.helper_statement,
                    goal_snapshot=attempt_state.proofctl_context.goal_snapshot,
                ),
            },
        )
    return _DecompositionOutcome(
        verified_helpers=verified,
        backend_call_count=backend_call_count,
        decomposition_count=decomposition_count,
    )


@dataclass(frozen=True)
class _RecursiveHelperOutcome:
    resolved_helper: VerifiedHelper | None
    nested_helpers: list[VerifiedHelper]
    backend_call_count: int
    decomposition_count: int


def _resolve_recursive_helper(
    *,
    tool_paths: ToolPaths,
    sandbox: SandboxWorkspace,
    backend: Backend,
    source_file: Path,
    source_theorem: str,
    step: DecompositionStep,
    helper_root: Path,
    timeout_seconds: float,
    current_depth: int,
    max_decomposition_depth: int,
    decomposition_attempt_index: int,
    max_backend_calls: int | None,
    dependency_helpers: list[VerifiedHelper],
) -> _RecursiveHelperOutcome:
    helper_root.mkdir(parents=True, exist_ok=True)
    helper_name = step.helper_name or step.step_id
    module_name = _managed_helper_module_name(
        source_file=source_file,
        source_theorem=source_theorem,
        helper_name=helper_name,
        sandbox_root=sandbox.sandbox_workspace.root,
        workspace_root=sandbox.real_workspace.root,
    )
    helper_file = sandbox.helper_file(module_name)
    helper_file.parent.mkdir(parents=True, exist_ok=True)
    dependency_refs = [_verified_helper_ref(helper, theorem=source_theorem) for helper in dependency_helpers]
    verification = _verify_helper_file(
        tool_paths=tool_paths,
        sandbox=sandbox,
        helper_file=helper_file,
        helper_root=helper_root,
        run_id=f"{helper_root.parent.parent.name}-helper-{helper_root.name}",
        step=step,
        timeout_seconds=timeout_seconds,
        dependency_refs=dependency_refs,
    )
    if verification is None:
        return _RecursiveHelperOutcome(
            resolved_helper=VerifiedHelper(
                helper_name=helper_name,
                module_name=module_name,
                file_path=str(helper_file),
                theorem=source_theorem,
                statement=step.helper_statement or "",
                source_step_id=step.step_id,
                decomposition_depth=current_depth,
            ),
            nested_helpers=[],
            backend_call_count=0,
            decomposition_count=0,
        )
    diagnostics = verification.diagnostics
    if current_depth >= max_decomposition_depth or (max_backend_calls is not None and max_backend_calls <= 0):
        helper_file.unlink(missing_ok=True)
        helper_file.with_suffix(".olean").unlink(missing_ok=True)
        return _RecursiveHelperOutcome(
            resolved_helper=None,
            nested_helpers=[],
            backend_call_count=0,
            decomposition_count=0,
        )
    request = _build_recursive_helper_request(
        sandbox=sandbox,
        helper_file=helper_file,
        helper_name=helper_name,
        helper_statement=step.helper_statement or "",
        helper_text=_render_helper_text(step=step, dependency_refs=dependency_refs),
        diagnostics=diagnostics,
        dependency_refs=dependency_refs,
        attempt_index=decomposition_attempt_index,
        schema_path=tool_paths.prompts / "schemas" / "result.schema.json",
    )
    response = backend.propose(request)
    recursive_root = helper_root / "recursive"
    _write_backend_artifacts(attempt_root=recursive_root, request=request, response=response)
    _write_retrieval_artifact(
        path=recursive_root / "backend" / "retrieval.json",
        retrieval_hits=[],
        active_helpers=dependency_refs,
    )
    if response.result_kind is not BackendResultKind.DECOMPOSITION:
        helper_file.unlink(missing_ok=True)
        helper_file.with_suffix(".olean").unlink(missing_ok=True)
        return _RecursiveHelperOutcome(
            resolved_helper=None,
            nested_helpers=[],
            backend_call_count=1,
            decomposition_count=0,
        )
    backend_call_count = 1
    decomposition_count = 1
    nested_helpers: list[VerifiedHelper] = []
    resolved_dependencies = list(dependency_helpers)
    for step_index, nested_step in enumerate(response.decomposition, start=1):
        if nested_step.layer is not DecompositionLayer.MISSING_LEMMA:
            continue
        nested_outcome = _resolve_recursive_helper(
            tool_paths=tool_paths,
            sandbox=sandbox,
            backend=backend,
            source_file=helper_file,
            source_theorem=helper_name,
            step=nested_step,
            helper_root=recursive_root / f"helper-{step_index:03d}",
            timeout_seconds=timeout_seconds,
            current_depth=current_depth + 1,
            max_decomposition_depth=max_decomposition_depth,
            decomposition_attempt_index=decomposition_attempt_index + backend_call_count,
            max_backend_calls=(None if max_backend_calls is None else max(0, max_backend_calls - backend_call_count)),
            dependency_helpers=resolved_dependencies,
        )
        backend_call_count += nested_outcome.backend_call_count
        decomposition_count += nested_outcome.decomposition_count
        if nested_outcome.resolved_helper is None:
            helper_file.unlink(missing_ok=True)
            helper_file.with_suffix(".olean").unlink(missing_ok=True)
            return _RecursiveHelperOutcome(
                resolved_helper=None,
                nested_helpers=nested_helpers,
                backend_call_count=backend_call_count,
                decomposition_count=decomposition_count,
            )
        nested_helpers.extend(nested_outcome.nested_helpers)
        nested_helpers.append(nested_outcome.resolved_helper)
        resolved_dependencies.append(nested_outcome.resolved_helper)
    retry = _verify_helper_file(
        tool_paths=tool_paths,
        sandbox=sandbox,
        helper_file=helper_file,
        helper_root=helper_root / "retry",
        run_id=f"{helper_root.parent.parent.name}-helper-{helper_root.name}-retry",
        step=step,
        timeout_seconds=timeout_seconds,
        dependency_refs=[_verified_helper_ref(helper, theorem=helper_name) for helper in resolved_dependencies],
    )
    if retry is not None:
        helper_file.unlink(missing_ok=True)
        helper_file.with_suffix(".olean").unlink(missing_ok=True)
        return _RecursiveHelperOutcome(
            resolved_helper=None,
            nested_helpers=nested_helpers,
            backend_call_count=backend_call_count,
            decomposition_count=decomposition_count,
        )
    return _RecursiveHelperOutcome(
        resolved_helper=VerifiedHelper(
            helper_name=helper_name,
            module_name=module_name,
            file_path=str(helper_file),
            theorem=source_theorem,
            statement=step.helper_statement or "",
            source_step_id=step.step_id,
            decomposition_depth=current_depth,
        ),
        nested_helpers=nested_helpers,
        backend_call_count=backend_call_count,
        decomposition_count=decomposition_count,
    )


def _verify_helper_file(
    *,
    tool_paths: ToolPaths,
    sandbox: SandboxWorkspace,
    helper_file: Path,
    helper_root: Path,
    run_id: str,
    step: DecompositionStep,
    timeout_seconds: float,
    dependency_refs: list[HelperRef],
) -> CheckResult | None:
    for helper_ref in dependency_refs:
        sandbox.materialize_helper(helper_ref)
    helper_text = _render_helper_text(step=step, dependency_refs=dependency_refs)
    helper_file.write_text(helper_text, encoding="utf-8")
    verification = _perform_check(
        tool_paths=tool_paths,
        workspace=sandbox.sandbox_workspace,
        target_file=helper_file,
        timeout_seconds=timeout_seconds,
        command="check",
        run_id=run_id,
        artifact_dir=helper_root / "check",
        started_at=datetime.now(UTC),
        use_lake_env=bool(dependency_refs),
    )
    _write_json(helper_root / "step.json", dataclass_to_jsonable(step))
    _write_json(helper_root / "verification.json", verification.to_json())
    if verification.status is not CheckStatus.OK:
        return verification
    build_result = run_lean_make_olean(sandbox.sandbox_workspace.root, helper_file, timeout_seconds)
    _write_json(
        helper_root / "build.json",
        {
            "argv": build_result.args,
            "returncode": build_result.returncode,
            "stdout": build_result.stdout,
            "stderr": build_result.stderr,
        },
    )
    return None


def _render_helper_text(*, step: DecompositionStep, dependency_refs: list[HelperRef]) -> str:
    helper_imports = list(dict.fromkeys([*step.helper_imports, *(helper.module_name for helper in dependency_refs)]))
    return render_helper_file(replace(step, helper_imports=helper_imports))


def _build_recursive_helper_request(
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
        allowed_output_files=[
            str(module_path),
            *[
                str((MANAGED_HELPER_ROOT / _module_hint(helper_file, helper_name, index)).with_suffix(".lean")).replace(
                    "\\",
                    "/",
                )
                for index in range(1, 3)
            ],
        ],
        schema_path=str(schema_path),
    )


def _verified_helper_ref(helper: VerifiedHelper, *, theorem: str) -> HelperRef:
    return HelperRef(
        helper_name=helper.helper_name,
        module_name=helper.module_name,
        file_path=helper.file_path,
        theorem=theorem,
        statement=helper.statement,
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


def _perform_check(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    target_file: Path,
    timeout_seconds: float,
    command: str,
    run_id: str,
    artifact_dir: Path,
    started_at: datetime,
    use_lake_env: bool = False,
) -> CheckResult:
    lean_result = run_lean_check(
        workspace_root=workspace.root,
        target_file=target_file,
        timeout_seconds=timeout_seconds,
        workspace_root_for_diagnostics=workspace.root,
        use_lake_env=use_lake_env,
    )
    finished_at = datetime.now(UTC)
    status, exit_code = _check_status_and_code(lean_result)
    return CheckResult(
        run_id=run_id,
        command=command,
        status=status,
        exit_code=exit_code,
        target_file=_project_relative(target_file, workspace.root),
        tool_root=str(tool_paths.root),
        workspace=_workspace_metadata(workspace),
        artifact_dir=str(artifact_dir),
        wall_time_seconds=lean_result.wall_time_seconds,
        diagnostic_count=len(lean_result.diagnostics),
        diagnostics=lean_result.diagnostics,
        lean=lean_result,
        started_at=started_at,
        finished_at=finished_at,
    )


def _check_status_and_code(lean_result: LeanRunResult) -> tuple[CheckStatus, ExitCode]:
    if lean_result.timed_out:
        return CheckStatus.TIMEOUT, ExitCode.TIMEOUT
    if lean_result.returncode == 0:
        return CheckStatus.OK, ExitCode.SUCCESS
    if lean_result.diagnostics:
        return CheckStatus.FAILED, ExitCode.CHECK_FAILED
    return CheckStatus.ERROR, ExitCode.INTERNAL_ERROR


def _loop_status(*, attempts: list[PatchAttempt], verified_attempt: int | None) -> tuple[LoopStatus, ExitCode]:
    if verified_attempt is not None:
        return LoopStatus.VERIFIED, ExitCode.SUCCESS
    if not attempts:
        return LoopStatus.NO_CANDIDATE, ExitCode.CHECK_FAILED
    if attempts[-1].verification.exit_code is ExitCode.TIMEOUT:
        return LoopStatus.FAILED, ExitCode.TIMEOUT
    if attempts[-1].verification.exit_code is ExitCode.INTERNAL_ERROR:
        return LoopStatus.ERROR, ExitCode.INTERNAL_ERROR
    return LoopStatus.FAILED, ExitCode.CHECK_FAILED


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


def _select_repair_strategy(*, diagnostics: list[Diagnostic]) -> tuple[RepairStrategy, RepairFailure | None]:
    if not diagnostics:
        return RepairStrategy.REGENERATE, None
    primary_index, primary = min(
        enumerate(diagnostics),
        key=lambda item: (_diagnostic_priority(item[1].class_name), item[0]),
    )
    return _strategy_for_class(primary.class_name), _repair_failure_from_diagnostic(
        diagnostic=primary,
        diagnostic_index=primary_index,
    )


def _diagnostic_priority(class_name: DiagnosticClass) -> int:
    if class_name in {DiagnosticClass.SYNTAX_ERROR, DiagnosticClass.PARSER}:
        return 0
    if class_name in {DiagnosticClass.UNKNOWN_IDENTIFIER, DiagnosticClass.UNKNOWN_NAME}:
        return 1
    if class_name is DiagnosticClass.TYPE_MISMATCH:
        return 2
    if class_name is DiagnosticClass.UNSOLVED_GOALS:
        return 3
    if class_name in {DiagnosticClass.TACTIC_FAILURE, DiagnosticClass.TACTIC}:
        return 4
    if class_name in {DiagnosticClass.TIMEOUT, DiagnosticClass.RESOURCE_EXHAUSTION}:
        return 5
    if class_name is DiagnosticClass.IMPORT:
        return 6
    return 7


def _strategy_for_class(class_name: DiagnosticClass) -> RepairStrategy:
    if class_name in {DiagnosticClass.SYNTAX_ERROR, DiagnosticClass.PARSER}:
        return RepairStrategy.SYNTAX_FIX
    if class_name in {
        DiagnosticClass.UNKNOWN_IDENTIFIER,
        DiagnosticClass.UNKNOWN_NAME,
        DiagnosticClass.TYPE_MISMATCH,
        DiagnosticClass.UNSOLVED_GOALS,
        DiagnosticClass.TACTIC_FAILURE,
        DiagnosticClass.TACTIC,
    }:
        return RepairStrategy.TARGETED_CORRECTION
    return RepairStrategy.REGENERATE


def _repair_failure_from_diagnostic(*, diagnostic: Diagnostic, diagnostic_index: int) -> RepairFailure:
    return RepairFailure(
        diagnostic_index=diagnostic_index,
        diagnostic_class=diagnostic.class_name,
        severity=diagnostic.severity,
        file=diagnostic.file,
        range=diagnostic.range,
        message=diagnostic.message,
        kind=diagnostic.kind,
        identifier=diagnostic.identifier,
        subexpression=diagnostic.subexpression,
    )


def _build_attempt_state(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    sandbox: SandboxWorkspace,
    target_file: Path,
    theorem: str,
    timeout_seconds: float,
    command_name: str,
) -> AttemptState:
    return build_inspection_state(
        tool_paths=tool_paths,
        workspace=workspace,
        sandbox=sandbox,
        target_file=target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        command_name=command_name,
    )


def _build_agent_request(
    *,
    task_kind: TaskKind,
    attempt_index: int,
    attempt_state: AttemptState,
    diagnostics: list[Diagnostic],
    prior_attempts: list[PatchAttempt],
    retrieval_hits: list[RetrievalHit],
    active_helpers: list[HelperRef],
    schema_path: Path,
    helper_output_limit: int,
    prompt_template_name: str | None,
    repair_strategy: RepairStrategy | None = None,
    selected_failure: RepairFailure | None = None,
) -> AgentRequest:
    target_module_path = str(attempt_state.target_module_path)
    allowed_output_files = [target_module_path]
    if task_kind is TaskKind.DECOMPOSE:
        allowed_output_files.extend(
            [
                str(
                    MANAGED_HELPER_ROOT / _module_hint(attempt_state.target_file, attempt_state.theorem, index)
                ).replace("\\", "/")
                for index in range(1, helper_output_limit + 1)
            ]
        )
    request = build_agent_request(
        task_kind=task_kind,
        attempt_index=attempt_index,
        state=attempt_state,
        diagnostics=diagnostics,
        prior_attempts=prior_attempts,
        retrieval_hits=retrieval_hits,
        active_helpers=active_helpers,
        allowed_output_files=allowed_output_files,
        schema_path=schema_path,
        candidate_limit=1,
        repair_strategy=repair_strategy,
        selected_failure=selected_failure,
    )
    if prompt_template_name is not None:
        request = replace(request, prompt_template_name=prompt_template_name)
    return request


def _module_hint(target_file: Path, theorem: str, index: int) -> str:
    stem = "".join(char if char.isalnum() else "_" for char in target_file.stem).strip("_") or "Target"
    theorem_stem = "".join(char if char.isalnum() else "_" for char in theorem).strip("_") or "Theorem"
    return f"{stem}/{theorem_stem}/Helper{index}"


def _write_backend_artifacts(*, attempt_root: Path, request: AgentRequest, response: AgentResponse) -> None:
    backend_dir = attempt_root / "backend"
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
    (backend_dir / "stdout.txt").write_text(response.raw_stdout, encoding="utf-8")
    (backend_dir / "stderr.txt").write_text(response.raw_stderr, encoding="utf-8")


def _write_retrieval_artifact(
    *, path: Path, retrieval_hits: list[RetrievalHit], active_helpers: list[HelperRef]
) -> None:
    _write_json(
        path,
        {
            "retrieval_hits": dataclass_to_jsonable(retrieval_hits),
            "active_helpers": dataclass_to_jsonable(active_helpers),
        },
    )


def _write_attempt_artifacts(*, sandbox_root: Path, candidate: object, candidate_body: str, diff_text: str) -> None:
    _write_json(sandbox_root / "candidate.json", candidate)
    (sandbox_root / "candidate.body.lean").write_text(candidate_body, encoding="utf-8")
    (sandbox_root / "patch.diff").write_text(diff_text, encoding="utf-8")


def _materialize_active_helpers(*, sandbox: SandboxWorkspace, active_helpers: list[HelperRef]) -> None:
    for helper in active_helpers:
        sandbox.materialize_helper(helper)


def _workspace_metadata(workspace: WorkspaceBinding) -> WorkspaceMetadata:
    return WorkspaceMetadata(
        root=str(workspace.root),
        package=workspace.package_name,
        toolchain=workspace.toolchain,
    )


def _record_accepted_patch(
    *,
    store: StorePaths,
    run_id: str,
    workspace_root: Path,
    target_module_path: Path,
    theorem: str,
    theorem_statement: str | None,
    namespace: str | None,
    imports: list[str],
    goal_snapshot: list[str],
    body: str,
    candidate_body_path: Path,
    diagnostics: list[Diagnostic],
    helper_modules: list[str],
    helper_module_root: str,
) -> None:
    append_jsonl(
        store.accepted_patches,
        {
            "run_id": run_id,
            "workspace_root": str(workspace_root),
            "target_module_path": str(target_module_path),
            "theorem": theorem,
            "theorem_statement": theorem_statement,
            "namespace": namespace,
            "imports": imports,
            "goal_snapshot": goal_snapshot,
            "goal_constants": _goal_constants(theorem_statement=theorem_statement, goal_snapshot=goal_snapshot),
            "type_heads": _type_heads(theorem_statement=theorem_statement, goal_snapshot=goal_snapshot),
            "body": body,
            "candidate_body_path": str(candidate_body_path),
            "failure_classes": [diagnostic.class_name.value for diagnostic in diagnostics],
            "helper_modules": helper_modules,
            "helper_module_root": helper_module_root,
            "proof_size_lines": len([line for line in body.splitlines() if line.strip()]),
        },
    )


def _record_failure_summary(
    *,
    store: StorePaths,
    run_id: str,
    workspace_root: Path,
    target_module_path: Path,
    theorem: str,
    theorem_statement: str | None,
    namespace: str | None,
    imports: list[str],
    goal_snapshot: list[str],
    diagnostics: list[Diagnostic],
    helper_modules: list[str],
    helper_module_root: str,
    success: bool,
) -> None:
    append_jsonl(
        store.failure_summaries,
        {
            "run_id": run_id,
            "workspace_root": str(workspace_root),
            "target_module_path": str(target_module_path),
            "theorem": theorem,
            "theorem_statement": theorem_statement,
            "namespace": namespace,
            "imports": imports,
            "goal_snapshot": goal_snapshot,
            "failure_classes": [diagnostic.class_name.value for diagnostic in diagnostics],
            "helper_modules": helper_modules,
            "helper_module_root": helper_module_root,
            "summary": "; ".join(diagnostic.message for diagnostic in diagnostics[:3]),
            "success": success,
        },
    )


def _apply_verified_transplant(
    *,
    workspace: WorkspaceBinding,
    sandbox: SandboxWorkspace,
    target_file: Path,
    theorem: str,
    candidate_body: str,
    helper_modules: list[str],
    timeout_seconds: float,
) -> None:
    apply_verified_transplant(
        sandbox=sandbox,
        target_file=target_file,
        theorem=theorem,
        candidate_body=candidate_body,
        helper_modules=helper_modules,
    )


def _goal_constants(*, theorem_statement: str | None, goal_snapshot: list[str]) -> list[str]:
    text = "\n".join([theorem_statement or "", *goal_snapshot])
    return sorted({token for token in IDENT_RE.findall(text) if token.lower() not in STOPWORDS and len(token) > 1})


def _type_heads(*, theorem_statement: str | None, goal_snapshot: list[str]) -> list[str]:
    text = "\n".join([theorem_statement or "", *goal_snapshot])
    heads: set[str] = set()
    for line in text.splitlines():
        candidate = line.split("⊢", maxsplit=1)[1].strip() if "⊢" in line else line
        if "/\\" in candidate or "\u2227" in candidate:
            heads.add("And")
        if "\\/" in candidate or "\u2228" in candidate:
            heads.add("Or")
        if "=" in candidate:
            heads.add("Eq")
        match = IDENT_RE.search(candidate)
        if match is not None:
            heads.add(match.group(0))
    return sorted(heads)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _existing_helper(
    *,
    store: StorePaths,
    workspace_root: Path,
    target_module_path: Path,
    theorem: str,
    helper_name: str | None,
) -> VerifiedHelper | None:
    if helper_name is None:
        return None
    for row in reversed(store.proved_helpers.read_text(encoding="utf-8").splitlines()):
        if not row.strip():
            continue
        loaded = json.loads(row)
        if not isinstance(loaded, dict):
            continue
        if (
            loaded.get("workspace_root") == str(workspace_root)
            and loaded.get("target_module_path") == str(target_module_path)
            and loaded.get("theorem") == theorem
            and loaded.get("helper_name") == helper_name
        ):
            module_name = loaded.get("module_name")
            file_path = loaded.get("file_path")
            statement = loaded.get("statement")
            source_step_id = loaded.get("source_step_id")
            if all(isinstance(value, str) for value in [module_name, file_path, statement, source_step_id]):
                return VerifiedHelper(
                    helper_name=helper_name,
                    module_name=module_name,
                    file_path=file_path,
                    theorem=theorem,
                    statement=statement,
                    source_step_id=source_step_id,
                    decomposition_depth=int(loaded.get("decomposition_depth") or 1),
                )
    return None
