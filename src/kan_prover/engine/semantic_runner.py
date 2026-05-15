"""Explicit one-shot semantic runner contracts for theorem-local probes."""

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import cast

from kan_prover.engine.config import ToolPaths, WorkspaceBinding
from kan_prover.engine.lean_helpers import ProbeResult
from kan_prover.engine.models import JsonValue
from kan_prover.engine.proofctl import semantic_context_bundle


class SemanticRunnerMode(StrEnum):
    """Stable semantic runner modes stored in durable artifacts."""

    ONE_SHOT = "one-shot"
    LIVE = "live"


@dataclass(frozen=True)
class SemanticContextRequest:
    """One theorem-local semantic context request."""

    tool_paths: ToolPaths
    workspace: WorkspaceBinding
    file_path: Path
    theorem: str
    timeout_seconds: float = 60.0
    source_text: str | None = None

    @property
    def resolved_file_path(self) -> Path:
        """Return the resolved file path used at the Lean boundary."""
        return self.file_path.expanduser().resolve()

    @property
    def content_digest(self) -> str | None:
        """Return the current file-content digest when text is already loaded."""
        if self.source_text is None:
            return None
        return sha256(self.source_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SemanticSessionFacts:
    """Small semantic-session facts safe to persist in artifacts."""

    mode: SemanticRunnerMode
    file_path: str
    content_digest: str | None = None
    reuse: str | None = None

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the durable semantic-session summary."""
        payload: dict[str, JsonValue] = {
            "mode": self.mode.value,
            "file_path": self.file_path,
        }
        if self.content_digest is not None:
            payload["content_digest"] = self.content_digest
        if self.reuse is not None:
            payload["reuse"] = self.reuse
        return cast(dict[str, JsonValue], payload)


@dataclass(frozen=True)
class SemanticContextProbeResult:
    """One semantic context-bundle probe plus durable runner facts."""

    bundle_probe: ProbeResult
    session_facts: SemanticSessionFacts


class OneShotSemanticRunner:
    """Direct executable semantic runner for cold or single-request flows."""

    def context_bundle(self, request: SemanticContextRequest) -> SemanticContextProbeResult:
        """Run one bundled theorem-context probe through the one-shot boundary."""
        bundle_probe = semantic_context_bundle(
            tool_paths=request.tool_paths,
            workspace=request.workspace,
            file_path=request.resolved_file_path,
            theorem=request.theorem,
            timeout_seconds=request.timeout_seconds,
        )
        return SemanticContextProbeResult(
            bundle_probe=bundle_probe,
            session_facts=SemanticSessionFacts(
                mode=SemanticRunnerMode.ONE_SHOT,
                file_path=str(request.resolved_file_path),
                content_digest=request.content_digest,
            ),
        )
