"""Lean JSON diagnostic normalization and classification."""

import json
import re
from pathlib import Path
from typing import cast

from lean_proofctl.engine.models import (
    Diagnostic,
    DiagnosticClass,
    JsonValue,
    Position,
    Range,
    RelatedDiagnostic,
    Severity,
)

_UNKNOWN_IDENTIFIER_RE = re.compile(
    r"unknown (?:constant|identifier|namespace|module prefix|package)\s+[`'\u2018\u2019\u201c\u201d]?"
    r"([^`'\u2018\u2019\u201c\u201d\s]+)[`'\u2018\u2019\u201c\u201d]?",
    re.IGNORECASE,
)
_INVALID_FIELD_RE = re.compile(
    r"invalid field\s+[`'\u2018\u2019\u201c\u201d]?([^`'\u2018\u2019\u201c\u201d\s]+)[`'\u2018\u2019\u201c\u201d]?",
    re.IGNORECASE,
)
_UNEXPECTED_TOKEN_RE = re.compile(
    r"unexpected(?:\s+\w+)?\s+[`'\u2018\u2019\u201c\u201d]?([^`'\u2018\u2019\u201c\u201d\n]+)"
    r"[`'\u2018\u2019\u201c\u201d]?",
    re.IGNORECASE,
)
_RESOURCE_EXHAUSTION_PATTERNS = (
    "maximum number of heartbeats",
    "maximum recursion depth",
    "exceeded the max recursion depth",
    "deep recursion was detected",
    "exceeded maxHeartbeats",
)
_TYPE_MISMATCH_PATTERNS = (
    "type mismatch",
    "application type mismatch",
    "has type",
    "is expected to have type",
)
_TACTIC_FAILURE_PATTERNS = (
    "tactic",
    "simp made no progress",
    "rewrite tactic failed",
    "linarith failed",
    "ring_nf failed",
    "omega failed",
    "aesop failed",
    "no goals to be solved",
)


def normalize_json_diagnostics(stdout: str, project_root: Path) -> tuple[list[Diagnostic], list[str]]:
    """Parse Lean JSON lines into the stable diagnostic schema."""
    diagnostics: list[Diagnostic] = []
    parse_errors: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            parse_errors.append(raw_line)
            continue
        if not isinstance(payload, dict):
            parse_errors.append(raw_line)
            continue
        diagnostics.append(normalize_diagnostic(payload, project_root))
    return diagnostics, parse_errors


def normalize_diagnostic(payload: dict[str, JsonValue], project_root: Path) -> Diagnostic:
    """Normalize one Lean JSON diagnostic object."""
    file_name = _normalize_file(payload.get("fileName"), project_root)
    pos = _position(payload.get("pos"))
    end_pos = _position_or_none(payload.get("endPos")) or pos
    caption = _as_optional_str(payload.get("caption"))
    kind = _as_optional_str(payload.get("kind"))
    message = _as_str(payload.get("data"))
    severity = _severity(payload.get("severity"))
    related = _related(payload.get("relatedInformation"), project_root)
    class_name = classify_diagnostic(kind=kind, message=message, severity=severity)
    identifier, subexpression = extract_failure_details(kind=kind, message=message, class_name=class_name)
    return Diagnostic(
        file=file_name,
        range=Range(
            startLine=pos.line,
            startCol=pos.column,
            endLine=end_pos.line,
            endCol=end_pos.column,
        ),
        severity=severity,
        message=message,
        related=related,
        class_name=class_name,
        kind=kind,
        caption=caption,
        identifier=identifier,
        subexpression=subexpression,
    )


def classify_diagnostic(*, kind: str | None, message: str, severity: Severity) -> DiagnosticClass:
    """Map Lean diagnostics onto a stable class set."""
    text = message.lower()
    kind_text = (kind or "").lower()
    if "timeout" in text:
        return DiagnosticClass.TIMEOUT
    if any(pattern in text for pattern in _RESOURCE_EXHAUSTION_PATTERNS):
        return DiagnosticClass.RESOURCE_EXHAUSTION
    if "unsolved goals" in text or kind_text == "tactic.unsolvedgoals":
        return DiagnosticClass.UNSOLVED_GOALS
    if kind_text.startswith("tactic.") or any(pattern in text for pattern in _TACTIC_FAILURE_PATTERNS):
        return DiagnosticClass.TACTIC_FAILURE
    if "unknown package" in text or "unknown module prefix" in text or ("import" in text and "error" in severity.value):
        return DiagnosticClass.IMPORT
    if (
        "unknown constant" in text
        or "unknown identifier" in text
        or "unknown namespace" in text
        or "invalid field" in text
    ):
        return DiagnosticClass.UNKNOWN_IDENTIFIER
    if "unexpected end of input" in text or text.startswith("unexpected ") or "parser" in kind_text:
        return DiagnosticClass.SYNTAX_ERROR
    if any(pattern in text for pattern in _TYPE_MISMATCH_PATTERNS):
        return DiagnosticClass.TYPE_MISMATCH
    return DiagnosticClass.OTHER


