"""Persistent cache for selected semantic probe results."""

import json
import re
from collections.abc import Callable
from hashlib import sha256
from os import environ
from pathlib import Path
from typing import cast

from kan_prover.engine.config import ToolPaths, WorkspaceBinding
from kan_prover.engine.lean_helpers import (
    LeanHelperCommand,
    LeanHelperRequest,
    ProbeResult,
    ProbeStatus,
    decode_probe_result_payload,
)
from kan_prover.engine.semantic_probe_runtime import semantic_probe_runtime_fingerprint

CACHE_SCHEMA_VERSION = "semantic-cache.v1"
_IMPORT_RE = re.compile(r"(?m)^import\s+(?P<modules>[^\n]+)$")
_CACHEABLE_COMMANDS = {
    LeanHelperCommand.BATCH_DECLARATION_INVENTORY,
    LeanHelperCommand.CONTEXT_BUNDLE,
    LeanHelperCommand.DECLARATION_INVENTORY,
}
_MEMORY_CACHE: dict[str, ProbeResult] = {}


def run_with_persistent_cache(
    request: LeanHelperRequest,
    *,
    runner: Callable[[LeanHelperRequest], ProbeResult],
) -> ProbeResult:
    """Reuse one cached semantic probe result when the request key is still valid."""
    if request.command not in _CACHEABLE_COMMANDS:
        return runner(request)
    cache_key = _cache_key(request)
    if cache_key is None:
        return runner(request)
    key_json = _stable_json(cache_key)
    cache_id = sha256(key_json.encode("utf-8")).hexdigest()
    cached = _MEMORY_CACHE.get(cache_id)
    if cached is not None:
        return cached
    record_path = _record_path(tool_paths=request.tool_paths, command=request.command, cache_id=cache_id)
    cached = _load_cached_result(request=request, record_path=record_path, expected_key=cache_key)
    if cached is not None:
        _MEMORY_CACHE[cache_id] = cached
        return cached
    result = runner(request)
    if result.status is ProbeStatus.OK:
        try:
            _write_cached_result(record_path=record_path, cache_key=cache_key, probe_result=result)
        except OSError:
            return result
        _MEMORY_CACHE[cache_id] = result
    return result


def clear_memory_cache() -> None:
    """Drop the in-process semantic cache to simulate a fresh CLI process."""
    _MEMORY_CACHE.clear()


