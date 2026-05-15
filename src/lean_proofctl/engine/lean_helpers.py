"""Typed Lean helper bindings for workspace-aware `proofctl` probes."""

import json
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import cast

from lean_proofctl.engine.config import ToolPaths, WorkspaceBinding
from lean_proofctl.engine.models import JsonValue, dataclass_to_jsonable
from lean_proofctl.engine.semantic_probe_runtime import (
    ensure_semantic_probe_executable,
    semantic_probe_environment,
)

PROBE_SCHEMA_VERSION = "probe.v2"
SEMANTIC_PROBE_SOURCE = "SemanticProbe.lean"


class ProbeLayer(StrEnum):
    """Stable probe layers."""

    SYNTAX = "syntax"
    SEMANTIC = "semantic"


class ProbeStatus(StrEnum):
    """Stable probe statuses."""

    OK = "ok"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class LeanHelperCommand(StrEnum):
    """Supported Lean helper commands."""

    BATCH_DECLARATION_INVENTORY = "batch-declaration-inventory"
    CONTEXT_BUNDLE = "context-bundle"
    DECLARATION_TARGET = "declaration-target"
    DECLARATION_INVENTORY = "declaration-inventory"
    WORKSPACE_DECLARATION_INDEX = "workspace-declaration-index"
    PROOF_STATE = "proof-state"
    LOCAL_CONTEXT = "local-context"
    GOAL_SNAPSHOT = "goal-snapshot"
    INSERTION_POINT = "insertion-point"


class LiveFileSessionReuse(StrEnum):
    """Live file-session reuse outcome reported by the Lean runtime."""

    CREATED = "created"
    REUSED = "reused"
    REBUILT = "rebuilt"


class LeanHelperErrorKind(StrEnum):
    """Machine-readable Lean helper failure categories."""

    BOOTSTRAP_FAILED = "bootstrap-failed"
    EXECUTION_FAILED = "execution-failed"
    MALFORMED_JSON = "malformed-json"
    DECODE_FAILED = "decode-failed"
    HELPER_STATUS = "helper-status"


class LiveLeanHelperError(RuntimeError):
    """Raised when the live SemanticProbe boundary fails."""


@dataclass(frozen=True)
class ProbePoint:
    """One 1-based source point."""

    line: int
    column: int


@dataclass(frozen=True)
class ProbeSpan:
    """One source span."""

    start: ProbePoint
    end: ProbePoint


@dataclass(frozen=True)
class ProofctlGroupMembership:
    """Structured declaration group membership from the Lean probe."""

    kind: str
    group_id: str | None = None
    member_index: int | None = None


@dataclass(frozen=True)
class ProofctlSafeEditBoundary:
    """Structured safe edit boundary metadata from the Lean probe."""

    kind: str
    declaration_span: ProbeSpan
    body_span: ProbeSpan


@dataclass(frozen=True)
class ProofctlDeclarationMetadata:
    """Structured Lean-backed declaration metadata for catalog surfaces."""

    module_path: str
    short_name: str
    declaration_name: str
    namespace: str | None
    namespace_stack: list[str]
    imports: list[str]
    declaration_kind: str
    modifiers: list[str]
    group: ProofctlGroupMembership
    declaration_span: ProbeSpan
    name_span: ProbeSpan
    body_span: ProbeSpan


@dataclass(frozen=True)
class ProofctlDeclarationTarget:
    """Structured Lean-backed proof-target contract."""

    module_path: str
    short_name: str
    declaration_name: str
    namespace: str | None
    namespace_stack: list[str]
    imports: list[str]
    keyword: str
    modifiers: list[str]
    group: ProofctlGroupMembership
    declaration_span: ProbeSpan
    name_span: ProbeSpan
    body_span: ProbeSpan
    safe_edit: ProofctlSafeEditBoundary | None
    proof_state_available: bool


@dataclass(frozen=True)
class ProofctlDeclarationInventory:
    """Structured Lean-backed declaration inventory for one file."""

    entries: list[ProofctlDeclarationMetadata]


