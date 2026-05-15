"""Workspace discovery helpers for tests that exercise a downstream Kan checkout."""

from __future__ import annotations

import os
from pathlib import Path


def downstream_kan_workspace(tool_root: Path) -> Path:
    """Find the Kan checkout used by downstream workspace tests."""
    candidates: list[Path] = []
    if env_root := os.environ.get("LEAN_PROOFCTL_DOWNSTREAM_WORKSPACE"):
        candidates.append(Path(env_root))
    candidates.extend(
        [
            tool_root.parent.parent,
            tool_root.parent / "kan",
            Path("/Users/jcreinhold/Code/kan"),
        ]
    )

    for candidate in candidates:
        root = candidate.expanduser().resolve()
        if (
            (root / "lakefile.toml").is_file()
            and (root / "lean-toolchain").is_file()
            and (root / "KanProofs").is_dir()
        ):
            return root

    searched = ", ".join(str(candidate) for candidate in candidates)
    msg = (
        "Could not find a downstream Kan workspace for tests. "
        "Set LEAN_PROOFCTL_DOWNSTREAM_WORKSPACE to a checkout containing lakefile.toml, lean-toolchain, and KanProofs. "
        f"Searched: {searched}"
    )
    raise FileNotFoundError(msg)