def semantic_cache_root(tool_paths: ToolPaths) -> Path:
    """Return the persistent cache root, separate from artifacts and replay state."""
    override = environ.get("KAN_PROVER_SEMANTIC_CACHE_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return tool_paths.root / ".cache" / "semantic-probes"


def _record_path(*, tool_paths: ToolPaths, command: LeanHelperCommand, cache_id: str) -> Path:
    return semantic_cache_root(tool_paths) / CACHE_SCHEMA_VERSION / command.value / f"{cache_id}.json"


def _load_cached_result(
    *,
    request: LeanHelperRequest,
    record_path: Path,
    expected_key: dict[str, object],
) -> ProbeResult | None:
    if not record_path.is_file():
        return None
    try:
        payload = json.loads(record_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
        return None
    stored_key = payload.get("key")
    if stored_key != expected_key:
        return None
    probe_payload = payload.get("probe_result")
    if not isinstance(probe_payload, dict):
        return None
    decoded = decode_probe_result_payload(
        cast(dict[str, object], probe_payload),
        command=request.command,
        file=str(request.file_path.expanduser().resolve()),
        theorem=request.theorem,
    )
    return decoded if decoded.status is ProbeStatus.OK else None


def _write_cached_result(
    *,
    record_path: Path,
    cache_key: dict[str, object],
    probe_result: ProbeResult,
) -> None:
    payload = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "key": cache_key,
        "probe_result": probe_result.to_json(),
    }
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(_stable_json(payload), encoding="utf-8")


def _cache_key(request: LeanHelperRequest) -> dict[str, object] | None:
    try:
        source_text = request.file_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if request.command is LeanHelperCommand.BATCH_DECLARATION_INVENTORY:
        frontier_files = _batch_inventory_files(request=request)
        if frontier_files is None:
            return None
        return {
            "command": request.command.value,
            "theorem": request.theorem,
            "workspace": _workspace_identity(request.workspace),
            "runtime_fingerprint": semantic_probe_runtime_fingerprint(request.tool_paths),
            "file": _file_identity(path=request.file_path, workspace=request.workspace, source_text=source_text),
            "frontier_files": [_file_identity(path=path, workspace=request.workspace) for path in frontier_files],
        }
    return {
        "command": request.command.value,
        "theorem": request.theorem,
        "workspace": _workspace_identity(request.workspace),
        "runtime_fingerprint": semantic_probe_runtime_fingerprint(request.tool_paths),
        "file": _file_identity(path=request.file_path, workspace=request.workspace, source_text=source_text),
        "direct_dependencies": _direct_dependency_identities(source_text=source_text, workspace=request.workspace),
    }


def _workspace_identity(workspace: WorkspaceBinding) -> dict[str, object]:
    return {
        "package": workspace.package_name,
        "toolchain": workspace.toolchain,
        "lakefile": _path_digest(workspace.lakefile),
        "manifest": _optional_path_digest(workspace.root / "lake-manifest.json"),
    }


def _batch_inventory_files(request: LeanHelperRequest) -> list[Path] | None:
    if len(request.extra_args) != 1:
        return None
    manifest_path = Path(request.extra_args[0]).expanduser().resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    files = sorted({Path(item).expanduser().resolve() for item in payload if isinstance(item, str) and item.strip()})
    return files or None


def _direct_dependency_identities(*, source_text: str, workspace: WorkspaceBinding) -> list[dict[str, object]]:
    identities: list[dict[str, object]] = []
    for module_name in _direct_import_modules(source_text):
        matches = _module_matches(workspace=workspace, module_name=module_name)
        if not matches:
            identities.append({"module": module_name, "matches": []})
            continue
        identities.append(
            {
                "module": module_name,
                "matches": [_file_identity(path=match, workspace=workspace) for match in matches],
            }
        )
    return identities


def _direct_import_modules(source_text: str) -> list[str]:
    modules: list[str] = []
    for match in _IMPORT_RE.finditer(source_text):
        modules.extend(token for token in match.group("modules").split() if token not in {"hiding", "renaming"})
    return list(dict.fromkeys(modules))


def _module_matches(*, workspace: WorkspaceBinding, module_name: str) -> list[Path]:
    relative = Path(*module_name.split(".")).with_suffix(".lean")
    matches: list[Path] = []
    workspace_candidate = workspace.root / relative
    if workspace_candidate.is_file():
        matches.append(workspace_candidate)
    if workspace.packages_dir.is_dir():
        for package_root in sorted(path for path in workspace.packages_dir.iterdir() if path.is_dir()):
            candidate = package_root / relative
            if candidate.is_file():
                matches.append(candidate)
    return matches


def _file_identity(*, path: Path, workspace: WorkspaceBinding, source_text: str | None = None) -> dict[str, object]:
    if source_text is None:
        source_text = path.read_text(encoding="utf-8")
    return {
        "logical_path": _logical_path(path=path, workspace=workspace),
        "content_sha256": sha256(source_text.encode("utf-8")).hexdigest(),
    }


def _logical_path(*, path: Path, workspace: WorkspaceBinding) -> str:
    resolved = path.expanduser().resolve()
    try:
        return f"workspace:{resolved.relative_to(workspace.root.resolve())}"
    except ValueError:
        pass
    if workspace.packages_dir.is_dir():
        for package_root in sorted(path for path in workspace.packages_dir.iterdir() if path.is_dir()):
            try:
                relative = resolved.relative_to(package_root.resolve())
            except ValueError:
                continue
            return f"package:{package_root.name}/{relative}"
    return str(resolved)


def _path_digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _optional_path_digest(path: Path) -> str | None:
    return _path_digest(path) if path.is_file() else None


def _stable_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
