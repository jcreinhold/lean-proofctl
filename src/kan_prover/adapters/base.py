"""Shared adapter interfaces and strict response parsing helpers."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast

from kan_prover.engine.models import (
    AgentRequest,
    AgentResponse,
    AppendTacticTextAction,
    BackendResultKind,
    DecompositionLayer,
    DecompositionStep,
    JsonValue,
    PatchCandidate,
    ProposedHelperLemma,
    ProposeHelperLemmaAction,
    RecommendStrategyShiftAction,
    ReplaceTheoremDraftAction,
    SuggestCheckpointLabelAction,
    TaskKind,
    TheoremPlan,
    TheoremPlanStep,
    TheoremSessionAction,
    TheoremSessionActionKind,
)


class Backend(Protocol):
    """Protocol for one strict backend proposal call."""

    def name(self) -> str:
        """Return the backend identifier."""

    def propose(self, request: AgentRequest) -> AgentResponse:
        """Return one strict response for one bounded backend attempt."""


class BackendProtocolError(ValueError):
    """Raised when strict backend output is malformed."""


class BackendCommandError(RuntimeError):
    """Raised when one backend call fails before returning a usable strict result."""

    def __init__(self, *, backend: str, backend_error_kind: str, message: str) -> None:
        """Store the backend name, failure class, and human-readable message."""
        self.backend = backend
        self.backend_error_kind = backend_error_kind
        super().__init__(message)

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize one machine-readable backend failure payload."""
        return {
            "status": "error",
            "exit_code": "internal-error",
            "error_kind": "backend-failed",
            "backend": self.backend,
            "backend_error_kind": self.backend_error_kind,
            "message": str(self),
        }


def prompts_root(adapter_file: str) -> Path:
    """Return the repo-local prompts directory."""
    return Path(adapter_file).resolve().parents[3] / "prompts"


def render_prompt(*, template_path: Path, request: AgentRequest) -> str:
    """Render one prompt template with the strict request payload."""
    template = template_path.read_text(encoding="utf-8")
    request_json = json.dumps(request.to_json(), indent=2, sort_keys=True)
    replacements = {
        "{{TASK_KIND}}": request.task_kind.value,
        "{{THEOREM_NAME}}": request.theorem_name,
        "{{TARGET_FILE}}": request.target_file,
        "{{SCHEMA_PATH}}": request.schema_path,
        "{{REQUEST_JSON}}": request_json,
    }
    rendered = template
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    return rendered


def parse_response_payload(
    *, raw_output: str, expected_task: TaskKind, backend_name: str
) -> tuple[
    BackendResultKind,
    dict[str, JsonValue],
    list[PatchCandidate],
    list[str],
    list[DecompositionStep],
    TheoremPlan | None,
    list[TheoremSessionAction],
    str | None,
]:
    """Parse one strict JSON payload from backend stdout."""
    payload = _load_json_payload(raw_output=raw_output, backend_name=backend_name)
    if payload.get("schema_version") != "v1":
        msg = f"{backend_name} output is missing schema_version='v1'."
        raise BackendProtocolError(msg)
    task_kind = payload.get("task_kind")
    if task_kind != expected_task.value:
        msg = f"{backend_name} output task_kind mismatch: expected '{expected_task.value}', got {task_kind!r}."
        raise BackendProtocolError(msg)
    result_kind_value = payload.get("result_kind")
    try:
        result_kind = BackendResultKind(str(result_kind_value))
    except ValueError as error:
        msg = f"{backend_name} output has unknown result_kind: {result_kind_value!r}."
        raise BackendProtocolError(msg) from error

    payload_object = cast(Mapping[str, object], payload)
    message = _read_optional_string(payload_object, "message")
    candidates: list[PatchCandidate] = []
    candidate_parse_errors: list[str] = []
    decomposition: list[DecompositionStep] = []
    theorem_plan: TheoremPlan | None = None
    theorem_session_actions: list[TheoremSessionAction] = []
    if result_kind is BackendResultKind.THEOREM_BODY_REPLACEMENTS:
        raw_candidates = payload.get("candidates")
        if not isinstance(raw_candidates, list):
            msg = f"{backend_name} output is missing a `candidates` list."
            raise BackendProtocolError(msg)
        for index, item in enumerate(raw_candidates, start=1):
            try:
                candidates.append(_parse_candidate(item=item, backend_name=backend_name))
            except BackendProtocolError as error:
                candidate_parse_errors.append(f"candidate {index}: {error}")
    elif result_kind is BackendResultKind.DECOMPOSITION:
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list):
            msg = f"{backend_name} output is missing a `steps` list."
            raise BackendProtocolError(msg)
        decomposition = [_parse_step(item=item, backend_name=backend_name) for item in raw_steps]
        if not decomposition:
            msg = f"{backend_name} returned decomposition without any steps."
            raise BackendProtocolError(msg)
    elif result_kind is BackendResultKind.THEOREM_PLAN:
        raw_plan = payload.get("plan")
        theorem_plan = _parse_plan(item=raw_plan, backend_name=backend_name)
    elif result_kind is BackendResultKind.THEOREM_SESSION_ACTIONS:
        raw_actions = payload.get("actions")
        if not isinstance(raw_actions, list):
            msg = f"{backend_name} output is missing an `actions` list."
            raise BackendProtocolError(msg)
        theorem_session_actions = [
            _parse_theorem_session_action(item=action, backend_name=backend_name) for action in raw_actions
        ]
        if not theorem_session_actions:
            msg = f"{backend_name} returned theorem_session_actions without any actions."
            raise BackendProtocolError(msg)
    return (
        result_kind,
        payload,
        candidates,
        candidate_parse_errors,
        decomposition,
        theorem_plan,
        theorem_session_actions,
        message,
    )


