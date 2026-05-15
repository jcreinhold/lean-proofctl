"""Source-only theorem and import probes for in-memory or no-workspace use."""

import re
from dataclasses import dataclass
from pathlib import Path

from lean_proofctl.engine.lean_helpers import (
    ProbeCandidate,
    ProbeLayer,
    ProbePoint,
    ProbeResult,
    ProbeSpan,
    ProbeStatus,
)
from lean_proofctl.engine.proofctl import ProofctlImportsAndNamespace, ProofctlTheoremSpan

IMPORT_RE = re.compile(r"(?m)^import\s+(?P<modules>[^\n]+)$")
NAMESPACE_RE = re.compile(r"(?m)^namespace\s+(?P<name>[A-Za-z_][\w.']*(?:\.[A-Za-z_][\w.']*)*)\s*$")
SECTION_RE = re.compile(r"(?m)^section(?:\s+[A-Za-z_][\w.']*)?\s*$")
END_RE = re.compile(r"(?m)^end(?:\s+[A-Za-z_][\w.']*(?:\.[A-Za-z_][\w.']*)*)?\s*$")
TOP_LEVEL_COMMAND_RE = re.compile(
    r"(?m)^"
    r"(?:@\[[^\n]+\]\s+)*"
    r"(?:(?:private|protected|noncomputable|unsafe|partial|scoped|local)\s+)*"
    r"[A-Za-z_][\w.']*\b"
)
DECLARATION_RE = re.compile(
    r"(?m)^"
    r"(?P<prefix>(?:@\[[^\n]+\]\s+)*)"
    r"(?:(?:private|protected|noncomputable|unsafe|partial|scoped|local)\s+)*"
    r"(?P<keyword>theorem|lemma)\s+"
    r"(?P<name>[A-Za-z_][\w.']*)\b"
)


@dataclass(frozen=True)
class _ScopeFrame:
    kind: str
    namespace_before: list[str]


@dataclass(frozen=True)
class _SyntaxEntry:
    keyword: str
    short_name: str
    declaration_name: str
    namespace: str | None
    namespace_stack: list[str]
    declaration_start: int
    name_start: int
    name_end: int
    declaration_end: int
    body_start: int


@dataclass(frozen=True)
class _ParsedSource:
    imports: list[str]
    entries: list[_SyntaxEntry]


def theorem_span_from_source(*, file_path: Path, source: str, theorem: str) -> ProbeResult:
    """Run the source-only theorem-span probe on in-memory Lean source."""
    return _source_probe(command="theorem-span", file_path=file_path, theorem=theorem, source=source)


def imports_and_namespace_from_source(*, file_path: Path, source: str, theorem: str) -> ProbeResult:
    """Run the source-only imports-and-namespace probe on in-memory Lean source."""
    return _source_probe(command="imports-and-namespace", file_path=file_path, theorem=theorem, source=source)


