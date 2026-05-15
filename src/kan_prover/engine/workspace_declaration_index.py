"""Workspace declaration index built from compiled Lean modules."""

import json
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import cast

from kan_prover.engine.config import ToolPaths, WorkspaceBinding
from kan_prover.engine.lean_helpers import (
    ProofctlWorkspaceDeclarationIndex,
    ProofctlWorkspaceDeclarationIndexEntry,
)
from kan_prover.engine.proofctl import ProbeStatus, workspace_declaration_index
from kan_prover.engine.semantic_cache import semantic_cache_root
from kan_prover.engine.semantic_probe_runtime import semantic_probe_runtime_fingerprint

IDENT_RE = re.compile(r"[A-Za-z_][\w.']*")
SYMBOLIC_HEADS = {
    "/\\": "And",
    "\u2227": "And",
    "\\/": "Or",
    "\u2228": "Or",
    "=": "Eq",
    "↔": "Iff",
    "->": "arrow",
    "→": "arrow",
    "Σ": "Sigma",
    "Π": "Pi",
    "∀": "Forall",
    "∃": "Exists",
}
STOPWORDS = {
    "by",
    "def",
    "end",
    "exact",
    "fun",
    "have",
    "import",
    "in",
    "lemma",
    "let",
    "match",
    "namespace",
    "open",
    "private",
    "protected",
    "scoped",
    "show",
    "theorem",
    "unsafe",
    "where",
}
INDEX_SCHEMA_VERSION = "workspace-declaration-index.v1"


@dataclass(frozen=True)
class LocalDeclaration:
    """One retrieval declaration candidate."""

    name: str
    declaration_name: str
    namespace: str | None
    file_path: Path
    module_name: str | None
    kind: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    text: str
    statement: str
    theorem_tokens: frozenset[str]
    constants: frozenset[str]
    type_heads: frozenset[str]


_INDEX_MEMORY_CACHE: dict[str, tuple[LocalDeclaration, ...]] = {}


@dataclass(frozen=True)
class WorkspaceModuleEntry:
    """One workspace module included in the compiled declaration index."""

    file_path: Path
    module_path: str


def load_workspace_declaration_index(
    *,
    workspace_root: Path,
    target_file: Path,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    imports: tuple[str, ...] = (),
    namespace: str | None = None,
) -> list[LocalDeclaration]:
    """Load the persisted workspace declaration index and filter it to the retrieval frontier."""
    frontier_files = set(
        _workspace_declaration_files(
            workspace_root=workspace_root,
            target_file=target_file,
            artifact_root=tool_paths.artifacts.resolve(),
            imports=imports,
            namespace=namespace,
        )
    )
    if not frontier_files:
        return []
    index_entries = _workspace_index_entries(
        workspace_root=workspace_root,
        tool_paths=tool_paths,
        workspace=workspace,
        target_file=target_file,
    )
    return [entry for entry in index_entries if entry.file_path in frontier_files]


def workspace_frontier_files(
    *,
    workspace_root: Path,
    target_file: Path,
    tool_paths: ToolPaths | None,
    imports: tuple[str, ...] = (),
    namespace: str | None = None,
) -> list[Path]:
    """Return the widened retrieval frontier file list."""
    artifact_root = None if tool_paths is None else tool_paths.artifacts.resolve()
    return _workspace_declaration_files(
        workspace_root=workspace_root,
        target_file=target_file,
        artifact_root=artifact_root,
        imports=imports,
        namespace=namespace,
    )


def load_declaration_texts(*, declarations: list[LocalDeclaration]) -> dict[tuple[Path, str], str]:
    """Read exact declaration source text lazily for a small declaration shortlist."""
    by_file: dict[Path, list[LocalDeclaration]] = {}
    for declaration in declarations:
        by_file.setdefault(declaration.file_path, []).append(declaration)
    resolved: dict[tuple[Path, str], str] = {}
    for file_path, file_declarations in by_file.items():
        try:
            source_text = file_path.read_text(encoding="utf-8")
        except OSError:
            continue
        for declaration in file_declarations:
            snippet = _source_slice(source_text=source_text, declaration=declaration)
            if snippet is not None:
                resolved[(declaration.file_path, declaration.name)] = snippet
    return resolved


def clear_workspace_index_memory_cache() -> None:
    """Drop the in-process workspace declaration index cache."""
    _INDEX_MEMORY_CACHE.clear()


