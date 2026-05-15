"""Artifact-backed trace, export, and replay helpers."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from kan_prover.adapters import create_backend
from kan_prover.engine.config import ToolPaths, WorkspaceBinding, resolve_workspace_binding
from kan_prover.engine.lean_runner import run_lean_check
from kan_prover.engine.ledger import (
    append_index_row,
    write_check_artifacts,
    write_export_artifacts,
    write_replay_artifacts,
    write_trace_artifacts,
)
from kan_prover.engine.loop import LoopOptions, run_decompose, run_loop
from kan_prover.engine.models import (
    AgentResponse,
    BackendInvocation,
    BackendResultKind,
    CheckResult,
    CheckStatus,
    DecomposeResult,
    DecompositionLayer,
    DecompositionStep,
    Diagnostic,
    ExitCode,
    ExportResult,
    HelperRef,
    HelperStatus,
    InspectionStatus,
    JsonValue,
    LoopResult,
    PatchCandidate,
    ReplayMode,
    ReplayResult,
    RunManifest,
    RunRequest,
    SandboxStrategy,
    SessionActionResult,
    TaskKind,
    TheoremBodyClassification,
    TheoremBodyResponseClass,
    TraceAttemptSummary,
    TraceResult,
    WorkspaceMetadata,
    dataclass_to_jsonable,
)
from kan_prover.engine.patching import create_sandbox_patch
from kan_prover.engine.run_id import new_run_id
from kan_prover.engine.sandbox import create_sandbox_workspace
from kan_prover.engine.session import SessionSemanticRuntime, finish_session, start_session, step_session
from kan_prover.engine.theorem_locator import locate_theorem_span_in_file


@dataclass(frozen=True)
class RunBundle:
    """The stored top-level files for one artifact bundle."""

    run_id: str
    run_dir: Path
    manifest: dict[str, JsonValue]
    request: dict[str, JsonValue]
    result: dict[str, JsonValue]


class StoredResponseBackend:
    """Replay backend that serves stored strict responses in order."""

    def __init__(
        self, *, backend_name: str, attempt_responses: list[AgentResponse], decompose_responses: list[AgentResponse]
    ) -> None:
        """Store the recorded strict responses for reuse-agent-output replay."""
        self._backend_name = backend_name
        self._attempt_responses = attempt_responses
        self._decompose_responses = decompose_responses

    def name(self) -> str:
        """Label this backend honestly as stored output replay."""
        return f"stored:{self._backend_name}"

    def propose(self, request: object) -> AgentResponse:
        """Return the recorded response for the current task kind and attempt index."""
        assert hasattr(request, "task_kind")
        assert hasattr(request, "attempt_index")
        task_kind = cast(TaskKind, request.task_kind)
        attempt_index = cast(int, request.attempt_index)
        if task_kind is TaskKind.DECOMPOSE:
            return self._response_at(self._decompose_responses, attempt_index, "decompose")
        return self._response_at(self._attempt_responses, attempt_index, "attempt")

    def _response_at(self, responses: list[AgentResponse], attempt_index: int, label: str) -> AgentResponse:
        if attempt_index < 1 or attempt_index > len(responses):
            return AgentResponse(
                backend=self.name(),
                result_kind=BackendResultKind.NO_PROGRESS,
                invocation=BackendInvocation(argv=[self.name(), f"{label}-{attempt_index:03d}"], cwd="."),
                prompt_path="",
                prompt_text="",
                schema_path="",
                raw_stdout='{"result_kind":"no_progress"}\n',
                raw_stderr="",
                raw_output='{"result_kind":"no_progress"}\n',
                parsed_payload={"result_kind": "no_progress"},
                message=f"no stored {label} response for attempt {attempt_index}",
            )
        return responses[attempt_index - 1]


def trace_run(*, tool_paths: ToolPaths, source_run_id: str) -> tuple[TraceResult, int]:
    """Summarize one stored run bundle from artifacts only."""
    started_at = datetime.now(UTC)
    bundle = load_run_bundle(tool_paths=tool_paths, run_id=source_run_id)
    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    attempt_summaries = _trace_attempts(bundle.result)
    artifact_paths = sorted(str(path) for path in bundle.run_dir.rglob("*") if path.is_file())
    finished_at = datetime.now(UTC)
    result = TraceResult(
        run_id=run_id,
        command="trace",
        status=InspectionStatus.OK,
        exit_code=ExitCode.SUCCESS,
        source_run_id=source_run_id,
        source_command=_string(bundle.manifest.get("command")) or _string(bundle.request.get("command")) or "unknown",
        source_status=_string(bundle.manifest.get("status")) or _string(bundle.result.get("status")) or "unknown",
        target_file=_string(bundle.result.get("target_file")) or _string(bundle.request.get("target_file")) or "",
        theorem=_string(bundle.result.get("theorem")) or _string(bundle.request.get("theorem")),
        backend=_string(bundle.result.get("backend")) or _string(bundle.request.get("backend")),
        artifact_dir=str(run_dir),
        attempt_count=len(attempt_summaries),
        attempts=attempt_summaries,
        artifact_paths=artifact_paths,
        started_at=started_at,
        finished_at=finished_at,
    )
    manifest = RunManifest(
        run_id=run_id,
        command="trace",
        status=InspectionStatus.OK,
        exit_code=ExitCode.SUCCESS,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    request = {"source_run_id": source_run_id, "source_artifact_dir": str(bundle.run_dir)}
    write_trace_artifacts(run_dir=run_dir, manifest=manifest, request=request, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, 0


def export_run(
    *,
    tool_paths: ToolPaths,
    workspace_override: WorkspaceBinding | None,
    source_run_id: str,
    attempt_index: int | None = None,
    output_path: Path | None = None,
) -> tuple[ExportResult, int]:
    """Export a self-contained theorem context bundle from stored artifacts."""
    started_at = datetime.now(UTC)
    bundle = load_run_bundle(tool_paths=tool_paths, run_id=source_run_id)
    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    selected_attempt, request_payload = _export_request(
        tool_paths=tool_paths,
        workspace_override=workspace_override,
        bundle=bundle,
        attempt_index=attempt_index,
    )
    target_file = _string(bundle.result.get("target_file")) or _string(bundle.request.get("target_file")) or ""
    target_module_path = _stored_target_module_path(bundle)
    workspace_root = _string(bundle.request.get("workspace_root")) or _string(
        cast(dict[str, JsonValue], bundle.result.get("workspace") or {}).get("root")
    )
    workspace_package = _string(bundle.request.get("workspace_package")) or _string(
        cast(dict[str, JsonValue], bundle.result.get("workspace") or {}).get("package")
    )
    helper_module_root = _stored_helper_module_root(bundle)
    syntax_probe_results = cast(
        dict[str, JsonValue],
        request_payload.get("syntax_probe_results") or bundle.request.get("syntax_probe_results") or {},
    )
    proof_target = cast(
        dict[str, JsonValue] | None,
        request_payload.get("proof_target") or bundle.request.get("proof_target"),
    )
    proof_state = cast(
        dict[str, JsonValue] | None,
        request_payload.get("proof_state") or bundle.request.get("proof_state"),
    )
    semantic_session = cast(
        dict[str, JsonValue] | None,
        request_payload.get("semantic_session"),
    )
    semantic_probe_results = cast(
        dict[str, JsonValue],
        request_payload.get("semantic_probe_results") or bundle.request.get("semantic_probe_results") or {},
    )
    probe_results = cast(
        dict[str, JsonValue],
        request_payload.get("probe_results") or bundle.request.get("probe_results") or {},
    )
    theorem = _string(bundle.result.get("theorem")) or _string(bundle.request.get("theorem"))
    export_bundle = {
        "schema_version": "v2",
        "source_run_id": source_run_id,
        "source_command": _string(bundle.manifest.get("command"))
        or _string(bundle.request.get("command"))
        or "unknown",
        "source_attempt_index": selected_attempt,
        "workspace_root": workspace_root,
        "workspace_package": workspace_package,
        "workspace_toolchain": _string(bundle.request.get("workspace_toolchain")),
        "target_file": target_file,
        "target_module_path": target_module_path,
        "theorem": theorem,
        "helper_module_root": helper_module_root,
        "helper_root_dir": _string(bundle.request.get("helper_root_dir")),
        "sandbox_strategy": _string(bundle.request.get("sandbox_strategy"))
        or _string(cast(dict[str, JsonValue], bundle.result.get("sandbox") or {}).get("strategy")),
        "editable_span": request_payload.get("editable_span"),
        "source_window": request_payload.get("source_window"),
        "proof_target": proof_target,
        "proof_state": proof_state,
        "semantic_session": semantic_session,
        "diagnostics": request_payload.get("diagnostics", bundle.result.get("diagnostics", [])),
        "prior_attempts": request_payload.get("prior_attempts", []),
        "retrieval_hits": request_payload.get("retrieval_hits", []),
        "helpers": request_payload.get("active_helpers", bundle.result.get("proved_helpers", [])),
        "helper_states": request_payload.get("helpers", bundle.result.get("helpers", [])),
        "syntax_probe_results": syntax_probe_results,
        "semantic_probe_results": semantic_probe_results,
        "probe_results": probe_results,
        "current_draft": request_payload.get("current_draft"),
        "graph_node_id": request_payload.get("graph_node_id"),
        "graph": bundle.result.get("graph"),
        "allowed_output_files": request_payload.get("allowed_output_files", []),
        "schema_path": request_payload.get("schema_path"),
        "task_kind": request_payload.get("task_kind"),
        "baseline": _load_json_if_exists(bundle.run_dir / "context" / "baseline-result.json"),
        "artifact_dir": str(bundle.run_dir),
    }
    final_output_path = output_path or (run_dir / "context" / "bundle.json")
    final_output_path.parent.mkdir(parents=True, exist_ok=True)
    final_output_path.write_text(json.dumps(export_bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    finished_at = datetime.now(UTC)
    result = ExportResult(
        run_id=run_id,
        command="export",
        status=InspectionStatus.OK,
        exit_code=ExitCode.SUCCESS,
        source_run_id=source_run_id,
        source_command=_string(bundle.manifest.get("command")) or _string(bundle.request.get("command")) or "unknown",
        source_attempt_index=selected_attempt,
        target_file=target_file,
        target_module_path=target_module_path,
        workspace_root=workspace_root,
        workspace_package=workspace_package,
        theorem=theorem,
        artifact_dir=str(run_dir),
        bundle_path=str(final_output_path),
        format="json",
        started_at=started_at,
        finished_at=finished_at,
        bundle=cast(dict[str, JsonValue], export_bundle),
    )
    manifest = RunManifest(
        run_id=run_id,
        command="export",
        status=InspectionStatus.OK,
        exit_code=ExitCode.SUCCESS,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    request = {
        "source_run_id": source_run_id,
        "source_attempt_index": selected_attempt,
        "output_path": str(final_output_path),
        "workspace_root": workspace_root,
        "target_module_path": target_module_path,
    }
    write_export_artifacts(run_dir=run_dir, manifest=manifest, request=request, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, 0


def replay_run(
    *,
    tool_paths: ToolPaths,
    workspace_override: WorkspaceBinding | None,
    source_run_id: str,
    mode: ReplayMode,
    attempt_index: int | None = None,
) -> tuple[ReplayResult, int]:
    """Replay one stored run bundle in an explicit mode."""
    started_at = datetime.now(UTC)
    bundle = load_run_bundle(tool_paths=tool_paths, run_id=source_run_id)
    source_command = _string(bundle.manifest.get("command")) or _string(bundle.request.get("command")) or "unknown"
    if source_command not in {"repair", "solve", "decompose", "prove"}:
        msg = f"`kan-prover replay` does not support source command `{source_command}`."
        raise ValueError(msg)

    if source_command == "prove" and mode is not ReplayMode.RECHECK_ONLY:
        msg = "`kan-prover replay` only supports `--recheck-only` for `prove` runs."
        raise ValueError(msg)

    if mode is ReplayMode.RECHECK_ONLY:
        child_result = _recheck_only(
            tool_paths=tool_paths,
            workspace_override=workspace_override,
            bundle=bundle,
            attempt_index=attempt_index,
        )
        child_exit = _exit_code_number(child_result.exit_code)
        replayed_run_id = child_result.run_id
        replayed_command = child_result.command
        replayed_status = child_result.status.value
        replayed_artifact_dir = child_result.artifact_dir
        child_payload = child_result.to_json()
        target_file = child_result.target_file
        target_module_path = _stored_target_module_path(bundle)
        workspace_root = _string(getattr(getattr(child_result, "workspace", None), "root", None))
        workspace_package = _string(getattr(getattr(child_result, "workspace", None), "package", None))
        theorem = _string(bundle.result.get("theorem")) or _string(bundle.request.get("theorem"))
        backend = _string(bundle.result.get("backend")) or _string(bundle.request.get("backend"))
        machine_label = "stored patch recheck; deterministic unless Lean or the file changed"
        exit_code = child_result.exit_code
    elif mode is ReplayMode.REUSE_AGENT_OUTPUT:
        child_result, child_exit = _reuse_agent_output(
            tool_paths=tool_paths,
            workspace_override=workspace_override,
            bundle=bundle,
        )
        replayed_run_id = child_result.run_id
        replayed_command = child_result.command
        replayed_status = child_result.status.value
        replayed_artifact_dir = child_result.artifact_dir
        child_payload = child_result.to_json()
        target_file = child_result.target_file
        target_module_path = _string(getattr(child_result, "target_module_path", None))
        workspace_root = _string(getattr(getattr(child_result, "workspace", None), "root", None))
        workspace_package = _string(getattr(getattr(child_result, "workspace", None), "package", None))
        theorem = _string(getattr(child_result, "theorem", None))
        backend = _string(getattr(child_result, "backend", None))
        machine_label = "stored backend output replay; may drift if the environment changed"
        exit_code = child_result.exit_code
    else:
        child_result, child_exit = _requery_agent(
            tool_paths=tool_paths,
            workspace_override=workspace_override,
            bundle=bundle,
        )
        replayed_run_id = child_result.run_id
        replayed_command = child_result.command
        replayed_status = child_result.status.value
        replayed_artifact_dir = child_result.artifact_dir
        child_payload = child_result.to_json()
        target_file = child_result.target_file
        target_module_path = _string(getattr(child_result, "target_module_path", None))
        workspace_root = _string(getattr(getattr(child_result, "workspace", None), "root", None))
        workspace_package = _string(getattr(getattr(child_result, "workspace", None), "package", None))
        theorem = _string(getattr(child_result, "theorem", None))
        backend = _string(getattr(child_result, "backend", None))
        machine_label = "fresh backend query replay; intentionally nondeterministic"
        exit_code = child_result.exit_code

    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    finished_at = datetime.now(UTC)
    status = InspectionStatus.OK if child_exit == 0 else InspectionStatus.ERROR
    result = ReplayResult(
        run_id=run_id,
        command="replay",
        status=status,
        exit_code=exit_code,
        mode=mode,
        source_run_id=source_run_id,
        source_command=source_command,
        source_attempt_index=attempt_index,
        target_file=target_file,
        target_module_path=target_module_path,
        workspace_root=workspace_root,
        workspace_package=workspace_package,
        sandbox_strategy=_stored_sandbox_strategy_text(bundle),
        theorem=theorem,
        backend=backend,
        artifact_dir=str(run_dir),
        replayed_run_id=replayed_run_id,
        replayed_command=replayed_command,
        replayed_status=replayed_status,
        replayed_artifact_dir=replayed_artifact_dir,
        machine_label=machine_label,
        started_at=started_at,
        finished_at=finished_at,
        child_result=child_payload,
    )
    manifest = RunManifest(
        run_id=run_id,
        command="replay",
        status=status,
        exit_code=exit_code,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    request = {
        "source_run_id": source_run_id,
        "source_command": source_command,
        "mode": mode.value,
        "source_attempt_index": attempt_index,
        "workspace_root": workspace_root,
        "target_module_path": target_module_path,
        "sandbox_strategy": _stored_sandbox_strategy_text(bundle),
    }
    write_replay_artifacts(run_dir=run_dir, manifest=manifest, request=request, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, child_exit


def load_run_bundle(*, tool_paths: ToolPaths, run_id: str) -> RunBundle:
    """Load the stored top-level JSON files for one run."""
    run_dir = tool_paths.artifact_runs / run_id
    if not run_dir.is_dir():
        msg = f"Run bundle does not exist: {run_dir}"
        raise FileNotFoundError(msg)
    manifest = _load_json(run_dir / "manifest.json")
    request = _load_json(run_dir / "request.json")
    result = _load_json(run_dir / "result.json")
    return RunBundle(run_id=run_id, run_dir=run_dir, manifest=manifest, request=request, result=result)


def _recheck_only(
    *,
    tool_paths: ToolPaths,
    workspace_override: WorkspaceBinding | None,
    bundle: RunBundle,
    attempt_index: int | None,
) -> CheckResult | SessionActionResult:
    source_command = _required_string(bundle.request.get("command"))
    if source_command == "prove":
        return _recheck_prove_branch(
            tool_paths=tool_paths,
            workspace_override=workspace_override,
            bundle=bundle,
            attempt_index=attempt_index,
        )
    source_attempt = _select_attempt(bundle=bundle, attempt_index=attempt_index)
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
    theorem = _string(bundle.result.get("theorem")) or _string(bundle.request.get("theorem"))
    if theorem is None:
        msg = f"Run `{bundle.run_id}` is missing theorem metadata for replay."
        raise ValueError(msg)
    replay_started = datetime.now(UTC)
    replay_root = tool_paths.artifact_runs / new_run_id(replay_started)
    sandbox_strategy = _stored_sandbox_strategy(bundle)
    sandbox_workspace = create_sandbox_workspace(workspace=workspace, run_dir=replay_root, strategy=sandbox_strategy)
    sandbox_target_file = sandbox_workspace.target_file(target_file)
    target_text = sandbox_target_file.read_text(encoding="utf-8")
    span = locate_theorem_span_in_file(
        target_file=sandbox_target_file,
        source=target_text,
        theorem_name=theorem,
    )
    work_dir = replay_root / "work" / f"recheck-{source_attempt['attempt_index']:03d}"
    request_payload = _load_json(Path(_required_string(source_attempt.get("backend_request_path"))))
    helper_refs = [_helper_ref_from_json(item) for item in _json_list(request_payload.get("active_helpers"))]
    candidate_payload = cast(dict[str, JsonValue], source_attempt.get("candidate"))
    candidate = _candidate_from_json(candidate_payload)
    for helper in helper_refs:
        sandbox_workspace.materialize_helper(helper)
    patch = create_sandbox_patch(
        sandbox=sandbox_workspace,
        sandbox_root=work_dir,
        target_file=target_file,
        source_text=target_text,
        span=span,
        candidate_body=candidate.body,
        active_helpers=helper_refs,
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    (patch.sandbox_root / "candidate.body.lean").write_text(candidate.body, encoding="utf-8")
    (patch.sandbox_root / "patch.diff").write_text(patch.diff_text, encoding="utf-8")
    (patch.sandbox_root / "candidate.json").write_text(
        json.dumps(dataclass_to_jsonable(candidate), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lean_result = run_lean_check(
        workspace_root=sandbox_workspace.sandbox_workspace.root,
        target_file=patch.verification_file,
        timeout_seconds=_float(bundle.request.get("timeout_seconds")) or 60.0,
        workspace_root_for_diagnostics=sandbox_workspace.sandbox_workspace.root,
        use_lake_env=bool(helper_refs),
    )
    replay_finished = datetime.now(UTC)
    status, exit_code = _check_status_and_code(lean_result)
    child_result = CheckResult(
        run_id=replay_root.name + "-check",
        command="recheck-only",
        status=status,
        exit_code=exit_code,
        target_file=_project_relative(target_file, workspace.root),
        tool_root=str(tool_paths.root),
        workspace=_workspace_metadata(workspace),
        artifact_dir=str(patch.sandbox_root / "check"),
        wall_time_seconds=lean_result.wall_time_seconds,
        diagnostic_count=len(lean_result.diagnostics),
        diagnostics=lean_result.diagnostics,
        lean=lean_result,
        started_at=replay_started,
        finished_at=replay_finished,
    )
    child_request = RunRequest(
        command="recheck-only",
        target_file=_project_relative(target_file, workspace.root),
        cwd=str(workspace.root),
        tool_root=str(tool_paths.root),
        workspace_root=str(workspace.root),
        workspace_package=workspace.package_name,
        workspace_toolchain=workspace.toolchain,
        target_module_path=_stored_target_module_path(bundle) or _project_relative(target_file, workspace.root),
        timeout_seconds=_float(bundle.request.get("timeout_seconds")) or 60.0,
        json_output=False,
        jsonl_output=False,
        started_at=replay_started,
        sandbox_strategy=sandbox_workspace.strategy,
        sandbox_root=str(sandbox_workspace.sandbox_root),
        sandbox_workspace_root=str(sandbox_workspace.sandbox_workspace.root),
        helper_module_root=_stored_helper_module_root(bundle),
        helper_root_dir=str(sandbox_workspace.helper_root_dir),
        proof_target=cast(
            dict[str, JsonValue] | None,
            bundle.request.get("proof_target") or source_attempt.get("proof_target"),
        ),
        syntax_probe_results=cast(
            dict[str, JsonValue],
            bundle.request.get("syntax_probe_results") or source_attempt.get("syntax_probe_results") or {},
        ),
        semantic_probe_results=cast(
            dict[str, JsonValue],
            bundle.request.get("semantic_probe_results") or source_attempt.get("semantic_probe_results") or {},
        ),
        probe_results=_merge_probe_results(bundle=bundle, source_attempt=source_attempt),
        theorem=theorem,
        backend=_string(bundle.result.get("backend")) or _string(bundle.request.get("backend")),
    )
    child_manifest = RunManifest(
        run_id=child_result.run_id,
        command="recheck-only",
        status=status,
        exit_code=exit_code,
        created_at=replay_finished,
        artifact_dir=child_result.artifact_dir,
    )
    write_check_artifacts(
        run_dir=patch.sandbox_root / "check", manifest=child_manifest, request=child_request, result=child_result
    )
    return child_result


def _recheck_prove_branch(
    *,
    tool_paths: ToolPaths,
    workspace_override: WorkspaceBinding | None,
    bundle: RunBundle,
    attempt_index: int | None,
) -> SessionActionResult:
    selected_node = _select_prove_node(bundle=bundle, attempt_index=attempt_index)
    helper_refs = _prove_node_helpers(bundle=bundle, node=selected_node)
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
    timeout_seconds = _float(bundle.request.get("timeout_seconds")) or 60.0
    theorem_draft = _required_string(selected_node.get("theorem_draft"))
    session_runtime = SessionSemanticRuntime(tool_paths=tool_paths)
    try:
        session_start, _start_exit = start_session(
            tool_paths=tool_paths,
            workspace=workspace,
            target_file=target_file,
            theorem=theorem,
            timeout_seconds=timeout_seconds,
            source_run_id=bundle.run_id,
            active_helpers=helper_refs,
            semantic_runtime=session_runtime,
        )
        action_payload = cast(dict[str, JsonValue], selected_node.get("action_payload") or {})
        if _string(action_payload.get("kind")) != "activate_helper":
            _step_result, _step_exit = step_session(
                tool_paths=tool_paths,
                session_id=session_start.session_id,
                body=theorem_draft,
                timeout_seconds=timeout_seconds,
                semantic_runtime=session_runtime,
            )
        finish_result, _finish_exit = finish_session(
            tool_paths=tool_paths,
            session_id=session_start.session_id,
            timeout_seconds=timeout_seconds,
            semantic_runtime=session_runtime,
        )
        return finish_result
    finally:
        session_runtime.close()


def _reuse_agent_output(
    *,
    tool_paths: ToolPaths,
    workspace_override: WorkspaceBinding | None,
    bundle: RunBundle,
) -> tuple[LoopResult | DecomposeResult, int]:
    source_command = _required_string(bundle.request.get("command"))
    backend_name = _string(bundle.result.get("backend")) or _string(bundle.request.get("backend")) or "unknown"
    backend = StoredResponseBackend(
        backend_name=backend_name,
        attempt_responses=[
            _agent_response_from_json(attempt["backend_response"]) for attempt in _attempt_dicts(bundle.result)
        ],
        decompose_responses=[
            _agent_response_from_json(_load_json(path))
            for path in sorted(bundle.run_dir.glob("decompose-*/backend/response.json"))
        ],
    )
    timeout_seconds = _float(bundle.request.get("timeout_seconds")) or 60.0
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
    if source_command == "decompose":
        return run_decompose(
            tool_paths=tool_paths,
            workspace=workspace,
            target_file=target_file,
            theorem=theorem,
            timeout_seconds=timeout_seconds,
            max_decomposition_depth=_int(bundle.request.get("max_decomposition_depth")) or 2,
            backend=backend,
        )
    options = LoopOptions(
        command=source_command,
        target_file=target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        apply_patch=False,
        max_attempts=_int(bundle.request.get("max_attempts")) or max(1, len(_attempt_dicts(bundle.result))),
        max_decomposition_depth=_int(bundle.request.get("max_decomposition_depth")) or 2,
    )
    return run_loop(tool_paths=tool_paths, workspace=workspace, options=options, backend=backend)


def _requery_agent(
    *,
    tool_paths: ToolPaths,
    workspace_override: WorkspaceBinding | None,
    bundle: RunBundle,
) -> tuple[LoopResult | DecomposeResult, int]:
    source_command = _required_string(bundle.request.get("command"))
    backend_name = _string(bundle.result.get("backend")) or _string(bundle.request.get("backend"))
    if backend_name is None:
        msg = f"Run `{bundle.run_id}` is missing backend metadata for agent replay."
        raise ValueError(msg)
    backend = create_backend(agent=backend_name)
    timeout_seconds = _float(bundle.request.get("timeout_seconds")) or 60.0
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
    if source_command == "decompose":
        return run_decompose(
            tool_paths=tool_paths,
            workspace=workspace,
            target_file=target_file,
            theorem=theorem,
            timeout_seconds=timeout_seconds,
            max_decomposition_depth=_int(bundle.request.get("max_decomposition_depth")) or 2,
            backend=backend,
        )
    options = LoopOptions(
        command=source_command,
        target_file=target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        apply_patch=False,
        max_attempts=_int(bundle.request.get("max_attempts")) or max(1, len(_attempt_dicts(bundle.result))),
        max_decomposition_depth=_int(bundle.request.get("max_decomposition_depth")) or 2,
    )
    return run_loop(tool_paths=tool_paths, workspace=workspace, options=options, backend=backend)


def _export_request(
    *,
    tool_paths: ToolPaths,
    workspace_override: WorkspaceBinding | None,
    bundle: RunBundle,
    attempt_index: int | None,
) -> tuple[int | None, dict[str, JsonValue]]:
    if _required_string(bundle.request.get("command")) == "prove":
        selected_node = _select_prove_node(bundle=bundle, attempt_index=attempt_index)
        return _int(selected_node.get("graph_index")), {
            "diagnostics": selected_node.get("diagnostics", []),
            "source_window": bundle.request.get("source_window", {}),
            "proof_target": cast(dict[str, JsonValue], selected_node.get("probe_snapshot") or {}).get("proof_target")
            or bundle.request.get("proof_target"),
            "proof_state": cast(dict[str, JsonValue], selected_node.get("probe_snapshot") or {}).get("proof_state")
            or bundle.request.get("proof_state"),
            "semantic_session": cast(dict[str, JsonValue], selected_node.get("probe_snapshot") or {}).get(
                "semantic_session"
            ),
            "retrieval_hits": bundle.request.get("retrieval_hits", []),
            "active_helpers": [
                _helper_ref_to_json(helper) for helper in _prove_node_helpers(bundle=bundle, node=selected_node)
            ],
            "syntax_probe_results": bundle.request.get("syntax_probe_results", {}),
            "semantic_probe_results": bundle.request.get("semantic_probe_results", {}),
            "probe_results": selected_node.get("probe_snapshot", {}),
            "current_draft": selected_node.get("theorem_draft"),
            "graph_node_id": selected_node.get("node_id"),
            "helpers": bundle.result.get("helpers", []),
        }
    attempts = _attempt_dicts(bundle.result)
    if attempts:
        attempt = _select_attempt(bundle=bundle, attempt_index=attempt_index)
        path = Path(_required_string(attempt.get("backend_request_path")))
        return _int(attempt.get("attempt_index")), _load_json(path)
    decompose_request = bundle.run_dir / "decompose" / "backend" / "request.json"
    if decompose_request.is_file():
        return 1, _load_json(decompose_request)
    file_path = _resolve_stored_target_file(
        tool_paths=tool_paths,
        workspace_override=workspace_override,
        bundle=bundle,
    )
    source_text = file_path.read_text(encoding="utf-8") if file_path.is_file() else ""
    return None, {
        "diagnostics": bundle.result.get("diagnostics", []),
        "source_window": {
            "file": _string(bundle.result.get("target_file")) or _string(bundle.request.get("target_file")) or "",
            "start_line": 1,
            "end_line": len(source_text.splitlines()) if source_text else 1,
            "text": source_text,
        },
    }


def _trace_attempts(result: dict[str, JsonValue]) -> list[TraceAttemptSummary]:
    prove_nodes = _prove_trace_nodes(result)
    if prove_nodes:
        return prove_nodes
    attempts: list[TraceAttemptSummary] = []
    for attempt in _attempt_dicts(result):
        verification = cast(dict[str, JsonValue], attempt.get("verification", {}))
        candidate = cast(dict[str, JsonValue], attempt.get("candidate", {}))
        attempts.append(
            TraceAttemptSummary(
                attempt_index=_int(attempt.get("attempt_index")) or len(attempts) + 1,
                verified=bool(attempt.get("verified")),
                diagnostic_count=_int(verification.get("diagnostic_count")) or 0,
                candidate_id=_string(candidate.get("candidate_id")),
                backend_request_path=_string(attempt.get("backend_request_path")),
                backend_response_path=_string(attempt.get("backend_response_path")),
                candidate_body_path=_string(attempt.get("candidate_body_path")),
                diff_path=_string(attempt.get("diff_path")),
            )
        )
    return attempts


def _select_attempt(bundle: RunBundle, attempt_index: int | None) -> dict[str, JsonValue]:
    attempts = _attempt_dicts(bundle.result)
    if not attempts:
        msg = f"Run `{bundle.run_id}` has no stored patch attempts to replay."
        raise ValueError(msg)
    selected_index = attempt_index
    if selected_index is None:
        verified_attempt = _int(bundle.result.get("verified_attempt"))
        selected_index = (
            verified_attempt
            if verified_attempt is not None
            else _int(attempts[-1].get("attempt_index")) or len(attempts)
        )
    matches = [attempt for attempt in attempts if _int(attempt.get("attempt_index")) == selected_index]
    if not matches:
        msg = f"Run `{bundle.run_id}` has no attempt `{selected_index}`."
        raise ValueError(msg)
    return matches[-1]


def _attempt_dicts(result: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    return [cast(dict[str, JsonValue], item) for item in _json_list(result.get("attempts")) if isinstance(item, dict)]


def _prove_trace_nodes(result: dict[str, JsonValue]) -> list[TraceAttemptSummary]:
    nodes: list[TraceAttemptSummary] = []
    for index, node in enumerate(_prove_nodes(result), start=1):
        diagnostics = _json_list(node.get("diagnostics"))
        action = cast(dict[str, JsonValue], node.get("action_payload") or {})
        nodes.append(
            TraceAttemptSummary(
                attempt_index=index,
                verified=_string(node.get("status")) == "verified",
                diagnostic_count=len(diagnostics),
                candidate_id=_string(action.get("candidate_id")) or _string(action.get("kind")),
                backend_request_path=_string(node.get("backend_request_path")),
                backend_response_path=_string(node.get("backend_response_path")),
                candidate_body_path=None,
                diff_path=None,
            )
        )
    return nodes


def _prove_nodes(result: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    return [cast(dict[str, JsonValue], item) for item in _json_list(result.get("nodes")) if isinstance(item, dict)]


def _select_prove_node(bundle: RunBundle, attempt_index: int | None) -> dict[str, JsonValue]:
    nodes = _prove_nodes(bundle.result)
    if not nodes:
        msg = f"Run `{bundle.run_id}` has no stored proof-graph nodes."
        raise ValueError(msg)
    if attempt_index is not None:
        index = max(1, attempt_index) - 1
        if index >= len(nodes):
            msg = f"Run `{bundle.run_id}` has no graph node at position `{attempt_index}`."
            raise ValueError(msg)
        return nodes[index]
    summary = cast(dict[str, JsonValue], bundle.result.get("graph") or {})
    preferred_id = _string(summary.get("verified_node_id"))
    if preferred_id is None:
        frontier = _string_list(summary.get("frontier_node_ids"))
        preferred_id = frontier[0] if frontier else None
    if preferred_id is not None:
        for node in nodes:
            if _string(node.get("node_id")) == preferred_id:
                return node
    return nodes[-1]


def _agent_response_from_json(payload: object) -> AgentResponse:
    data = cast(dict[str, JsonValue], payload)
    return AgentResponse(
        backend=_required_string(data.get("backend")),
        result_kind=BackendResultKind(_required_string(data.get("result_kind"))),
        invocation=BackendInvocation(
            argv=list(_string_list(cast(dict[str, JsonValue], data.get("invocation", {})).get("argv"))),
            cwd=_string(cast(dict[str, JsonValue], data.get("invocation", {})).get("cwd")) or ".",
        ),
        prompt_path=_string(data.get("prompt_path")) or "",
        prompt_text=_string(data.get("prompt_text")) or "",
        schema_path=_string(data.get("schema_path")) or "",
        raw_stdout=_string(data.get("raw_stdout")) or "",
        raw_stderr=_string(data.get("raw_stderr")) or "",
        raw_output=_string(data.get("raw_output")) or "",
        parsed_payload=cast(dict[str, JsonValue] | None, data.get("parsed_payload")),
        candidates=[
            _candidate_from_json(item) for item in _json_list(data.get("candidates")) if isinstance(item, dict)
        ],
        candidate_parse_errors=_string_list(data.get("candidate_parse_errors")),
        decomposition=[
            _decomposition_step_from_json(item)
            for item in _json_list(data.get("decomposition"))
            if isinstance(item, dict)
        ],
        message=_string(data.get("message")),
        theorem_body_classification=_theorem_body_classification_from_json(data.get("theorem_body_classification")),
    )


def _candidate_from_json(payload: dict[str, JsonValue]) -> PatchCandidate:
    return PatchCandidate(
        candidate_id=_required_string(payload.get("candidate_id")),
        theorem=_required_string(payload.get("theorem")),
        body=_required_string(payload.get("body")),
        source=_string(payload.get("source")) or "stored",
        replay_from=_string(payload.get("replay_from")),
    )


def _theorem_body_classification_from_json(payload: object) -> TheoremBodyClassification | None:
    if not isinstance(payload, dict):
        return None
    data = cast(dict[str, JsonValue], payload)
    response_class = _string(data.get("response_class"))
    if response_class is None:
        return None
    return TheoremBodyClassification(
        response_class=TheoremBodyResponseClass(response_class),
        usable_candidate_count=_int(data.get("usable_candidate_count") or 0) or 0,
        rejected_candidate_count=_int(data.get("rejected_candidate_count") or 0) or 0,
        invalid_candidate_count=_int(data.get("invalid_candidate_count") or 0) or 0,
        selected_candidate_id=_string(data.get("selected_candidate_id")),
        message=_string(data.get("message")),
        errors=_string_list(data.get("errors")),
    )


def _decomposition_step_from_json(payload: dict[str, JsonValue]) -> DecompositionStep:
    return DecompositionStep(
        step_id=_required_string(payload.get("step_id")),
        title=_required_string(payload.get("title")),
        goal=_required_string(payload.get("goal")),
        layer=DecompositionLayer(_required_string(payload.get("layer"))),
        helper_name=_string(payload.get("helper_name")),
        helper_statement=_string(payload.get("helper_statement")),
        helper_proof=_string(payload.get("helper_proof")),
        helper_imports=_string_list(payload.get("helper_imports")),
        file_hint=_string(payload.get("file_hint")),
    )


def _helper_ref_from_json(payload: object) -> HelperRef:
    data = cast(dict[str, JsonValue], payload)
    return HelperRef(
        helper_name=_required_string(data.get("helper_name")),
        module_name=_required_string(data.get("module_name")),
        file_path=_required_string(data.get("file_path")),
        theorem=_required_string(data.get("theorem")),
        statement=_required_string(data.get("statement")),
    )


def _helper_ref_to_json(helper: HelperRef) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], dataclass_to_jsonable(helper))


def _prove_node_helpers(*, bundle: RunBundle, node: dict[str, JsonValue]) -> list[HelperRef]:
    active_ids = set(_string_list(node.get("active_helper_ids")))
    if not active_ids:
        return []
    helpers: list[HelperRef] = []
    for item in _json_list(bundle.result.get("helpers")):
        if not isinstance(item, dict):
            continue
        helper_id = _string(item.get("helper_id"))
        status = _string(item.get("status"))
        if helper_id not in active_ids or status != HelperStatus.VERIFIED.value:
            continue
        helpers.append(
            HelperRef(
                helper_name=_string(item.get("helper_name")) or helper_id or "helper",
                module_name=_required_string(item.get("module_name")),
                file_path=_required_string(item.get("file_path")),
                theorem=_required_string(item.get("source_theorem")),
                statement=_required_string(item.get("statement")),
            )
        )
    return helpers


def _check_status_and_code(lean_result: object) -> tuple[CheckStatus, ExitCode]:
    assert hasattr(lean_result, "timed_out")
    assert hasattr(lean_result, "returncode")
    assert hasattr(lean_result, "diagnostics")
    timed_out = cast(bool, lean_result.timed_out)
    returncode = cast(int | None, lean_result.returncode)
    diagnostics = cast(list[Diagnostic], lean_result.diagnostics)
    if timed_out:
        return CheckStatus.TIMEOUT, ExitCode.TIMEOUT
    if returncode == 0:
        return CheckStatus.OK, ExitCode.SUCCESS
    if diagnostics:
        return CheckStatus.FAILED, ExitCode.CHECK_FAILED
    return CheckStatus.ERROR, ExitCode.INTERNAL_ERROR


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


def _workspace_metadata(workspace: WorkspaceBinding) -> WorkspaceMetadata:
    return WorkspaceMetadata(
        root=str(workspace.root),
        package=workspace.package_name,
        toolchain=workspace.toolchain,
    )


def _resolve_replay_workspace(
    *,
    tool_paths: ToolPaths,
    workspace_override: WorkspaceBinding | None,
    bundle: RunBundle,
    target_file: Path,
) -> WorkspaceBinding:
    if workspace_override is not None:
        return workspace_override
    stored_root = _stored_workspace_root(bundle)
    return resolve_workspace_binding(
        tool_paths=tool_paths,
        workspace=str(stored_root) if stored_root is not None else None,
        target_file=target_file,
    )


def _resolve_stored_target_file(
    *,
    tool_paths: ToolPaths,
    workspace_override: WorkspaceBinding | None,
    bundle: RunBundle,
) -> Path:
    target_file = _string(bundle.request.get("target_file")) or _string(bundle.result.get("target_file"))
    root = (
        workspace_override.root
        if workspace_override is not None
        else _stored_workspace_root(bundle) or _stored_tool_root(bundle) or tool_paths.root
    )
    return _resolve_target_file(root, target_file)


def _stored_workspace_root(bundle: RunBundle) -> Path | None:
    root_text = _string(bundle.request.get("workspace_root")) or _string(
        cast(dict[str, JsonValue], bundle.result.get("workspace") or {}).get("root")
    )
    return Path(root_text).expanduser().resolve() if root_text else None


def _stored_target_module_path(bundle: RunBundle) -> str | None:
    return _string(bundle.request.get("target_module_path")) or _string(bundle.result.get("target_module_path"))


def _stored_helper_module_root(bundle: RunBundle) -> str | None:
    return (
        _string(bundle.request.get("helper_module_root"))
        or _string(bundle.result.get("helper_module_root"))
        or _string(cast(dict[str, JsonValue], bundle.result.get("sandbox") or {}).get("helper_module_root"))
    )


def _stored_sandbox_strategy_text(bundle: RunBundle) -> str | None:
    return _string(bundle.request.get("sandbox_strategy")) or _string(
        cast(dict[str, JsonValue], bundle.result.get("sandbox") or {}).get("strategy")
    )


def _stored_sandbox_strategy(bundle: RunBundle) -> SandboxStrategy | None:
    strategy = _stored_sandbox_strategy_text(bundle)
    return SandboxStrategy(strategy) if strategy is not None else None


def _merge_probe_results(*, bundle: RunBundle, source_attempt: dict[str, JsonValue]) -> dict[str, JsonValue]:
    merged = cast(dict[str, JsonValue], bundle.request.get("probe_results") or {}).copy()
    attempt_syntax = cast(dict[str, JsonValue], source_attempt.get("syntax_probe_results") or {})
    attempt_semantic = cast(dict[str, JsonValue], source_attempt.get("semantic_probe_results") or {})
    for key, value in attempt_syntax.items():
        merged[key] = value
    for key, value in attempt_semantic.items():
        merged[key] = value
    return merged


def _stored_tool_root(bundle: RunBundle) -> Path | None:
    root_text = _string(bundle.request.get("tool_root")) or _string(bundle.result.get("tool_root"))
    return Path(root_text).expanduser().resolve() if root_text else None


def _resolve_target_file(root: Path, target_file: str | None) -> Path:
    if target_file is None:
        msg = "Stored run is missing `target_file`."
        raise ValueError(msg)
    candidate = Path(target_file).expanduser()
    return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()


def _load_json(path: Path) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], json.loads(path.read_text(encoding="utf-8")))


def _load_json_if_exists(path: Path) -> dict[str, JsonValue] | None:
    return _load_json(path) if path.is_file() else None


def _json_list(value: JsonValue | None) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _string_list(value: JsonValue | None) -> list[str]:
    return [item for item in _json_list(value) if isinstance(item, str)]


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _required_string(value: object) -> str:
    if isinstance(value, str):
        return value
    msg = f"Expected string, got {type(value).__name__}."
    raise ValueError(msg)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None