def _source_probe(
    *,
    command: str,
    file_path: Path,
    theorem: str,
    source: str | None = None,
) -> ProbeResult:
    resolved_file = str(file_path.expanduser().resolve())
    source_text: str
    try:
        source_text = source if source is not None else file_path.read_text(encoding="utf-8")
        parsed = _parse_source(source_text)
    except FileNotFoundError as error:
        return ProbeResult(
            layer=ProbeLayer.SYNTAX,
            command=command,
            status=ProbeStatus.ERROR,
            file=resolved_file,
            theorem=theorem,
            message=str(error),
        )
    except UnicodeDecodeError as error:
        return ProbeResult(
            layer=ProbeLayer.SYNTAX,
            command=command,
            status=ProbeStatus.ERROR,
            file=resolved_file,
            theorem=theorem,
            message=f"Could not decode Lean source as UTF-8: {error!s}",
        )
    except ValueError as error:
        return ProbeResult(
            layer=ProbeLayer.SYNTAX,
            command=command,
            status=ProbeStatus.ERROR,
            file=resolved_file,
            theorem=theorem,
            message=str(error),
        )

    matches = _match_entries(parsed.entries, theorem)
    if not matches:
        return ProbeResult(
            layer=ProbeLayer.SYNTAX,
            command=command,
            status=ProbeStatus.MISSING,
            file=resolved_file,
            theorem=theorem,
        )
    if len(matches) > 1:
        return ProbeResult(
            layer=ProbeLayer.SYNTAX,
            command=command,
            status=ProbeStatus.AMBIGUOUS,
            file=resolved_file,
            theorem=theorem,
            candidates=[_candidate(entry, source_text) for entry in matches],
        )

    entry = matches[0]
    if command == "imports-and-namespace":
        return ProbeResult(
            layer=ProbeLayer.SYNTAX,
            command=command,
            status=ProbeStatus.OK,
            file=resolved_file,
            theorem=theorem,
            result=ProofctlImportsAndNamespace(
                declaration_name=entry.declaration_name,
                namespace=entry.namespace,
                namespace_stack=entry.namespace_stack,
                imports=parsed.imports,
            ),
        )
    if command != "theorem-span":
        return ProbeResult(
            layer=ProbeLayer.SYNTAX,
            command=command,
            status=ProbeStatus.ERROR,
            file=resolved_file,
            theorem=theorem,
            message=f"Unknown syntax probe `{command}`.",
        )
    return ProbeResult(
        layer=ProbeLayer.SYNTAX,
        command=command,
        status=ProbeStatus.OK,
        file=resolved_file,
        theorem=theorem,
        result=ProofctlTheoremSpan(
            keyword=entry.keyword,
            declaration_name=entry.declaration_name,
            namespace=entry.namespace,
            namespace_stack=entry.namespace_stack,
            declaration_span=_span_from_offsets(entry.declaration_start, entry.declaration_end, source_text),
            selection_span=_span_from_offsets(entry.name_start, entry.name_end, source_text),
            body_span=_span_from_offsets(entry.body_start, entry.declaration_end, source_text),
        ),
    )


def _parse_source(source: str) -> _ParsedSource:
    masked = _mask_comments_and_strings(source)
    events = _command_events(masked)
    command_starts = sorted({match.start() for match in TOP_LEVEL_COMMAND_RE.finditer(masked)})
    imports: list[str] = []
    frames: list[_ScopeFrame] = []
    namespace_stack: list[str] = []
    entries: list[_SyntaxEntry] = []

    for start, kind, match in events:
        if kind == "import":
            modules = [token for token in match.group("modules").split() if token not in {"hiding", "renaming"}]
            imports.extend(modules)
            continue
        if kind == "namespace":
            frames.append(_ScopeFrame(kind="namespace", namespace_before=list(namespace_stack)))
            namespace_stack = [*namespace_stack, *match.group("name").split(".")]
            continue
        if kind == "section":
            frames.append(_ScopeFrame(kind="section", namespace_before=list(namespace_stack)))
            continue
        if kind == "end":
            if frames:
                namespace_stack = list(frames.pop().namespace_before)
            continue
        if kind != "declaration":
            raise ValueError(f"Unknown command kind `{kind}`.")

        declaration_end = next((point for point in command_starts if point > start), len(source))
        body_marker = masked.find(":=", match.end(), declaration_end)
        if body_marker < 0:
            msg = (
                f"Could not find an unambiguous `:=` body marker for "
                f"`{match.group('name')}` before the next declaration."
            )
            raise ValueError(msg)
        body_start = body_marker + 2
        while body_start < declaration_end and source[body_start].isspace():
            body_start += 1
        if body_start >= declaration_end:
            msg = f"Found an empty theorem body for `{match.group('name')}`."
            raise ValueError(msg)
        short_name = match.group("name")
        namespace = ".".join(namespace_stack) or None
        full_components = [*namespace_stack, *short_name.split(".")]
        declaration_name = ".".join(full_components)
        entries.append(
            _SyntaxEntry(
                keyword=match.group("keyword"),
                short_name=short_name,
                declaration_name=declaration_name,
                namespace=namespace,
                namespace_stack=list(namespace_stack),
                declaration_start=start,
                name_start=match.start("name"),
                name_end=match.end("name"),
                declaration_end=declaration_end,
                body_start=body_start,
            )
        )
    return _ParsedSource(imports=imports, entries=entries)


