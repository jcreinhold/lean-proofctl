"""Classify strict theorem-body backend responses for engine loops."""

from dataclasses import dataclass, replace

from kan_prover.engine.models import (
    AgentResponse,
    BackendResultKind,
    PatchCandidate,
    TheoremBodyClassification,
    TheoremBodyResponseClass,
)


@dataclass(frozen=True)
class ClassifiedTheoremBodyResponse:
    """One theorem-body response plus its usable candidates."""

    response: AgentResponse
    usable_candidates: list[PatchCandidate]


def classify_theorem_body_response(*, response: AgentResponse, expected_theorem: str) -> ClassifiedTheoremBodyResponse:
    """Classify one backend theorem-body response without weakening parsing."""
    errors = list(response.candidate_parse_errors)
    usable_candidates: list[PatchCandidate] = []
    rejected_candidate_count = 0

    if response.result_kind is BackendResultKind.NO_PROGRESS:
        classification = TheoremBodyClassification(
            response_class=TheoremBodyResponseClass.NO_PROGRESS,
            usable_candidate_count=0,
            rejected_candidate_count=0,
            invalid_candidate_count=len(response.candidate_parse_errors),
            message=response.message or "backend reported no progress",
            errors=errors,
        )
        return ClassifiedTheoremBodyResponse(
            response=replace(response, theorem_body_classification=classification),
            usable_candidates=[],
        )

    if response.result_kind is not BackendResultKind.THEOREM_BODY_REPLACEMENTS:
        classification = TheoremBodyClassification(
            response_class=TheoremBodyResponseClass.UNUSABLE_RESPONSE,
            usable_candidate_count=0,
            rejected_candidate_count=0,
            invalid_candidate_count=len(response.candidate_parse_errors),
            message=(
                f"backend returned `{response.result_kind.value}` for a theorem-body request "
                f"that expected theorem-body replacements"
            ),
            errors=errors,
        )
        return ClassifiedTheoremBodyResponse(
            response=replace(response, theorem_body_classification=classification),
            usable_candidates=[],
        )

    for candidate in response.candidates:
        if candidate.theorem != expected_theorem:
            rejected_candidate_count += 1
            errors.append(
                f"candidate `{candidate.candidate_id}` targeted theorem `{candidate.theorem}` "
                f"instead of `{expected_theorem}`"
            )
            continue
        usable_candidates.append(candidate)

    if not usable_candidates:
        classification = TheoremBodyClassification(
            response_class=TheoremBodyResponseClass.NO_USABLE_CANDIDATE,
            usable_candidate_count=0,
            rejected_candidate_count=rejected_candidate_count,
            invalid_candidate_count=len(response.candidate_parse_errors),
            message="backend returned theorem-body replacements without a usable candidate",
            errors=errors,
        )
    elif len(usable_candidates) == 1:
        classification = TheoremBodyClassification(
            response_class=TheoremBodyResponseClass.SINGLE_USABLE_CANDIDATE,
            usable_candidate_count=1,
            rejected_candidate_count=rejected_candidate_count,
            invalid_candidate_count=len(response.candidate_parse_errors),
            selected_candidate_id=usable_candidates[0].candidate_id,
            errors=errors,
        )
    else:
        classification = TheoremBodyClassification(
            response_class=TheoremBodyResponseClass.MULTIPLE_USABLE_CANDIDATES,
            usable_candidate_count=len(usable_candidates),
            rejected_candidate_count=rejected_candidate_count,
            invalid_candidate_count=len(response.candidate_parse_errors),
            message="backend returned multiple usable theorem-body candidates",
            errors=errors,
        )

    return ClassifiedTheoremBodyResponse(
        response=replace(response, theorem_body_classification=classification),
        usable_candidates=usable_candidates,
    )
