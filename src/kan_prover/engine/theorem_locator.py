"""Theorem body span resolution from already selected theorem metadata."""

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from kan_prover.engine.proofctl import (
    ProbeLayer,
    ProbeResult,
    ProbeStatus,
    ProofctlDeclarationTarget,
    ProofctlTheoremSpan,
    theorem_span,
)


class TheoremLocatorError(ValueError):
    """Raised when a theorem body span cannot be identified safely."""


@dataclass(frozen=True)
class TheoremSpan:
    """A conservative source span for one theorem or lemma body."""

    keyword: str
    name: str
    declaration_start: int
    body_start: int
    body_end: int


def locate_theorem_span_in_file(
    *,
    target_file: Path,
    source: str,
    theorem_name: str,
) -> TheoremSpan:
    """Resolve one theorem span from the portable syntax probe contract."""
    probe = theorem_span(file_path=target_file, theorem=theorem_name)
    return locate_theorem_span_from_probe(source=source, theorem_name=theorem_name, probe=probe)


def locate_theorem_span_from_probe(*, source: str, theorem_name: str, probe: ProbeResult) -> TheoremSpan:
    """Resolve one theorem span from an already computed probe result."""
    status = probe.status
    if status is ProbeStatus.MISSING:
        msg = f"Could not find theorem or lemma named `{theorem_name}`."
        raise TheoremLocatorError(msg)
    if status is ProbeStatus.AMBIGUOUS:
        candidate_names = ", ".join(candidate.declaration_name for candidate in probe.candidates)
        msg = f"Found multiple theorem or lemma declarations named `{theorem_name}`: {candidate_names}."
        raise TheoremLocatorError(msg)
    if status is not ProbeStatus.OK or probe.result is None:
        detail = probe.message if getattr(probe, "message", None) else "probe failed"
        msg = f"Theorem probe failed for `{theorem_name}`: {detail}."
        raise TheoremLocatorError(msg)
    span_payload = cast(ProofctlTheoremSpan, probe.result)
    declaration_span = span_payload.declaration_span
    body_span = span_payload.body_span
    if span_payload.safe_edit_boundary is not None:
        declaration_span = span_payload.safe_edit_boundary.declaration_span
        body_span = span_payload.safe_edit_boundary.body_span
    return TheoremSpan(
        keyword=span_payload.keyword,
        name=span_payload.declaration_name,
        declaration_start=_offset_for_point(source, declaration_span.start.line, declaration_span.start.column),
        body_start=_offset_for_point(source, body_span.start.line, body_span.start.column),
        body_end=_offset_for_point(source, body_span.end.line, body_span.end.column),
    )


def locate_theorem_span_from_target(
    *, source: str, theorem_name: str, target: ProofctlDeclarationTarget
) -> TheoremSpan:
    """Resolve one theorem span directly from Lean-backed declaration metadata."""
    return locate_theorem_span_from_probe(
        source=source,
        theorem_name=theorem_name,
        probe=ProbeResult(
            layer=ProbeLayer.SEMANTIC,
            command="theorem-span",
            status=ProbeStatus.OK,
            file="<declaration-target>",
            theorem=theorem_name,
            result=ProofctlTheoremSpan(
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
            ),
        ),
    )


def _offset_for_point(source: str, line: int, column: int) -> int:
    if line < 1 or column < 0:
        msg = f"Invalid source point {line}:{column}."
        raise TheoremLocatorError(msg)
    current_line = 1
    offset = 0
    for line_text in source.splitlines(keepends=True):
        if current_line == line:
            line_body = line_text.rstrip("\n")
            if column > len(line_body):
                msg = f"Column {column} is outside line {line}."
                raise TheoremLocatorError(msg)
            return offset + column
        offset += len(line_text)
        current_line += 1
    if current_line == line:
        return offset + column
    msg = f"Line {line} is outside the source file."
    raise TheoremLocatorError(msg)
