"""Sandbox-backed theorem sessions with durable state and node history."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from kan_prover.engine.config import ToolPaths, WorkspaceBinding, bind_workspace
from kan_prover.engine.inspection import build_inspection_state
from kan_prover.engine.lean_runner import run_lean_check
from kan_prover.engine.ledger import append_index_row, write_session_artifacts
from kan_prover.engine.models import (
    CheckResult,
    CheckStatus,
    Diagnostic,
    ExitCode,
    FileSpan,
    HelperRef,
    JsonValue,
    RunManifest,
    SandboxStrategy,
    SessionActionKind,
    SessionActionResult,
    SessionStatus,
    WorkspaceMetadata,
    dataclass_to_jsonable,
)
from kan_prover.engine.proofctl import ProofctlContextBundle, semantic_context_probes_from_bundle_probe
from kan_prover.engine.run_id import new_run_id
from kan_prover.engine.sandbox import SandboxWorkspace, create_sandbox_workspace
from kan_prover.engine.semantic_live import LiveSemanticSession
from kan_prover.engine.semantic_runner import OneShotSemanticRunner, SemanticContextRequest

SESSION_SCHEMA_VERSION = "session.v1"


class SessionSemanticRuntime:
    """Policy layer that keeps session semantics separate from transport details."""

    def __init__(
        self,
        *,
        tool_paths: ToolPaths,
        prefer_live_followups: bool = True,
    ) -> None:
        """Build one explicit semantic runner split for theorem-session work."""
        self._tool_paths = tool_paths
        self._prefer_live_followups = prefer_live_followups
        self._one_shot = OneShotSemanticRunner()
        self._live_sessions: dict[str, LiveSemanticSession] = {}

    def start_context(
        self,
        *,
        workspace: WorkspaceBinding,
        target_file: Path,
        theorem: str,
        source_text: str,
        timeout_seconds: float,
    ) -> tuple[ProofctlContextBundle, dict[str, JsonValue]]:
        """Keep session start on the cheap one-shot path."""
        return self._context_bundle(
            session_id=None,
            use_live=False,
            workspace=workspace,
            target_file=target_file,
            theorem=theorem,
            source_text=source_text,
            timeout_seconds=timeout_seconds,
        )

    def followup_context(
        self,
        *,
        session_id: str,
        workspace: WorkspaceBinding,
        target_file: Path,
        theorem: str,
        source_text: str,
        timeout_seconds: float,
    ) -> tuple[ProofctlContextBundle, dict[str, JsonValue]]:
        """Promote follow-up session work to the live path when policy allows it."""
        return self._context_bundle(
            session_id=session_id,
            use_live=self._prefer_live_followups,
            workspace=workspace,
            target_file=target_file,
            theorem=theorem,
            source_text=source_text,
            timeout_seconds=timeout_seconds,
        )

    def close_session(self, session_id: str) -> None:
        """Close the live session handle for one stored theorem session."""
        live = self._live_sessions.pop(session_id, None)
        if live is not None:
            live.close()

    def close(self) -> None:
        """Close every live handle owned by this same-process runtime."""
        for session_id in list(self._live_sessions):
            self.close_session(session_id)

    def _context_bundle(
        self,
        *,
        session_id: str | None,
        use_live: bool,
        workspace: WorkspaceBinding,
        target_file: Path,
        theorem: str,
        source_text: str,
        timeout_seconds: float,
    ) -> tuple[ProofctlContextBundle, dict[str, JsonValue]]:
        request = SemanticContextRequest(
            tool_paths=self._tool_paths,
            workspace=workspace,
            file_path=target_file,
            theorem=theorem,
            timeout_seconds=timeout_seconds,
            source_text=source_text,
        )
        if use_live:
            if session_id is None:
                msg = "Live semantic follow-up requests require a session id."
                raise ValueError(msg)
            live = self._live_sessions.get(session_id)
            if live is None:
                live = LiveSemanticSession(
                    tool_paths=self._tool_paths,
                    workspace=workspace,
                    timeout_seconds=timeout_seconds,
                )
                self._live_sessions[session_id] = live
            result = live.context_bundle(request)
        else:
            result = self._one_shot.context_bundle(request)
        probes = semantic_context_probes_from_bundle_probe(result.bundle_probe)
        return probes.context_bundle(require_semantic_probes=True), result.session_facts.to_json()


def start_session(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    target_file: Path,
    theorem: str,
    timeout_seconds: float,
    source_run_id: str | None = None,
    active_helpers: list[HelperRef] | None = None,
    semantic_runtime: SessionSemanticRuntime | None = None,
) -> tuple[SessionActionResult, int]:
    """Create one theorem session rooted in a real file and theorem."""
    started_at = datetime.now(UTC)
    session_id = new_run_id(started_at)
    session_dir = tool_paths.artifact_sessions / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    sandbox = create_sandbox_workspace(workspace=workspace, run_dir=session_dir)
    state = build_inspection_state(
        tool_paths=tool_paths,
        workspace=workspace,
        sandbox=sandbox,
        target_file=target_file,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        command_name="session start",
        active_helpers=active_helpers,
    )
    current_source = state.sandbox_target_file.read_text(encoding="utf-8")
    theorem_draft = current_source[state.span.body_start : state.span.body_end]
    runtime = semantic_runtime or SessionSemanticRuntime(tool_paths=tool_paths)
    check_result = _perform_check(
        tool_paths=tool_paths,
        workspace=sandbox.sandbox_workspace,
        target_file=state.sandbox_target_file,
        timeout_seconds=timeout_seconds,
        command="session-start",
        run_id=session_id,
        artifact_dir=session_dir,
        started_at=started_at,
        use_lake_env=bool(active_helpers),
    )
    proofctl_context, semantic_session = runtime.start_context(
        workspace=sandbox.sandbox_workspace,
        target_file=state.sandbox_target_file,
        theorem=theorem,
        source_text=current_source,
        timeout_seconds=timeout_seconds,
    )
    snapshot = _build_snapshot(
        theorem=theorem,
        theorem_statement=state.theorem_statement,
        body_start=state.span.body_start,
        body_end=state.span.body_end,
        source_text=current_source,
        diagnostics=check_result.diagnostics,
        active_helpers=dataclass_to_jsonable(active_helpers or []),
        terminal_verified=False,
        proofctl_context=proofctl_context,
        semantic_session=semantic_session,
    )
    node = _build_node(
        session_id=session_id,
        node_index=0,
        action=SessionActionKind.START,
        parent_node_id=None,
        theorem_draft=theorem_draft,
        source_text=current_source,
        snapshot=snapshot,
        check_result=check_result,
        session_dir=session_dir,
        details={},
    )
    session = cast(
        dict[str, JsonValue],
        {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": session_id,
            "source_run_id": source_run_id,
            "tool_root": str(tool_paths.root),
            "status": SessionStatus.ACTIVE.value,
            "target_file": _project_relative(target_file, workspace.root),
            "target_module_path": str(sandbox.target_relative_path(target_file)),
            "theorem": theorem,
            "theorem_statement": state.theorem_statement,
            "body_start": state.span.body_start,
            "body_end": state.span.body_end,
            "workspace": dataclass_to_jsonable(_workspace_metadata(workspace)),
            "sandbox": dataclass_to_jsonable(sandbox.metadata()),
            "helper_module_root": sandbox.helper_module_root,
            "active_helpers": dataclass_to_jsonable(active_helpers or []),
            "artifact_dir": str(session_dir),
            "semantic_session": semantic_session,
            "current_node_id": node["node_id"],
            "current_draft": theorem_draft,
            "current_state": snapshot,
            "nodes": [node],
            "checkpoints": [],
            "action_history": [
                _action_record(
                    action=SessionActionKind.START,
                    node_id=cast(str, node["node_id"]),
                    details={},
                )
            ],
            "created_at": dataclass_to_jsonable(started_at),
            "updated_at": dataclass_to_jsonable(datetime.now(UTC)),
        },
    )
    _write_session_state(session_dir=session_dir, session=session)
    return _finalize_session_action(
        tool_paths=tool_paths,
        action=SessionActionKind.START,
        status=SessionStatus.ACTIVE,
        session=session,
        request={
            "session_id": session_id,
            "source_run_id": source_run_id,
            "target_file": _project_relative(target_file, workspace.root),
            "theorem": theorem,
            "timeout_seconds": timeout_seconds,
        },
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )


def step_session(
    *,
    tool_paths: ToolPaths,
    session_id: str,
    body: str,
    timeout_seconds: float,
    semantic_runtime: SessionSemanticRuntime | None = None,
) -> tuple[SessionActionResult, int]:
    """Apply one bounded theorem-body step inside an existing session sandbox."""
    started_at = datetime.now(UTC)
    session_dir, session = _load_session(tool_paths=tool_paths, session_id=session_id)
    _require_active_session(session)
    sandbox = _bind_sandbox(session)
    sandbox_target = sandbox.target_file(_real_target_file(session, sandbox.real_workspace.root))
    source_text = sandbox_target.read_text(encoding="utf-8")
    body_start = _require_int(session, "body_start")
    body_end = _require_int(session, "body_end")
    patched_text = _replace_body(
        source_text=source_text,
        body_start=body_start,
        body_end=body_end,
        body=body,
    )
    sandbox_target.write_text(patched_text, encoding="utf-8")
    normalized_body = patched_text[body_start : body_start + _normalized_body_length(body)]
    new_body_end = body_start + len(normalized_body)
    session["body_end"] = new_body_end
    owns_runtime = semantic_runtime is None
    runtime = semantic_runtime or SessionSemanticRuntime(tool_paths=tool_paths)
    node = _capture_followup_node(
        tool_paths=tool_paths,
        semantic_runtime=runtime,
        session=session,
        session_dir=session_dir,
        sandbox=sandbox,
        action=SessionActionKind.STEP,
        source_text=patched_text,
        theorem_draft=normalized_body,
        timeout_seconds=timeout_seconds,
    )
    session["current_node_id"] = node["node_id"]
    session["current_draft"] = normalized_body
    session["current_state"] = node["snapshot"]
    session["semantic_session"] = cast(dict[str, JsonValue], node["snapshot"]).get("semantic_session")
    cast(list[dict[str, JsonValue]], session["nodes"]).append(node)
    cast(list[dict[str, JsonValue]], session["action_history"]).append(
        _action_record(action=SessionActionKind.STEP, node_id=cast(str, node["node_id"]), details={})
    )
    session["updated_at"] = dataclass_to_jsonable(datetime.now(UTC))
    _write_session_state(session_dir=session_dir, session=session)
    if owns_runtime:
        runtime.close_session(session_id)
    return _finalize_session_action(
        tool_paths=tool_paths,
        action=SessionActionKind.STEP,
        status=SessionStatus.ACTIVE,
        session=session,
        request={
            "session_id": session_id,
            "timeout_seconds": timeout_seconds,
        },
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )


def goals_session(
    *,
    tool_paths: ToolPaths,
    session_id: str,
    timeout_seconds: float,
    semantic_runtime: SessionSemanticRuntime | None = None,
) -> tuple[SessionActionResult, int]:
    """Refresh and return the current theorem-local proof state."""
    started_at = datetime.now(UTC)
    session_dir, session = _load_session(tool_paths=tool_paths, session_id=session_id)
    if session.get("status") == SessionStatus.ACTIVE.value:
        sandbox = _bind_sandbox(session)
        sandbox_target = sandbox.target_file(_real_target_file(session, sandbox.real_workspace.root))
        source_text = sandbox_target.read_text(encoding="utf-8")
        theorem_draft = _current_draft(session=session, source_text=source_text)
        owns_runtime = semantic_runtime is None
        runtime = semantic_runtime or SessionSemanticRuntime(tool_paths=tool_paths)
        node = _capture_followup_node(
            tool_paths=tool_paths,
            semantic_runtime=runtime,
            session=session,
            session_dir=session_dir,
            sandbox=sandbox,
            action=SessionActionKind.GOALS,
            source_text=source_text,
            theorem_draft=theorem_draft,
            timeout_seconds=timeout_seconds,
        )
        session["current_node_id"] = node["node_id"]
        session["current_draft"] = theorem_draft
        session["current_state"] = node["snapshot"]
        session["semantic_session"] = cast(dict[str, JsonValue], node["snapshot"]).get("semantic_session")
        cast(list[dict[str, JsonValue]], session["nodes"]).append(node)
        cast(list[dict[str, JsonValue]], session["action_history"]).append(
            _action_record(action=SessionActionKind.GOALS, node_id=cast(str, node["node_id"]), details={})
        )
        session["updated_at"] = dataclass_to_jsonable(datetime.now(UTC))
        _write_session_state(session_dir=session_dir, session=session)
        if owns_runtime:
            runtime.close_session(session_id)
    return _finalize_session_action(
        tool_paths=tool_paths,
        action=SessionActionKind.GOALS,
        status=SessionStatus(cast(str, session["status"])),
        session=session,
        request={"session_id": session_id, "timeout_seconds": timeout_seconds},
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )


def checkpoint_session(
    *,
    tool_paths: ToolPaths,
    session_id: str,
    label: str | None = None,
) -> tuple[SessionActionResult, int]:
    """Record one named checkpoint for the current session node."""
    started_at = datetime.now(UTC)
    session_dir, session = _load_session(tool_paths=tool_paths, session_id=session_id)
    _require_active_session(session)
    checkpoints = cast(list[dict[str, JsonValue]], session["checkpoints"])
    checkpoint_id = f"checkpoint-{len(checkpoints):03d}"
    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "label": label or checkpoint_id,
        "node_id": session.get("current_node_id"),
        "created_at": dataclass_to_jsonable(datetime.now(UTC)),
    }
    checkpoints.append(checkpoint)
    cast(list[dict[str, JsonValue]], session["action_history"]).append(
        _action_record(
            action=SessionActionKind.CHECKPOINT,
            node_id=cast(str | None, session.get("current_node_id")),
            details={"checkpoint_id": checkpoint_id, "label": checkpoint["label"]},
        )
    )
    _write_json(session_dir / "checkpoints.json", checkpoints)
    session["updated_at"] = dataclass_to_jsonable(datetime.now(UTC))
    _write_session_state(session_dir=session_dir, session=session)
    return _finalize_session_action(
        tool_paths=tool_paths,
        action=SessionActionKind.CHECKPOINT,
        status=SessionStatus.ACTIVE,
        session=session,
        request={"session_id": session_id, "label": checkpoint["label"]},
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )


def finish_session(
    *,
    tool_paths: ToolPaths,
    session_id: str,
    timeout_seconds: float,
    semantic_runtime: SessionSemanticRuntime | None = None,
) -> tuple[SessionActionResult, int]:
    """Finalize one theorem session after one last Lean recheck."""
    started_at = datetime.now(UTC)
    session_dir, session = _load_session(tool_paths=tool_paths, session_id=session_id)
    _require_active_session(session)
    sandbox = _bind_sandbox(session)
    sandbox_target = sandbox.target_file(_real_target_file(session, sandbox.real_workspace.root))
    source_text = sandbox_target.read_text(encoding="utf-8")
    theorem_draft = _current_draft(session=session, source_text=source_text)
    runtime = semantic_runtime or SessionSemanticRuntime(tool_paths=tool_paths)
    node = _capture_followup_node(
        tool_paths=tool_paths,
        semantic_runtime=runtime,
        session=session,
        session_dir=session_dir,
        sandbox=sandbox,
        action=SessionActionKind.FINISH,
        source_text=source_text,
        theorem_draft=theorem_draft,
        timeout_seconds=timeout_seconds,
    )
    final_status = _finish_status(cast(dict[str, JsonValue], node["check"]))
    session["status"] = final_status.value
    session["current_node_id"] = node["node_id"]
    session["current_draft"] = theorem_draft
    session["current_state"] = node["snapshot"]
    session["semantic_session"] = cast(dict[str, JsonValue], node["snapshot"]).get("semantic_session")
    cast(list[dict[str, JsonValue]], session["nodes"]).append(node)
    cast(list[dict[str, JsonValue]], session["action_history"]).append(
        _action_record(action=SessionActionKind.FINISH, node_id=cast(str, node["node_id"]), details={})
    )
    session["updated_at"] = dataclass_to_jsonable(datetime.now(UTC))
    _write_session_state(session_dir=session_dir, session=session)
    runtime.close_session(session_id)
    return _finalize_session_action(
        tool_paths=tool_paths,
        action=SessionActionKind.FINISH,
        status=final_status,
        session=session,
        request={"session_id": session_id, "timeout_seconds": timeout_seconds},
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )


def load_session_state(*, tool_paths: ToolPaths, session_id: str) -> dict[str, JsonValue]:
    """Load one stored theorem session payload."""
    _session_dir, session = _load_session(tool_paths=tool_paths, session_id=session_id)
    return session


def _capture_followup_node(
    *,
    tool_paths: ToolPaths,
    semantic_runtime: SessionSemanticRuntime,
    session: dict[str, JsonValue],
    session_dir: Path,
    sandbox: SandboxWorkspace,
    action: SessionActionKind,
    source_text: str,
    theorem_draft: str,
    timeout_seconds: float,
) -> dict[str, JsonValue]:
    now = datetime.now(UTC)
    node_index = len(cast(list[dict[str, JsonValue]], session["nodes"]))
    target_file = _real_target_file(session, sandbox.real_workspace.root)
    sandbox_target = sandbox.target_file(target_file)
    check_result = _perform_check(
        tool_paths=tool_paths,
        workspace=sandbox.sandbox_workspace,
        target_file=sandbox_target,
        timeout_seconds=timeout_seconds,
        command=f"session-{action.value}",
        run_id=f"{cast(str, session['session_id'])}-{node_index:03d}",
        artifact_dir=session_dir / "nodes" / _node_id(node_index=node_index, action=action),
        started_at=now,
        use_lake_env=bool(session.get("active_helpers")),
    )
    proofctl_context, semantic_session = semantic_runtime.followup_context(
        session_id=cast(str, session["session_id"]),
        workspace=sandbox.sandbox_workspace,
        target_file=sandbox_target,
        theorem=cast(str, session["theorem"]),
        source_text=source_text,
        timeout_seconds=timeout_seconds,
    )
    snapshot = _build_snapshot(
        theorem=cast(str, session["theorem"]),
        theorem_statement=cast(str | None, session.get("theorem_statement")),
        body_start=_require_int(session, "body_start"),
        body_end=_require_int(session, "body_end"),
        source_text=source_text,
        diagnostics=check_result.diagnostics,
        active_helpers=cast(list[dict[str, JsonValue]], session.get("active_helpers", [])),
        terminal_verified=action is SessionActionKind.FINISH and check_result.status is CheckStatus.OK,
        proofctl_context=proofctl_context,
        semantic_session=semantic_session,
    )
    return _build_node(
        session_id=cast(str, session["session_id"]),
        node_index=node_index,
        action=action,
        parent_node_id=cast(str | None, session.get("current_node_id")),
        theorem_draft=theorem_draft,
        source_text=source_text,
        snapshot=snapshot,
        check_result=check_result,
        session_dir=session_dir,
        details={},
    )


def _build_node(
    *,
    session_id: str,
    node_index: int,
    action: SessionActionKind,
    parent_node_id: str | None,
    theorem_draft: str,
    source_text: str,
    snapshot: dict[str, JsonValue],
    check_result: CheckResult,
    session_dir: Path,
    details: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    node_id = _node_id(node_index=node_index, action=action)
    node_dir = session_dir / "nodes" / node_id
    node_dir.mkdir(parents=True, exist_ok=True)
    node = cast(
        dict[str, JsonValue],
        {
            "node_id": node_id,
            "session_id": session_id,
            "parent_node_id": parent_node_id,
            "action": action.value,
            "created_at": dataclass_to_jsonable(datetime.now(UTC)),
            "artifact_dir": str(node_dir),
            "theorem_draft": theorem_draft,
            "details": details,
            "snapshot": snapshot,
            "check": check_result.to_json(),
        },
    )
    _write_json(node_dir / "node.json", node)
    (node_dir / "target.lean").write_text(source_text, encoding="utf-8")
    (node_dir / "theorem-body.lean").write_text(theorem_draft, encoding="utf-8")
    _write_json(node_dir / "snapshot.json", snapshot)
    _write_json(node_dir / "check.json", check_result.to_json())
    return node


def _build_snapshot(
    *,
    theorem: str,
    theorem_statement: str | None,
    body_start: int,
    body_end: int,
    source_text: str,
    diagnostics: list[Diagnostic],
    active_helpers: object,
    terminal_verified: bool,
    proofctl_context: ProofctlContextBundle,
    semantic_session: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Build the durable theorem snapshot from already selected semantic facts."""
    proof_state_payload = (
        cast(dict[str, JsonValue], dataclass_to_jsonable(proofctl_context.proof_state))
        if proofctl_context.proof_state is not None
        else None
    )
    local_context = proofctl_context.local_context
    goals = proofctl_context.goal_snapshot
    if terminal_verified:
        declaration_name = theorem
        namespace = proofctl_context.namespace
        insertion_point = proofctl_context.insertion_point
        if proofctl_context.proof_state is not None:
            declaration_name = proofctl_context.proof_state.declaration_name
            namespace = proofctl_context.proof_state.namespace
            insertion_point = proofctl_context.proof_state.insertion_point
        proof_state_payload = cast(
            dict[str, JsonValue],
            {
                "source": "verification-finished",
                "declaration_name": declaration_name,
                "namespace": namespace,
                "locals": [],
                "local_context": [],
                "goals": [],
                "insertion_point": None
                if insertion_point is None
                else cast(dict[str, JsonValue], dataclass_to_jsonable(insertion_point)),
            },
        )
        local_context = []
        goals = []
    editable_span = FileSpan(
        start_line=_line_for_offset(source_text, body_start),
        end_line=_line_for_offset(source_text, body_end),
    )
    return cast(
        dict[str, JsonValue],
        {
            "theorem_statement": theorem_statement,
            "editable_span": dataclass_to_jsonable(editable_span),
            "imports": proofctl_context.imports,
            "namespace": proofctl_context.namespace,
            "proof_target": None
            if proofctl_context.proof_target is None
            else cast(dict[str, JsonValue], dataclass_to_jsonable(proofctl_context.proof_target)),
            "proof_state": proof_state_payload,
            "local_context": local_context,
            "goals": goals,
            "active_helpers": cast(list[JsonValue], active_helpers or []),
            "semantic_session": semantic_session,
            "pretty_proof_state": _pretty_proof_state(local_context, goals, diagnostics),
            "diagnostic_count": len(diagnostics),
            "diagnostics": [diagnostic.to_json() for diagnostic in diagnostics],
            "syntax_probe_results": proofctl_context.syntax_probe_results,
            "semantic_probe_results": proofctl_context.semantic_probe_results,
            "probe_results": proofctl_context.probe_results,
        },
    )