@dataclass(frozen=True)
class ProofctlBatchDeclarationInventoryFile:
    """Structured Lean-backed declaration inventory for one frontier file."""

    file_path: str
    entries: list[ProofctlDeclarationMetadata]


@dataclass(frozen=True)
class ProofctlBatchDeclarationInventory:
    """Structured Lean-backed declaration inventory for one frontier batch."""

    files: list[ProofctlBatchDeclarationInventoryFile]


@dataclass(frozen=True)
class ProofctlWorkspaceDeclarationIndexEntry:
    """One declaration row from the compiled workspace declaration index."""

    file_path: str
    module_path: str
    declaration_name: str
    short_name: str
    namespace: str | None
    declaration_kind: str
    declaration_span: ProbeSpan
    type_text: str


@dataclass(frozen=True)
class ProofctlWorkspaceDeclarationIndex:
    """Structured Lean-backed declaration index for one workspace snapshot."""

    entries: list[ProofctlWorkspaceDeclarationIndexEntry]


@dataclass(frozen=True)
class ProofctlLocalContext:
    """Structured local context rows from the semantic probe."""

    source: str
    declaration_name: str
    namespace: str | None
    locals: list[str]


@dataclass(frozen=True)
class ProofctlGoalSnapshot:
    """Structured goal snapshot rows from the semantic probe."""

    source: str
    declaration_name: str
    namespace: str | None
    goals: list[str]


@dataclass(frozen=True)
class ProofctlInsertionPoint:
    """Structured insertion point data from the semantic probe."""

    kind: str
    declaration_name: str
    namespace: str | None
    position: ProbePoint


@dataclass(frozen=True)
class ProofctlLocalDecl:
    """One structured local declaration from the proof-state probe."""

    name: str
    binder_info: str
    type: str
    value: str | None = None


@dataclass(frozen=True)
class ProofctlProofState:
    """Structured theorem-local proof state from the semantic probe."""

    source: str
    declaration_name: str
    namespace: str | None
    locals: list[ProofctlLocalDecl]
    local_context: list[str]
    goals: list[str]
    insertion_point: ProofctlInsertionPoint


@dataclass(frozen=True)
class ProofctlSemanticContextBundle:
    """Canonical semantic theorem-context bundle from one Lean helper request."""

    declaration_target: ProofctlDeclarationTarget
    proof_state: ProofctlProofState | None


@dataclass(frozen=True)
class ProbeCandidate:
    """One candidate returned for an ambiguous theorem name."""

    declaration_name: str
    short_name: str
    namespace: str | None
    namespace_stack: list[str]
    keyword: str
    declaration_span: ProbeSpan