def _workspace_index_entries(
    *,
    workspace_root: Path,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    target_file: Path,
) -> tuple[LocalDeclaration, ...]:
    modules = _workspace_module_manifest(workspace_root=workspace_root, tool_paths=tool_paths)
    if not modules:
        return ()
    cache_key = _cache_key(workspace=workspace, tool_paths=tool_paths, modules=modules)
    cache_id = sha256(_stable_json(cache_key).encode("utf-8")).hexdigest()
    cached = _INDEX_MEMORY_CACHE.get(cache_id)
    if cached is not None:
        return cached
    record_path = _record_path(tool_paths=tool_paths, cache_id=cache_id)
    cached = _read_cached_index(record_path=record_path, expected_key=cache_key)
    if cached is not None:
        _INDEX_MEMORY_CACHE[cache_id] = cached
        return cached
    built = _build_workspace_index(
        tool_paths=tool_paths,
        workspace=workspace,
        target_file=target_file,
        modules=modules,
    )
    with suppress(OSError):
        _write_cached_index(record_path=record_path, cache_key=cache_key, declarations=built)
    _INDEX_MEMORY_CACHE[cache_id] = built
    return built


def _build_workspace_index(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    target_file: Path,
    modules: list[WorkspaceModuleEntry],
) -> tuple[LocalDeclaration, ...]:
    manifest_payload = [
        {
            "file_path": str(entry.file_path),
            "module_path": entry.module_path,
        }
        for entry in modules
    ]
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as manifest:
        manifest.write(json.dumps(manifest_payload))
        manifest_path = Path(manifest.name)
    try:
        probe = workspace_declaration_index(
            tool_paths=tool_paths,
            workspace=workspace,
            file_path=target_file,
            manifest_path=manifest_path,
        )
    finally:
        manifest_path.unlink(missing_ok=True)
    if probe.status is not ProbeStatus.OK or probe.result is None:
        return ()
    index = cast(ProofctlWorkspaceDeclarationIndex, probe.result)
    declarations = sorted(
        (_declaration_from_index_entry(entry) for entry in index.entries),
        key=lambda item: (
            str(item.file_path),
            item.start_line,
            item.start_column,
            item.declaration_name,
        ),
    )
    return tuple(declarations)


def _declaration_from_index_entry(entry: ProofctlWorkspaceDeclarationIndexEntry) -> LocalDeclaration:
    statement = f"{entry.declaration_kind} {entry.short_name} : {entry.type_text}"
    return LocalDeclaration(
        name=entry.short_name,
        declaration_name=entry.declaration_name,
        namespace=entry.namespace,
        file_path=Path(entry.file_path).expanduser().resolve(),
        module_name=entry.module_path,
        kind=entry.declaration_kind,
        start_line=entry.declaration_span.start.line,
        start_column=entry.declaration_span.start.column,
        end_line=entry.declaration_span.end.line,
        end_column=entry.declaration_span.end.column,
        text=statement,
        statement=statement,
        theorem_tokens=frozenset(_name_tokens(entry.short_name)),
        constants=frozenset(_extract_constants(statement)),
        type_heads=frozenset(_extract_type_heads(statement)),
    )


def _workspace_module_manifest(*, workspace_root: Path, tool_paths: ToolPaths) -> list[WorkspaceModuleEntry]:
    artifact_root = tool_paths.artifacts.resolve()
    managed_root = (workspace_root / "KanProverGenerated").resolve()
    modules: list[WorkspaceModuleEntry] = []
    for path in sorted(workspace_root.rglob("*.lean")):
        resolved = path.resolve()
        if ".lake" in resolved.parts:
            continue
        if resolved.is_relative_to(artifact_root) or resolved.is_relative_to(managed_root):
            continue
        module_path = _module_name_for_path(workspace_root=workspace_root, file_path=resolved)
        if module_path is None:
            continue
        modules.append(WorkspaceModuleEntry(file_path=resolved, module_path=module_path))
    return modules


def _cache_key(
    *,
    workspace: WorkspaceBinding,
    tool_paths: ToolPaths,
    modules: list[WorkspaceModuleEntry],
) -> dict[str, object]:
    return {
        "workspace": {
            "package": workspace.package_name,
            "toolchain": workspace.toolchain,
            "lakefile": _path_digest(workspace.lakefile),
            "manifest": _optional_path_digest(workspace.root / "lake-manifest.json"),
        },
        "runtime_fingerprint": semantic_probe_runtime_fingerprint(tool_paths),
        "modules": [
            {
                "module_path": entry.module_path,
                "logical_path": _logical_path(path=entry.file_path, workspace=workspace),
                "mtime_ns": entry.file_path.stat().st_mtime_ns,
                "size": entry.file_path.stat().st_size,
            }
            for entry in modules
        ],
    }


