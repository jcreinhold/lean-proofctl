"""Backend adapter factory."""

from pathlib import Path

from kan_prover.adapters.base import Backend
from kan_prover.adapters.claude import ClaudeBackend
from kan_prover.adapters.codex import CodexBackend
from kan_prover.adapters.none import NoneBackend


def create_backend(
    *,
    agent: str,
    candidate_files: list[Path] | None = None,
    replay_attempts: list[Path] | None = None,
    decomposition_files: list[Path] | None = None,
    plan_files: list[Path] | None = None,
    theorem_step_files: list[Path] | None = None,
) -> Backend:
    """Build one strict backend from CLI inputs."""
    if agent == "none":
        return NoneBackend(
            candidate_files=candidate_files,
            replay_attempts=replay_attempts,
            decomposition_files=decomposition_files,
            plan_files=plan_files,
            theorem_step_files=theorem_step_files,
        )
    if candidate_files or replay_attempts or decomposition_files or plan_files or theorem_step_files:
        msg = (
            f"`--candidate-file`, `--replay-attempt`, `--decomposition-file`, `--plan-file`, and "
            f"`--theorem-step-file` "
            f"are only supported with `--agent none`, got `{agent}`."
        )
        raise ValueError(msg)
    if agent == "claude":
        return ClaudeBackend()
    if agent == "codex":
        return CodexBackend()
    msg = f"Unknown backend `{agent}`."
    raise ValueError(msg)