@dataclass(frozen=True)
class ProbeResult:
    """One machine-readable probe result."""

    layer: ProbeLayer
    command: str
    status: ProbeStatus
    file: str
    theorem: str
    result: object | None = None
    candidates: list[ProbeCandidate] = field(default_factory=list)
    message: str | None = None
    error_kind: LeanHelperErrorKind | None = None

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize one probe result for durable artifacts."""
        payload: dict[str, JsonValue] = {
            "schema_version": PROBE_SCHEMA_VERSION,
            "layer": self.layer.value,
            "command": self.command,
            "status": self.status.value,
            "file": self.file,
            "theorem": self.theorem,
        }
        if self.result is not None:
            payload["result"] = dataclass_to_jsonable(self.result)
        if self.candidates:
            payload["candidates"] = dataclass_to_jsonable(self.candidates)
        if self.message is not None:
            payload["message"] = self.message
        if self.error_kind is not None:
            payload["error_kind"] = self.error_kind.value
        return cast(dict[str, JsonValue], payload)


@dataclass(frozen=True)
class LeanHelperRequest:
    """Typed Lean helper execution inputs."""

    tool_paths: ToolPaths
    workspace: WorkspaceBinding
    file_path: Path
    theorem: str
    command: LeanHelperCommand
    timeout_seconds: float = 60.0
    requires_writable: bool = True
    extra_args: tuple[str, ...] = ()


def run_lean_helper(request: LeanHelperRequest) -> ProbeResult:
    """Run one Lean helper command through the canonical subprocess boundary."""
    resolved_file = str(request.file_path.expanduser().resolve())
    if request.requires_writable and not request.workspace.writable:
        return ProbeResult(
            layer=ProbeLayer.SEMANTIC,
            command=request.command.value,
            status=ProbeStatus.UNAVAILABLE,
            file=resolved_file,
            theorem=request.theorem,
            message="Semantic probes require a writable sandboxed workspace.",
            error_kind=LeanHelperErrorKind.EXECUTION_FAILED,
        )

    try:
        executable_path = ensure_semantic_probe_executable(
            tool_paths=request.tool_paths,
            timeout_seconds=request.timeout_seconds,
        )
    except OSError as error:
        return ProbeResult(
            layer=ProbeLayer.SEMANTIC,
            command=request.command.value,
            status=ProbeStatus.ERROR,
            file=resolved_file,
            theorem=request.theorem,
            message=f"Could not prepare semantic probe executable: {error!s}",
            error_kind=LeanHelperErrorKind.BOOTSTRAP_FAILED,
        )

    argv = [
        str(executable_path),
        request.command.value,
        str(request.workspace.root),
        resolved_file,
        request.theorem,
        *request.extra_args,
    ]
    try:
        completed = subprocess.run(
            argv,
            cwd=request.workspace.root,
            capture_output=True,
            text=True,
            check=False,
            timeout=request.timeout_seconds,
            env=semantic_probe_environment(workspace=request.workspace),
        )
    except subprocess.TimeoutExpired as error:
        return ProbeResult(
            layer=ProbeLayer.SEMANTIC,
            command=request.command.value,
            status=ProbeStatus.ERROR,
            file=resolved_file,
            theorem=request.theorem,
            message=f"Semantic probe timed out: {error!s}",
            error_kind=LeanHelperErrorKind.EXECUTION_FAILED,
        )

    payload_text = completed.stdout.strip()
    if not payload_text:
        message = completed.stderr.strip() or "Semantic probe produced empty stdout."
        return ProbeResult(
            layer=ProbeLayer.SEMANTIC,
            command=request.command.value,
            status=ProbeStatus.ERROR,
            file=resolved_file,
            theorem=request.theorem,
            message=message,
            error_kind=LeanHelperErrorKind.EXECUTION_FAILED,
        )

    payload = load_probe_payload(payload_text)
    if payload is None:
        message = completed.stderr.strip() or "Semantic probe returned invalid JSON."
        return ProbeResult(
            layer=ProbeLayer.SEMANTIC,
            command=request.command.value,
            status=ProbeStatus.ERROR,
            file=resolved_file,
            theorem=request.theorem,
            message=message,
            error_kind=LeanHelperErrorKind.MALFORMED_JSON,
        )
    if not isinstance(payload, dict):
        return ProbeResult(
            layer=ProbeLayer.SEMANTIC,
            command=request.command.value,
            status=ProbeStatus.ERROR,
            file=resolved_file,
            theorem=request.theorem,
            message="Semantic probe did not return a JSON object.",
            error_kind=LeanHelperErrorKind.MALFORMED_JSON,
        )
    return _decode_semantic_payload(
        cast(dict[str, object], payload),
        command=request.command,
        file=resolved_file,
        theorem=request.theorem,
    )


def load_probe_payload(payload_text: str) -> object | None:
    """Decode one semantic probe payload, tolerating leading Lake bootstrap logs."""
    try:
        return json.loads(payload_text)
    except json.JSONDecodeError:
        pass

    for index, char in enumerate(payload_text):
        if char != "{":
            continue
        try:
            return json.loads(payload_text[index:])
        except json.JSONDecodeError:
            continue
    return None


def decode_probe_result_payload(
    payload: dict[str, object],
    *,
    command: LeanHelperCommand,
    file: str,
    theorem: str,
) -> ProbeResult:
    """Decode one stored semantic probe payload back into typed probe data."""
    return _decode_semantic_payload(
        _normalize_serialized_probe_payload(payload, command=command),
        command=command,
        file=file,
        theorem=theorem,
    )


def decode_declaration_target_payload(payload: dict[str, object]) -> ProofctlDeclarationTarget:
    """Decode one stored proof-target payload without the outer probe wrapper."""
    return _parse_declaration_target(payload)


def _normalize_serialized_probe_payload(
    payload: dict[str, object],
    *,
    command: LeanHelperCommand,
) -> dict[str, object]:
    normalized = dict(payload)
    result_payload = normalized.get("result")
    if not isinstance(result_payload, dict):
        return normalized
    if command is LeanHelperCommand.PROOF_STATE:
        normalized["result"] = _normalize_serialized_proof_state(cast("dict[str, object]", result_payload))
        return normalized
    if command is LeanHelperCommand.CONTEXT_BUNDLE:
        bundle_payload = dict(result_payload)
        proof_state_payload = bundle_payload.get("proof_state")
        if isinstance(proof_state_payload, dict):
            bundle_payload["proof_state"] = _normalize_serialized_proof_state(proof_state_payload)
        normalized["result"] = bundle_payload
    return normalized


def _normalize_serialized_proof_state(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    insertion_payload = normalized.get("insertion_point")
    if not isinstance(insertion_payload, dict):
        return normalized
    insertion = dict(insertion_payload)
    position = insertion.pop("position", None)
    if position is not None and "insert_position" not in insertion:
        insertion["insert_position"] = position
    normalized["insertion_point"] = insertion
    return normalized


def _decode_semantic_payload(
    payload: dict[str, object],
    *,
    command: LeanHelperCommand,
    file: str,
    theorem: str,
) -> ProbeResult:
    schema_version = payload.get("schema_version")
    if schema_version != PROBE_SCHEMA_VERSION:
        return _decode_error(
            command=command,
            file=file,
            theorem=theorem,
            message=f"Semantic probe returned unsupported schema version {schema_version!r}.",
        )
    layer = payload.get("layer")
    if layer != ProbeLayer.SEMANTIC.value:
        return _decode_error(
            command=command,
            file=file,
            theorem=theorem,
            message=f"Semantic probe returned unexpected layer {layer!r}.",
        )
    payload_command = payload.get("command")
    if payload_command != command.value:
        return _decode_error(
            command=command,
            file=file,
            theorem=theorem,
            message=f"Semantic probe returned unexpected command {payload_command!r}.",
        )
    status_text = payload.get("status")
    try:
        status = ProbeStatus(status_text)
    except ValueError:
        return _decode_error(
            command=command,
            file=file,
            theorem=theorem,
            message=f"Semantic probe returned invalid status {status_text!r}.",
        )
    message = payload.get("message")
    if message is not None and not isinstance(message, str):
        message = str(message)
    try:
        candidates_payload = payload.get("candidates")
        candidates = _parse_candidates(candidates_payload) if candidates_payload is not None else []
    except ValueError as error:
        return _decode_error(command=command, file=file, theorem=theorem, message=str(error))
    result_payload = payload.get("result")

    if status is ProbeStatus.OK:
        if not isinstance(result_payload, dict):
            return _decode_error(
                command=command,
                file=file,
                theorem=theorem,
                message="Semantic probe payload is missing an object `result`.",
            )
        try:
            parsed_result = _parse_semantic_result(command, cast(dict[str, object], result_payload))
        except ValueError as error:
            return _decode_error(command=command, file=file, theorem=theorem, message=str(error))
        if isinstance(parsed_result, str):
            return _decode_error(command=command, file=file, theorem=theorem, message=parsed_result)
        return ProbeResult(
            layer=ProbeLayer.SEMANTIC,
            command=command.value,
            status=status,
            file=file,
            theorem=theorem,
            result=parsed_result,
        )

    return ProbeResult(
        layer=ProbeLayer.SEMANTIC,
        command=command.value,
        status=status,
        file=file,
        theorem=theorem,
        candidates=candidates,
        message=message,
        error_kind=LeanHelperErrorKind.HELPER_STATUS,
    )


def _decode_error(*, command: LeanHelperCommand, file: str, theorem: str, message: str) -> ProbeResult:
    return ProbeResult(
        layer=ProbeLayer.SEMANTIC,
        command=command.value,
        status=ProbeStatus.ERROR,
        file=file,
        theorem=theorem,
        message=message,
        error_kind=LeanHelperErrorKind.DECODE_FAILED,
    )


def _parse_semantic_result(command: LeanHelperCommand, payload: dict[str, object]) -> object | str:
    if command is LeanHelperCommand.CONTEXT_BUNDLE:
        declaration_payload = _require_object(payload, "declaration_target")
        proof_state_payload = payload.get("proof_state")
        return ProofctlSemanticContextBundle(
            declaration_target=_parse_declaration_target(declaration_payload),
            proof_state=(
                _parse_proof_state_result(_require_mapping(proof_state_payload, "proof_state"))
                if proof_state_payload is not None
                else None
            ),
        )
    if command is LeanHelperCommand.DECLARATION_TARGET:
        return _parse_declaration_target(payload)
    if command is LeanHelperCommand.DECLARATION_INVENTORY:
        entries_payload = payload.get("entries")
        if not isinstance(entries_payload, list):
            return "Semantic declaration inventory result is missing `entries`."
        return ProofctlDeclarationInventory(
            entries=[_parse_declaration_metadata(_require_mapping(item, "entries[]")) for item in entries_payload]
        )
    if command is LeanHelperCommand.BATCH_DECLARATION_INVENTORY:
        files_payload = payload.get("files")
        if not isinstance(files_payload, list):
            return "Semantic batch declaration inventory result is missing `files`."
        files: list[ProofctlBatchDeclarationInventoryFile] = []
        for item in files_payload:
            file_payload = _require_mapping(item, "files[]")
            file_path = file_payload.get("file_path")
            if not isinstance(file_path, str) or not file_path.strip():
                return "Semantic batch declaration inventory file is missing `file_path`."
            entries_payload = file_payload.get("entries")
            if not isinstance(entries_payload, list):
                return "Semantic batch declaration inventory file is missing `entries`."
            files.append(
                ProofctlBatchDeclarationInventoryFile(
                    file_path=file_path,
                    entries=[
                        _parse_declaration_metadata(_require_mapping(entry, "files[].entries[]"))
                        for entry in entries_payload
                    ],
                )
            )
        return ProofctlBatchDeclarationInventory(files=files)
    if command is LeanHelperCommand.WORKSPACE_DECLARATION_INDEX:
        entries_payload = payload.get("entries")
        if not isinstance(entries_payload, list):
            return "Semantic workspace declaration index result is missing `entries`."
        return ProofctlWorkspaceDeclarationIndex(
            entries=[
                _parse_workspace_declaration_index_entry(_require_mapping(item, "entries[]"))
                for item in entries_payload
            ]
        )
    if command is LeanHelperCommand.PROOF_STATE:
        return _parse_proof_state_result(payload)
    if command is LeanHelperCommand.LOCAL_CONTEXT:
        raw_locals = payload.get("locals")
        if not isinstance(raw_locals, list):
            return "Semantic local-context result is missing `locals`."
        locals_text = [_render_local_row(item) for item in raw_locals]
        return ProofctlLocalContext(
            source=_require_string(payload, "source"),
            declaration_name=_require_string(payload, "declaration_name"),
            namespace=_optional_string(payload, "namespace"),
            locals=locals_text,
        )
    if command is LeanHelperCommand.GOAL_SNAPSHOT:
        return ProofctlGoalSnapshot(
            source=_require_string(payload, "source"),
            declaration_name=_require_string(payload, "declaration_name"),
            namespace=_optional_string(payload, "namespace"),
            goals=_string_list(payload, "goals"),
        )
    if command is LeanHelperCommand.INSERTION_POINT:
        position_object = _require_object(payload, "insert_position")
        return ProofctlInsertionPoint(
            kind=_require_string(payload, "kind"),
            declaration_name=_require_string(payload, "declaration_name"),
            namespace=_optional_string(payload, "namespace"),
            position=_parse_point(position_object, "insert_position"),
        )
    return f"Unknown semantic command `{command.value}`."


def _parse_declaration_metadata(payload: dict[str, object]) -> ProofctlDeclarationMetadata:
    group_payload = _require_object(payload, "group")
    return ProofctlDeclarationMetadata(
        module_path=_require_string(payload, "module_path"),
        short_name=_require_string(payload, "short_name"),
        declaration_name=_require_string(payload, "declaration_name"),
        namespace=_optional_string(payload, "namespace"),
        namespace_stack=_string_list(payload, "namespace_stack"),
        imports=_string_list(payload, "imports"),
        declaration_kind=_require_string(payload, "declaration_kind"),
        modifiers=_string_list(payload, "modifiers"),
        group=ProofctlGroupMembership(
            kind=_require_string(group_payload, "kind"),
            group_id=_optional_string(group_payload, "group_id"),
            member_index=_optional_int(group_payload, "member_index"),
        ),
        declaration_span=_require_span(payload, "declaration_span"),
        name_span=_require_span(payload, "name_span"),
        body_span=_require_span(payload, "body_span"),
    )


def _parse_declaration_target(payload: dict[str, object]) -> ProofctlDeclarationTarget:
    group_payload = _require_object(payload, "group")
    safe_edit_payload = payload.get("safe_edit")
    return ProofctlDeclarationTarget(
        module_path=_require_string(payload, "module_path"),
        short_name=_require_string(payload, "short_name"),
        declaration_name=_require_string(payload, "declaration_name"),
        namespace=_optional_string(payload, "namespace"),
        namespace_stack=_string_list(payload, "namespace_stack"),
        imports=_string_list(payload, "imports"),
        keyword=_require_string(payload, "keyword"),
        modifiers=_string_list(payload, "modifiers"),
        group=ProofctlGroupMembership(
            kind=_require_string(group_payload, "kind"),
            group_id=_optional_string(group_payload, "group_id"),
            member_index=_optional_int(group_payload, "member_index"),
        ),
        declaration_span=_require_span(payload, "declaration_span"),
        name_span=_require_span(payload, "name_span"),
        body_span=_require_span(payload, "body_span"),
        safe_edit=(
            ProofctlSafeEditBoundary(
                kind=_require_string(_require_mapping(safe_edit_payload, "safe_edit"), "kind"),
                declaration_span=_require_span(_require_mapping(safe_edit_payload, "safe_edit"), "declaration_span"),
                body_span=_require_span(_require_mapping(safe_edit_payload, "safe_edit"), "body_span"),
            )
            if safe_edit_payload is not None
            else None
        ),
        proof_state_available=_require_bool(payload, "proof_state_available"),
    )


def _parse_proof_state_result(payload: dict[str, object]) -> ProofctlProofState:
    raw_locals = payload.get("locals")
    if not isinstance(raw_locals, list):
        msg = "Semantic proof-state result is missing `locals`."
        raise ValueError(msg)
    goals = _string_list(payload, "goals")
    insertion_payload = _require_object(payload, "insertion_point")
    declaration_name = _require_string(payload, "declaration_name")
    namespace = _optional_string(payload, "namespace")
    local_entries = [_parse_local_decl(item) for item in raw_locals]
    return ProofctlProofState(
        source=_require_string(payload, "source"),
        declaration_name=declaration_name,
        namespace=namespace,
        locals=local_entries,
        local_context=[_render_local_decl(entry) for entry in local_entries],
        goals=goals,
        insertion_point=ProofctlInsertionPoint(
            kind=_require_string(insertion_payload, "kind"),
            declaration_name=declaration_name,
            namespace=namespace,
            position=_parse_point(_require_object(insertion_payload, "insert_position"), "insert_position"),
        ),
    )


def _parse_workspace_declaration_index_entry(payload: dict[str, object]) -> ProofctlWorkspaceDeclarationIndexEntry:
    return ProofctlWorkspaceDeclarationIndexEntry(
        file_path=_require_string(payload, "file_path"),
        module_path=_require_string(payload, "module_path"),
        declaration_name=_require_string(payload, "declaration_name"),
        short_name=_require_string(payload, "short_name"),
        namespace=_optional_string(payload, "namespace"),
        declaration_kind=_require_string(payload, "declaration_kind"),
        declaration_span=_require_span(payload, "declaration_span"),
        type_text=_require_string(payload, "type_text"),
    )


def _parse_candidates(payload: object) -> list[ProbeCandidate]:
    if not isinstance(payload, list):
        return []
    candidates: list[ProbeCandidate] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        candidate = cast(dict[str, object], item)
        declaration_span = _require_span(candidate, "declaration_span")
        namespace_stack = _string_list(candidate, "namespace_stack") if "namespace_stack" in candidate else []
        candidates.append(
            ProbeCandidate(
                declaration_name=_require_string(candidate, "declaration_name"),
                short_name=_require_string(candidate, "short_name"),
                namespace=_optional_string(candidate, "namespace"),
                namespace_stack=namespace_stack,
                keyword=_require_string(candidate, "declaration_kind"),
                declaration_span=declaration_span,
            )
        )
    return candidates


def _require_object(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        msg = f"Probe payload field `{key}` must be an object."
        raise ValueError(msg)
    return cast(dict[str, object], value)


def _require_mapping(value: object, key: str) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"Probe payload field `{key}` must be an object."
        raise ValueError(msg)
    return cast(dict[str, object], value)


def _require_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        msg = f"Probe payload field `{key}` must be a string."
        raise ValueError(msg)
    return value


def _optional_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"Probe payload field `{key}` must be a string when present."
        raise ValueError(msg)
    return value


def _optional_int(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        msg = f"Probe payload field `{key}` must be an integer when present."
        raise ValueError(msg)
    return value


def _require_bool(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        msg = f"Probe payload field `{key}` must be a boolean."
        raise ValueError(msg)
    return value


def _string_list(payload: dict[str, object], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        msg = f"Probe payload field `{key}` must be a string list."
        raise ValueError(msg)
    return cast(list[str], value)


def _parse_point(payload: dict[str, object], key: str) -> ProbePoint:
    line = payload.get("line")
    column = payload.get("column")
    if not isinstance(line, int) or not isinstance(column, int):
        msg = f"Probe payload field `{key}` must contain integer `line` and `column`."
        raise ValueError(msg)
    return ProbePoint(line=line, column=column)


def _require_span(payload: dict[str, object], key: str) -> ProbeSpan:
    span_object = _require_object(payload, key)
    start = _require_object(span_object, "start")
    end = _require_object(span_object, "end")
    return ProbeSpan(start=_parse_point(start, f"{key}.start"), end=_parse_point(end, f"{key}.end"))


def _render_local_row(item: object) -> str:
    return _render_local_decl(_parse_local_decl(item))


def _parse_local_decl(item: object) -> ProofctlLocalDecl:
    if not isinstance(item, dict):
        msg = "Semantic local context row must be an object."
        raise ValueError(msg)
    row = cast(dict[str, object], item)
    name = row.get("name")
    binder_info = row.get("binder_info")
    type_text = row.get("type")
    value_text = row.get("value")
    if not isinstance(name, str) or not isinstance(binder_info, str) or not isinstance(type_text, str):
        msg = "Semantic local context row is missing string fields."
        raise ValueError(msg)
    if value_text is not None and not isinstance(value_text, str):
        msg = "Semantic local context row `value` must be a string when present."
        raise ValueError(msg)
    return ProofctlLocalDecl(name=name, binder_info=binder_info, type=type_text, value=value_text)


def _render_local_decl(local_decl: ProofctlLocalDecl) -> str:
    binder_info = local_decl.binder_info.removeprefix("Lean.BinderInfo.")
    pieces = [local_decl.name]
    if binder_info and binder_info != "default":
        pieces.append(f"[{binder_info}]")
    pieces.append(f": {local_decl.type}")
    if isinstance(local_decl.value, str):
        pieces.append(f" := {local_decl.value}")
    return "".join(pieces)
