"""Artifact-backed run and session inspection helpers."""

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from kan_prover.engine.config import ToolPaths
from kan_prover.engine.ledger import append_index_row, write_show_artifacts
from kan_prover.engine.models import (
    ArtifactProbeSummary,
    ArtifactRetrievalHitSummary,
    ExitCode,
    InspectionStatus,
    JsonValue,
    RunGraphSummary,
    RunManifest,
    RunSelectionSummary,
    SessionNodeDetail,
    SessionNodeSummary,
    ShowRunResult,
    ShowSessionResult,
)
from kan_prover.engine.replay import (
    RunBundle,
    _json_list,
    _load_json_if_exists,
    _required_string,
    _select_attempt,
    _select_prove_node,
    _string,
    _string_list,
    load_run_bundle,
)
from kan_prover.engine.run_id import new_run_id
from kan_prover.engine.session import load_session_state


def show_run(
    *, tool_paths: ToolPaths, source_run_id: str, attempt_index: int | None = None
) -> tuple[ShowRunResult, int]:
    """Render one stored run bundle without rerunning proof search."""
    started_at = datetime.now(UTC)
    bundle = load_run_bundle(tool_paths=tool_paths, run_id=source_run_id)
    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    selected = _run_selection(bundle=bundle, attempt_index=attempt_index)
    semantic_probes = selected.semantic_probes if selected is not None else _bundle_probe_summaries(bundle)
    result = ShowRunResult(
        run_id=run_id,
        command="show-run",
        status=InspectionStatus.OK,
        exit_code=ExitCode.SUCCESS,
        source_run_id=source_run_id,
        stored_command=_stored_command(bundle),
        stored_status=_stored_status(bundle),
        target_file=_stored_target_file(bundle),
        target_module_path=_stored_target_module_path(bundle),
        theorem=_stored_theorem(bundle),
        workspace_root=_stored_workspace_root(bundle),
        workspace_package=_stored_workspace_package(bundle),
        backend=_stored_backend(bundle),
        artifact_dir=str(run_dir),
        source_artifact_dir=str(bundle.run_dir),
        retrieval_hits=_bundle_retrieval_hits(bundle),
        proof_target=_bundle_proof_target(bundle),
        semantic_probes=semantic_probes,
        selected=selected,
        graph=_graph_summary(bundle),
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
    manifest = RunManifest(
        run_id=run_id,
        command="show-run",
        status=InspectionStatus.OK,
        exit_code=ExitCode.SUCCESS,
        created_at=result.finished_at,
        artifact_dir=str(run_dir),
    )
    request = {"source_run_id": source_run_id, "attempt_index": attempt_index}
    write_show_artifacts(run_dir=run_dir, manifest=manifest, request=request, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, 0


def show_session(
    *,
    tool_paths: ToolPaths,
    source_session_id: str,
    node_id: str | None = None,
    node_index: int | None = None,
) -> tuple[ShowSessionResult, int]:
    """Render one stored theorem session without reopening raw JSON."""
    started_at = datetime.now(UTC)
    session = load_session_state(tool_paths=tool_paths, session_id=source_session_id)
    run_id = new_run_id(started_at)
    run_dir = tool_paths.artifact_runs / run_id
    nodes = [cast(dict[str, JsonValue], item) for item in _json_list(session.get("nodes")) if isinstance(item, dict)]
    selected_index, selected_node = _select_session_node(
        session=session,
        nodes=nodes,
        node_id=node_id,
        node_index=node_index,
    )
    result = ShowSessionResult(
        run_id=run_id,
        command="show-session",
        status=InspectionStatus.OK,
        exit_code=ExitCode.SUCCESS,
        source_session_id=source_session_id,
        session_status=_string(session.get("status")) or "unknown",
        target_file=_required_string(session.get("target_file")),
        target_module_path=_string(session.get("target_module_path")),
        theorem=_required_string(session.get("theorem")),
        artifact_dir=str(run_dir),
        session_artifact_dir=_required_string(session.get("artifact_dir")),
        current_node_id=_string(session.get("current_node_id")),
        checkpoints=[_string(item.get("label")) or "-" for item in _checkpoint_dicts(session)],
        nodes=[_session_node_summary(index, item) for index, item in enumerate(nodes, start=1)],
        selected=_session_node_detail(selected_index, selected_node),
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
    manifest = RunManifest(
        run_id=run_id,
        command="show-session",
        status=InspectionStatus.OK,
        exit_code=ExitCode.SUCCESS,
        created_at=result.finished_at,
        artifact_dir=str(run_dir),
    )
    request = {"source_session_id": source_session_id, "node_id": node_id, "node_index": node_index}
    write_show_artifacts(run_dir=run_dir, manifest=manifest, request=request, result=result)
    append_index_row(tool_paths.artifact_index, result)
    return result, 0


def _run_selection(bundle: RunBundle, attempt_index: int | None) -> RunSelectionSummary | None:
    command = _stored_command(bundle)
    if command in {"repair", "solve"}:
        attempt = _select_attempt(bundle, attempt_index)
        verification = cast(dict[str, JsonValue], attempt.get("verification") or {})
        backend_response = cast(dict[str, JsonValue], attempt.get("backend_response") or {})
        classification = cast(dict[str, JsonValue], backend_response.get("theorem_body_classification") or {})
        candidate = cast(dict[str, JsonValue], attempt.get("candidate") or {})
        semantic_probes = _probe_summaries(cast(dict[str, JsonValue], attempt.get("semantic_probe_results") or {}))
        return RunSelectionSummary(
            selection_kind="attempt",
            index=_int(attempt.get("attempt_index")) or 1,
            label=f"attempt #{_int(attempt.get('attempt_index')) or 1}",
            node_id=None,
            action_kind=None,
            candidate_id=_string(candidate.get("candidate_id")),
            status=_string(verification.get("status")) or ("verified" if bool(attempt.get("verified")) else "failed"),
            verified=bool(attempt.get("verified")),
            diagnostic_count=_int(verification.get("diagnostic_count")) or 0,
            response_class=_string(classification.get("response_class")),
            message=_string(classification.get("message")) or _string(backend_response.get("message")),
            semantic_session=None,
            semantic_probes=semantic_probes,
        )
    if command in {"prove", "resume"}:
        node = _select_prove_node(bundle, attempt_index)
        session_snapshot = _session_snapshot_from_graph_node(node)
        action = cast(dict[str, JsonValue], node.get("action_payload") or {})
        semantic_probes = _probe_summaries(
            cast(dict[str, JsonValue], session_snapshot.get("semantic_probe_results") or {}) if session_snapshot else {}
        )
        graph_index = _int(node.get("graph_index"))
        return RunSelectionSummary(
            selection_kind="graph-node",
            index=(graph_index + 1) if graph_index is not None else 1,
            label=_graph_node_label(node),
            node_id=_string(node.get("node_id")),
            action_kind=_string(action.get("kind")) or _string(node.get("node_kind")),
            candidate_id=_string(action.get("candidate_id")) or _string(action.get("kind")),
            status=_string(node.get("status")),
            verified=_string(node.get("status")) == "verified",
            diagnostic_count=len(_json_list(node.get("diagnostics"))),
            goal_count=len(_json_list(node.get("goals"))),
            session_id=_string(node.get("session_id")),
            session_node_id=_string(node.get("session_node_id")),
            pretty_proof_state=_string(session_snapshot.get("pretty_proof_state")) if session_snapshot else None,
            semantic_session=cast(
                dict[str, JsonValue] | None,
                (session_snapshot.get("semantic_session") if session_snapshot else None)
                or cast(dict[str, JsonValue], node.get("probe_snapshot") or {}).get("semantic_session"),
            ),
            semantic_probes=semantic_probes,
        )
    return None


def _graph_summary(bundle: RunBundle) -> RunGraphSummary | None:
    if _stored_command(bundle) not in {"prove", "resume"}:
        return None
    graph = cast(dict[str, JsonValue], bundle.result.get("graph") or {})
    return RunGraphSummary(
        root_node_id=_required_string(graph.get("root_node_id")),
        verified_node_id=_string(graph.get("verified_node_id")),
        frontier_node_ids=_string_list(graph.get("frontier_node_ids")),
        branch_node_ids=_string_list(graph.get("branch_node_ids")),
        node_count=_int(graph.get("node_count")) or 0,
        edge_count=_int(graph.get("edge_count")) or 0,
    )


def _bundle_retrieval_hits(bundle: RunBundle) -> list[ArtifactRetrievalHitSummary]:
    hits = _retrieval_payloads(bundle)
    return [
        ArtifactRetrievalHitSummary(
            title=_string(hit.get("title")) or "-",
            source_kind=_string(hit.get("source_kind")) or "unknown",
            score=_int(hit.get("score")) or 0,
            theorem=_string(hit.get("theorem")),
            path=_string(hit.get("path")),
            summary=_string(hit.get("summary")),
        )
        for hit in hits[:5]
    ]


def _bundle_probe_summaries(bundle: RunBundle) -> list[ArtifactProbeSummary]:
    snapshot = cast(dict[str, JsonValue], bundle.result.get("snapshot") or {})
    inspection = cast(dict[str, JsonValue], bundle.result.get("inspection") or {})
    for payload in (
        cast(dict[str, JsonValue], snapshot.get("semantic_probe_results") or {}),
        cast(dict[str, JsonValue], inspection.get("semantic_probe_results") or {}),
        cast(dict[str, JsonValue], bundle.request.get("semantic_probe_results") or {}),
    ):
        probes = _probe_summaries(payload)
        if probes:
            return probes
    return []


def _bundle_proof_target(bundle: RunBundle) -> dict[str, JsonValue] | None:
    snapshot = cast(dict[str, JsonValue], bundle.result.get("snapshot") or {})
    inspection = cast(dict[str, JsonValue], bundle.result.get("inspection") or {})
    for payload in (
        snapshot.get("proof_target"),
        inspection.get("proof_target"),
        bundle.request.get("proof_target"),
    ):
        if isinstance(payload, dict):
            return cast(dict[str, JsonValue], payload)
    return None


def _retrieval_payloads(bundle: RunBundle) -> list[dict[str, JsonValue]]:
    snapshot = cast(dict[str, JsonValue], bundle.result.get("snapshot") or {})
    inspection = cast(dict[str, JsonValue], bundle.result.get("inspection") or {})
    retrieval = cast(dict[str, JsonValue], snapshot.get("retrieval") or {})
    context_payload = _load_json_if_exists(bundle.run_dir / "context" / "retrieval.json")
    if context_payload is not None:
        if isinstance(context_payload, list):
            hits = _object_list(context_payload)
            if hits:
                return hits
        context_dict = context_payload
        hits = _object_list(context_dict.get("retrieval_hits"))
        if hits:
            return hits
        retrieval = cast(dict[str, JsonValue], context_dict.get("retrieval") or {})
        hits = _object_list(retrieval.get("hits"))
        if hits:
            return hits
    for payload in (
        bundle.result.get("retrieval_hits"),
        snapshot.get("retrieval_hits"),
        retrieval.get("hits"),
        inspection.get("retrieval_hits"),
    ):
        hits = _object_list(payload)
        if hits:
            return hits
    return []


def _probe_summaries(payload: dict[str, JsonValue]) -> list[ArtifactProbeSummary]:
    return [
        ArtifactProbeSummary(
            probe=name,
            status=_string(cast(dict[str, JsonValue], value).get("status")) or "unknown",
            summary=_probe_summary(cast(dict[str, JsonValue], value)),
        )
        for name, value in sorted(payload.items())
        if isinstance(value, dict)
    ]


def _probe_summary(payload: dict[str, JsonValue]) -> str | None:
    for key in ("message", "error_kind", "command", "source"):
        value = _string(payload.get(key))
        if value:
            return value
    return None


def _select_session_node(
    *,
    session: dict[str, JsonValue],
    nodes: list[dict[str, JsonValue]],
    node_id: str | None,
    node_index: int | None,
) -> tuple[int, dict[str, JsonValue]]:
    if not nodes:
        msg = f"Session `{_required_string(session.get('session_id'))}` has no stored nodes."
        raise ValueError(msg)
    if node_id is not None:
        for index, node in enumerate(nodes, start=1):
            if _string(node.get("node_id")) == node_id:
                return index, node
        msg = f"Session `{_required_string(session.get('session_id'))}` has no node `{node_id}`."
        raise ValueError(msg)
    if node_index is not None:
        if node_index < 1 or node_index > len(nodes):
            msg = f"Session `{_required_string(session.get('session_id'))}` has no node at position `{node_index}`."
            raise ValueError(msg)
        return node_index, nodes[node_index - 1]
    current_node_id = _string(session.get("current_node_id"))
    if current_node_id is not None:
        for index, node in enumerate(nodes, start=1):
            if _string(node.get("node_id")) == current_node_id:
                return index, node
    return len(nodes), nodes[-1]


def _session_node_summary(index: int, node: dict[str, JsonValue]) -> SessionNodeSummary:
    snapshot = cast(dict[str, JsonValue], node.get("snapshot") or {})
    check = cast(dict[str, JsonValue], node.get("check") or {})
    return SessionNodeSummary(
        index=index,
        node_id=_required_string(node.get("node_id")),
        action=_string(node.get("action")) or "unknown",
        parent_node_id=_string(node.get("parent_node_id")),
        status=_string(check.get("status")),
        diagnostic_count=_int(check.get("diagnostic_count")) or 0,
        goal_count=len(_json_list(snapshot.get("goals"))),
    )


def _session_node_detail(index: int, node: dict[str, JsonValue]) -> SessionNodeDetail:
    snapshot = cast(dict[str, JsonValue], node.get("snapshot") or {})
    check = cast(dict[str, JsonValue], node.get("check") or {})
    return SessionNodeDetail(
        index=index,
        node_id=_required_string(node.get("node_id")),
        action=_string(node.get("action")) or "unknown",
        parent_node_id=_string(node.get("parent_node_id")),
        status=_string(check.get("status")),
        theorem_draft=_string(node.get("theorem_draft")) or "",
        diagnostic_count=_int(check.get("diagnostic_count")) or 0,
        goal_count=len(_json_list(snapshot.get("goals"))),
        proof_target=cast(dict[str, JsonValue] | None, snapshot.get("proof_target")),
        pretty_proof_state=_string(snapshot.get("pretty_proof_state")),
        semantic_session=cast(dict[str, JsonValue] | None, snapshot.get("semantic_session")),
        semantic_probes=_probe_summaries(cast(dict[str, JsonValue], snapshot.get("semantic_probe_results") or {})),
    )


def _session_snapshot_from_graph_node(node: dict[str, JsonValue]) -> dict[str, JsonValue]:
    session_dir = _string(node.get("session_artifact_dir"))
    session_node_id = _string(node.get("session_node_id"))
    if session_dir is None or session_node_id is None:
        return {}
    snapshot = _load_json_if_exists(Path(session_dir) / "nodes" / session_node_id / "snapshot.json")
    return cast(dict[str, JsonValue], snapshot or {})


def _checkpoint_dicts(session: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    return [
        cast(dict[str, JsonValue], item) for item in _json_list(session.get("checkpoints")) if isinstance(item, dict)
    ]


def _object_list(value: JsonValue | None) -> list[dict[str, JsonValue]]:
    return [cast(dict[str, JsonValue], item) for item in _json_list(value) if isinstance(item, dict)]


def _stored_command(bundle: RunBundle) -> str:
    return _string(bundle.manifest.get("command")) or _string(bundle.request.get("command")) or "unknown"


def _stored_status(bundle: RunBundle) -> str:
    return _string(bundle.manifest.get("status")) or _string(bundle.result.get("status")) or "unknown"


def _stored_target_file(bundle: RunBundle) -> str:
    return _string(bundle.result.get("target_file")) or _string(bundle.request.get("target_file")) or ""


def _stored_target_module_path(bundle: RunBundle) -> str | None:
    return _string(bundle.result.get("target_module_path")) or _string(bundle.request.get("target_module_path"))


def _stored_theorem(bundle: RunBundle) -> str | None:
    return _string(bundle.result.get("theorem")) or _string(bundle.request.get("theorem"))


def _stored_workspace_root(bundle: RunBundle) -> str | None:
    return _string(bundle.request.get("workspace_root")) or _string(
        cast(dict[str, JsonValue], bundle.result.get("workspace") or {}).get("root")
    )


def _stored_workspace_package(bundle: RunBundle) -> str | None:
    return _string(bundle.request.get("workspace_package")) or _string(
        cast(dict[str, JsonValue], bundle.result.get("workspace") or {}).get("package")
    )


def _stored_backend(bundle: RunBundle) -> str | None:
    return _string(bundle.result.get("backend")) or _string(bundle.request.get("backend"))


def _graph_node_label(node: dict[str, JsonValue]) -> str:
    node_id = _string(node.get("node_id")) or "node"
    kind = _string(node.get("node_kind")) or "state"
    return f"{node_id} ({kind})"


def _int(value: JsonValue | None) -> int | None:
    return value if isinstance(value, int) else None
