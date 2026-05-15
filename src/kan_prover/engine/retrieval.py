"""Local, replayable retrieval over declarations and stored verifier artifacts."""

import json
import math
import re
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from os import environ
from pathlib import Path
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from kan_prover.engine.cache import StorePaths, read_jsonl
from kan_prover.engine.config import ToolPaths, WorkspaceBinding
from kan_prover.engine.lean_helpers import (
    ProbeStatus,
    ProofctlDeclarationInventory,
    ProofctlDeclarationMetadata,
)
from kan_prover.engine.models import (
    DiagnosticClass,
    HelperRef,
    RetrievalExplanation,
    RetrievalHit,
    RetrievalNearMiss,
    RetrievalScoreComponent,
    RetrievalSourceKind,
)
from kan_prover.engine.proofctl import declaration_inventory
from kan_prover.engine.workspace_declaration_index import (
    LocalDeclaration,
    load_declaration_texts,
    load_workspace_declaration_index,
    workspace_frontier_files,
)

NAMESPACE_RE = re.compile(r"^(?P<indent>\s*)namespace\s+(?P<name>[A-Za-z_][\w.']*)\s*$")
END_RE = re.compile(r"^(?P<indent>\s*)end(?:\s+(?P<name>[A-Za-z_][\w.']*))?\s*$")
IMPORT_RE = re.compile(r"(?m)^import\s+(?P<modules>[^\n]+)$")
DECLARATION_RE = re.compile(
    r"(?m)^"
    r"(?:(?:@\[[^\n]+\]\s+)*)"
    r"(?:(?:private|protected|noncomputable|unsafe|partial|scoped|local)\s+)*"
    r"(?P<keyword>theorem|lemma)\s+"
    r"(?P<name>[A-Za-z_][\w.']*)\b"
)
IDENT_RE = re.compile(r"[A-Za-z_][\w.']*")
GOAL_RE = re.compile(r"⊢\s*(?P<goal>.+)")

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

_EMBEDDING_TEXT_CACHE: dict[tuple[str, str], dict[str, tuple[float, ...]]] = {}
_NEAR_MISS_SOURCE_KINDS = {
    RetrievalSourceKind.SAME_FILE,
    RetrievalSourceKind.SAME_NAMESPACE,
    RetrievalSourceKind.GOAL_DECLARATION,
}
_DEFAULT_EMBEDDING_PROVIDER = "openai-compatible"
_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
_DEFAULT_EMBEDDING_ENDPOINT = "https://api.openai.com/v1/embeddings"
_DEFAULT_EMBEDDING_API_KEY_ENV = "OPENAI_API_KEY"
_DEFAULT_EMBEDDING_WEIGHT = 24


@dataclass(frozen=True)
class TargetProfile:
    """Goal-shaped retrieval features for the target theorem."""

    theorem: str
    theorem_tokens: frozenset[str]
    namespace: str | None
    namespace_segments: tuple[str, ...]
    imports: tuple[str, ...]
    goal_texts: tuple[str, ...]
    goal_constants: frozenset[str]
    type_heads: frozenset[str]


