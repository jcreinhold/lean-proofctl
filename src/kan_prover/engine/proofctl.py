"""
Lean-backed workspace probes plus shared probe contracts.

This module owns the Python view of the Lean helper boundary.

The real semantic contract is small:

- `declaration-target` says which declaration Lean resolved and which span is
  safe to replace.
- `proof-state` says what elaboration reports for that declaration inside the
  bound workspace sandbox.

Older views such as `theorem-span` and `imports-and-namespace` still exist for
artifact compatibility, but they are derived from `declaration-target` rather
than treated as separate sources of truth.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from kan_prover.engine import lean_helpers
from kan_prover.engine.config import ToolPaths, WorkspaceBinding, discover_tool_paths, resolve_workspace_binding
from kan_prover.engine.lean_helpers import (
    LeanHelperCommand,
    LeanHelperRequest,
    ProbeLayer,
    ProbeResult,
    ProbeSpan,
    ProbeStatus,
    ProofctlDeclarationTarget,
    ProofctlInsertionPoint,
    ProofctlProofState,
    ProofctlSafeEditBoundary,
    ProofctlSemanticContextBundle,
    run_lean_helper,
)
from kan_prover.engine.models import JsonValue
from kan_prover.engine.semantic_cache import run_with_persistent_cache

PROBE_SCHEMA_VERSION = lean_helpers.PROBE_SCHEMA_VERSION
_PROOF_TARGET_CONTRACT_HINT = "proof-target workflows only support proof declarations with editable proof bodies"


@dataclass(frozen=True)
class ProofctlTheoremSpan:
    """Structured theorem span data from a probe result."""

    keyword: str
    declaration_name: str
    namespace: str | None
    namespace_stack: list[str]
    declaration_span: ProbeSpan
    selection_span: ProbeSpan
    body_span: ProbeSpan
    module_path: str | None = None
    short_name: str | None = None
    modifiers: list[str] = field(default_factory=list)
    group_kind: str = "standalone"
    group_id: str | None = None
    group_member_index: int | None = None
    safe_edit_boundary: ProofctlSafeEditBoundary | None = None


@dataclass(frozen=True)
class ProofctlImportsAndNamespace:
    """Structured import and namespace data from a probe result."""

    declaration_name: str
    namespace: str | None
    namespace_stack: list[str]
    imports: list[str]
    module_path: str | None = None


@dataclass(frozen=True)
class ProofctlContextBundle:
    """
    Probe data stored in artifacts and passed to backends.

    The bundle keeps both the normalized fields that callers usually need and
    the raw probe payloads needed for replay, export, and audit.
    """

    imports: list[str] = field(default_factory=list)
    namespace: str | None = None
    proof_target: ProofctlDeclarationTarget | None = None
    proof_state: ProofctlProofState | None = None
    local_context: list[str] = field(default_factory=list)
    goal_snapshot: list[str] = field(default_factory=list)
    insertion_point: ProofctlInsertionPoint | None = None
    syntax_probe_results: dict[str, JsonValue] = field(default_factory=dict)
    semantic_probe_results: dict[str, JsonValue] = field(default_factory=dict)
    probe_results: dict[str, JsonValue] = field(default_factory=dict)


class RequiredWorkspaceProbeError(RuntimeError):
    """Raised when a workspace-backed command cannot get required semantic probe data."""

    def __init__(self, *, required_for: str, probe: ProbeResult) -> None:
        """Store the required semantic probe name and the failing probe payload."""
        self.required_for = required_for
        self.probe = probe
        super().__init__(self._message())

    def _message(self) -> str:
        detail = self.probe.message or "semantic probe failed"
        error_kind = f" ({self.probe.error_kind.value})" if self.probe.error_kind is not None else ""
        return (
            "Workspace-backed commands require semantic "
            f"`{self.required_for}` probe data, but `{self.probe.command}` returned "
            f"`{self.probe.status.value}`{error_kind}: {detail}"
        )

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize one machine-readable command failure payload."""
        return {
            "status": "error",
            "exit_code": "internal-error",
            "error_kind": "required-semantic-probe-failed",
            "required_for": self.required_for,
            "message": str(self),
            "probe_result": self.probe.to_json(),
        }


