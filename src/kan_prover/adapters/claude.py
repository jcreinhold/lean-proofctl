"""Strict headless Claude backend adapter."""

import shutil
import subprocess
from pathlib import Path

from kan_prover.adapters.base import (
    BackendCommandError,
    BackendProtocolError,
    parse_response_payload,
    prompts_root,
    render_prompt,
)
from kan_prover.engine.models import AgentRequest, AgentResponse, BackendInvocation


class ClaudeBackend:
    """Bounded theorem-body synthesizer backed by the Claude CLI."""

    def __init__(self, *, executable: str = "claude") -> None:
        """Store the CLI executable name."""
        self._executable = executable
        self._prompts_root = prompts_root(__file__)

    def name(self) -> str:
        """Return the backend identifier."""
        return "claude"

    def propose(self, request: AgentRequest) -> AgentResponse:
        """Run one strict headless Claude call and parse its JSON payload."""
        executable = shutil.which(self._executable)
        if executable is None:
            msg = f"Claude CLI is not available on PATH: expected `{self._executable}`."
            raise BackendCommandError(backend=self.name(), backend_error_kind="unavailable", message=msg)
        template_path = _template_path(self._prompts_root, request)
        prompt_text = render_prompt(template_path=template_path, request=request)
        argv = [
            executable,
            "--bare",
            "-p",
            prompt_text,
            "--output-format",
            "json",
            "--json-schema",
            request.schema_path,
        ]
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
        raw_output = completed.stdout if completed.stdout.strip() else completed.stderr
        if completed.returncode != 0:
            msg = (
                f"Claude CLI failed with exit code {completed.returncode}. "
                f"stderr: {completed.stderr.strip() or '<empty>'}"
            )
            raise BackendCommandError(backend=self.name(), backend_error_kind="execution-failed", message=msg)
        try:
            (
                result_kind,
                payload,
                candidates,
                candidate_parse_errors,
                decomposition,
                theorem_plan,
                theorem_session_actions,
                message,
            ) = parse_response_payload(
                raw_output=completed.stdout,
                expected_task=request.task_kind,
                backend_name=self.name(),
            )
        except BackendProtocolError as error:
            raise BackendCommandError(
                backend=self.name(),
                backend_error_kind="protocol-failed",
                message=str(error),
            ) from error
        return AgentResponse(
            backend=self.name(),
            result_kind=result_kind,
            invocation=BackendInvocation(argv=argv, cwd=str(Path.cwd())),
            prompt_path=str(template_path),
            prompt_text=prompt_text,
            schema_path=request.schema_path,
            raw_stdout=completed.stdout,
            raw_stderr=completed.stderr,
            raw_output=raw_output,
            parsed_payload=payload,
            candidates=candidates,
            candidate_parse_errors=candidate_parse_errors,
            decomposition=decomposition,
            theorem_plan=theorem_plan,
            theorem_session_actions=theorem_session_actions,
            message=message,
        )


def _template_path(prompts_dir: Path, request: AgentRequest) -> Path:
    template_name = request.prompt_template_name or request.task_kind.value
    return prompts_dir / f"{template_name}.md"
