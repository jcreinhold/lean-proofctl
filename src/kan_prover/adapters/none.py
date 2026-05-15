"""Deterministic strict backend used for tests and replay."""

import json
from pathlib import Path
from typing import cast

from kan_prover.engine.models import (
    AgentRequest,
    AgentResponse,
    AppendTacticTextAction,
    BackendInvocation,
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


class NoneBackend:
    """A bounded backend that replays stored candidates or returns no progress."""

    def __init__(
        self,
        *,
        candidate_files: list[Path] | None = None,
        replay_attempts: list[Path] | None = None,
        decomposition_files: list[Path] | None = None,
        plan_files: list[Path] | None = None,
        theorem_step_files: list[Path] | None = None,
    ) -> None:
        """Store explicit candidate and replay inputs for deterministic strict calls."""
        self._candidate_files = candidate_files or []
        self._replay_attempts = replay_attempts or []
        self._decomposition_files = decomposition_files or []
        self._plan_files = plan_files or []
        self._theorem_step_files = theorem_step_files or []

    def name(self) -> str:
        """Return the backend identifier."""
        return "none"

    def propose(self, request: AgentRequest) -> AgentResponse:
        """Load at most one theorem-body candidate for the current attempt."""
        if request.task_kind is TaskKind.DECOMPOSE:
            return self._propose_decomposition(request)
        if request.task_kind is TaskKind.PLAN:
            return self._propose_plan(request)
        if request.task_kind is TaskKind.THEOREM_STEP:
            return self._propose_theorem_step(request)
        source_index = request.attempt_index - 1
        candidate = self._load_candidate(request=request, source_index=source_index)
        parsed_payload: dict[str, JsonValue]
        candidates: list[PatchCandidate]
        if candidate is None:
            parsed_payload = {
                "schema_version": "v1",
                "task_kind": request.task_kind.value,
                "result_kind": BackendResultKind.NO_PROGRESS.value,
                "message": "no configured candidate for this attempt",
            }
            candidates = []
        else:
            parsed_payload = {
                "schema_version": "v1",
                "task_kind": request.task_kind.value,
                "result_kind": BackendResultKind.THEOREM_BODY_REPLACEMENTS.value,
                "candidates": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "theorem_name": candidate.theorem,
                        "body": candidate.body,
                        "replay_from": candidate.replay_from,
                    }
                ],
            }
            candidates = [candidate]
        raw_output = json.dumps(parsed_payload, indent=2, sort_keys=True)
        return AgentResponse(
            backend=self.name(),
            result_kind=(BackendResultKind.THEOREM_BODY_REPLACEMENTS if candidates else BackendResultKind.NO_PROGRESS),
            invocation=BackendInvocation(argv=["none", f"attempt-{request.attempt_index:03d}"], cwd="."),
            prompt_path="",
            prompt_text="",
            schema_path=request.schema_path,
            raw_stdout=raw_output + "\n",
            raw_stderr="",
            raw_output=raw_output + "\n",
            parsed_payload=parsed_payload,
            candidates=candidates,
            message=None if candidates else "no configured candidate for this attempt",
        )

    def _propose_plan(self, request: AgentRequest) -> AgentResponse:
        source_index = request.attempt_index - 1
        theorem_plan = self._load_plan(source_index=source_index)
        if theorem_plan is None:
            parsed_payload: dict[str, JsonValue] = {
                "schema_version": "v1",
                "task_kind": request.task_kind.value,
                "result_kind": BackendResultKind.NO_PROGRESS.value,
                "message": "no configured theorem plan for this attempt",
            }
        else:
            parsed_payload = cast(
                dict[str, JsonValue],
                {
                    "schema_version": "v1",
                    "task_kind": request.task_kind.value,
                    "result_kind": BackendResultKind.THEOREM_PLAN.value,
                    "plan": {
                        "summary": theorem_plan.summary,
                        "strategy": theorem_plan.strategy,
                        "steps": [
                            {
                                "step_id": step.step_id,
                                "title": step.title,
                                "goal": step.goal,
                                "rationale": step.rationale,
                                "depends_on": step.depends_on,
                                "helper_suggestions": step.helper_suggestions,
                                "retrieval_refs": step.retrieval_refs,
                                "file_actions": step.file_actions,
                            }
                            for step in theorem_plan.steps
                        ],
                        "helper_gaps": theorem_plan.helper_gaps,
                        "risks": theorem_plan.risks,
                    },
                },
            )
        raw_output = json.dumps(parsed_payload, indent=2, sort_keys=True)
        return AgentResponse(
            backend=self.name(),
            result_kind=BackendResultKind.THEOREM_PLAN if theorem_plan is not None else BackendResultKind.NO_PROGRESS,
            invocation=BackendInvocation(argv=["none", f"plan-{request.attempt_index:03d}"], cwd="."),
            prompt_path="",
            prompt_text="",
            schema_path=request.schema_path,
            raw_stdout=raw_output + "\n",
            raw_stderr="",
            raw_output=raw_output + "\n",
            parsed_payload=parsed_payload,
            theorem_plan=theorem_plan,
            message=None if theorem_plan is not None else "no configured theorem plan for this attempt",
        )

    def _propose_theorem_step(self, request: AgentRequest) -> AgentResponse:
        source_index = request.attempt_index - 1
        theorem_session_actions = self._load_theorem_step_actions(source_index=source_index)
        if theorem_session_actions:
            parsed_payload = cast(
                dict[str, JsonValue],
                {
                    "schema_version": "v1",
                    "task_kind": request.task_kind.value,
                    "result_kind": BackendResultKind.THEOREM_SESSION_ACTIONS.value,
                    "actions": [_theorem_step_action_payload(action) for action in theorem_session_actions],
                },
            )
        else:
            parsed_payload = cast(
                dict[str, JsonValue],
                {
                    "schema_version": "v1",
                    "task_kind": request.task_kind.value,
                    "result_kind": BackendResultKind.NO_PROGRESS.value,
                    "message": "no configured theorem step actions for this attempt",
                },
            )
        raw_output = json.dumps(parsed_payload, indent=2, sort_keys=True)
        return AgentResponse(
            backend=self.name(),
            result_kind=(
                BackendResultKind.THEOREM_SESSION_ACTIONS if theorem_session_actions else BackendResultKind.NO_PROGRESS
            ),
            invocation=BackendInvocation(argv=["none", f"theorem-step-{request.attempt_index:03d}"], cwd="."),
            prompt_path="",
            prompt_text="",
            schema_path=request.schema_path,
            raw_stdout=raw_output + "\n",
            raw_stderr="",
            raw_output=raw_output + "\n",
            parsed_payload=parsed_payload,
            theorem_session_actions=theorem_session_actions,
            message=None if theorem_session_actions else "no configured theorem step actions for this attempt",
        )

    def _propose_decomposition(self, request: AgentRequest) -> AgentResponse:
        source_index = request.attempt_index - 1
        steps = self._load_decomposition(source_index=source_index)
        payload: object
        if steps:
            payload = {
                "schema_version": "v1",
                "task_kind": request.task_kind.value,
                "result_kind": BackendResultKind.DECOMPOSITION.value,
                "steps": [
                    {
                        "step_id": step.step_id,
                        "title": step.title,
                        "goal": step.goal,
                        "layer": step.layer.value,
                        "helper_name": step.helper_name,
                        "helper_statement": step.helper_statement,
                        "helper_proof": step.helper_proof,
                        "helper_imports": step.helper_imports,
                        "file_hint": step.file_hint,
                    }
                    for step in steps
                ],
            }
        else:
            payload = {
                "schema_version": "v1",
                "task_kind": request.task_kind.value,
                "result_kind": BackendResultKind.NO_PROGRESS.value,
                "message": "no configured decomposition for this attempt",
            }
        parsed_payload = cast(dict[str, JsonValue], payload)
        raw_output = json.dumps(parsed_payload, indent=2, sort_keys=True)
        return AgentResponse(
            backend=self.name(),
            result_kind=BackendResultKind.DECOMPOSITION if steps else BackendResultKind.NO_PROGRESS,
            invocation=BackendInvocation(argv=["none", f"decompose-{request.attempt_index:03d}"], cwd="."),
            prompt_path="",
            prompt_text="",
            schema_path=request.schema_path,
            raw_stdout=raw_output + "\n",
            raw_stderr="",
            raw_output=raw_output + "\n",
            parsed_payload=parsed_payload,
            decomposition=steps,
            message=None if steps else "no configured decomposition for this attempt",
        )

    def _load_plan(self, *, source_index: int) -> TheoremPlan | None:
        if source_index < 0 or source_index >= len(self._plan_files):
            return None
        payload = json.loads(self._plan_files[source_index].read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            msg = f"Plan file must be a JSON object: {self._plan_files[source_index]}"
            raise ValueError(msg)
        plan = payload.get("plan")
        if not isinstance(plan, dict):
            msg = f"Plan file is missing `plan`: {self._plan_files[source_index]}"
            raise ValueError(msg)
        steps = plan.get("steps")
        if not isinstance(steps, list):
            msg = f"Plan file is missing `plan.steps`: {self._plan_files[source_index]}"
            raise ValueError(msg)
        return TheoremPlan(
            summary=str(plan["summary"]),
            strategy=str(plan["strategy"]),
            steps=[
                TheoremPlanStep(
                    step_id=str(step["step_id"]),
                    title=str(step["title"]),
                    goal=str(step["goal"]),
                    rationale=str(step["rationale"]),
                    depends_on=[item for item in step.get("depends_on", []) if isinstance(item, str)],
                    helper_suggestions=[item for item in step.get("helper_suggestions", []) if isinstance(item, str)],
                    retrieval_refs=[item for item in step.get("retrieval_refs", []) if isinstance(item, str)],
                    file_actions=[item for item in step.get("file_actions", []) if isinstance(item, str)],
                )
                for step in steps
                if isinstance(step, dict)
            ],
            helper_gaps=[item for item in plan.get("helper_gaps", []) if isinstance(item, str)],
            risks=[item for item in plan.get("risks", []) if isinstance(item, str)],
        )

    def _load_candidate(self, *, request: AgentRequest, source_index: int) -> PatchCandidate | None:
        if source_index < len(self._candidate_files):
            path = self._candidate_files[source_index]
            body = path.read_text(encoding="utf-8")
            return PatchCandidate(
                candidate_id=f"candidate-{request.attempt_index:03d}",
                theorem=request.theorem_name,
                body=body,
                source=f"none:file:{path}",
            )
        replay_index = source_index - len(self._candidate_files)
        if replay_index < 0 or replay_index >= len(self._replay_attempts):
            return None
        path = self._replay_attempts[replay_index]
        payload = json.loads(path.read_text(encoding="utf-8"))
        theorem = payload.get("theorem")
        body = payload.get("body")
        if not isinstance(theorem, str) or not isinstance(body, str):
            msg = f"Replay attempt file is missing `theorem` or `body`: {path}"
            raise ValueError(msg)
        return PatchCandidate(
            candidate_id=f"candidate-{request.attempt_index:03d}",
            theorem=theorem,
            body=body,
            source=f"none:replay:{path}",
            replay_from=str(path),
        )

    def _load_decomposition(self, *, source_index: int) -> list[DecompositionStep]:
        if source_index < 0 or source_index >= len(self._decomposition_files):
            return []
        payload = json.loads(self._decomposition_files[source_index].read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            msg = f"Decomposition file must be a JSON object: {self._decomposition_files[source_index]}"
            raise ValueError(msg)
        steps = payload.get("steps")
        if not isinstance(steps, list):
            msg = f"Decomposition file is missing `steps`: {self._decomposition_files[source_index]}"
            raise ValueError(msg)
        return [
            DecompositionStep(
                step_id=str(step["step_id"]),
                title=str(step["title"]),
                goal=str(step["goal"]),
                layer=DecompositionLayer(str(step["layer"])),
                helper_name=step.get("helper_name"),
                helper_statement=step.get("helper_statement"),
                helper_proof=step.get("helper_proof"),
                helper_imports=[item for item in step.get("helper_imports", []) if isinstance(item, str)],
                file_hint=step.get("file_hint"),
            )
            for step in steps
            if isinstance(step, dict)
        ]

    def _load_theorem_step_actions(self, *, source_index: int) -> list[TheoremSessionAction]:
        if source_index < 0 or source_index >= len(self._theorem_step_files):
            return []
        payload = json.loads(self._theorem_step_files[source_index].read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            msg = f"Theorem step file must be a JSON object: {self._theorem_step_files[source_index]}"
            raise ValueError(msg)
        actions = payload.get("actions")
        if not isinstance(actions, list):
            msg = f"Theorem step file is missing `actions`: {self._theorem_step_files[source_index]}"
            raise ValueError(msg)
        parsed_actions: list[TheoremSessionAction] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            kind = TheoremSessionActionKind(str(action["kind"]))
            rationale = action.get("rationale")
            if rationale is not None and not isinstance(rationale, str):
                rationale = None
            if kind is TheoremSessionActionKind.APPEND_TACTIC_TEXT:
                parsed_actions.append(
                    AppendTacticTextAction(
                        action_id=str(action["action_id"]),
                        kind=kind,
                        tactic_text=str(action["tactic_text"]),
                        rationale=rationale,
                    )
                )
            elif kind is TheoremSessionActionKind.REPLACE_THEOREM_DRAFT:
                parsed_actions.append(
                    ReplaceTheoremDraftAction(
                        action_id=str(action["action_id"]),
                        kind=kind,
                        theorem_name=str(action["theorem_name"]),
                        body=str(action["body"]),
                        rationale=rationale,
                    )
                )
            elif kind is TheoremSessionActionKind.PROPOSE_HELPER_LEMMA:
                helper = action.get("helper")
                if not isinstance(helper, dict):
                    msg = f"Theorem step helper action is missing `helper`: {self._theorem_step_files[source_index]}"
                    raise ValueError(msg)
                parsed_actions.append(
                    ProposeHelperLemmaAction(
                        action_id=str(action["action_id"]),
                        kind=kind,
                        helper=ProposedHelperLemma(
                            helper_name=str(helper["helper_name"]),
                            helper_statement=str(helper["helper_statement"]),
                            proof_stub=str(helper["proof_stub"]),
                            helper_imports=[item for item in helper.get("helper_imports", []) if isinstance(item, str)],
                        ),
                        rationale=rationale,
                    )
                )
            elif kind is TheoremSessionActionKind.SUGGEST_CHECKPOINT_LABEL:
                parsed_actions.append(
                    SuggestCheckpointLabelAction(
                        action_id=str(action["action_id"]),
                        kind=kind,
                        label=str(action["label"]),
                        rationale=rationale,
                    )
                )
            else:
                parsed_actions.append(
                    RecommendStrategyShiftAction(
                        action_id=str(action["action_id"]),
                        kind=kind,
                        strategy=str(action["strategy"]),
                        rationale=str(action["rationale"]),
                    )
                )
        return parsed_actions


def _theorem_step_action_payload(action: TheoremSessionAction) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "action_id": action.action_id,
        "kind": action.kind.value,
    }
    if isinstance(action, AppendTacticTextAction):
        payload["tactic_text"] = action.tactic_text
        if action.rationale is not None:
            payload["rationale"] = action.rationale
        return payload
    if isinstance(action, ReplaceTheoremDraftAction):
        payload["theorem_name"] = action.theorem_name
        payload["body"] = action.body
        if action.rationale is not None:
            payload["rationale"] = action.rationale
        return payload
    if isinstance(action, ProposeHelperLemmaAction):
        payload["helper"] = {
            "helper_name": action.helper.helper_name,
            "helper_statement": action.helper.helper_statement,
            "proof_stub": action.helper.proof_stub,
            "helper_imports": action.helper.helper_imports,
        }
        if action.rationale is not None:
            payload["rationale"] = action.rationale
        return payload
    if isinstance(action, SuggestCheckpointLabelAction):
        payload["label"] = action.label
        if action.rationale is not None:
            payload["rationale"] = action.rationale
        return payload
    assert isinstance(action, RecommendStrategyShiftAction)
    payload["strategy"] = action.strategy
    payload["rationale"] = action.rationale
    return payload