class EmbeddingRetriever(Protocol):
    """One explicit embedding provider used only as a retrieval ranking signal."""

    def provider_name(self) -> str:
        """Return one stable provider name for cache keys and explanations."""

    def model_name(self) -> str | None:
        """Return the configured model name when the provider exposes one."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""


@dataclass(frozen=True)
class EmbeddingRetrievalConfig:
    """Optional dense retrieval settings."""

    enabled: bool = False
    provider: str = _DEFAULT_EMBEDDING_PROVIDER
    model: str = _DEFAULT_EMBEDDING_MODEL
    endpoint: str = _DEFAULT_EMBEDDING_ENDPOINT
    api_key_env: str = _DEFAULT_EMBEDDING_API_KEY_ENV
    weight: int = _DEFAULT_EMBEDDING_WEIGHT

    @classmethod
    def from_environment(cls) -> EmbeddingRetrievalConfig:
        """Read optional embedding settings from the current process environment."""
        enabled = environ.get("KAN_PROVER_ENABLE_EMBEDDING_RETRIEVAL", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        provider = environ.get("KAN_PROVER_EMBEDDING_PROVIDER", _DEFAULT_EMBEDDING_PROVIDER).strip()
        model = environ.get("KAN_PROVER_EMBEDDING_MODEL", _DEFAULT_EMBEDDING_MODEL).strip()
        endpoint = environ.get("KAN_PROVER_EMBEDDING_ENDPOINT", _DEFAULT_EMBEDDING_ENDPOINT).strip()
        api_key_env = environ.get("KAN_PROVER_EMBEDDING_API_KEY_ENV", _DEFAULT_EMBEDDING_API_KEY_ENV).strip()
        weight_value = environ.get("KAN_PROVER_EMBEDDING_WEIGHT", str(_DEFAULT_EMBEDDING_WEIGHT)).strip()
        with suppress(ValueError):
            weight = int(weight_value)
            return cls(
                enabled=enabled,
                provider=provider or _DEFAULT_EMBEDDING_PROVIDER,
                model=model or _DEFAULT_EMBEDDING_MODEL,
                endpoint=endpoint or _DEFAULT_EMBEDDING_ENDPOINT,
                api_key_env=api_key_env or _DEFAULT_EMBEDDING_API_KEY_ENV,
                weight=max(1, weight),
            )
        return cls(
            enabled=enabled,
            provider=provider or _DEFAULT_EMBEDDING_PROVIDER,
            model=model or _DEFAULT_EMBEDDING_MODEL,
            endpoint=endpoint or _DEFAULT_EMBEDDING_ENDPOINT,
            api_key_env=api_key_env or _DEFAULT_EMBEDDING_API_KEY_ENV,
        )

    def cache_namespace(self) -> str:
        """Return one stable cache namespace for this provider configuration."""
        return f"{self.provider}|{self.model}|{self.endpoint}"


@dataclass(frozen=True)
class OpenAICompatibleEmbeddingRetriever:
    """Small guarded adapter for OpenAI-compatible embedding endpoints."""

    endpoint: str
    model: str
    api_key: str

    def provider_name(self) -> str:
        """Return the stable provider name used in retrieval explanations."""
        return _DEFAULT_EMBEDDING_PROVIDER

    def model_name(self) -> str | None:
        """Return the configured embedding model name."""
        return self.model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Fetch one embedding vector per text from an OpenAI-compatible endpoint."""
        if not texts:
            return []
        request = Request(
            self.endpoint,
            data=json.dumps({"input": texts, "model": self.model}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            msg = f"Embedding request failed with HTTP {error.code}: {detail}"
            raise RuntimeError(msg) from error
        except URLError as error:
            msg = f"Embedding request failed: {error.reason}"
            raise RuntimeError(msg) from error
        rows = payload.get("data")
        if not isinstance(rows, list):
            msg = "Embedding provider returned no `data` list."
            raise RuntimeError(msg)
        ordered_rows = sorted(rows, key=_embedding_row_index)
        embeddings: list[list[float]] = []
        for row in ordered_rows:
            embedding = cast(dict[str, object], row).get("embedding")
            if not isinstance(embedding, list) or not all(isinstance(value, int | float) for value in embedding):
                msg = "Embedding provider returned a malformed embedding vector."
                raise RuntimeError(msg)
            embeddings.append([float(cast(int | float, value)) for value in embedding])
        if len(embeddings) != len(texts):
            msg = "Embedding provider returned the wrong number of vectors."
            raise RuntimeError(msg)
        return embeddings


def collect_retrieval_hits(
    *,
    workspace_root: Path,
    target_module_path: Path,
    target_file: Path,
    theorem: str,
    source_text: str,
    store: StorePaths,
    failure_classes: list[str],
    theorem_statement: str | None = None,
    goal_snapshot: list[str] | None = None,
    imports: list[str] | None = None,
    namespace: str | None = None,
    tool_paths: ToolPaths | None = None,
    workspace: WorkspaceBinding | None = None,
    limit: int = 12,
    embedding_config: EmbeddingRetrievalConfig | None = None,
    embedding_retriever: EmbeddingRetriever | None = None,
) -> tuple[list[RetrievalHit], list[HelperRef]]:
    """Collect ranked retrieval hits and active helpers for one theorem."""
    explanation = collect_retrieval_explanation(
        workspace_root=workspace_root,
        target_module_path=target_module_path,
        target_file=target_file,
        theorem=theorem,
        source_text=source_text,
        store=store,
        failure_classes=failure_classes,
        theorem_statement=theorem_statement,
        goal_snapshot=goal_snapshot,
        imports=imports,
        namespace=namespace,
        tool_paths=tool_paths,
        workspace=workspace,
        hit_limit=limit,
        embedding_config=embedding_config,
        embedding_retriever=embedding_retriever,
    )
    return explanation.hits, explanation.active_helpers


def collect_retrieval_explanation(
    *,
    workspace_root: Path,
    target_module_path: Path,
    target_file: Path,
    theorem: str,
    source_text: str,
    store: StorePaths,
    failure_classes: list[str],
    theorem_statement: str | None = None,
    goal_snapshot: list[str] | None = None,
    imports: list[str] | None = None,
    namespace: str | None = None,
    tool_paths: ToolPaths | None = None,
    workspace: WorkspaceBinding | None = None,
    hit_limit: int = 12,
    near_miss_limit: int = 5,
    embedding_config: EmbeddingRetrievalConfig | None = None,
    embedding_retriever: EmbeddingRetriever | None = None,
) -> RetrievalExplanation:
    """
    Collect ranked retrieval hits plus nearby declaration misses from one ranking pass.

    Retrieval is intentionally local and replayable. Every hit must come either
    from the bound workspace, stored run artifacts, or managed helper outputs so
    a later replay/export can explain why that hit was available.
    """
    workspace_root = workspace_root.expanduser().resolve()
    target_file = target_file.expanduser().resolve()
    resolved_embedding_config = embedding_config or EmbeddingRetrievalConfig.from_environment()
    workspace_imports = tuple(_direct_imports(source_text) or (imports or []))
    declarations = _file_declarations(
        file_path=target_file,
        workspace_root=workspace_root,
        source_text=source_text,
        tool_paths=tool_paths,
        workspace=workspace,
    )
    target_declaration = next((item for item in declarations if item.name == theorem), None)
    target_namespace = namespace or _target_namespace(theorem=theorem, declaration=target_declaration)
    profile = _target_profile(
        theorem=theorem,
        theorem_statement=(theorem_statement or target_declaration.statement)
        if target_declaration
        else theorem_statement,
        goal_snapshot=goal_snapshot or [],
        imports=imports or [],
        namespace=target_namespace,
    )
    workspace_declarations = _workspace_declarations(
        workspace_root=workspace_root,
        target_file=target_file,
        tool_paths=tool_paths,
        workspace=workspace,
        imports=workspace_imports,
        namespace=target_namespace,
    )
    hits = [
        *_same_file_hits(declarations=declarations, theorem=theorem, profile=profile),
        *_same_namespace_hits(
            workspace_root=workspace_root,
            target_file=target_file,
            theorem=theorem,
            namespace=target_namespace,
            profile=profile,
            target_declarations=declarations,
            workspace_declarations=workspace_declarations,
        ),
        *_goal_declaration_hits(
            workspace_root=workspace_root,
            theorem=theorem,
            namespace=target_namespace,
            profile=profile,
            workspace_declarations=workspace_declarations,
        ),
        *_accepted_patch_hits(
            store=store,
            workspace_root=workspace_root,
            target_module_path=target_module_path,
            theorem=theorem,
        ),
        *_proved_helper_hits(
            store=store,
            workspace_root=workspace_root,
            target_module_path=target_module_path,
            theorem=theorem,
        ),
        *_proof_fragment_hits(
            store=store,
            workspace_root=workspace_root,
            target_module_path=target_module_path,
            theorem=theorem,
            profile=profile,
        ),
        *_graph_run_hits(
            store=store,
            workspace_root=workspace_root,
            target_module_path=target_module_path,
            theorem=theorem,
            profile=profile,
        ),
        *_failure_summary_hits(
            store=store,
            workspace_root=workspace_root,
            target_module_path=target_module_path,
            theorem=theorem,
            failure_classes=failure_classes,
        ),
    ]
    active_helpers = _active_helpers(
        store=store,
        workspace_root=workspace_root,
        target_module_path=target_module_path,
        theorem=theorem,
    )
    hits = _dedupe_hits(hits)
    if resolved_embedding_config.enabled:
        resolved_theorem_statement = (
            (theorem_statement or target_declaration.statement) if target_declaration else theorem_statement
        )
        retriever = embedding_retriever or _build_embedding_retriever(config=resolved_embedding_config)
        hits = _augment_hits_with_embedding_scores(
            workspace_root=workspace_root,
            theorem=theorem,
            theorem_statement=resolved_theorem_statement,
            goal_texts=profile.goal_texts,
            hits=hits,
            config=resolved_embedding_config,
            retriever=retriever,
        )
    hits.sort(key=lambda item: (-item.score, item.hit_id))
    returned_hits = _enrich_returned_declaration_hits(
        hits=hits[:hit_limit],
        workspace_root=workspace_root,
        declarations=workspace_declarations,
    )
    near_misses: list[RetrievalNearMiss] = []
    for hit in hits[hit_limit:]:
        if hit.source_kind not in _NEAR_MISS_SOURCE_KINDS:
            continue
        near_misses.append(
            RetrievalNearMiss(
                title=hit.title,
                source_kind=hit.source_kind,
                summary=hit.summary,
                score=hit.score,
                omission_reason=f"ranked below the returned top {hit_limit} hits",
                path=hit.path,
                theorem=hit.theorem,
                match_reasons=list(hit.match_reasons),
                score_breakdown=list(hit.score_breakdown),
            )
        )
        if len(near_misses) >= near_miss_limit:
            break
    return RetrievalExplanation(
        hit_limit=hit_limit,
        near_miss_limit=near_miss_limit,
        hits=returned_hits,
        near_misses=near_misses,
        active_helpers=active_helpers,
    )


def _scan_local_declarations(source_text: str, *, file_path: Path, workspace_root: Path) -> list[LocalDeclaration]:
    lines = source_text.splitlines()
    line_offsets = _line_offsets(source_text)
    namespace_stack: list[str] = []
    namespace_by_line: dict[int, str | None] = {}
    module_name = _module_name_for_path(workspace_root=workspace_root, file_path=file_path)
    for index, line in enumerate(lines, start=1):
        namespace_by_line[index] = ".".join(namespace_stack) if namespace_stack else None
        namespace_match = NAMESPACE_RE.match(line)
        if namespace_match:
            namespace_stack.extend(namespace_match.group("name").split("."))
            namespace_by_line[index] = ".".join(namespace_stack)
            continue
        end_match = END_RE.match(line)
        if end_match and namespace_stack:
            end_name = end_match.group("name")
            if end_name:
                for _segment in end_name.split("."):
                    if namespace_stack:
                        namespace_stack.pop()
            else:
                namespace_stack.pop()
    matches = list(DECLARATION_RE.finditer(source_text))
    declarations: list[LocalDeclaration] = []
    for index, match in enumerate(matches):
        start_offset = match.start()
        end_offset = matches[index + 1].start() if index + 1 < len(matches) else len(source_text)
        start_line = _line_for_offset(line_offsets, start_offset)
        end_line = _line_for_offset(line_offsets, end_offset)
        text = "\n".join(lines[start_line - 1 : end_line]).strip()
        statement = _statement_from_declaration(text)
        declarations.append(
            LocalDeclaration(
                name=match.group("name"),
                declaration_name=match.group("name"),
                namespace=namespace_by_line.get(start_line),
                file_path=file_path,
                module_name=module_name,
                kind="theorem",
                start_line=start_line,
                start_column=0,
                end_line=end_line,
                end_column=0,
                text=text,
                statement=statement,
                theorem_tokens=frozenset(_name_tokens(match.group("name"))),
                constants=frozenset(_extract_constants(statement)),
                type_heads=frozenset(_extract_type_heads(statement)),
            )
        )
    return declarations


def _direct_imports(source_text: str) -> list[str]:
    imports: list[str] = []
    for match in IMPORT_RE.finditer(source_text):
        imports.extend(module for module in match.group("modules").split() if module)
    return imports


def _file_declarations(
    *,
    file_path: Path,
    workspace_root: Path,
    source_text: str | None = None,
    tool_paths: ToolPaths | None = None,
    workspace: WorkspaceBinding | None = None,
) -> list[LocalDeclaration]:
    """
    Return declarations from Lean when possible, then fall back to source text.

    The Lean inventory is preferred because it respects the same workspace and
    parser rules that verification uses. The text scan remains as a degraded
    fallback so retrieval can still produce local context when semantic probing
    is unavailable.
    """
    if tool_paths is not None and workspace is not None:
        declarations = _lean_file_declarations(
            tool_paths=tool_paths,
            workspace=workspace,
            file_path=file_path,
            workspace_root=workspace_root,
        )
        if declarations is not None:
            return declarations
    if source_text is None:
        with suppress(OSError):
            source_text = file_path.read_text(encoding="utf-8")
    if source_text is None:
        return []
    return _scan_local_declarations(source_text, file_path=file_path, workspace_root=workspace_root)


def _lean_file_declarations(
    *,
    tool_paths: ToolPaths,
    workspace: WorkspaceBinding,
    file_path: Path,
    workspace_root: Path,
) -> list[LocalDeclaration] | None:
    """
    Decode declaration inventory into local retrieval rows for one file.

    Returning `None` means "fall back to source scanning", not "there are no
    declarations". That distinction lets callers keep retrieval available
    without pretending the semantic helper succeeded.
    """
    probe = declaration_inventory(
        tool_paths=tool_paths,
        workspace=workspace,
        file_path=file_path,
    )
    if probe.status is not ProbeStatus.OK or probe.result is None:
        return None
    inventory = cast(ProofctlDeclarationInventory, probe.result)
    with suppress(OSError):
        source_text = file_path.read_text(encoding="utf-8")
        return _inventory_to_local_declarations(
            inventory=inventory,
            file_path=file_path,
            workspace_root=workspace_root,
            source_text=source_text,
        )
    return None


def _inventory_to_local_declarations(
    *,
    inventory: ProofctlDeclarationInventory,
    file_path: Path,
    workspace_root: Path,
    source_text: str,
) -> list[LocalDeclaration]:
    declarations: list[LocalDeclaration] = []
    for entry in inventory.entries:
        declaration = _local_declaration_from_target(
            target=entry,
            file_path=file_path,
            workspace_root=workspace_root,
            source_text=source_text,
        )
        if declaration is not None:
            declarations.append(declaration)
    return declarations


def _inventory_entries_to_local_declarations(
    *,
    entries: tuple[ProofctlDeclarationMetadata, ...],
    file_path: Path,
    workspace_root: Path,
) -> list[LocalDeclaration]:
    with suppress(OSError):
        source_text = file_path.read_text(encoding="utf-8")
        declarations: list[LocalDeclaration] = []
        for entry in entries:
            declaration = _local_declaration_from_target(
                target=entry,
                file_path=file_path,
                workspace_root=workspace_root,
                source_text=source_text,
            )
            if declaration is not None:
                declarations.append(declaration)
        return declarations
    return []


def _local_declaration_from_target(
    *,
    target: ProofctlDeclarationMetadata,
    file_path: Path,
    workspace_root: Path,
    source_text: str,
) -> LocalDeclaration | None:
    try:
        declaration_start = _offset_for_point(
            source_text,
            target.declaration_span.start.line,
            target.declaration_span.start.column,
        )
        declaration_end = _offset_for_point(
            source_text,
            target.declaration_span.end.line,
            target.declaration_span.end.column,
        )
        body_start = _offset_for_point(
            source_text,
            target.body_span.start.line,
            target.body_span.start.column,
        )
    except ValueError:
        return None
    text = source_text[declaration_start:declaration_end].strip()
    statement = source_text[declaration_start:body_start].strip() or _statement_from_declaration(text)
    if not text:
        return None
    return LocalDeclaration(
        name=target.short_name,
        declaration_name=target.declaration_name,
        namespace=target.namespace,
        file_path=file_path,
        module_name=target.module_path or _module_name_for_path(workspace_root=workspace_root, file_path=file_path),
        kind=target.declaration_kind,
        start_line=target.declaration_span.start.line,
        start_column=target.declaration_span.start.column,
        end_line=target.declaration_span.end.line,
        end_column=target.declaration_span.end.column,
        text=text,
        statement=statement,
        theorem_tokens=frozenset(_name_tokens(target.short_name)),
        constants=frozenset(_extract_constants(statement)),
        type_heads=frozenset(_extract_type_heads(statement)),
    )


def _same_file_hits(
    *, declarations: list[LocalDeclaration], theorem: str, profile: TargetProfile
) -> list[RetrievalHit]:
    target_line = next((declaration.start_line for declaration in declarations if declaration.name == theorem), 1)
    hits: list[RetrievalHit] = []
    for index, declaration in enumerate(declarations, start=1):
        if declaration.name == theorem:
            continue
        components = [
            RetrievalScoreComponent(label="same-file", points=max(60, 100 - abs(declaration.start_line - target_line)))
        ]
        proximity = max(0, 12 - abs(declaration.start_line - target_line) // 5)
        if proximity:
            components.append(
                RetrievalScoreComponent(
                    label="line-proximity",
                    points=proximity,
                    detail=f"declaration starts {abs(declaration.start_line - target_line)} lines away",
                )
            )
        components.extend(_declaration_signal_components(profile=profile, declaration=declaration))
        hits.append(
            _build_hit(
                hit_id=f"same-file-{index:03d}",
                source_kind=RetrievalSourceKind.SAME_FILE,
                title=declaration.name,
                summary=(
                    f"Local declaration from {declaration.file_path.name} "
                    f"lines {declaration.start_line}-{declaration.end_line}."
                ),
                content=declaration.text,
                theorem=declaration.name,
                components=components,
            )
        )
    return hits


def _same_namespace_hits(
    *,
    workspace_root: Path,
    target_file: Path,
    theorem: str,
    namespace: str | None,
    profile: TargetProfile,
    target_declarations: list[LocalDeclaration],
    workspace_declarations: list[LocalDeclaration],
) -> list[RetrievalHit]:
    if namespace is None:
        return []
    hits: list[RetrievalHit] = []
    declarations = list(workspace_declarations)
    declarations.extend(target_declarations)
    for index, declaration in enumerate(declarations, start=1):
        if declaration.name == theorem or declaration.namespace != namespace:
            continue
        components = [
            RetrievalScoreComponent(
                label="same-namespace",
                points=85 if declaration.file_path == target_file else 78,
                detail=f"namespace `{namespace}`",
            )
        ]
        components.extend(_declaration_signal_components(profile=profile, declaration=declaration))
        hits.append(
            _build_hit(
                hit_id=f"same-namespace-{index:03d}",
                source_kind=RetrievalSourceKind.SAME_NAMESPACE,
                title=declaration.name,
                summary=f"Declaration from namespace `{namespace}` in `{declaration.file_path.name}`.",
                content=declaration.text,
                path=_project_relative(declaration.file_path, workspace_root),
                theorem=declaration.name,
                components=components,
            )
        )
    return hits


def _goal_declaration_hits(
    *,
    workspace_root: Path,
    theorem: str,
    namespace: str | None,
    profile: TargetProfile,
    workspace_declarations: list[LocalDeclaration],
) -> list[RetrievalHit]:
    hits: list[RetrievalHit] = []
    for index, declaration in enumerate(workspace_declarations, start=1):
        if declaration.name == theorem:
            continue
        if namespace is not None and declaration.namespace == namespace:
            continue
        components = [
            RetrievalScoreComponent(
                label="goal-declaration",
                points=30,
                detail="workspace declaration outside the target file",
            )
        ]
        components.extend(_declaration_signal_components(profile=profile, declaration=declaration))
        if _signal_points(components) < 42:
            continue
        hits.append(
            _build_hit(
                hit_id=f"goal-declaration-{index:03d}",
                source_kind=RetrievalSourceKind.GOAL_DECLARATION,
                title=declaration.name,
                summary="Workspace declaration ranked by goal overlap and declaration shape.",
                content=declaration.text,
                path=_project_relative(declaration.file_path, workspace_root),
                theorem=declaration.name,
                components=components,
            )
        )
    return hits


def _accepted_patch_hits(
    *, store: StorePaths, workspace_root: Path, target_module_path: Path, theorem: str
) -> list[RetrievalHit]:
    hits: list[RetrievalHit] = []
    for index, row in enumerate(read_jsonl(store.accepted_patches), start=1):
        if not _artifact_row_matches(
            row=row,
            workspace_root=workspace_root,
            target_module_path=target_module_path,
            theorem=theorem,
        ):
            continue
        body = row.get("body")
        if not isinstance(body, str):
            continue
        source_run = _string(row.get("run_id"))
        hits.append(
            _build_hit(
                hit_id=f"accepted-patch-{index:03d}",
                source_kind=RetrievalSourceKind.ACCEPTED_PATCH,
                title="Prior accepted patch",
                summary=f"Verified patch from run `{source_run}`.",
                content=body,
                path=_string(row.get("candidate_body_path")),
                theorem=theorem,
                source_run_id=source_run,
                components=[
                    RetrievalScoreComponent(label="accepted-patch", points=80, detail="exact theorem match"),
                    RetrievalScoreComponent(label="verified-history", points=8, detail="previously checked by Lean"),
                ],
            )
        )
    return hits


def _proved_helper_hits(
    *, store: StorePaths, workspace_root: Path, target_module_path: Path, theorem: str
) -> list[RetrievalHit]:
    hits: list[RetrievalHit] = []
    for index, row in enumerate(read_jsonl(store.proved_helpers), start=1):
        if not _artifact_row_matches(
            row=row,
            workspace_root=workspace_root,
            target_module_path=target_module_path,
            theorem=theorem,
        ):
            continue
        helper_name = row.get("helper_name")
        statement = row.get("statement")
        module_name = row.get("module_name")
        if not isinstance(helper_name, str) or not isinstance(statement, str):
            continue
        hits.append(
            _build_hit(
                hit_id=f"proved-helper-{index:03d}",
                source_kind=RetrievalSourceKind.PROVED_HELPER,
                title=helper_name,
                summary=f"Previously proved helper from module `{module_name}`.",
                content=statement,
                path=_string(row.get("file_path")),
                theorem=theorem,
                source_run_id=_string(row.get("run_id")),
                components=[
                    RetrievalScoreComponent(label="proved-helper", points=75, detail="exact theorem helper reuse"),
                    RetrievalScoreComponent(
                        label="verified-history", points=8, detail="helper already checked by Lean"
                    ),
                ],
            )
        )
    return hits


def _proof_fragment_hits(
    *,
    store: StorePaths,
    workspace_root: Path,
    target_module_path: Path,
    theorem: str,
    profile: TargetProfile,
) -> list[RetrievalHit]:
    hits: list[RetrievalHit] = []
    accepted_rows = read_jsonl(store.accepted_patches)
    for index, row in enumerate(accepted_rows, start=1):
        if row.get("workspace_root") != str(workspace_root):
            continue
        if _artifact_row_matches(
            row=row, workspace_root=workspace_root, target_module_path=target_module_path, theorem=theorem
        ):
            continue
        content = _artifact_text(row=row, content_key="body", path_key="candidate_body_path")
        if content is None:
            continue
        components = [
            RetrievalScoreComponent(label="proof-fragment", points=32, detail="verified theorem body fragment")
        ]
        components.extend(_artifact_signal_components(profile=profile, row=row, content=content))
        if _signal_points(components) < 44:
            continue
        hits.append(
            _build_hit(
                hit_id=f"proof-fragment-accepted-{index:03d}",
                source_kind=RetrievalSourceKind.PROOF_FRAGMENT,
                title=f"Accepted fragment: {_string(row.get('theorem')) or 'unknown theorem'}",
                summary="Verified theorem body fragment ranked against the current goal.",
                content=content,
                path=_string(row.get("candidate_body_path")),
                theorem=_string(row.get("theorem")),
                source_run_id=_string(row.get("run_id")),
                components=components,
            )
        )
    helper_rows = read_jsonl(store.proved_helpers)
    for index, row in enumerate(helper_rows, start=1):
        if row.get("workspace_root") != str(workspace_root):
            continue
        if _artifact_row_matches(
            row=row, workspace_root=workspace_root, target_module_path=target_module_path, theorem=theorem
        ):
            continue
        content = _artifact_text(row=row, content_key="statement", path_key="file_path")
        if content is None:
            continue
        components = [RetrievalScoreComponent(label="proof-fragment", points=28, detail="verified helper fragment")]
        components.extend(_artifact_signal_components(profile=profile, row=row, content=content))
        if _signal_points(components) < 40:
            continue
        hits.append(
            _build_hit(
                hit_id=f"proof-fragment-helper-{index:03d}",
                source_kind=RetrievalSourceKind.PROOF_FRAGMENT,
                title=_string(row.get("helper_name")) or "Verified helper fragment",
                summary="Verified helper fragment ranked against the current goal.",
                content=content,
                path=_string(row.get("file_path")),
                theorem=_string(row.get("theorem")),
                source_run_id=_string(row.get("run_id")),
                components=components,
            )
        )
    return hits


def _graph_run_hits(
    *,
    store: StorePaths,
    workspace_root: Path,
    target_module_path: Path,
    theorem: str,
    profile: TargetProfile,
) -> list[RetrievalHit]:
    hits: list[RetrievalHit] = []
    artifacts_root = store.root.parent
    for index, row in enumerate(read_jsonl(artifacts_root / "index.jsonl"), start=1):
        if row.get("command") != "prove" or row.get("status") != "verified":
            continue
        if row.get("workspace_root") != str(workspace_root):
            continue
        run_dir = Path(_string(row.get("artifact_dir")) or "")
        if not run_dir.is_dir():
            continue
        summary = _read_json_object(run_dir / "graph" / "summary.json")
        branch_ids = set(_string_list(summary.get("branch_node_ids")))
        nodes = _read_json_list(run_dir / "graph" / "nodes.json")
        for node in nodes:
            node_id = _string(node.get("node_id"))
            if node_id is None or (branch_ids and node_id not in branch_ids and node.get("status") != "verified"):
                continue
            if row.get("target_module_path") == str(target_module_path) and row.get("theorem") == theorem:
                continue
            snapshot = _graph_snapshot(node=node)
            state_content = _graph_state_content(snapshot)
            if state_content is not None:
                components = [
                    RetrievalScoreComponent(
                        label="graph-state",
                        points=34,
                        detail=f"verified prove-graph state from run `{_string(row.get('run_id'))}`",
                    )
                ]
                components.extend(
                    _artifact_signal_components(
                        profile=profile,
                        row={
                            **row,
                            "goal_snapshot": snapshot.get("goals"),
                            "theorem_statement": snapshot.get("theorem_statement"),
                            "imports": snapshot.get("imports"),
                            "namespace": snapshot.get("namespace"),
                        },
                        content=state_content,
                    )
                )
                if _signal_points(components) >= 44:
                    hits.append(
                        _build_hit(
                            hit_id=f"graph-state-{index:03d}-{node_id}",
                            source_kind=RetrievalSourceKind.GRAPH_STATE,
                            title=f"Graph state: {_string(row.get('theorem')) or 'unknown theorem'}",
                            summary="Goal snapshot and local context from a verified prove run branch.",
                            content=state_content,
                            theorem=_string(row.get("theorem")),
                            source_run_id=_string(row.get("run_id")),
                            components=components,
                        )
                    )
            tactic_text = _graph_tactic_text(node)
            if tactic_text is None:
                continue
            tactic_components = [
                RetrievalScoreComponent(
                    label="tactic-fragment",
                    points=30,
                    detail=f"tactic step from verified run `{_string(row.get('run_id'))}`",
                )
            ]
            tactic_components.extend(
                _artifact_signal_components(
                    profile=profile,
                    row={
                        **row,
                        "goal_snapshot": snapshot.get("goals"),
                        "theorem_statement": snapshot.get("theorem_statement"),
                        "imports": snapshot.get("imports"),
                        "namespace": snapshot.get("namespace"),
                    },
                    content=tactic_text,
                )
            )
            if _signal_points(tactic_components) < 40:
                continue
            hits.append(
                _build_hit(
                    hit_id=f"tactic-fragment-{index:03d}-{node_id}",
                    source_kind=RetrievalSourceKind.TACTIC_FRAGMENT,
                    title=f"Tactic fragment: {_string(row.get('theorem')) or 'unknown theorem'}",
                    summary="Tactic text recovered from a verified prove run branch.",
                    content=tactic_text,
                    theorem=_string(row.get("theorem")),
                    source_run_id=_string(row.get("run_id")),
                    components=tactic_components,
                )
            )
    return hits


def _failure_summary_hits(
    *,
    store: StorePaths,
    workspace_root: Path,
    target_module_path: Path,
    theorem: str,
    failure_classes: list[str],
) -> list[RetrievalHit]:
    failures = {_normalize_failure_class_name(item) for item in failure_classes}
    if not failures:
        return []
    hits: list[RetrievalHit] = []
    for index, row in enumerate(read_jsonl(store.failure_summaries), start=1):
        if not _artifact_row_matches(
            row=row,
            workspace_root=workspace_root,
            target_module_path=target_module_path,
            theorem=theorem,
        ):
            continue
        row_failures = {_normalize_failure_class_name(item) for item in _string_list(row.get("failure_classes"))}
        if not row_failures.intersection(failures):
            continue
        summary = row.get("summary")
        if not isinstance(summary, str):
            continue
        hits.append(
            _build_hit(
                hit_id=f"failure-summary-{index:03d}",
                source_kind=RetrievalSourceKind.FAILURE_SUMMARY,
                title="Prior failure summary",
                summary=f"Failure classes: {', '.join(sorted(row_failures))}.",
                content=summary,
                theorem=theorem,
                source_run_id=_string(row.get("run_id")),
                components=[
                    RetrievalScoreComponent(
                        label="failure-summary",
                        points=65,
                        detail="same theorem and overlapping verifier failure classes",
                    )
                ],
            )
        )
    return hits


def _normalize_failure_class_name(value: str) -> str:
    if value == DiagnosticClass.PARSER.value:
        return DiagnosticClass.SYNTAX_ERROR.value
    if value == DiagnosticClass.UNKNOWN_NAME.value:
        return DiagnosticClass.UNKNOWN_IDENTIFIER.value
    if value == DiagnosticClass.TACTIC.value:
        return DiagnosticClass.TACTIC_FAILURE.value
    return value


def _active_helpers(
    *, store: StorePaths, workspace_root: Path, target_module_path: Path, theorem: str
) -> list[HelperRef]:
    helpers: list[HelperRef] = []
    for row in read_jsonl(store.proved_helpers):
        if not _artifact_row_matches(
            row=row,
            workspace_root=workspace_root,
            target_module_path=target_module_path,
            theorem=theorem,
        ):
            continue
        helper_name = row.get("helper_name")
        module_name = row.get("module_name")
        file_path = row.get("file_path")
        statement = row.get("statement")
        if not all(isinstance(value, str) for value in [helper_name, module_name, file_path, statement]):
            continue
        helpers.append(
            HelperRef(
                helper_name=cast(str, helper_name),
                module_name=cast(str, module_name),
                file_path=cast(str, file_path),
                theorem=theorem,
                statement=cast(str, statement),
                workspace_root=cast(str | None, row.get("workspace_root")),
                target_module_path=cast(str | None, row.get("target_module_path")),
                helper_module_root=cast(str | None, row.get("helper_module_root")),
            )
        )
    return helpers


def _workspace_declarations(
    *,
    workspace_root: Path,
    target_file: Path,
    tool_paths: ToolPaths | None,
    workspace: WorkspaceBinding | None,
    imports: tuple[str, ...] = (),
    namespace: str | None = None,
) -> list[LocalDeclaration]:
    if tool_paths is not None and workspace is not None:
        return load_workspace_declaration_index(
            workspace_root=workspace_root,
            target_file=target_file,
            tool_paths=tool_paths,
            workspace=workspace,
            imports=imports,
            namespace=namespace,
        )
    declarations: list[LocalDeclaration] = []
    for file_path in workspace_frontier_files(
        workspace_root=workspace_root,
        target_file=target_file,
        tool_paths=tool_paths,
        imports=imports,
        namespace=namespace,
    ):
        declarations.extend(
            _file_declarations(
                file_path=file_path,
                workspace_root=workspace_root,
                tool_paths=tool_paths,
                workspace=workspace,
            )
        )
    return declarations


def _artifact_row_matches(
    *, row: dict[str, object], workspace_root: Path, target_module_path: Path, theorem: str
) -> bool:
    return (
        row.get("theorem") == theorem
        and row.get("workspace_root") == str(workspace_root)
        and row.get("target_module_path") == str(target_module_path)
    )


def _target_profile(
    *,
    theorem: str,
    theorem_statement: str | None,
    goal_snapshot: list[str],
    imports: list[str],
    namespace: str | None,
) -> TargetProfile:
    goal_texts = tuple(goal_snapshot or _goal_texts_from_statement(theorem_statement))
    return TargetProfile(
        theorem=theorem,
        theorem_tokens=frozenset(_name_tokens(theorem)),
        namespace=namespace,
        namespace_segments=tuple(namespace.split(".")) if namespace else (),
        imports=tuple(imports),
        goal_texts=goal_texts,
        goal_constants=frozenset(_extract_constants("\n".join([theorem_statement or "", *goal_texts]))),
        type_heads=frozenset(_extract_type_heads("\n".join([theorem_statement or "", *goal_texts]))),
    )


def _declaration_signal_components(
    *, profile: TargetProfile, declaration: LocalDeclaration
) -> list[RetrievalScoreComponent]:
    components: list[RetrievalScoreComponent] = []
    overlap = sorted(profile.theorem_tokens.intersection(declaration.theorem_tokens))
    if overlap:
        components.append(
            RetrievalScoreComponent(
                label="theorem-name-overlap",
                points=min(12, 4 * len(overlap)),
                detail=", ".join(overlap),
            )
        )
    goal_overlap = sorted(profile.goal_constants.intersection(declaration.constants))
    if goal_overlap:
        components.append(
            RetrievalScoreComponent(
                label="goal-constant-overlap",
                points=min(18, 3 * len(goal_overlap)),
                detail=", ".join(goal_overlap[:6]),
            )
        )
    shared_namespace = _shared_prefix_len(
        profile.namespace_segments, tuple(declaration.namespace.split(".")) if declaration.namespace else ()
    )
    if shared_namespace:
        components.append(
            RetrievalScoreComponent(
                label="namespace-proximity",
                points=min(12, 4 * shared_namespace),
                detail=f"shared namespace depth {shared_namespace}",
            )
        )
    if declaration.module_name and declaration.module_name in profile.imports:
        components.append(
            RetrievalScoreComponent(
                label="import-match",
                points=10,
                detail=f"import `{declaration.module_name}`",
            )
        )
    head_overlap = sorted(profile.type_heads.intersection(declaration.type_heads))
    if head_overlap:
        components.append(
            RetrievalScoreComponent(
                label="type-head-overlap",
                points=min(14, 7 * len(head_overlap)),
                detail=", ".join(head_overlap),
            )
        )
    return components


def _artifact_signal_components(
    *, profile: TargetProfile, row: dict[str, object], content: str
) -> list[RetrievalScoreComponent]:
    theorem_name = _string(row.get("theorem")) or ""
    theorem_tokens = frozenset(_name_tokens(theorem_name))
    statement = _string(row.get("theorem_statement")) or _string(row.get("statement")) or content
    constants = frozenset(_string_list(row.get("goal_constants"))) or frozenset(_extract_constants(statement))
    type_heads = frozenset(_string_list(row.get("type_heads"))) or frozenset(_extract_type_heads(statement))
    namespace = _string(row.get("namespace"))
    imports = frozenset(_string_list(row.get("imports")))
    components: list[RetrievalScoreComponent] = []
    overlap = sorted(profile.theorem_tokens.intersection(theorem_tokens))
    if overlap:
        components.append(
            RetrievalScoreComponent(
                label="theorem-name-overlap",
                points=min(12, 4 * len(overlap)),
                detail=", ".join(overlap),
            )
        )
    goal_overlap = sorted(profile.goal_constants.intersection(constants))
    if goal_overlap:
        components.append(
            RetrievalScoreComponent(
                label="goal-constant-overlap",
                points=min(18, 3 * len(goal_overlap)),
                detail=", ".join(goal_overlap[:6]),
            )
        )
    shared_namespace = _shared_prefix_len(profile.namespace_segments, tuple(namespace.split(".")) if namespace else ())
    if shared_namespace:
        components.append(
            RetrievalScoreComponent(
                label="namespace-proximity",
                points=min(12, 4 * shared_namespace),
                detail=f"shared namespace depth {shared_namespace}",
            )
        )
    imported = next((item for item in profile.imports if item in imports), None)
    if imported is not None:
        components.append(
            RetrievalScoreComponent(
                label="import-match",
                points=10,
                detail=f"import `{imported}`",
            )
        )
    head_overlap = sorted(profile.type_heads.intersection(type_heads))
    if head_overlap:
        components.append(
            RetrievalScoreComponent(
                label="type-head-overlap",
                points=min(14, 7 * len(head_overlap)),
                detail=", ".join(head_overlap),
            )
        )
    return components


def _build_hit(
    *,
    hit_id: str,
    source_kind: RetrievalSourceKind,
    title: str,
    summary: str,
    content: str,
    components: list[RetrievalScoreComponent],
    path: str | None = None,
    theorem: str | None = None,
    source_run_id: str | None = None,
) -> RetrievalHit:
    ordered_components = [component for component in components if component.points > 0]
    return RetrievalHit(
        hit_id=hit_id,
        source_kind=source_kind,
        title=title,
        summary=summary,
        content=content,
        score=sum(component.points for component in ordered_components),
        path=path,
        theorem=theorem,
        source_run_id=source_run_id,
        match_reasons=[_reason_text(component) for component in ordered_components[1:] or ordered_components[:1]],
        score_breakdown=ordered_components,
    )


def _build_embedding_retriever(*, config: EmbeddingRetrievalConfig) -> EmbeddingRetriever:
    if config.provider != _DEFAULT_EMBEDDING_PROVIDER:
        msg = f"Unsupported embedding provider `{config.provider}`."
        raise ValueError(msg)
    api_key = environ.get(config.api_key_env, "").strip()
    if not api_key:
        msg = (
            "Embedding retrieval was enabled, but no API key was configured. "
            f"Set `{config.api_key_env}` before using `{config.provider}`."
        )
        raise ValueError(msg)
    return OpenAICompatibleEmbeddingRetriever(endpoint=config.endpoint, model=config.model, api_key=api_key)


def _embedding_row_index(row: object) -> int:
    if not isinstance(row, dict):
        return 0
    payload = cast(dict[str, object], row)
    index = payload.get("index")
    if isinstance(index, int):
        return index
    if isinstance(index, float):
        return int(index)
    return 0


def _augment_hits_with_embedding_scores(
    *,
    workspace_root: Path,
    theorem: str,
    theorem_statement: str | None,
    goal_texts: tuple[str, ...],
    hits: list[RetrievalHit],
    config: EmbeddingRetrievalConfig,
    retriever: EmbeddingRetriever,
) -> list[RetrievalHit]:
    if not hits:
        return hits
    query_text = _embedding_query_text(
        theorem=theorem,
        theorem_statement=theorem_statement,
        goal_texts=goal_texts,
    )
    texts = [query_text, *[_embedding_candidate_text(hit) for hit in hits]]
    vectors = _cached_embeddings(
        workspace_root=workspace_root,
        cache_namespace=config.cache_namespace(),
        retriever=retriever,
        texts=texts,
    )
    query_vector = vectors[0]
    updated_hits: list[RetrievalHit] = []
    for hit, candidate_vector in zip(hits, vectors[1:], strict=True):
        similarity = _cosine_similarity(query_vector, candidate_vector)
        points = round(config.weight * max(0.0, similarity))
        if points <= 0:
            updated_hits.append(hit)
            continue
        detail_parts = [f"cosine {similarity:.3f}", retriever.provider_name()]
        model_name = retriever.model_name()
        if model_name:
            detail_parts.append(model_name)
        component = RetrievalScoreComponent(
            label="embedding-similarity",
            points=points,
            detail=", ".join(detail_parts),
        )
        ordered_components = [*hit.score_breakdown, component]
        updated_hits.append(
            RetrievalHit(
                hit_id=hit.hit_id,
                source_kind=hit.source_kind,
                title=hit.title,
                summary=hit.summary,
                content=hit.content,
                score=sum(item.points for item in ordered_components),
                path=hit.path,
                theorem=hit.theorem,
                source_run_id=hit.source_run_id,
                match_reasons=[*hit.match_reasons, _reason_text(component)],
                score_breakdown=ordered_components,
            )
        )
    return updated_hits


def _embedding_query_text(*, theorem: str, theorem_statement: str | None, goal_texts: tuple[str, ...]) -> str:
    sections = [f"Theorem: {theorem}"]
    if theorem_statement:
        sections.append(f"Statement:\n{theorem_statement}")
    if goal_texts:
        sections.append("Goals:\n" + "\n".join(goal_texts))
    return "\n\n".join(sections)


def _embedding_candidate_text(hit: RetrievalHit) -> str:
    sections = [f"Title: {hit.title}", f"Source: {hit.source_kind.value}"]
    if hit.theorem:
        sections.append(f"Theorem: {hit.theorem}")
    sections.append(hit.content)
    return "\n\n".join(sections)


def _cached_embeddings(
    *,
    workspace_root: Path,
    cache_namespace: str,
    retriever: EmbeddingRetriever,
    texts: list[str],
) -> list[tuple[float, ...]]:
    workspace_key = (str(workspace_root), cache_namespace)
    cache = _EMBEDDING_TEXT_CACHE.setdefault(workspace_key, {})
    missing_texts: list[str] = []
    missing_keys: list[str] = []
    for text in texts:
        text_key = sha256(text.encode("utf-8")).hexdigest()
        if text_key not in cache:
            missing_keys.append(text_key)
            missing_texts.append(text)
    if missing_texts:
        embeddings = retriever.embed(missing_texts)
        if len(embeddings) != len(missing_texts):
            msg = "Embedding retriever returned the wrong number of vectors."
            raise RuntimeError(msg)
        for text_key, embedding in zip(missing_keys, embeddings, strict=True):
            cache[text_key] = tuple(float(value) for value in embedding)
    return [cache[sha256(text.encode("utf-8")).hexdigest()] for text in texts]


def _cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        msg = "Embedding vectors must have the same dimension."
        raise ValueError(msg)
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def _reason_text(component: RetrievalScoreComponent) -> str:
    if component.detail:
        return f"{component.label}: {component.detail}"
    return component.label


def _dedupe_hits(hits: list[RetrievalHit]) -> list[RetrievalHit]:
    best: dict[tuple[str, str | None, str], RetrievalHit] = {}
    for hit in hits:
        key = (hit.title, hit.path, hit.content)
        existing = best.get(key)
        if (
            existing is None
            or hit.score > existing.score
            or (hit.score == existing.score and hit.hit_id < existing.hit_id)
        ):
            best[key] = hit
    return list(best.values())


def _enrich_returned_declaration_hits(
    *,
    hits: list[RetrievalHit],
    workspace_root: Path,
    declarations: list[LocalDeclaration],
) -> list[RetrievalHit]:
    requested_keys = {
        (hit.path, hit.theorem)
        for hit in hits
        if hit.source_kind in {RetrievalSourceKind.SAME_NAMESPACE, RetrievalSourceKind.GOAL_DECLARATION}
        and hit.path is not None
        and hit.theorem is not None
    }
    if not requested_keys:
        return hits
    selected_declarations = [
        declaration
        for declaration in declarations
        if (_project_relative(declaration.file_path, workspace_root), declaration.name) in requested_keys
    ]
    if not selected_declarations:
        return hits
    source_texts = load_declaration_texts(declarations=selected_declarations)
    if not source_texts:
        return hits
    enriched_hits: list[RetrievalHit] = []
    for hit in hits:
        if hit.path is None or hit.theorem is None:
            enriched_hits.append(hit)
            continue
        content = source_texts.get(((workspace_root / hit.path).resolve(), hit.theorem))
        if content is None:
            enriched_hits.append(hit)
            continue
        enriched_hits.append(
            RetrievalHit(
                hit_id=hit.hit_id,
                source_kind=hit.source_kind,
                title=hit.title,
                summary=hit.summary,
                content=content,
                score=hit.score,
                path=hit.path,
                theorem=hit.theorem,
                source_run_id=hit.source_run_id,
                match_reasons=list(hit.match_reasons),
                score_breakdown=list(hit.score_breakdown),
            )
        )
    return enriched_hits


def _signal_points(components: list[RetrievalScoreComponent]) -> int:
    return sum(component.points for component in components)


def _artifact_text(*, row: dict[str, object], content_key: str, path_key: str) -> str | None:
    content = _string(row.get(content_key))
    path_text = _string(row.get(path_key))
    if path_text:
        with suppress(OSError):
            stored = Path(path_text)
            if stored.is_file():
                return stored.read_text(encoding="utf-8")
    return content


def _graph_snapshot(node: dict[str, object]) -> dict[str, object]:
    session_dir = Path(_string(node.get("session_artifact_dir")) or "")
    session_node_id = _string(node.get("session_node_id"))
    if not session_dir.is_dir() or session_node_id is None:
        return {}
    return _read_json_object(session_dir / "nodes" / session_node_id / "snapshot.json")


def _graph_state_content(snapshot: dict[str, object]) -> str | None:
    proof_state = cast(dict[str, object], snapshot.get("proof_state") or {})
    goals = _string_list(proof_state.get("goals")) or _string_list(snapshot.get("goals"))
    context = _string_list(proof_state.get("local_context")) or _string_list(snapshot.get("local_context"))
    if not goals and not context:
        return _string(snapshot.get("pretty_proof_state"))
    parts = [
        "Local context:",
        *(context or ["<none>"]),
        "",
        "Goals:",
        *(goals or ["<none>"]),
    ]
    return "\n".join(parts)


def _graph_tactic_text(node: dict[str, object]) -> str | None:
    action = node.get("action_payload")
    if not isinstance(action, dict):
        return None
    payload = cast(dict[str, object], action)
    if payload.get("kind") != "append_tactic_text":
        return None
    tactic_text = payload.get("tactic_text")
    return tactic_text if isinstance(tactic_text, str) and tactic_text.strip() else None


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        loaded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except OSError, ValueError:
        return {}
    return cast(dict[str, object], loaded) if isinstance(loaded, dict) else {}


def _read_json_list(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    try:
        loaded = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except OSError, ValueError:
        return []
    if not isinstance(loaded, list):
        return []
    return [cast(dict[str, object], item) for item in loaded if isinstance(item, dict)]


def _statement_from_declaration(text: str) -> str:
    if ":=" in text:
        return text.split(":=", maxsplit=1)[0].strip()
    if " where" in text:
        return text.split(" where", maxsplit=1)[0].strip()
    return text.strip()


def _goal_texts_from_statement(statement: str | None) -> list[str]:
    if statement is None:
        return []
    goal = _goal_like_text(statement)
    return [goal] if goal else []


def _goal_like_text(statement: str) -> str:
    if ":" not in statement:
        return statement.strip()
    return statement.split(":", maxsplit=1)[1].strip()


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
    for goal_text in _goal_fragments(text):
        for symbol, head in SYMBOLIC_HEADS.items():
            if symbol in goal_text:
                heads.add(head)
        match = IDENT_RE.search(goal_text)
        if match is not None:
            heads.add(match.group(0))
    return heads


def _goal_fragments(text: str) -> list[str]:
    fragments: list[str] = []
    for raw_line in text.splitlines():
        goal_match = GOAL_RE.search(raw_line)
        if goal_match is not None:
            fragments.append(goal_match.group("goal").strip())
    if fragments:
        return fragments
    statement = _goal_like_text(text)
    return [statement] if statement else []


def _name_tokens(name: str) -> set[str]:
    expanded = re.sub(r"(?<!^)(?=[A-Z])", "_", name.replace(".", "_"))
    return {token.lower() for token in re.split(r"[^A-Za-z0-9]+", expanded) if token}


def _module_name_for_path(*, workspace_root: Path, file_path: Path) -> str | None:
    try:
        relative = file_path.relative_to(workspace_root)
    except ValueError:
        return None
    return ".".join(relative.with_suffix("").parts)


def _shared_prefix_len(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    shared = 0
    for lhs, rhs in zip(left, right, strict=False):
        if lhs != rhs:
            break
        shared += 1
    return shared


def _project_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _line_offsets(source_text: str) -> list[int]:
    offsets = [0]
    for index, char in enumerate(source_text):
        if char == "\n":
            offsets.append(index + 1)
    offsets.append(len(source_text))
    return offsets


def _line_for_offset(line_offsets: list[int], offset: int) -> int:
    for index, line_offset in enumerate(line_offsets[1:], start=1):
        if offset < line_offset:
            return index
    return max(1, len(line_offsets) - 1)


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


def _target_namespace(*, theorem: str, declaration: LocalDeclaration | None) -> str | None:
    if declaration and declaration.namespace:
        return declaration.namespace
    if "." in theorem:
        return theorem.rsplit(".", maxsplit=1)[0]
    return None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