def _command_events(masked_source: str) -> list[tuple[int, str, re.Match[str]]]:
    events: list[tuple[int, str, re.Match[str]]] = []
    events.extend((match.start(), "import", match) for match in IMPORT_RE.finditer(masked_source))
    events.extend((match.start(), "namespace", match) for match in NAMESPACE_RE.finditer(masked_source))
    events.extend((match.start(), "section", match) for match in SECTION_RE.finditer(masked_source))
    events.extend((match.start(), "end", match) for match in END_RE.finditer(masked_source))
    events.extend((match.start(), "declaration", match) for match in DECLARATION_RE.finditer(masked_source))
    return sorted(events, key=lambda item: item[0])


def _mask_comments_and_strings(source: str) -> str:
    chars = list(source)
    block_depth = 0
    in_string = False
    line_comment = False
    index = 0
    while index < len(chars):
        current = source[index]
        next_two = source[index : index + 2]
        if line_comment:
            if current == "\n":
                line_comment = False
            else:
                chars[index] = " "
            index += 1
            continue
        if in_string:
            if current == "\\" and index + 1 < len(chars):
                chars[index] = " "
                chars[index + 1] = " "
                index += 2
                continue
            chars[index] = " " if current != "\n" else current
            if current == '"':
                in_string = False
            index += 1
            continue
        if block_depth > 0:
            if next_two == "/-":
                chars[index] = " "
                if index + 1 < len(chars):
                    chars[index + 1] = " "
                block_depth += 1
                index += 2
                continue
            if next_two == "-/":
                chars[index] = " "
                if index + 1 < len(chars):
                    chars[index + 1] = " "
                block_depth -= 1
                index += 2
                continue
            chars[index] = " " if current != "\n" else current
            index += 1
            continue
        if next_two == "--":
            chars[index] = " "
            if index + 1 < len(chars):
                chars[index + 1] = " "
            line_comment = True
            index += 2
            continue
        if next_two == "/-":
            chars[index] = " "
            if index + 1 < len(chars):
                chars[index + 1] = " "
            block_depth = 1
            index += 2
            continue
        if current == '"':
            chars[index] = " "
            in_string = True
            index += 1
            continue
        index += 1
    return "".join(chars)


def _match_entries(entries: list[_SyntaxEntry], theorem: str) -> list[_SyntaxEntry]:
    full_matches = [entry for entry in entries if entry.declaration_name == theorem]
    if full_matches:
        return full_matches
    return [entry for entry in entries if entry.short_name == theorem]


def _candidate(entry: _SyntaxEntry, source: str) -> ProbeCandidate:
    return ProbeCandidate(
        declaration_name=entry.declaration_name,
        short_name=entry.short_name,
        namespace=entry.namespace,
        namespace_stack=entry.namespace_stack,
        keyword=entry.keyword,
        declaration_span=_span_from_offsets(entry.declaration_start, entry.declaration_end, source),
    )


def _span_from_offsets(start: int, end: int, source_or_path: str | Path) -> ProbeSpan:
    source = (
        source_or_path
        if isinstance(source_or_path, str)
        else source_or_path.expanduser().resolve().read_text(encoding="utf-8")
    )
    return ProbeSpan(start=_point_from_offset(source, start), end=_point_from_offset(source, end))


def _point_from_offset(source: str, offset: int) -> ProbePoint:
    if offset < 0 or offset > len(source):
        msg = f"Offset {offset} is outside the source file."
        raise ValueError(msg)
    line = source.count("\n", 0, offset) + 1
    line_start = source.rfind("\n", 0, offset)
    column = offset if line_start < 0 else offset - line_start - 1
    return ProbePoint(line=line, column=column)