class UnsupportedTargetError(RuntimeError):
    """Raised when a workflow requires the Lean proof-target contract and does not get it."""

    def __init__(
        self,
        *,
        command: str,
        target: ProofctlDeclarationTarget | None = None,
        probe: ProbeResult | None = None,
    ) -> None:
        """Store the command plus whichever proof-target evidence Lean returned."""
        if target is None and probe is None:
            msg = "UnsupportedTargetError requires a declaration target or declaration-target probe."
            raise ValueError(msg)
        self.command = command
        self.target = target
        self.probe = probe
        self.declaration_name = (
            target.declaration_name if target is not None else probe.theorem if probe is not None else "<unknown>"
        )
        self.detail = (
            _proof_target_contract_detail(target)
            if target is not None
            else probe.message
            if probe is not None
            else None
        )
        super().__init__(self._message())

    def _message(self) -> str:
        return f"`{self.declaration_name}` does not satisfy the proof-target contract required by `{self.command}`."

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize one machine-readable unsupported-target failure payload."""
        payload: dict[str, JsonValue] = {
            "status": "error",
            "exit_code": "internal-error",
            "error_kind": "unsupported-target-kind",
            "command": self.command,
            "message": str(self),
            "declaration_name": self.declaration_name,
        }
        if self.detail is not None:
            payload["detail"] = self.detail
        if self.probe is not None:
            payload["probe_result"] = self.probe.to_json()
        return payload


@dataclass(frozen=True)
class SemanticContextProbes:
    """
    Semantic probe batch for one theorem inside one bound workspace.

    Callers usually need two facts together: which declaration Lean selected and
    what the elaborated proof state looks like there. This object keeps that
    pair together and derives the legacy compatibility views from the selected
    declaration so callers do not need to understand that wiring.
    """

    proof_state_result: ProbeResult
    declaration_target_result: ProbeResult | None = None
    context_bundle_result: ProbeResult | None = None

    @classmethod
    def from_workspace(
        cls,
        *,
        tool_paths: ToolPaths,
        workspace: WorkspaceBinding,
        file_path: Path,
        theorem: str,
        timeout_seconds: float = 60.0,
        include_declaration_target: bool = False,
    ) -> SemanticContextProbes:
        """
        Run the semantic probes needed to inspect one theorem in one workspace.

        `include_declaration_target` remains for API compatibility, but the
        semantic hot path now uses one bundled helper request and derives the
        theorem-target payload from that result.
        """
        _ = include_declaration_target
        bundle_probe = semantic_context_bundle(
            tool_paths=tool_paths,
            workspace=workspace,
            file_path=file_path,
            theorem=theorem,
            timeout_seconds=timeout_seconds,
        )
        return semantic_context_probes_from_bundle_probe(bundle_probe)

    def require_semantic_probes(self) -> None:
        """Reject degraded probe data when the caller needs full semantic context."""
        if self.declaration_target_result is not None:
            self.require_proof_target(command="proof workflow")
            _require_semantic_probe(required_for="theorem-span", probe=self.theorem_span_result())
            _require_semantic_probe(
                required_for="imports-and-namespace",
                probe=self.imports_and_namespace_result(),
            )
        _require_semantic_probe(required_for="proof-state", probe=self.proof_state_result)

    def declaration_target_payload(self) -> ProofctlDeclarationTarget | None:
        """Return the decoded declaration target when the probe succeeded."""
        probe = self.declaration_target_result
        if probe is None or probe.status is not ProbeStatus.OK:
            return None
        return cast(ProofctlDeclarationTarget, probe.result)

    def require_proof_target(self, *, command: str) -> ProofctlDeclarationTarget:
        """Return one target whose Lean proof-target contract is usable for workflows."""
        probe = self.declaration_target_result
        if probe is None:
            raise ValueError("SemanticContextProbes has no declaration-target result.")
        return _require_proof_target_contract(command=command, probe=probe)

    def theorem_span_result(self) -> ProbeResult:
        """Return the compatibility `theorem-span` view derived from the target probe."""
        probe = self.declaration_target_result
        if probe is None:
            raise ValueError("SemanticContextProbes has no declaration-target result.")
        return _declaration_target_to_theorem_span_result(probe)

    def imports_and_namespace_result(self) -> ProbeResult:
        """Return the compatibility imports/namespace view derived from the target probe."""
        probe = self.declaration_target_result
        if probe is None:
            raise ValueError("SemanticContextProbes has no declaration-target result.")
        return _declaration_target_to_imports_result(probe)

    def context_bundle(
        self,
        *,
        theorem_span_result: ProbeResult | None = None,
        imports_and_namespace_result: ProbeResult | None = None,
        require_semantic_probes: bool = False,
    ) -> ProofctlContextBundle:
        """Assemble one durable backend-facing context bundle from the probe batch."""
        probe_results: dict[str, JsonValue] = {}
        syntax_probe_results: dict[str, JsonValue] = {}
        semantic_probe_results: dict[str, JsonValue] = {}

        if self.context_bundle_result is not None:
            _record_probe_result(
                key="context-bundle",
                probe=self.context_bundle_result,
                probe_results=probe_results,
                syntax_probe_results=syntax_probe_results,
                semantic_probe_results=semantic_probe_results,
            )

        if self.declaration_target_result is not None:
            _record_probe_result(
                key="declaration-target",
                probe=self.declaration_target_result,
                probe_results=probe_results,
                syntax_probe_results=syntax_probe_results,
                semantic_probe_results=semantic_probe_results,
            )

        span_probe = theorem_span_result
        if span_probe is None and self.declaration_target_result is not None:
            span_probe = self.theorem_span_result()
        if span_probe is not None:
            _record_probe_result(
                key="theorem-span",
                probe=span_probe,
                probe_results=probe_results,
                syntax_probe_results=syntax_probe_results,
                semantic_probe_results=semantic_probe_results,
            )

        imports_probe = imports_and_namespace_result
        if imports_probe is None and self.declaration_target_result is not None:
            imports_probe = self.imports_and_namespace_result()
        if imports_probe is not None:
            _record_probe_result(
                key="imports-and-namespace",
                probe=imports_probe,
                probe_results=probe_results,
                syntax_probe_results=syntax_probe_results,
                semantic_probe_results=semantic_probe_results,
            )

        _record_probe_result(
            key="proof-state",
            probe=self.proof_state_result,
            probe_results=probe_results,
            syntax_probe_results=syntax_probe_results,
            semantic_probe_results=semantic_probe_results,
        )

        if require_semantic_probes:
            self.require_semantic_probes()

        imports: list[str] = []
        namespace: str | None = None
        proof_target_payload = self.declaration_target_payload()
        if imports_probe is not None and imports_probe.status is ProbeStatus.OK:
            imports_payload = cast(ProofctlImportsAndNamespace, imports_probe.result)
            imports = imports_payload.imports
            namespace = imports_payload.namespace

        proof_state_payload: ProofctlProofState | None = None
        local_rows: list[str] = []
        goals: list[str] = []
        insertion: ProofctlInsertionPoint | None = None
        if self.proof_state_result.status is ProbeStatus.OK:
            proof_state_payload = cast(ProofctlProofState, self.proof_state_result.result)
            local_rows = proof_state_payload.local_context
            goals = proof_state_payload.goals
            insertion = proof_state_payload.insertion_point
            if namespace is None:
                namespace = proof_state_payload.namespace

        return ProofctlContextBundle(
            imports=imports,
            namespace=namespace,
            proof_target=proof_target_payload,
            proof_state=proof_state_payload,
            local_context=local_rows,
            goal_snapshot=goals,
            insertion_point=insertion,
            syntax_probe_results=syntax_probe_results,
            semantic_probe_results=semantic_probe_results,
            probe_results=probe_results,
        )


def theorem_span(*, file_path: Path, theorem: str) -> ProbeResult:
    """Return the portable `theorem-span` compatibility view for one theorem."""
    declaration_probe = _workspace_declaration_target(file_path=file_path, theorem=theorem)
    return _declaration_target_to_theorem_span_result(declaration_probe)


def imports_and_namespace(*, file_path: Path, theorem: str) -> ProbeResult:
    """Return the portable imports/namespace compatibility view for one theorem."""
    declaration_probe = _workspace_declaration_target(file_path=file_path, theorem=theorem)
    return _declaration_target_to_imports_result(declaration_probe)


def declaration_target(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    file_path: Path,
    theorem: str,
    timeout_seconds: float = 60.0,
) -> ProbeResult:
    """Run the Lean-backed declaration-target probe."""
    return run_lean_helper(
        LeanHelperRequest(
            tool_paths=tool_paths,
            workspace=workspace,
            file_path=file_path,
            theorem=theorem,
            command=LeanHelperCommand.DECLARATION_TARGET,
            timeout_seconds=timeout_seconds,
            requires_writable=False,
        )
    )


def semantic_context_bundle(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    file_path: Path,
    theorem: str,
    timeout_seconds: float = 60.0,
) -> ProbeResult:
    """Run the Lean-backed bundled theorem-context probe."""
    return run_with_persistent_cache(
        LeanHelperRequest(
            tool_paths=tool_paths,
            workspace=workspace,
            file_path=file_path,
            theorem=theorem,
            command=LeanHelperCommand.CONTEXT_BUNDLE,
            timeout_seconds=timeout_seconds,
            requires_writable=False,
        ),
        runner=run_lean_helper,
    )


def semantic_context_probes_from_bundle_probe(bundle_probe: ProbeResult) -> SemanticContextProbes:
    """Decode one bundled theorem-context probe into the stable pair of semantic views."""
    declaration_probe, proof_state_probe = _split_context_bundle_probe(bundle_probe)
    return SemanticContextProbes(
        context_bundle_result=bundle_probe,
        declaration_target_result=declaration_probe,
        proof_state_result=proof_state_probe,
    )


def declaration_inventory(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    file_path: Path,
    timeout_seconds: float = 60.0,
) -> ProbeResult:
    """Run the Lean-backed declaration-inventory probe."""
    return run_with_persistent_cache(
        LeanHelperRequest(
            tool_paths=tool_paths,
            workspace=workspace,
            file_path=file_path,
            theorem="*",
            command=LeanHelperCommand.DECLARATION_INVENTORY,
            timeout_seconds=timeout_seconds,
            requires_writable=False,
        ),
        runner=run_lean_helper,
    )


def batch_declaration_inventory(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    file_path: Path,
    manifest_path: Path,
    timeout_seconds: float = 60.0,
) -> ProbeResult:
    """Run the Lean-backed batch declaration-inventory probe for one frontier manifest."""
    return run_with_persistent_cache(
        LeanHelperRequest(
            tool_paths=tool_paths,
            workspace=workspace,
            file_path=file_path,
            theorem="*",
            command=LeanHelperCommand.BATCH_DECLARATION_INVENTORY,
            timeout_seconds=timeout_seconds,
            requires_writable=False,
            extra_args=(str(manifest_path.expanduser().resolve()),),
        ),
        runner=run_lean_helper,
    )


def workspace_declaration_index(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    file_path: Path,
    manifest_path: Path,
    timeout_seconds: float = 60.0,
) -> ProbeResult:
    """Run the Lean-backed compiled workspace declaration index build."""
    return run_lean_helper(
        LeanHelperRequest(
            tool_paths=tool_paths,
            workspace=workspace,
            file_path=file_path,
            theorem="*",
            command=LeanHelperCommand.WORKSPACE_DECLARATION_INDEX,
            timeout_seconds=timeout_seconds,
            requires_writable=False,
            extra_args=(str(manifest_path.expanduser().resolve()),),
        )
    )


def local_context(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    file_path: Path,
    theorem: str,
    timeout_seconds: float = 60.0,
) -> ProbeResult:
    """Run the workspace-local semantic local-context probe."""
    return run_lean_helper(
        LeanHelperRequest(
            tool_paths=tool_paths,
            workspace=workspace,
            file_path=file_path,
            theorem=theorem,
            command=LeanHelperCommand.LOCAL_CONTEXT,
            timeout_seconds=timeout_seconds,
            requires_writable=False,
        )
    )


def proof_state(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    file_path: Path,
    theorem: str,
    timeout_seconds: float = 60.0,
) -> ProbeResult:
    """Run the workspace-local semantic proof-state probe."""
    return run_lean_helper(
        LeanHelperRequest(
            tool_paths=tool_paths,
            workspace=workspace,
            file_path=file_path,
            theorem=theorem,
            command=LeanHelperCommand.PROOF_STATE,
            timeout_seconds=timeout_seconds,
            requires_writable=False,
        )
    )


def goal_snapshot(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    file_path: Path,
    theorem: str,
    timeout_seconds: float = 60.0,
) -> ProbeResult:
    """Run the workspace-local semantic goal-snapshot probe."""
    return run_lean_helper(
        LeanHelperRequest(
            tool_paths=tool_paths,
            workspace=workspace,
            file_path=file_path,
            theorem=theorem,
            command=LeanHelperCommand.GOAL_SNAPSHOT,
            timeout_seconds=timeout_seconds,
            requires_writable=False,
        )
    )


def insertion_point(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    file_path: Path,
    theorem: str,
    timeout_seconds: float = 60.0,
) -> ProbeResult:
    """Run the workspace-local semantic insertion-point probe."""
    return run_lean_helper(
        LeanHelperRequest(
            tool_paths=tool_paths,
            workspace=workspace,
            file_path=file_path,
            theorem=theorem,
            command=LeanHelperCommand.INSERTION_POINT,
            timeout_seconds=timeout_seconds,
            requires_writable=False,
        )
    )


def collect_context_bundle(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    file_path: Path,
    theorem: str,
    theorem_span_result: ProbeResult | None = None,
    imports_and_namespace_result: ProbeResult | None = None,
    declaration_target_result: ProbeResult | None = None,
    timeout_seconds: float = 60.0,
    require_semantic_probes: bool = False,
) -> ProofctlContextBundle:
    """
    Collect one backend-facing context bundle for compatibility callers.

    New internal callers should prefer `SemanticContextProbes`, which keeps the
    semantic probe batch together and derives the compatibility views in one
    place. This function stays as a compatibility wrapper for existing tests and
    call sites that already pass partially computed probe results.
    """
    probes = SemanticContextProbes.from_workspace(
        tool_paths=tool_paths,
        workspace=workspace,
        file_path=file_path,
        theorem=theorem,
        timeout_seconds=timeout_seconds,
        include_declaration_target=True,
    )
    effective_declaration_target = declaration_target_result or probes.declaration_target_result
    effective_probes = SemanticContextProbes(
        context_bundle_result=probes.context_bundle_result,
        declaration_target_result=effective_declaration_target,
        proof_state_result=probes.proof_state_result,
    )
    imports_probe = imports_and_namespace_result
    if imports_probe is None and declaration_target_result is None:
        imports_probe = imports_and_namespace(file_path=file_path, theorem=theorem)
    return effective_probes.context_bundle(
        theorem_span_result=theorem_span_result,
        imports_and_namespace_result=imports_probe,
        require_semantic_probes=require_semantic_probes,
    )


def _record_probe_result(
    *,
    key: str,
    probe: ProbeResult,
    probe_results: dict[str, JsonValue],
    syntax_probe_results: dict[str, JsonValue],
    semantic_probe_results: dict[str, JsonValue],
) -> None:
    payload = probe.to_json()
    probe_results[key] = payload
    if probe.layer is ProbeLayer.SEMANTIC:
        semantic_probe_results[key] = payload
        return
    syntax_probe_results[key] = payload


def _workspace_declaration_target(*, file_path: Path, theorem: str) -> ProbeResult:
    resolved_file = str(file_path.expanduser().resolve())
    try:
        tool_paths = discover_tool_paths()
        workspace = resolve_workspace_binding(tool_paths=tool_paths, target_file=file_path)
    except FileNotFoundError:
        return ProbeResult(
            layer=ProbeLayer.SEMANTIC,
            command=LeanHelperCommand.DECLARATION_TARGET.value,
            status=ProbeStatus.UNAVAILABLE,
            file=resolved_file,
            theorem=theorem,
            message="Workspace-backed semantic probes require a Lake workspace binding.",
        )
    return declaration_target(
        tool_paths=tool_paths,
        workspace=workspace,
        file_path=file_path,
        theorem=theorem,
    )


def _require_semantic_probe(*, required_for: str, probe: ProbeResult) -> None:
    if probe.layer is ProbeLayer.SEMANTIC and probe.status is ProbeStatus.OK:
        return
    raise RequiredWorkspaceProbeError(required_for=required_for, probe=probe)


def stored_proof_target_contract_error(
    *,
    command: str,
    proof_target: JsonValue | None = None,
    payload: JsonValue | None,
) -> str | None:
    """Return one stored-run contract failure message, or `None` when the contract is usable."""
    if isinstance(proof_target, dict):
        try:
            target = lean_helpers.decode_declaration_target_payload(cast(dict[str, object], proof_target))
        except ValueError as error:
            return f"stored prove run has malformed `proof_target` data for `{command}`: {error}"
        try:
            if target.safe_edit is not None and target.proof_state_available:
                return None
            raise UnsupportedTargetError(command=command, target=target)
        except UnsupportedTargetError as error:
            detail = error.detail or str(error)
            return f"stored prove run does not carry a usable proof-target contract for `{command}`: {detail}"
    if not isinstance(payload, dict):
        return f"stored prove run is missing semantic `declaration-target` probe data for `{command}`"
    try:
        probe = lean_helpers.decode_probe_result_payload(
            cast(dict[str, object], payload),
            command=LeanHelperCommand.DECLARATION_TARGET,
            file=_string_value(payload.get("file")),
            theorem=_string_value(payload.get("theorem")),
        )
    except ValueError as error:
        return f"stored prove run has malformed semantic `declaration-target` probe data for `{command}`: {error}"
    try:
        _require_proof_target_contract(command=command, probe=probe)
    except UnsupportedTargetError as error:
        detail = error.detail or str(error)
        return f"stored prove run does not carry a usable proof-target contract for `{command}`: {detail}"
    except RequiredWorkspaceProbeError as error:
        detail = error.probe.message or str(error)
        return f"stored prove run does not carry a usable proof-target contract for `{command}`: {detail}"
    except ValueError as error:
        return f"stored prove run has malformed semantic `declaration-target` probe data for `{command}`: {error}"
    return None


def _require_proof_target_contract(*, command: str, probe: ProbeResult) -> ProofctlDeclarationTarget:
    if probe.status is ProbeStatus.OK and isinstance(probe.result, ProofctlDeclarationTarget):
        target = probe.result
        if target.safe_edit is not None and target.proof_state_available:
            return target
        raise UnsupportedTargetError(command=command, target=target)
    if _probe_reports_unsupported_target(probe):
        raise UnsupportedTargetError(command=command, probe=probe)
    _require_semantic_probe(required_for="declaration-target", probe=probe)
    msg = "Semantic declaration-target probe did not decode to a declaration target."
    raise ValueError(msg)


def _probe_reports_unsupported_target(probe: ProbeResult) -> bool:
    message = probe.message or ""
    return probe.command == LeanHelperCommand.DECLARATION_TARGET.value and _PROOF_TARGET_CONTRACT_HINT in message


def _string_value(value: JsonValue | None) -> str:
    return value if isinstance(value, str) else ""


def _proof_target_contract_detail(target: ProofctlDeclarationTarget) -> str:
    if target.safe_edit is None and not target.proof_state_available:
        return "Lean did not expose a safe-edit boundary or proof-state data for this declaration."
    if target.safe_edit is None:
        return "Lean did not expose the safe-edit boundary required for proof workflows."
    if not target.proof_state_available:
        return "Lean did not expose the proof-state data required for proof workflows."
    return "Lean did not expose a usable proof-target contract for this declaration."


def _split_context_bundle_probe(bundle_probe: ProbeResult) -> tuple[ProbeResult, ProbeResult]:
    if bundle_probe.status is ProbeStatus.OK and isinstance(bundle_probe.result, ProofctlSemanticContextBundle):
        payload = bundle_probe.result
        proof_state_status = ProbeStatus.OK if payload.proof_state is not None else ProbeStatus.UNAVAILABLE
        proof_state_message = None
        if payload.proof_state is None:
            proof_state_message = "proof-state data is unavailable for this proof target"
        return (
            ProbeResult(
                layer=bundle_probe.layer,
                command=LeanHelperCommand.DECLARATION_TARGET.value,
                status=bundle_probe.status,
                file=bundle_probe.file,
                theorem=bundle_probe.theorem,
                result=payload.declaration_target,
            ),
            ProbeResult(
                layer=bundle_probe.layer,
                command=LeanHelperCommand.PROOF_STATE.value,
                status=proof_state_status,
                file=bundle_probe.file,
                theorem=bundle_probe.theorem,
                result=payload.proof_state,
                message=proof_state_message,
            ),
        )
    return (
        ProbeResult(
            layer=bundle_probe.layer,
            command=LeanHelperCommand.DECLARATION_TARGET.value,
            status=bundle_probe.status,
            file=bundle_probe.file,
            theorem=bundle_probe.theorem,
            candidates=bundle_probe.candidates,
            message=bundle_probe.message,
            error_kind=bundle_probe.error_kind,
        ),
        ProbeResult(
            layer=bundle_probe.layer,
            command=LeanHelperCommand.PROOF_STATE.value,
            status=bundle_probe.status,
            file=bundle_probe.file,
            theorem=bundle_probe.theorem,
            candidates=bundle_probe.candidates,
            message=bundle_probe.message,
            error_kind=bundle_probe.error_kind,
        ),
    )


def _declaration_target_to_theorem_span_result(probe: ProbeResult) -> ProbeResult:
    result: object | None = None
    if probe.status is ProbeStatus.OK and isinstance(probe.result, ProofctlDeclarationTarget):
        target = probe.result
        result = ProofctlTheoremSpan(
            keyword=target.keyword,
            declaration_name=target.declaration_name,
            namespace=target.namespace,
            namespace_stack=target.namespace_stack,
            declaration_span=target.declaration_span,
            selection_span=target.name_span,
            body_span=target.body_span,
            module_path=target.module_path,
            short_name=target.short_name,
            modifiers=target.modifiers,
            group_kind=target.group.kind,
            group_id=target.group.group_id,
            group_member_index=target.group.member_index,
            safe_edit_boundary=target.safe_edit,
        )
    return ProbeResult(
        layer=probe.layer,
        command="theorem-span",
        status=probe.status,
        file=probe.file,
        theorem=probe.theorem,
        result=result,
        candidates=probe.candidates,
        message=probe.message,
    )


def _declaration_target_to_imports_result(probe: ProbeResult) -> ProbeResult:
    result: object | None = None
    if probe.status is ProbeStatus.OK and isinstance(probe.result, ProofctlDeclarationTarget):
        target = probe.result
        result = ProofctlImportsAndNamespace(
            declaration_name=target.declaration_name,
            namespace=target.namespace,
            namespace_stack=target.namespace_stack,
            imports=target.imports,
            module_path=target.module_path,
        )
    return ProbeResult(
        layer=probe.layer,
        command="imports-and-namespace",
        status=probe.status,
        file=probe.file,
        theorem=probe.theorem,
        result=result,
        candidates=probe.candidates,
        message=probe.message,
    )
