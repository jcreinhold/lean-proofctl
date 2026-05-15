"""Tests for strict backend request and adapter behavior."""

import subprocess
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from kan_prover.adapters.base import BackendCommandError
from kan_prover.adapters.claude import ClaudeBackend
from kan_prover.adapters.codex import CodexBackend
from kan_prover.adapters.none import NoneBackend
from kan_prover.engine.models import (
    AgentRequest,
    AppendTacticTextAction,
    Diagnostic,
    DiagnosticClass,
    FileSpan,
    ProposeHelperLemmaAction,
    Range,
    RepairFailure,
    RepairStrategy,
    Severity,
    SourceWindow,
    TaskKind,
    TheoremSessionActionKind,
)


class TestStrictAdapters:
    """Unit tests for strict request serialization and backend parsing."""

    def test_strict_request_serialization(self) -> None:
        request = _sample_request()
        payload = request.to_json()
        diagnostics = cast(list[dict[str, object]], payload["diagnostics"])
        assert payload["task_kind"] == "repair"
        assert payload["theorem_name"] == "Demo.fix_me"
        assert payload["target_module_path"] == "Problems/Demo.lean"
        assert payload["helper_module_root"] == "KanProverGenerated.Helpers"
        assert payload["editable_span"] == {"start_line": 10, "end_line": 12}
        assert payload["allowed_output_files"] == ["Problems/Demo.lean"]
        assert payload["repair_strategy"] == "targeted-correction"
        selected_failure = cast(dict[str, object], payload["selected_failure"])
        assert selected_failure["diagnostic_class"] == "unsolved-goals"
        assert diagnostics[0]["class"] == "unsolved-goals"

    def test_none_backend_is_deterministic_per_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            first = temp_root / "first.lean"
            second = temp_root / "second.lean"
            first.write_text("by\n  exact False.elim ?h\n", encoding="utf-8")
            second.write_text("by\n  exact True.intro\n", encoding="utf-8")
            backend = NoneBackend(candidate_files=[first, second])
            first_response = backend.propose(_sample_request(attempt_index=1))
            second_response = backend.propose(_sample_request(attempt_index=2))
            third_response = backend.propose(_sample_request(attempt_index=3))
            assert first_response.candidates[0].body == "by\n  exact False.elim ?h\n"
            assert second_response.candidates[0].body == "by\n  exact True.intro\n"
            assert third_response.result_kind.value == "no_progress"

    def test_none_backend_replays_deterministic_theorem_step_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            step_file = temp_root / "theorem-step.json"
            step_file.write_text(
                (
                    "{\n"
                    '  "actions": [\n'
                    "    {\n"
                    '      "action_id": "action-001",\n'
                    '      "kind": "append_tactic_text",\n'
                    '      "tactic_text": "  exact True.intro",\n'
                    '      "rationale": "The goal is `True`."\n'
                    "    },\n"
                    "    {\n"
                    '      "action_id": "action-002",\n'
                    '      "kind": "propose_helper_lemma",\n'
                    '      "helper": {\n'
                    '        "helper_name": "helper_truth",\n'
                    '        "helper_statement": "theorem helper_truth : True :=",\n'
                    '        "proof_stub": "by\\n  exact True.intro\\n",\n'
                    '        "helper_imports": ["KanRuntime"]\n'
                    "      }\n"
                    "    }\n"
                    "  ]\n"
                    "}\n"
                ),
                encoding="utf-8",
            )
            backend = NoneBackend(theorem_step_files=[step_file])
            response = backend.propose(_sample_request(task_kind=TaskKind.THEOREM_STEP))
            assert response.result_kind.value == "theorem_session_actions"
            assert len(response.theorem_session_actions) == 2
            assert isinstance(response.theorem_session_actions[0], AppendTacticTextAction)
            assert isinstance(response.theorem_session_actions[1], ProposeHelperLemmaAction)
            helper_action = response.theorem_session_actions[1]
            assert isinstance(helper_action, ProposeHelperLemmaAction)
            assert helper_action.helper.helper_name == "helper_truth"

    @patch("kan_prover.adapters.claude.subprocess.run")
    @patch("kan_prover.adapters.claude.shutil.which")
    def test_claude_adapter_parses_structured_output(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_which.return_value = "/usr/bin/claude"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=(
                '{"schema_version":"v1","task_kind":"repair","result_kind":"theorem_body_replacements",'
                '"candidates":[{"candidate_id":"candidate-001","theorem_name":"Demo.fix_me","body":"by\\n  exact '
                'True.intro\\n"}]}\n'
            ),
            stderr="",
        )
        response = ClaudeBackend().propose(_sample_request())
        assert response.backend == "claude"
        assert response.candidates[0].theorem == "Demo.fix_me"
        assert "--json-schema" in response.invocation.argv

    @patch("kan_prover.adapters.codex.subprocess.run")
    @patch("kan_prover.adapters.codex.shutil.which")
    def test_codex_adapter_parses_jsonl_result_payload(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_which.return_value = "/usr/bin/codex"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout=(
                '{"event":"started"}\n'
                '{"event":"result","payload":{"schema_version":"v1","task_kind":"repair","result_kind":"no_progress",'
                '"message":"stuck"}}\n'
            ),
            stderr="",
        )
        response = CodexBackend().propose(_sample_request())
        assert response.backend == "codex"
        assert response.result_kind.value == "no_progress"
        assert response.message == "stuck"

    @patch("kan_prover.adapters.codex.subprocess.run")
    @patch("kan_prover.adapters.codex.shutil.which")
    def test_codex_adapter_accepts_empty_candidate_list_for_engine_classification(
        self, mock_which: MagicMock, mock_run: MagicMock
    ) -> None:
        mock_which.return_value = "/usr/bin/codex"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout=(
                '{"schema_version":"v1","task_kind":"repair","result_kind":"theorem_body_replacements",'
                '"candidates":[],"message":"nothing usable"}\n'
            ),
            stderr="",
        )
        response = CodexBackend().propose(_sample_request())
        assert response.result_kind.value == "theorem_body_replacements"
        assert response.candidates == []
        assert response.candidate_parse_errors == []
        assert response.message == "nothing usable"

    @patch("kan_prover.adapters.claude.subprocess.run")
    @patch("kan_prover.adapters.claude.shutil.which")
    def test_claude_adapter_keeps_valid_candidates_and_records_invalid_ones(
        self, mock_which: MagicMock, mock_run: MagicMock
    ) -> None:
        mock_which.return_value = "/usr/bin/claude"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=(
                '{"schema_version":"v1","task_kind":"repair","result_kind":"theorem_body_replacements",'
                '"candidates":[{"candidate_id":"candidate-001","theorem_name":"Demo.fix_me","body":"by\\n  exact '
                'True.intro\\n"},{"candidate_id":"candidate-002","theorem_name":"Demo.fix_me"}]}\n'
            ),
            stderr="",
        )
        response = ClaudeBackend().propose(_sample_request())
        assert [candidate.candidate_id for candidate in response.candidates] == ["candidate-001"]
        assert len(response.candidate_parse_errors) == 1
        assert "candidate 2" in response.candidate_parse_errors[0]

    @patch("kan_prover.adapters.claude.subprocess.run")
    @patch("kan_prover.adapters.claude.shutil.which")
    def test_claude_adapter_parses_theorem_step_actions(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_which.return_value = "/usr/bin/claude"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=(
                '{"schema_version":"v1","task_kind":"theorem_step","result_kind":"theorem_session_actions",'
                '"actions":[{"action_id":"action-001","kind":"append_tactic_text","tactic_text":"  exact True.intro"},'
                '{"action_id":"action-002","kind":"suggest_checkpoint_label","label":"after-intro"}]}\n'
            ),
            stderr="",
        )
        response = ClaudeBackend().propose(_sample_request(task_kind=TaskKind.THEOREM_STEP))
        assert response.result_kind.value == "theorem_session_actions"
        assert response.theorem_session_actions[0].kind == TheoremSessionActionKind.APPEND_TACTIC_TEXT
        assert response.theorem_session_actions[1].kind == TheoremSessionActionKind.SUGGEST_CHECKPOINT_LABEL
        assert response.prompt_path.endswith("theorem_step.md")

    @patch("kan_prover.adapters.claude.subprocess.run")
    @patch("kan_prover.adapters.claude.shutil.which")
    def test_malformed_backend_output_fails_closed(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_which.return_value = "/usr/bin/claude"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout='{"task_kind":"repair"}\n',
            stderr="",
        )
        with pytest.raises(BackendCommandError) as error:
            ClaudeBackend().propose(_sample_request())
        assert error.value.backend == "claude"
        assert error.value.backend_error_kind == "protocol-failed"

    @patch("kan_prover.adapters.codex.shutil.which", return_value=None)
    def test_unavailable_backend_fails_clearly(self, _mock_which: MagicMock) -> None:
        with pytest.raises(BackendCommandError) as error:
            CodexBackend().propose(_sample_request())
        assert error.value.backend == "codex"
        assert error.value.backend_error_kind == "unavailable"