def _record_path(*, tool_paths: ToolPaths, cache_id: str) -> Path:
    return semantic_cache_root(tool_paths) / INDEX_SCHEMA_VERSION / f"{cache_id}.json"


def _read_cached_index(
    *,
    record_path: Path,
    expected_key: dict[str, object],
) -> tuple[LocalDeclaration, ...] | None:
    if not record_path.is_file():
        return None
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != INDEX_SCHEMA_VERSION:
        return None
    if payload.get("key") != expected_key:
        return None
    rows = payload.get("entries")
    if not isinstance(rows, list):
        return None
    declarations: list[LocalDeclaration] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        declaration = _decode_cached_declaration(cast(dict[str, object], row))
        if declaration is None:
            return None
        declarations.append(declaration)
    return tuple(declarations)


def _write_cached_index(
    *,
    record_path: Path,
    cache_key: dict[str, object],
    declarations: tuple[LocalDeclaration, ...],
) -> None:
    payload = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "key": cache_key,
        "entries": [_encode_cached_declaration(declaration) for declaration in declarations],
    }
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(_stable_json(payload), encoding="utf-8")


def _encode_cached_declaration(declaration: LocalDeclaration) -> dict[str, object]:
    return {
        "name": declaration.name,
        "declaration_name": declaration.declaration_name,
        "namespace": declaration.namespace,
        "file_path": str(declaration.file_path),
        "module_name": declaration.module_name,
        "kind": declaration.kind,
        "start_line": declaration.start_line,
        "start_column": declaration.start_column,
        "end_line": declaration.end_line,
        "end_column": declaration.end_column,
        "text": declaration.text,
        "statement": declaration.statement,
    }


def _decode_cached_declaration(payload: dict[str, object]) -> LocalDeclaration | None:
    name = payload.get("name")
    declaration_name = payload.get("declaration_name")
    file_path = payload.get("file_path")
    module_name = payload.get("module_name")
    kind = payload.get("kind")
    statement = payload.get("statement")
    text = payload.get("text")
    start_line = payload.get("start_line")
    start_column = payload.get("start_column")
    end_line = payload.get("end_line")
    end_column = payload.get("end_column")
    namespace = payload.get("namespace")
    if not all(isinstance(item, str) for item in [name, declaration_name, file_path, kind, statement, text]):
        return None
    if module_name is not None and not isinstance(module_name, str):
        return None
    if namespace is not None and not isinstance(namespace, str):
        return None
    if not all(isinstance(item, int) for item in [start_line, start_column, end_line, end_column]):
        return None
    typed_name = cast(str, name)
    typed_statement = cast(str, statement)
    return LocalDeclaration(
        name=typed_name,
        declaration_name=cast(str, declaration_name),
        namespace=namespace,
        file_path=Path(cast(str, file_path)).expanduser().resolve(),
        module_name=module_name,
        kind=cast(str, kind),
        start_line=cast(int, start_line),
        start_column=cast(int, start_column),
        end_line=cast(int, end_line),
        end_column=cast(int, end_column),
        text=cast(str, text),
        statement=typed_statement,
        theorem_tokens=frozenset(_name_tokens(typed_name)),
        constants=frozenset(_extract_constants(typed_statement)),
        type_heads=frozenset(_extract_type_heads(typed_statement)),
    )


def _source_slice(*, source_text: str, declaration: LocalDeclaration) -> str | None:
    try:
        start = _offset_for_point(source_text, declaration.start_line, declaration.start_column)
        end = _offset_for_point(source_text, declaration.end_line, declaration.end_column)
    except ValueError:
        return None
    snippet = source_text[start:end].strip()
    return snippet or None


def _module_name_for_path(*, workspace_root: Path, file_path: Path) -> str | None:
    try:
        relative = file_path.relative_to(workspace_root)
    except ValueError:
        return None
    return ".".join(relative.with_suffix("").parts)