def extract_failure_details(
    *, kind: str | None, message: str, class_name: DiagnosticClass
) -> tuple[str | None, str | None]:
    """Extract conservative failure details from one Lean diagnostic message."""
    identifier = _extract_identifier(message)
    if class_name is DiagnosticClass.UNKNOWN_IDENTIFIER:
        return identifier, None
    if class_name is DiagnosticClass.SYNTAX_ERROR:
        return identifier, _extract_syntax_subject(message)
    if class_name is DiagnosticClass.TYPE_MISMATCH:
        return identifier, _extract_type_mismatch_subject(message)
    if class_name in {DiagnosticClass.TACTIC_FAILURE, DiagnosticClass.UNSOLVED_GOALS}:
        return identifier, _extract_goal_or_subject(message)
    if class_name in {DiagnosticClass.TIMEOUT, DiagnosticClass.RESOURCE_EXHAUSTION}:
        return identifier, _extract_goal_or_subject(message)
    return identifier, None


def timeout_diagnostic(file_name: str, timeout_seconds: float) -> Diagnostic:
    """Create a synthetic timeout diagnostic."""
    message = f"Lean check timed out after {timeout_seconds:.2f}s"
    return Diagnostic(
        file=file_name,
        range=Range(startLine=1, startCol=0, endLine=1, endCol=0),
        severity=Severity.ERROR,
        message=message,
        related=[],
        class_name=DiagnosticClass.TIMEOUT,
        kind="timeout",
        caption="timeout",
    )


def _extract_identifier(message: str) -> str | None:
    for pattern in (_UNKNOWN_IDENTIFIER_RE, _INVALID_FIELD_RE):
        match = pattern.search(message)
        if match is not None:
            return match.group(1)
    return None


def _extract_syntax_subject(message: str) -> str | None:
    match = _UNEXPECTED_TOKEN_RE.search(message)
    if match is not None:
        return match.group(1).strip() or None
    return _first_relevant_message_line(message)


def _extract_type_mismatch_subject(message: str) -> str | None:
    return _first_relevant_message_line(message)


def _extract_goal_or_subject(message: str) -> str | None:
    for line in message.splitlines():
        stripped = line.strip()
        if stripped.startswith("⊢"):
            return stripped
    return _first_relevant_message_line(message)


def _first_relevant_message_line(message: str) -> str | None:
    skip = {
        "argument",
        "has type",
        "but is expected to have type",
        "type mismatch",
        "application type mismatch",
        "unsolved goals",
    }
    for line in message.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower() in skip:
            continue
        if stripped.lower().startswith("case "):
            continue
        return stripped
    return None


def _normalize_file(raw: object, project_root: Path) -> str:
    if not isinstance(raw, str) or not raw:
        return "<unknown>"
    path = Path(raw).resolve()
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)


def _position(raw: object) -> Position:
    if not isinstance(raw, dict):
        return Position(line=1, column=0)
    mapping = cast(dict[str, object], raw)
    line = mapping.get("line")
    column = mapping.get("column")
    return Position(
        line=line if isinstance(line, int) else 1,
        column=column if isinstance(column, int) else 0,
    )


def _position_or_none(raw: object) -> Position | None:
    if raw is None:
        return None
    return _position(raw)


def _severity(raw: object) -> Severity:
    if raw == "warning":
        return Severity.WARNING
    if raw == "information":
        return Severity.INFO
    return Severity.ERROR


def _related(raw: object, project_root: Path) -> list[RelatedDiagnostic]:
    if not isinstance(raw, list):
        return []
    normalized: list[RelatedDiagnostic] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        mapping = cast(dict[str, object], item)
        start = _position(mapping.get("pos"))
        end = _position_or_none(mapping.get("endPos")) or start
        normalized.append(
            RelatedDiagnostic(
                file=_normalize_file(mapping.get("fileName"), project_root),
                range=Range(
                    startLine=start.line,
                    startCol=start.column,
                    endLine=end.line,
                    endCol=end.column,
                ),
                message=_as_str(mapping.get("data")),
            )
        )
    return normalized


def _as_str(raw: object) -> str:
    return raw if isinstance(raw, str) else ""


def _as_optional_str(raw: object) -> str | None:
    return raw if isinstance(raw, str) else None