def _sample_request(*, attempt_index: int = 1, task_kind: TaskKind = TaskKind.REPAIR) -> AgentRequest:
    return AgentRequest(
        task_kind=task_kind,
        attempt_index=attempt_index,
        candidate_limit=1,
        theorem_name="Demo.fix_me",
        target_file="Problems/Demo.lean",
        workspace_root="/tmp/workspace",
        workspace_package="Demo",
        sandbox_workspace_root="/tmp/workspace/.sandbox",
        target_module_path="Problems/Demo.lean",
        helper_module_root="KanProverGenerated.Helpers",
        helper_root_dir="/tmp/workspace/.sandbox/KanProverGenerated/Helpers",
        theorem_statement="theorem Demo.fix_me : True :=",
        editable_span=FileSpan(start_line=10, end_line=12),
        source_window=SourceWindow(
            file="Problems/Demo.lean",
            start_line=4,
            end_line=14,
            text="theorem Demo.fix_me : True :=\nby\n  exact False.elim ?h\n",
        ),
        diagnostics=[
            Diagnostic(
                file="Problems/Demo.lean",
                range=Range(startLine=11, startCol=2, endLine=11, endCol=24),
                severity=Severity.ERROR,
                message="unsolved goals",
                class_name=DiagnosticClass.UNSOLVED_GOALS,
            )
        ],
        prior_attempts=[],
        retrieval_hits=[],
        active_helpers=[],
        allowed_output_files=["Problems/Demo.lean"],
        repair_strategy=RepairStrategy.TARGETED_CORRECTION,
        selected_failure=RepairFailure(
            diagnostic_index=0,
            diagnostic_class=DiagnosticClass.UNSOLVED_GOALS,
            severity=Severity.ERROR,
            file="Problems/Demo.lean",
            range=Range(startLine=11, startCol=2, endLine=11, endCol=24),
            message="unsolved goals",
            subexpression="⊢ True",
        ),
        session_id="session-001" if task_kind is TaskKind.THEOREM_STEP else None,
        current_draft="by\n  exact ?goal\n" if task_kind is TaskKind.THEOREM_STEP else None,
        checkpoint_labels=["start"] if task_kind is TaskKind.THEOREM_STEP else [],
        schema_path="/tmp/result.schema.json",
    )