def _workspace_declaration_files(
    *,
    workspace_root: Path,
    target_file: Path,
    artifact_root: Path | None,
    imports: tuple[str, ...],
    namespace: str | None,
) -> list[Path]:
    candidates: set[Path] = set()
    for root in _workspace_frontier_roots(
        workspace_root=workspace_root,
        target_file=target_file,
        imports=imports,
        namespace=namespace,
    ):
        candidates.update(root.rglob("*.lean"))
    for module_name in imports:
        module_path = _module_name_to_path(workspace_root=workspace_root, module_name=module_name)
        if module_path is not None:
            candidates.add(module_path)
    managed_root = (workspace_root / "KanProverGenerated").resolve()
    files: list[Path] = []
    for path in sorted(candidates):
        resolved_path = path.resolve()
        if resolved_path == target_file or ".lake" in resolved_path.parts:
            continue
        if artifact_root is not None and resolved_path.is_relative_to(artifact_root):
            continue
        if resolved_path.is_relative_to(managed_root):
            continue
        files.append(resolved_path)
    return files


def _workspace_frontier_roots(
    *,
    workspace_root: Path,
    target_file: Path,
    imports: tuple[str, ...],
    namespace: str | None,
) -> list[Path]:
    roots: list[Path] = []
    roots.extend(_module_ancestry_roots(workspace_root=workspace_root, file_path=target_file))
    for module_name in imports:
        module_path = _module_name_to_path(workspace_root=workspace_root, module_name=module_name)
        if module_path is not None:
            roots.extend(_module_ancestry_roots(workspace_root=workspace_root, file_path=module_path))
    if namespace is not None:
        roots.extend(_namespace_roots(workspace_root=workspace_root, namespace=namespace))
    return _normalize_frontier_roots(workspace_root=workspace_root, roots=roots)


def _module_ancestry_roots(*, workspace_root: Path, file_path: Path) -> list[Path]:
    try:
        relative = file_path.relative_to(workspace_root)
    except ValueError:
        return []
    roots: list[Path] = []
    current = workspace_root
    for segment in relative.parent.parts:
        current = current / segment
        roots.append(current)
    return roots


def _namespace_roots(*, workspace_root: Path, namespace: str) -> list[Path]:
    roots: list[Path] = []
    current = workspace_root
    for segment in namespace.split("."):
        current = current / segment
        if current.is_dir():
            roots.append(current)
        else:
            break
    return roots


def _normalize_frontier_roots(*, workspace_root: Path, roots: list[Path]) -> list[Path]:
    normalized: list[Path] = []
    resolved_roots = {path.resolve() for path in roots if path.is_dir()}
    for root in sorted(resolved_roots, key=lambda path: (len(path.parts), str(path))):
        if root == workspace_root:
            continue
        if any(root.is_relative_to(existing) for existing in normalized):
            continue
        normalized.append(root)
    return normalized


def _module_name_to_path(*, workspace_root: Path, module_name: str) -> Path | None:
    candidate = workspace_root.joinpath(*module_name.split(".")).with_suffix(".lean")
    return candidate.resolve() if candidate.is_file() else None


def _name_tokens(name: str) -> set[str]:
    expanded = re.sub(r"(?<!^)(?=[A-Z])", "_", name.replace(".", "_"))
    return {token.lower() for token in re.split(r"[^A-Za-z0-9]+", expanded) if token}


def _extract_constants(text: str) -> set[str]:
    constants: set[str] = set()
    for token in IDENT_RE.findall(text):
        lowered = token.lower()
        if lowered in STOPWORDS or len(token) <= 1:
            continue
        constants.add(token)
    return constants


def _extract_type_heads(text: str) -> set[str]:
    heads: set[str] = set()
    for symbol, head in SYMBOLIC_HEADS.items():
        if symbol in text:
            heads.add(head)
    match = IDENT_RE.search(text)
    if match is not None:
        heads.add(match.group(0))
    return heads


def _offset_for_point(source: str, line: int, column: int) -> int:
    if line < 1 or column < 0:
        msg = f"Invalid source point {line}:{column}."
        raise ValueError(msg)
    current_line = 1
    offset = 0
    for line_text in source.splitlines(keepends=True):
        if current_line == line:
            line_body = line_text.rstrip("\n")
            if column > len(line_body):
                msg = f"Column {column} is outside line {line}."
                raise ValueError(msg)
            return offset + column
        offset += len(line_text)
        current_line += 1
    if current_line == line:
        return offset + column
    msg = f"Line {line} is outside the source file."
    raise ValueError(msg)


def _logical_path(*, path: Path, workspace: WorkspaceBinding) -> str:
    resolved = path.expanduser().resolve()
    try:
        return f"workspace:{resolved.relative_to(workspace.root.resolve())}"
    except ValueError:
        return str(resolved)


def _path_digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _optional_path_digest(path: Path) -> str | None:
    return _path_digest(path) if path.is_file() else None


def _stable_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