def _finalize_session_action(
    *,
    tool_paths: ToolPaths,
    action: SessionActionKind,
    status: SessionStatus,
    session: dict[str, JsonValue],
    request: dict[str, JsonValue],
    started_at: datetime,
    finished_at: datetime,
) -> tuple[SessionActionResult, int]:
    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    diagnostic_count = _require_int(cast(dict[str, JsonValue], session["current_state"]), "diagnostic_count")
    result = SessionActionResult(
        run_id=run_id,
        command=f"session-{action.value}",
        action=action,
        status=status,
        exit_code=_session_exit_code(status),
        session_id=cast(str, session["session_id"]),
        target_file=cast(str, session["target_file"]),
        target_module_path=cast(str, session["target_module_path"]),
        theorem=cast(str, session["theorem"]),
        artifact_dir=str(run_dir),
        session_artifact_dir=cast(str, session["artifact_dir"]),
        current_node_id=cast(str | None, session.get("current_node_id")),
        diagnostic_count=diagnostic_count,
        started_at=started_at,
        finished_at=finished_at,
        session=session,
    )
    manifest = RunManifest(
        run_id=run_id,
        command=result.command,
        status=status,
        exit_code=result.exit_code,
        created_at=finished_at,
        artifact_dir=str(run_dir),
    )
    write_session_artifacts(run_dir=run_dir, manifest=manifest, request=request, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, _exit_code_number(result.exit_code)


def _write_session_state(*, session_dir: Path, session: dict[str, JsonValue]) -> None:
    """
    Write the durable session views that replay/export read independently.

    The split files are intentional. Operators often want the current state or
    action history without loading the whole session graph, and replay code must
    tolerate future additive fields in `session.json`.
    """
    _write_json(session_dir / "session.json", session)
    _write_json(session_dir / "current-state.json", session["current_state"])
    _write_json(session_dir / "action-history.json", session["action_history"])
    _write_json(session_dir / "checkpoints.json", session["checkpoints"])


def _load_session(*, tool_paths: ToolPaths, session_id: str) -> tuple[Path, dict[str, JsonValue]]:
    session_dir = tool_paths.artifact_sessions / session_id
    session_path = session_dir / "session.json"
    if not session_path.is_file():
        msg = f"Session `{session_id}` does not exist at `{session_path}`."
        raise FileNotFoundError(msg)
    payload = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        msg = f"Session `{session_id}` is not a JSON object."
        raise ValueError(msg)
    return session_dir, cast(dict[str, JsonValue], payload)


def _bind_sandbox(session: dict[str, JsonValue]) -> SandboxWorkspace:
    """Rebind the persisted session sandbox metadata into live workspace objects."""
    sandbox_payload = cast(dict[str, JsonValue], session["sandbox"])
    workspace_payload = cast(dict[str, JsonValue], session["workspace"])
    real_workspace = bind_workspace(Path(_require_string(workspace_payload, "root")))
    sandbox_workspace = bind_workspace(Path(_require_string(sandbox_payload, "sandbox_workspace_root")))
    return SandboxWorkspace(
        real_workspace=real_workspace,
        sandbox_workspace=sandbox_workspace,
        sandbox_root=Path(_require_string(sandbox_payload, "sandbox_root")),
        strategy=SandboxStrategy(_require_string(sandbox_payload, "strategy")),
        helper_root_dir=Path(_require_string(sandbox_payload, "helper_root_dir")),
        helper_module_root=_require_string(sandbox_payload, "helper_module_root"),
    )


def _real_target_file(session: dict[str, JsonValue], workspace_root: Path) -> Path:
    return workspace_root / cast(str, session["target_module_path"])


def _action_record(
    *, action: SessionActionKind, node_id: str | None, details: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    return {
        "action_id": new_run_id(datetime.now(UTC)),
        "action": action.value,
        "node_id": node_id,
        "created_at": dataclass_to_jsonable(datetime.now(UTC)),
        "details": details,
    }


def _current_draft(*, session: dict[str, JsonValue], source_text: str) -> str:
    body_start = _require_int(session, "body_start")
    body_end = _require_int(session, "body_end")
    return source_text[body_start:body_end]


def _pretty_proof_state(local_context: list[str], goals: list[str], diagnostics: list[Diagnostic]) -> str:
    sections = [
        "Local context:",
        *(local_context or ["<none>"]),
        "",
        "Goals:",
        *(goals or ["<none>"]),
    ]
    if diagnostics:
        sections.extend(["", "Diagnostics:"])
        sections.extend(
            f"{diagnostic.severity.value} {diagnostic.class_name.value}: {diagnostic.message}"
            for diagnostic in diagnostics
        )
    return "\n".join(sections)


def _replace_body(*, source_text: str, body_start: int, body_end: int, body: str) -> str:
    """
    Replace exactly the recorded theorem body span with one full new body.

    Session steps always replace whole bodies. The leading-space normalization
    keeps the replacement valid when the caller passes `by` or another body
    form without the single separating space already present in the file.
    """
    normalized = body if not body or body[:1].isspace() else f" {body}"
    return f"{source_text[:body_start]}{normalized}{source_text[body_end:]}"


def _normalized_body_length(body: str) -> int:
    if not body:
        return 1
    return len(body) if body[:1].isspace() else len(body) + 1


def _node_id(*, node_index: int, action: SessionActionKind) -> str:
    return f"node-{node_index:03d}-{action.value}"


def _finish_status(check: dict[str, JsonValue]) -> SessionStatus:
    if check.get("status") == CheckStatus.OK.value:
        return SessionStatus.FINISHED_VERIFIED
    return SessionStatus.FINISHED_FAILED


def _session_exit_code(status: SessionStatus) -> ExitCode:
    if status is SessionStatus.ERROR:
        return ExitCode.INTERNAL_ERROR
    if status is SessionStatus.FINISHED_FAILED:
        return ExitCode.CHECK_FAILED
    return ExitCode.SUCCESS


def _require_active_session(session: dict[str, JsonValue]) -> None:
    if session.get("status") != SessionStatus.ACTIVE.value:
        msg = f"Session `{session.get('session_id')}` is not active."
        raise ValueError(msg)


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
    status, exit_code = _check_status_and_code(lean_result.returncode, lean_result.timed_out, lean_result.diagnostics)
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


def _check_status_and_code(
    returncode: int | None, timed_out: bool, diagnostics: list[Diagnostic]
) -> tuple[CheckStatus, ExitCode]:
    if timed_out:
        return CheckStatus.TIMEOUT, ExitCode.TIMEOUT
    if returncode == 0:
        return CheckStatus.OK, ExitCode.SUCCESS
    if diagnostics:
        return CheckStatus.FAILED, ExitCode.CHECK_FAILED
    return CheckStatus.ERROR, ExitCode.INTERNAL_ERROR


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


def _line_for_offset(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _require_int(payload: dict[str, JsonValue], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        msg = f"Session is missing integer field `{key}`."
        raise ValueError(msg)
    return value


def _require_string(payload: dict[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        msg = f"Session is missing string field `{key}`."
        raise ValueError(msg)
    return value


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