def _load_json_payload(*, raw_output: str, backend_name: str) -> dict[str, JsonValue]:
    text = raw_output.strip()
    if not text:
        msg = f"{backend_name} produced empty output."
        raise BackendProtocolError(msg)
    json_candidates = [*_json_lines(text), text]
    for candidate in reversed(json_candidates):
        try:
            loaded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        payload = _extract_payload(loaded)
        if isinstance(payload, dict):
            return cast(dict[str, JsonValue], payload)
    msg = f"{backend_name} did not produce a valid JSON payload."
    raise BackendProtocolError(msg)


def _json_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip().startswith("{")]


def _extract_payload(loaded: object) -> object:
    if isinstance(loaded, dict):
        loaded_dict = cast(dict[str, object], loaded)
        payload = loaded_dict.get("payload")
        if isinstance(payload, dict):
            return payload
        result = loaded_dict.get("result")
        if isinstance(result, dict):
            return result
    return loaded


def _parse_candidate(*, item: object, backend_name: str) -> PatchCandidate:
    if not isinstance(item, dict):
        msg = f"{backend_name} candidate must be an object."
        raise BackendProtocolError(msg)
    item_dict = cast(Mapping[str, object], item)
    candidate_id = _read_required_string(item_dict, "candidate_id", backend_name)
    theorem = _read_required_string(item_dict, "theorem_name", backend_name)
    body = _read_required_string(item_dict, "body", backend_name)
    replay_from = _read_optional_string(item_dict, "replay_from")
    return PatchCandidate(
        candidate_id=candidate_id,
        theorem=theorem,
        body=body,
        source=f"{backend_name}:strict",
        replay_from=replay_from,
    )


def _parse_step(*, item: object, backend_name: str) -> DecompositionStep:
    if not isinstance(item, dict):
        msg = f"{backend_name} decomposition step must be an object."
        raise BackendProtocolError(msg)
    item_dict = cast(Mapping[str, object], item)
    layer_value = _read_required_string(item_dict, "layer", backend_name)
    try:
        layer = DecompositionLayer(layer_value)
    except ValueError as error:
        msg = f"{backend_name} decomposition step has unknown layer: {layer_value!r}."
        raise BackendProtocolError(msg) from error
    return DecompositionStep(
        step_id=_read_required_string(item_dict, "step_id", backend_name),
        title=_read_required_string(item_dict, "title", backend_name),
        goal=_read_required_string(item_dict, "goal", backend_name),
        layer=layer,
        helper_name=_read_optional_string(item_dict, "helper_name"),
        helper_statement=_read_optional_string(item_dict, "helper_statement"),
        helper_proof=_read_optional_string(item_dict, "helper_proof"),
        helper_imports=_read_optional_string_list(item_dict, "helper_imports", backend_name),
        file_hint=_read_optional_string(item_dict, "file_hint"),
    )


def _parse_plan(*, item: object, backend_name: str) -> TheoremPlan:
    if not isinstance(item, dict):
        msg = f"{backend_name} theorem plan must be an object."
        raise BackendProtocolError(msg)
    item_dict = cast(Mapping[str, object], item)
    raw_steps = item_dict.get("steps")
    if not isinstance(raw_steps, list):
        msg = f"{backend_name} theorem plan is missing a `steps` list."
        raise BackendProtocolError(msg)
    steps = [_parse_plan_step(item=step, backend_name=backend_name) for step in raw_steps]
    if not steps:
        msg = f"{backend_name} theorem plan must include at least one step."
        raise BackendProtocolError(msg)
    return TheoremPlan(
        summary=_read_required_string(item_dict, "summary", backend_name),
        strategy=_read_required_string(item_dict, "strategy", backend_name),
        steps=steps,
        helper_gaps=_read_optional_string_list(item_dict, "helper_gaps", backend_name),
        risks=_read_optional_string_list(item_dict, "risks", backend_name),
    )


