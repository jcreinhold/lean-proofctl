"""Small JSONL-backed local store for retrieval and helper reuse."""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StorePaths:
    """Stable store file locations under `artifacts/store/`."""

    root: Path
    accepted_patches: Path
    proved_helpers: Path
    failure_summaries: Path


def store_paths(artifacts_root: Path) -> StorePaths:
    """Return the store layout rooted under the artifact directory."""
    root = artifacts_root / "store"
    return StorePaths(
        root=root,
        accepted_patches=root / "accepted_patches.jsonl",
        proved_helpers=root / "proved_helpers.jsonl",
        failure_summaries=root / "failure_summaries.jsonl",
    )


def ensure_store(paths: StorePaths) -> None:
    """Create the store directory and empty JSONL files when needed."""
    paths.root.mkdir(parents=True, exist_ok=True)
    for file_path in [paths.accepted_patches, paths.proved_helpers, paths.failure_summaries]:
        if not file_path.exists():
            file_path.write_text("", encoding="utf-8")


def append_jsonl(path: Path, payload: object) -> None:
    """Append one JSON object to a JSONL file."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")


def read_jsonl(path: Path) -> list[dict[str, object]]:
    """Read one JSONL file into plain dictionaries."""
    if not path.is_file():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        loaded = json.loads(line)
        if isinstance(loaded, dict):
            rows.append(loaded)
    return rows
