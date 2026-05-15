"""Explicit live semantic runner for repeated same-file interaction."""

import json
import subprocess
from typing import cast

from lean_proofctl.engine.config import ToolPaths, WorkspaceBinding
from lean_proofctl.engine.lean_helpers import (
    LeanHelperCommand,
    LiveFileSessionReuse,
    LiveLeanHelperError,
    ProbeResult,
    decode_probe_result_payload,
    load_probe_payload,
)
from lean_proofctl.engine.semantic_probe_runtime import launch_semantic_probe_server
from lean_proofctl.engine.semantic_runner import (
    SemanticContextProbeResult,
    SemanticContextRequest,
    SemanticRunnerMode,
    SemanticSessionFacts,
)

LIVE_PROBE_SCHEMA_VERSION = "probe.live.v1"


class LiveSemanticSession:
    """Lazy live SemanticProbe session for one repeated theorem-local file lane."""

    def __init__(
        self,
        *,
        tool_paths: ToolPaths,
        workspace: WorkspaceBinding,
        timeout_seconds: float = 60.0,
    ) -> None:
        """Store the bound workspace and delay process startup until the first request."""
        self._tool_paths = tool_paths
        self._workspace = workspace
        self._timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._request_index = 0

    def context_bundle(self, request: SemanticContextRequest) -> SemanticContextProbeResult:
        """Run one theorem-context request against the live file-session runtime."""
        if request.source_text is None:
            msg = "Live semantic requests require explicit file content."
            raise ValueError(msg)
        process = self._ensure_process()
        stdin = process.stdin
        stdout = process.stdout
        if stdin is None or stdout is None:
            msg = "SemanticProbe live server pipes are unavailable."
            raise LiveLeanHelperError(msg)

        self._request_index += 1
        payload = {
            "schema_version": LIVE_PROBE_SCHEMA_VERSION,
            "request_id": f"live-{self._request_index:04d}",
            "command": LeanHelperCommand.CONTEXT_BUNDLE.value,
            "file_path": str(request.resolved_file_path),
            "theorem": request.theorem,
            "content_digest": request.content_digest,
            "content": request.source_text,
        }
        try:
            stdin.write(json.dumps(payload) + "\n")
            stdin.flush()
            line = stdout.readline()
        except OSError as error:
            msg = f"SemanticProbe live server I/O failed: {error!s}"
            raise LiveLeanHelperError(msg) from error
        if not line:
            stderr = process.stderr.read().strip() if process.stderr is not None else ""
            msg = "SemanticProbe live server produced empty stdout."
            if stderr:
                msg = f"{msg} Stderr: {stderr}"
            raise LiveLeanHelperError(msg)

        file_path, content_digest, reuse, probe = _decode_live_context_bundle(
            payload_text=line.strip(),
            file=str(request.resolved_file_path),
            theorem=request.theorem,
        )
        return SemanticContextProbeResult(
            bundle_probe=probe,
            session_facts=SemanticSessionFacts(
                mode=SemanticRunnerMode.LIVE,
                file_path=file_path,
                content_digest=content_digest,
                reuse=reuse.value,
            ),
        )

    def close(self) -> None:
        """Stop the live runtime process when one was started."""
        process = self._process
        if process is None:
            return
        self._process = None
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)

    def __enter__(self) -> LiveSemanticSession:
        """Enter a context manager for one live semantic session."""
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        """Close the live session on context-manager exit."""
        self.close()

    def _ensure_process(self) -> subprocess.Popen[str]:
        process = self._process
        if process is not None:
            return process
        process = launch_semantic_probe_server(
            tool_paths=self._tool_paths,
            workspace=self._workspace,
            timeout_seconds=self._timeout_seconds,
        )
        if process.stdin is None or process.stdout is None:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            process.terminate()
            process.wait(timeout=1.0)
            msg = "SemanticProbe live server did not expose stdin/stdout pipes."
            raise LiveLeanHelperError(msg)
        self._process = process
        return process


def _decode_live_context_bundle(
    *,
    payload_text: str,
    file: str,
    theorem: str,
) -> tuple[str, str, LiveFileSessionReuse, ProbeResult]:
    payload_object = load_probe_payload(payload_text)
    if not isinstance(payload_object, dict):
        msg = "SemanticProbe live server returned invalid JSON."
        raise LiveLeanHelperError(msg)
    payload = cast("dict[str, object]", payload_object)
    schema_version = payload.get("schema_version")
    if schema_version != LIVE_PROBE_SCHEMA_VERSION:
        msg = f"SemanticProbe live server returned unsupported schema version {schema_version!r}."
        raise LiveLeanHelperError(msg)
    status = payload.get("status")
    if status != "ok":
        message = payload.get("message")
        detail = message if isinstance(message, str) else "live request failed"
        raise LiveLeanHelperError(detail)

    session_payload_object = payload.get("session")
    if not isinstance(session_payload_object, dict):
        msg = "SemanticProbe live server response is missing `session`."
        raise LiveLeanHelperError(msg)
    session_payload = cast("dict[str, object]", session_payload_object)
    reuse_text = session_payload.get("reuse")
    if not isinstance(reuse_text, str):
        msg = "SemanticProbe live server response is missing session `reuse`."
        raise LiveLeanHelperError(msg)
    try:
        reuse = LiveFileSessionReuse(reuse_text)
    except ValueError as error:
        msg = f"SemanticProbe live server returned invalid reuse kind {reuse_text!r}."
        raise LiveLeanHelperError(msg) from error

    probe_payload_object = payload.get("probe")
    if not isinstance(probe_payload_object, dict):
        msg = "SemanticProbe live server response is missing `probe`."
        raise LiveLeanHelperError(msg)
    probe_payload = cast("dict[str, object]", probe_payload_object)
    probe = decode_probe_result_payload(
        probe_payload,
        command=LeanHelperCommand.CONTEXT_BUNDLE,
        file=file,
        theorem=theorem,
    )
    file_path = session_payload.get("file_path")
    content_digest = session_payload.get("content_digest")
    if not isinstance(file_path, str):
        msg = "SemanticProbe live server response is missing session `file_path`."
        raise LiveLeanHelperError(msg)
    if not isinstance(content_digest, str):
        msg = "SemanticProbe live server response is missing session `content_digest`."
        raise LiveLeanHelperError(msg)
    return file_path, content_digest, reuse, probe