def _parse_plan_step(*, item: object, backend_name: str) -> TheoremPlanStep:
    if not isinstance(item, dict):
        msg = f"{backend_name} theorem plan step must be an object."
        raise BackendProtocolError(msg)
    item_dict = cast(Mapping[str, object], item)
    return TheoremPlanStep(
        step_id=_read_required_string(item_dict, "step_id", backend_name),
        title=_read_required_string(item_dict, "title", backend_name),
        goal=_read_required_string(item_dict, "goal", backend_name),
        rationale=_read_required_string(item_dict, "rationale", backend_name),
        depends_on=_read_optional_string_list(item_dict, "depends_on", backend_name),
        helper_suggestions=_read_optional_string_list(item_dict, "helper_suggestions", backend_name),
        retrieval_refs=_read_optional_string_list(item_dict, "retrieval_refs", backend_name),
        file_actions=_read_optional_string_list(item_dict, "file_actions", backend_name),
    )


def _parse_theorem_session_action(*, item: object, backend_name: str) -> TheoremSessionAction:
    if not isinstance(item, dict):
        msg = f"{backend_name} theorem session action must be an object."
        raise BackendProtocolError(msg)
    item_dict = cast(Mapping[str, object], item)
    action_id = _read_required_string(item_dict, "action_id", backend_name)
    kind_value = _read_required_string(item_dict, "kind", backend_name)
    try:
        kind = TheoremSessionActionKind(kind_value)
    except ValueError as error:
        msg = f"{backend_name} theorem session action has unknown kind: {kind_value!r}."
        raise BackendProtocolError(msg) from error
    rationale = _read_optional_string(item_dict, "rationale")
    if kind is TheoremSessionActionKind.APPEND_TACTIC_TEXT:
        return AppendTacticTextAction(
            action_id=action_id,
            kind=kind,
            tactic_text=_read_required_string(item_dict, "tactic_text", backend_name),
            rationale=rationale,
        )
    if kind is TheoremSessionActionKind.REPLACE_THEOREM_DRAFT:
        return ReplaceTheoremDraftAction(
            action_id=action_id,
            kind=kind,
            theorem_name=_read_required_string(item_dict, "theorem_name", backend_name),
            body=_read_required_string(item_dict, "body", backend_name),
            rationale=rationale,
        )
    if kind is TheoremSessionActionKind.PROPOSE_HELPER_LEMMA:
        helper_item = item_dict.get("helper")
        if not isinstance(helper_item, dict):
            msg = f"{backend_name} helper lemma action is missing a `helper` object."
            raise BackendProtocolError(msg)
        helper_dict = cast(Mapping[str, object], helper_item)
        return ProposeHelperLemmaAction(
            action_id=action_id,
            kind=kind,
            helper=ProposedHelperLemma(
                helper_name=_read_required_string(helper_dict, "helper_name", backend_name),
                helper_statement=_read_required_string(helper_dict, "helper_statement", backend_name),
                proof_stub=_read_required_string(helper_dict, "proof_stub", backend_name),
                helper_imports=_read_optional_string_list(helper_dict, "helper_imports", backend_name),
            ),
            rationale=rationale,
        )
    if kind is TheoremSessionActionKind.SUGGEST_CHECKPOINT_LABEL:
        return SuggestCheckpointLabelAction(
            action_id=action_id,
            kind=kind,
            label=_read_required_string(item_dict, "label", backend_name),
            rationale=rationale,
        )
    return RecommendStrategyShiftAction(
        action_id=action_id,
        kind=kind,
        strategy=_read_required_string(item_dict, "strategy", backend_name),
        rationale=_read_required_string(item_dict, "rationale", backend_name),
    )


def _read_required_string(payload: Mapping[str, object], key: str, backend_name: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        msg = f"{backend_name} output field `{key}` must be a non-empty string."
        raise BackendProtocolError(msg)
    return value


def _read_optional_string(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        msg = f"Output field `{key}` must be a string when present."
        raise BackendProtocolError(msg)
    return value


def _read_optional_string_list(payload: Mapping[str, object], key: str, backend_name: str) -> list[str]:
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"{backend_name} output field `{key}` must be a string list when present."
        raise BackendProtocolError(msg)
    return cast(list[str], value)
