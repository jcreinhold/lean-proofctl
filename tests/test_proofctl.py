"""Tests for the syntax and semantic probe boundary."""

import ast
import io
import json
import shutil
import subprocess
import tempfile
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from kan_prover.adapters.base import BackendCommandError
from kan_prover.cli.commands import repair
from kan_prover.cli.main import main
from kan_prover.engine.config import ToolPaths, bind_workspace, discover_tool_paths
from kan_prover.engine.inspection import build_inspection_state, run_inspect, run_retrieve
from kan_prover.engine.lean_helpers import (
    LeanHelperCommand,
    LeanHelperErrorKind,
    LeanHelperRequest,
    LiveFileSessionReuse,
    ProbePoint,
    ProbeSpan,
    ProofctlBatchDeclarationInventory,
    ProofctlBatchDeclarationInventoryFile,
    ProofctlDeclarationInventory,
    ProofctlDeclarationMetadata,
    ProofctlGoalSnapshot,
    ProofctlGroupMembership,
    ProofctlLocalContext,
    ProofctlProofState,
    ProofctlSafeEditBoundary,
    ProofctlSemanticContextBundle,
    ProofctlWorkspaceDeclarationIndex,
    run_lean_helper,
)
from kan_prover.engine.patching import replace_theorem_body
from kan_prover.engine.proofctl import (
    ProbeLayer,
    ProbeResult,
    ProbeStatus,
    ProofctlDeclarationTarget,
    ProofctlImportsAndNamespace,
    ProofctlInsertionPoint,
    ProofctlTheoremSpan,
    RequiredWorkspaceProbeError,
    SemanticContextProbes,
    UnsupportedTargetError,
    batch_declaration_inventory,
    declaration_inventory,
    declaration_target,
    goal_snapshot,
    imports_and_namespace,
    insertion_point,
    local_context,
    proof_state,
    semantic_context_bundle,
    theorem_span,
)
from kan_prover.engine.sandbox import create_sandbox_workspace
from kan_prover.engine.semantic_cache import clear_memory_cache
from kan_prover.engine.semantic_live import LiveSemanticSession
from kan_prover.engine.semantic_runner import SemanticContextRequest
from kan_prover.engine.source_probing import (
    imports_and_namespace_from_source,
    theorem_span_from_source,
)
from kan_prover.engine.theorem_locator import locate_theorem_span_from_probe, locate_theorem_span_in_file
from tests.workspaces import downstream_kan_workspace

GOOD_BODY = """by
  exact True.intro
"""

type JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
type JsonObject = dict[str, JsonValue]


class TestProofctl:
    """Integration and contract tests for probe execution."""

    @classmethod
    def setup_class(cls) -> None:
        cls.tool_paths = discover_tool_paths()
        cls.repo_root = downstream_kan_workspace(cls.tool_paths.root)
        cls.runtime_workspace_root = cls.tool_paths.root / "lean-runtime"
        cls.repo_target = cls.repo_root / "KanProofs" / "Algebra" / "ControlFlowGrades.lean"
        cls.repo_prop_target = cls.repo_root / "KanProofs" / "Structural" / "Substitution" / "ValueSubstitution.lean"
        cls.runtime_lemma_fixture = (
            cls.runtime_workspace_root / "Tools" / "SemanticProbe" / "Fixtures" / "SemanticProbeLemmaFixture.lean"
        )
        cls.repo_mutual_target = cls.repo_root / "KanProofs" / "Structural" / "TypedSubstitution.lean"
        cls.fixtures = cls.tool_paths.root / "tests" / "fixtures"

    @pytest.mark.integration
    def test_theorem_span_probe_finds_real_kanproofs_theorem(self) -> None:
        result = theorem_span(file_path=self.repo_target, theorem="le_refl")
        assert result.status == ProbeStatus.OK
        payload = cast(ProofctlTheoremSpan, result.result)
        assert payload.declaration_name == "CFGrade.le_refl"
        assert payload.namespace == "CFGrade"
        assert payload.body_span.start.line == self._line_number(
            self.repo_target,
            "theorem le_refl (cf : CFGrade) : cf ≤ cf := by",
        )
        assert payload.declaration_span.start.line <= payload.body_span.start.line

    @pytest.mark.integration
    def test_imports_and_namespace_probe_reports_real_file_namespace(self) -> None:
        result = imports_and_namespace(file_path=self.repo_target, theorem="CFGrade.le_refl")
        assert result.status == ProbeStatus.OK
        payload = cast(ProofctlImportsAndNamespace, result.result)
        assert payload.declaration_name == "CFGrade.le_refl"
        assert payload.namespace == "CFGrade"
        assert "KanProofs.Algebra.Types" in payload.imports
        assert "Mathlib.Order.GaloisConnection.Defs" in payload.imports

    def test_lean_helper_binding_decodes_successful_declaration_target(self) -> None:
        request = self._helper_request(command=LeanHelperCommand.DECLARATION_TARGET)
        payload = self._helper_payload(
            command=LeanHelperCommand.DECLARATION_TARGET,
            status="ok",
            result={
                "module_path": "KanProofs.Structural.TypedSubstitution",
                "short_name": "substitution_comp_core",
                "declaration_name": "substitution_comp_core",
                "namespace": None,
                "namespace_stack": [],
                "imports": ["KanProofs.Structural.TypedSubstitution"],
                "keyword": "theorem",
                "modifiers": ["private"],
                "group": {"kind": "mutual", "group_id": "mutual-1", "member_index": 2},
                "declaration_span": self._span_payload(619, 0, 631, 0),
                "name_span": self._span_payload(619, 16, 619, 38),
                "body_span": self._span_payload(619, 64, 631, 0),
                "safe_edit": {
                    "kind": "replace-body",
                    "declaration_span": self._span_payload(619, 0, 631, 0),
                    "body_span": self._span_payload(619, 64, 631, 0),
                },
                "proof_state_available": True,
            },
        )
        with (
            patch(
                "kan_prover.engine.lean_helpers.ensure_semantic_probe_executable",
                return_value=self.tool_paths.bundled_demo_runtime / ".lake" / "build" / "bin" / "SemanticProbe",
            ),
            patch(
                "kan_prover.engine.lean_helpers.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                ),
            ),
        ):
            result = run_lean_helper(request)
        assert result.status == ProbeStatus.OK
        assert result.error_kind is None
        target = cast(ProofctlDeclarationTarget, result.result)
        assert target.declaration_name == "substitution_comp_core"
        assert target.group.kind == "mutual"
        assert target.group.member_index == 2

    def test_lean_helper_binding_reports_non_proof_declaration_target_as_unavailable(self) -> None:
        request = self._helper_request(command=LeanHelperCommand.DECLARATION_TARGET)
        payload = self._helper_payload(
            command=LeanHelperCommand.DECLARATION_TARGET,
            status="unavailable",
            message=(
                "`substitution_comp` is a `def` declaration. `declaration-inventory` and "
                "`workspace-declaration-index` expose declaration metadata, but proof-target "
                "workflows only support proof declarations with editable proof bodies"
            ),
        )
        with (
            patch(
                "kan_prover.engine.lean_helpers.ensure_semantic_probe_executable",
                return_value=self.tool_paths.bundled_demo_runtime / ".lake" / "build" / "bin" / "SemanticProbe",
            ),
            patch(
                "kan_prover.engine.lean_helpers.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                ),
            ),
        ):
            result = run_lean_helper(request)
        assert result.status == ProbeStatus.UNAVAILABLE
        assert result.result is None
        assert "proof-target workflows only support proof declarations" in cast(str, result.message)

    def test_lean_helper_binding_decodes_successful_declaration_inventory(self) -> None:
        request = self._helper_request(command=LeanHelperCommand.DECLARATION_INVENTORY)
        payload = self._helper_payload(
            command=LeanHelperCommand.DECLARATION_INVENTORY,
            status="ok",
            result={
                "entries": [
                    {
                        "module_path": "KanProofs.Structural.TypedSubstitution",
                        "short_name": "substitution_value_core",
                        "declaration_name": "substitution_value_core",
                        "namespace": None,
                        "namespace_stack": [],
                        "imports": ["KanProofs.Structural.TypedSubstitution"],
                        "declaration_kind": "theorem",
                        "modifiers": ["private"],
                        "group": {"kind": "mutual", "group_id": "mutual-1", "member_index": 1},
                        "declaration_span": self._span_payload(602, 0, 619, 0),
                        "name_span": self._span_payload(602, 16, 602, 39),
                        "body_span": self._span_payload(603, 15, 619, 0),
                        "safe_edit": {
                            "kind": "replace-body",
                            "declaration_span": self._span_payload(602, 0, 619, 0),
                            "body_span": self._span_payload(603, 15, 619, 0),
                        },
                    },
                    {
                        "module_path": "KanProofs.Structural.TypedSubstitution",
                        "short_name": "substitution_comp_core",
                        "declaration_name": "substitution_comp_core",
                        "namespace": None,
                        "namespace_stack": [],
                        "imports": ["KanProofs.Structural.TypedSubstitution"],
                        "declaration_kind": "theorem",
                        "modifiers": ["private"],
                        "group": {"kind": "mutual", "group_id": "mutual-1", "member_index": 2},
                        "declaration_span": self._span_payload(619, 0, 631, 0),
                        "name_span": self._span_payload(619, 16, 619, 38),
                        "body_span": self._span_payload(620, 15, 631, 0),
                        "safe_edit": {
                            "kind": "replace-body",
                            "declaration_span": self._span_payload(619, 0, 631, 0),
                            "body_span": self._span_payload(620, 15, 631, 0),
                        },
                    },
                ]
            },
        )
        with (
            patch(
                "kan_prover.engine.lean_helpers.ensure_semantic_probe_executable",
                return_value=self.tool_paths.bundled_demo_runtime / ".lake" / "build" / "bin" / "SemanticProbe",
            ),
            patch(
                "kan_prover.engine.lean_helpers.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                ),
            ),
        ):
            result = run_lean_helper(request)
        assert result.status == ProbeStatus.OK
        inventory = cast(ProofctlDeclarationInventory, result.result)
        assert [entry.short_name for entry in inventory.entries] == [
            "substitution_value_core",
            "substitution_comp_core",
        ]
        assert all(entry.group.kind == "mutual" for entry in inventory.entries)

    def test_lean_helper_binding_decodes_successful_batch_declaration_inventory(self) -> None:
        request = LeanHelperRequest(
            tool_paths=self.tool_paths,
            workspace=bind_workspace(self.repo_root),
            file_path=self.repo_mutual_target,
            theorem="*",
            command=LeanHelperCommand.BATCH_DECLARATION_INVENTORY,
            requires_writable=False,
            extra_args=("/tmp/frontier.json",),
        )
        payload = self._helper_payload(
            command=LeanHelperCommand.BATCH_DECLARATION_INVENTORY,
            status="ok",
            result={
                "files": [
                    {
                        "file_path": str(self.repo_mutual_target),
                        "entries": [
                            {
                                "module_path": "KanProofs.Structural.TypedSubstitution",
                                "short_name": "substitution_value_core",
                                "declaration_name": "substitution_value_core",
                                "namespace": None,
                                "namespace_stack": [],
                                "imports": ["KanProofs.Structural.TypedSubstitution"],
                                "declaration_kind": "theorem",
                                "modifiers": ["private"],
                                "group": {"kind": "mutual", "group_id": "mutual-1", "member_index": 1},
                                "declaration_span": self._span_payload(602, 0, 619, 0),
                                "name_span": self._span_payload(602, 16, 602, 39),
                                "body_span": self._span_payload(603, 15, 619, 0),
                            }
                        ],
                    }
                ]
            },
        )
        with (
            patch(
                "kan_prover.engine.lean_helpers.ensure_semantic_probe_executable",
                return_value=self.tool_paths.bundled_demo_runtime / ".lake" / "build" / "bin" / "SemanticProbe",
            ),
            patch(
                "kan_prover.engine.lean_helpers.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                ),
            ),
        ):
            result = run_lean_helper(request)
        assert result.status == ProbeStatus.OK
        inventory = cast(ProofctlBatchDeclarationInventory, result.result)
        assert len(inventory.files) == 1
        assert inventory.files[0].file_path == str(self.repo_mutual_target)
        assert [entry.short_name for entry in inventory.files[0].entries] == ["substitution_value_core"]

    def test_lean_helper_binding_decodes_successful_workspace_declaration_index(self) -> None:
        request = LeanHelperRequest(
            tool_paths=self.tool_paths,
            workspace=bind_workspace(self.repo_root),
            file_path=self.repo_target,
            theorem="*",
            command=LeanHelperCommand.WORKSPACE_DECLARATION_INDEX,
            requires_writable=False,
            extra_args=("/tmp/workspace-modules.json",),
        )
        payload = self._helper_payload(
            command=LeanHelperCommand.WORKSPACE_DECLARATION_INDEX,
            status="ok",
            result={
                "entries": [
                    {
                        "file_path": str(self.repo_target),
                        "module_path": "KanProofs.Algebra.ControlFlowGrades",
                        "declaration_name": "CFGrade.le_refl",
                        "short_name": "le_refl",
                        "namespace": "CFGrade",
                        "declaration_kind": "theorem",
                        "declaration_span": self._span_payload(33, 0, 36, 0),
                        "type_text": "grade <= grade",
                    }
                ]
            },
        )
        with (
            patch(
                "kan_prover.engine.lean_helpers.ensure_semantic_probe_executable",
                return_value=self.tool_paths.bundled_demo_runtime / ".lake" / "build" / "bin" / "SemanticProbe",
            ),
            patch(
                "kan_prover.engine.lean_helpers.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                ),
            ),
        ):
            result = run_lean_helper(request)
        assert result.status == ProbeStatus.OK
        index = cast(ProofctlWorkspaceDeclarationIndex, result.result)
        assert len(index.entries) == 1
        assert index.entries[0].declaration_name == "CFGrade.le_refl"
        assert index.entries[0].type_text == "grade <= grade"

    def test_lean_helper_binding_tolerates_lake_bootstrap_logs_before_json(self) -> None:
        request = self._helper_request(command=LeanHelperCommand.DECLARATION_TARGET)
        payload = self._helper_payload(
            command=LeanHelperCommand.DECLARATION_TARGET,
            status="ok",
            result={
                "module_path": "MiniF2FBench.TestSubset",
                "short_name": "mathd_numbertheory_3",
                "declaration_name": "mathd_numbertheory_3",
                "namespace": None,
                "namespace_stack": [],
                "imports": ["Mathlib"],
                "keyword": "theorem",
                "modifiers": [],
                "group": {"kind": "standalone"},
                "declaration_span": self._span_payload(1, 0, 3, 0),
                "name_span": self._span_payload(1, 8, 1, 27),
                "body_span": self._span_payload(2, 0, 3, 0),
                "safe_edit": {
                    "kind": "replace-body",
                    "declaration_span": self._span_payload(1, 0, 3, 0),
                    "body_span": self._span_payload(2, 0, 3, 0),
                },
                "proof_state_available": True,
            },
        )
        stdout = "\n".join(
            [
                "info: MiniF2FBench: no previous manifest, creating one from scratch",
                "info: leanprover-community/mathlib: cloning https://github.com/leanprover-community/mathlib4",
                json.dumps(payload),
            ]
        )
        with (
            patch(
                "kan_prover.engine.lean_helpers.ensure_semantic_probe_executable",
                return_value=self.tool_paths.bundled_demo_runtime / ".lake" / "build" / "bin" / "SemanticProbe",
            ),
            patch(
                "kan_prover.engine.lean_helpers.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=stdout,
                    stderr="",
                ),
            ),
        ):
            result = run_lean_helper(request)
        assert result.status == ProbeStatus.OK
        target = cast(ProofctlDeclarationTarget, result.result)
        assert target.declaration_name == "mathd_numbertheory_3"

    def test_lean_helper_binding_decodes_successful_proof_state(self) -> None:
        request = self._helper_request(command=LeanHelperCommand.PROOF_STATE)
        payload = self._helper_payload(
            command=LeanHelperCommand.PROOF_STATE,
            status="ok",
            result={
                "source": "tactic-goals-before",
                "declaration_name": "substitution_comp_core",
                "namespace": None,
                "locals": [
                    {
                        "name": "Γ",
                        "binder_info": "default",
                        "type": "Ctx",
                    }
                ],
                "goals": ["Γ ⊢ True"],
                "insertion_point": {
                    "kind": "before-theorem",
                    "insert_position": {"line": 619, "column": 0},
                },
            },
        )
        with (
            patch(
                "kan_prover.engine.lean_helpers.ensure_semantic_probe_executable",
                return_value=self.tool_paths.bundled_demo_runtime / ".lake" / "build" / "bin" / "SemanticProbe",
            ),
            patch(
                "kan_prover.engine.lean_helpers.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                ),
            ),
        ):
            result = run_lean_helper(request)
        assert result.status == ProbeStatus.OK
        proof_state_payload = cast(ProofctlProofState, result.result)
        assert proof_state_payload.declaration_name == "substitution_comp_core"
        assert proof_state_payload.local_context == ["Γ: Ctx"]
        assert proof_state_payload.goals == ["Γ ⊢ True"]
        assert proof_state_payload.insertion_point.position.line == 619

    def test_lean_helper_binding_decodes_successful_context_bundle(self) -> None:
        request = self._helper_request(command=LeanHelperCommand.CONTEXT_BUNDLE)
        payload = self._helper_payload(
            command=LeanHelperCommand.CONTEXT_BUNDLE,
            status="ok",
            result={
                "declaration_target": {
                    "module_path": "KanProofs.Structural.TypedSubstitution",
                    "short_name": "substitution_comp_core",
                    "declaration_name": "substitution_comp_core",
                    "namespace": None,
                    "namespace_stack": [],
                    "imports": ["KanProofs.Structural.TypedSubstitution"],
                    "keyword": "theorem",
                    "modifiers": ["private"],
                    "group": {"kind": "mutual", "group_id": "mutual-1", "member_index": 2},
                    "declaration_span": self._span_payload(619, 0, 631, 0),
                    "name_span": self._span_payload(619, 16, 619, 38),
                    "body_span": self._span_payload(620, 15, 631, 0),
                    "safe_edit": {
                        "kind": "replace-body",
                        "declaration_span": self._span_payload(619, 0, 631, 0),
                        "body_span": self._span_payload(620, 15, 631, 0),
                    },
                    "proof_state_available": True,
                },
                "proof_state": {
                    "source": "tactic-goals-before",
                    "declaration_name": "substitution_comp_core",
                    "namespace": None,
                    "locals": [
                        {
                            "name": "Γ",
                            "binder_info": "default",
                            "type": "Ctx",
                        }
                    ],
                    "goals": ["Γ ⊢ True"],
                    "insertion_point": {
                        "kind": "before-theorem",
                        "insert_position": {"line": 619, "column": 0},
                    },
                },
            },
        )
        with (
            patch(
                "kan_prover.engine.lean_helpers.ensure_semantic_probe_executable",
                return_value=self.tool_paths.bundled_demo_runtime / ".lake" / "build" / "bin" / "SemanticProbe",
            ),
            patch(
                "kan_prover.engine.lean_helpers.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                ),
            ),
        ):
            result = run_lean_helper(request)
        assert result.status == ProbeStatus.OK
        bundle = cast(ProofctlSemanticContextBundle, result.result)
        assert bundle.declaration_target.group.kind == "mutual"
        assert bundle.declaration_target.group.member_index == 2
        assert bundle.proof_state is not None
        assert bundle.proof_state.local_context == ["Γ: Ctx"]
        assert bundle.proof_state.goals == ["Γ ⊢ True"]

    def test_lean_helper_binding_reports_helper_status_errors(self) -> None:
        request = self._helper_request(command=LeanHelperCommand.PROOF_STATE)
        payload = self._helper_payload(
            command=LeanHelperCommand.PROOF_STATE,
            status="error",
            message="no info tree is available for the target theorem",
        )
        with (
            patch(
                "kan_prover.engine.lean_helpers.ensure_semantic_probe_executable",
                return_value=self.tool_paths.bundled_demo_runtime / ".lake" / "build" / "bin" / "SemanticProbe",
            ),
            patch(
                "kan_prover.engine.lean_helpers.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=1,
                    stdout=json.dumps(payload),
                    stderr="",
                ),
            ),
        ):
            result = run_lean_helper(request)
        assert result.status == ProbeStatus.ERROR
        assert result.error_kind == LeanHelperErrorKind.HELPER_STATUS
        assert result.message == "no info tree is available for the target theorem"

    def test_read_only_lean_helper_invokes_built_executable_with_workspace_overlay(self) -> None:
        request = self._helper_request(command=LeanHelperCommand.PROOF_STATE)
        read_only_workspace = replace(request.workspace, writable=False)
        payload = self._helper_payload(
            command=LeanHelperCommand.PROOF_STATE,
            status="ok",
            result={
                "source": "tactic-goals-before",
                "declaration_name": "substitution_comp_core",
                "namespace": None,
                "locals": [],
                "goals": ["True"],
                "insertion_point": {
                    "kind": "before-theorem",
                    "insert_position": {"line": 619, "column": 0},
                },
            },
        )
        with (
            patch(
                "kan_prover.engine.lean_helpers.ensure_semantic_probe_executable",
                return_value=self.tool_paths.bundled_demo_runtime / ".lake" / "build" / "bin" / "SemanticProbe",
            ),
            patch("kan_prover.engine.lean_helpers.subprocess.run") as subprocess_run,
        ):
            subprocess_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )
            result = run_lean_helper(replace(request, workspace=read_only_workspace))
        assert result.status == ProbeStatus.OK
        argv = subprocess_run.call_args.args[0]
        assert argv[0] == str(self.tool_paths.bundled_demo_runtime / ".lake" / "build" / "bin" / "SemanticProbe")
        env = subprocess_run.call_args.kwargs["env"]
        entries = env["LEAN_PATH"].split(":")
        assert entries[0] == str(read_only_workspace.build_dir / "lib" / "lean")
        assert str(read_only_workspace.root) not in entries

    def test_lean_helper_binding_reports_malformed_json(self) -> None:
        request = self._helper_request(command=LeanHelperCommand.DECLARATION_TARGET)
        with (
            patch(
                "kan_prover.engine.lean_helpers.ensure_semantic_probe_executable",
                return_value=self.tool_paths.bundled_demo_runtime / ".lake" / "build" / "bin" / "SemanticProbe",
            ),
            patch(
                "kan_prover.engine.lean_helpers.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=1,
                    stdout="{not json",
                    stderr="bad json",
                ),
            ),
        ):
            result = run_lean_helper(request)
        assert result.status == ProbeStatus.ERROR
        assert result.error_kind == LeanHelperErrorKind.MALFORMED_JSON
        assert result.message == "bad json"

    def test_lean_helper_binding_reports_bootstrap_failures(self) -> None:
        request = self._helper_request(command=LeanHelperCommand.DECLARATION_TARGET)
        with patch(
            "kan_prover.engine.lean_helpers.ensure_semantic_probe_executable",
            side_effect=OSError("build failed"),
        ):
            result = run_lean_helper(request)
        assert result.status == ProbeStatus.ERROR
        assert result.error_kind == LeanHelperErrorKind.BOOTSTRAP_FAILED
        assert result.message == "Could not prepare semantic probe executable: build failed"

    def test_lean_helper_binding_reports_typed_decode_failure(self) -> None:
        request = self._helper_request(command=LeanHelperCommand.DECLARATION_TARGET)
        payload = self._helper_payload(
            command=LeanHelperCommand.DECLARATION_TARGET,
            status="ok",
            result={
                "module_path": "KanProofs.Structural.TypedSubstitution",
                "short_name": "substitution_comp_core",
                "declaration_name": "substitution_comp_core",
                "namespace": None,
                "namespace_stack": [],
                "imports": [],
                "keyword": "theorem",
                "modifiers": ["private"],
                "declaration_span": self._span_payload(619, 0, 631, 0),
                "name_span": self._span_payload(619, 16, 619, 38),
                "body_span": self._span_payload(619, 64, 631, 0),
                "safe_edit": {
                    "kind": "replace-body",
                    "declaration_span": self._span_payload(619, 0, 631, 0),
                    "body_span": self._span_payload(619, 64, 631, 0),
                },
                "proof_state_available": True,
            },
        )
        with (
            patch(
                "kan_prover.engine.lean_helpers.ensure_semantic_probe_executable",
                return_value=self.tool_paths.bundled_demo_runtime / ".lake" / "build" / "bin" / "SemanticProbe",
            ),
            patch(
                "kan_prover.engine.lean_helpers.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(payload),
                    stderr="",
                ),
            ),
        ):
            result = run_lean_helper(request)
        assert result.status == ProbeStatus.ERROR
        assert result.error_kind == LeanHelperErrorKind.DECODE_FAILED
        assert result.message is not None
        assert "group" in result.message

    @pytest.mark.integration
    def test_declaration_target_probe_reports_fixture_lemma_contract(self) -> None:
        workspace = bind_workspace(self.runtime_workspace_root)
        result = declaration_target(
            tool_paths=self.tool_paths,
            workspace=workspace,
            file_path=self.runtime_lemma_fixture,
            theorem="helper_true",
        )
        assert result.status == ProbeStatus.OK
        payload = cast(ProofctlDeclarationTarget, result.result)
        assert payload.module_path == "Tools.SemanticProbe.Fixtures.SemanticProbeLemmaFixture"
        assert payload.declaration_name == "helper_true"
        assert payload.group.kind == "standalone"
        assert payload.keyword == "lemma"
        assert payload.safe_edit is not None
        assert payload.safe_edit.kind == "replace-body"
        assert payload.proof_state_available is True

    @pytest.mark.integration
    def test_lean_helper_binding_reaches_real_declaration_target(self) -> None:
        workspace = bind_workspace(self.runtime_workspace_root)
        result = run_lean_helper(
            LeanHelperRequest(
                tool_paths=self.tool_paths,
                workspace=workspace,
                file_path=self.runtime_lemma_fixture,
                theorem="helper_true",
                command=LeanHelperCommand.DECLARATION_TARGET,
                requires_writable=False,
            )
        )
        assert result.status == ProbeStatus.OK
        payload = cast(ProofctlDeclarationTarget, result.result)
        assert payload.declaration_name == "helper_true"
        assert payload.module_path == "Tools.SemanticProbe.Fixtures.SemanticProbeLemmaFixture"
        assert payload.keyword == "lemma"
        assert payload.safe_edit is not None
        assert payload.proof_state_available is True

    @pytest.mark.integration
    def test_declaration_inventory_probe_preserves_fixture_lemma_metadata(self) -> None:
        workspace = bind_workspace(self.runtime_workspace_root)
        result = declaration_inventory(
            tool_paths=self.tool_paths,
            workspace=workspace,
            file_path=self.runtime_lemma_fixture,
        )
        assert result.status == ProbeStatus.OK
        inventory = cast(ProofctlDeclarationInventory, result.result)
        entries = {entry.short_name: entry for entry in inventory.entries}
        assert entries["helper_true"].declaration_kind == "lemma"
        assert entries["helper_statement"].declaration_kind == "def"
        assert entries["helperInst"].declaration_kind == "instance"

    @pytest.mark.integration
    def test_declaration_target_probe_accepts_fixture_lemma(self) -> None:
        workspace = bind_workspace(self.runtime_workspace_root)
        result = declaration_target(
            tool_paths=self.tool_paths,
            workspace=workspace,
            file_path=self.runtime_lemma_fixture,
            theorem="helper_true",
        )
        assert result.status == ProbeStatus.OK
        payload = cast(ProofctlDeclarationTarget, result.result)
        assert payload.declaration_name == "helper_true"
        assert payload.keyword == "lemma"
        assert payload.safe_edit is not None
        assert payload.proof_state_available is True

    @pytest.mark.integration
    def test_declaration_target_probe_rejects_fixture_non_proof_declaration(self) -> None:
        workspace = bind_workspace(self.runtime_workspace_root)
        result = declaration_target(
            tool_paths=self.tool_paths,
            workspace=workspace,
            file_path=self.runtime_lemma_fixture,
            theorem="helper_statement",
        )
        assert result.status == ProbeStatus.UNAVAILABLE
        assert result.result is None
        assert "proof declarations with editable proof bodies" in cast(str, result.message)

    @pytest.mark.integration
    def test_declaration_target_probe_rejects_fixture_instance_declaration(self) -> None:
        workspace = bind_workspace(self.runtime_workspace_root)
        result = declaration_target(
            tool_paths=self.tool_paths,
            workspace=workspace,
            file_path=self.runtime_lemma_fixture,
            theorem="helperInst",
        )
        assert result.status == ProbeStatus.UNAVAILABLE
        assert result.result is None
        assert "`helperInst` is a `instance` declaration" in cast(str, result.message)

    @pytest.mark.integration
    def test_context_bundle_probe_matches_declaration_target_and_proof_state_contracts(self) -> None:
        workspace = bind_workspace(self.runtime_workspace_root)
        bundle_result = semantic_context_bundle(
            tool_paths=self.tool_paths,
            workspace=workspace,
            file_path=self.runtime_lemma_fixture,
            theorem="helper_true",
        )
        target_result = declaration_target(
            tool_paths=self.tool_paths,
            workspace=workspace,
            file_path=self.runtime_lemma_fixture,
            theorem="helper_true",
        )
        proof_result = proof_state(
            tool_paths=self.tool_paths,
            workspace=workspace,
            file_path=self.runtime_lemma_fixture,
            theorem="helper_true",
        )
        assert bundle_result.status == ProbeStatus.OK
        assert target_result.status == ProbeStatus.OK
        assert proof_result.status == ProbeStatus.OK
        bundle = cast(ProofctlSemanticContextBundle, bundle_result.result)
        target = cast(ProofctlDeclarationTarget, target_result.result)
        proof = cast(ProofctlProofState, proof_result.result)
        assert bundle.declaration_target.declaration_name == target.declaration_name
        assert bundle.declaration_target.imports == target.imports
        assert bundle.declaration_target.body_span == target.body_span
        assert bundle.proof_state is not None
        assert bundle.proof_state.declaration_name == proof.declaration_name
        assert bundle.proof_state.local_context == proof.local_context
        assert bundle.proof_state.goals == proof.goals
        assert bundle.proof_state.insertion_point == proof.insertion_point

    def test_semantic_context_probes_from_workspace_uses_one_bundled_helper_request(self) -> None:
        span = ProbeSpan(start=ProbePoint(line=619, column=0), end=ProbePoint(line=631, column=0))
        body_span = ProbeSpan(start=ProbePoint(line=620, column=15), end=ProbePoint(line=631, column=0))
        name_span = ProbeSpan(start=ProbePoint(line=619, column=16), end=ProbePoint(line=619, column=38))
        insert_position = ProbePoint(line=619, column=0)
        bundle = ProofctlSemanticContextBundle(
            declaration_target=ProofctlDeclarationTarget(
                module_path="KanProofs.Structural.TypedSubstitution",
                short_name="substitution_comp_core",
                declaration_name="substitution_comp_core",
                namespace=None,
                namespace_stack=[],
                imports=["KanProofs.Structural.TypedSubstitution"],
                keyword="theorem",
                modifiers=["private"],
                group=ProofctlGroupMembership(
                    kind="mutual",
                    group_id="mutual-1",
                    member_index=2,
                ),
                declaration_span=span,
                name_span=name_span,
                body_span=body_span,
                safe_edit=ProofctlSafeEditBoundary(
                    kind="replace-body",
                    declaration_span=span,
                    body_span=body_span,
                ),
                proof_state_available=True,
            ),
            proof_state=ProofctlProofState(
                source="tactic-goals-before",
                declaration_name="substitution_comp_core",
                namespace=None,
                locals=[],
                local_context=[],
                goals=["Γ ⊢ True"],
                insertion_point=ProofctlInsertionPoint(
                    kind="before-theorem",
                    declaration_name="substitution_comp_core",
                    namespace=None,
                    position=insert_position,
                ),
            ),
        )
        bundle_result = ProbeResult(
            layer=ProbeLayer.SEMANTIC,
            command="context-bundle",
            status=ProbeStatus.OK,
            file=str(self.repo_mutual_target),
            theorem="substitution_comp_core",
            result=bundle,
        )
        with (
            patch("kan_prover.engine.proofctl.semantic_context_bundle", return_value=bundle_result) as bundle_call,
            patch(
                "kan_prover.engine.proofctl.declaration_target",
                side_effect=AssertionError("from_workspace should not call declaration_target"),
            ),
            patch(
                "kan_prover.engine.proofctl.proof_state",
                side_effect=AssertionError("from_workspace should not call proof_state"),
            ),
        ):
            probes = SemanticContextProbes.from_workspace(
                tool_paths=self.tool_paths,
                workspace=bind_workspace(self.repo_root),
                file_path=self.repo_mutual_target,
                theorem="substitution_comp_core",
                include_declaration_target=True,
            )
        assert bundle_call.call_count == 1
        assert probes.context_bundle_result == bundle_result
        assert probes.declaration_target_result is not None
        assert probes.declaration_target_result.status == ProbeStatus.OK
        assert probes.proof_state_result.status == ProbeStatus.OK

    @pytest.mark.integration
    def test_declaration_inventory_probe_reports_metadata_for_non_proof_declarations(self) -> None:
        workspace = bind_workspace(self.runtime_workspace_root)
        result = declaration_inventory(
            tool_paths=self.tool_paths,
            workspace=workspace,
            file_path=self.runtime_lemma_fixture,
        )
        assert result.status == ProbeStatus.OK
        inventory = cast(ProofctlDeclarationInventory, result.result)
        entries = {entry.short_name: entry for entry in inventory.entries}
        assert entries["helper_statement"].declaration_kind == "def"
        assert entries["helper_statement"].group.kind == "standalone"
        assert entries["helper_statement"].module_path == "Tools.SemanticProbe.Fixtures.SemanticProbeLemmaFixture"
        assert entries["helperInst"].declaration_kind == "instance"
        assert entries["helperInst"].group.kind == "standalone"
        assert entries["helperInst"].module_path == "Tools.SemanticProbe.Fixtures.SemanticProbeLemmaFixture"

    @pytest.mark.integration
    def test_theorem_span_probe_reports_private_mutual_repo_shape(self) -> None:
        result = theorem_span(file_path=self.repo_mutual_target, theorem="substitution_comp_core")
        assert result.status == ProbeStatus.OK
        payload = cast(ProofctlTheoremSpan, result.result)
        assert payload.module_path == "KanProofs.Structural.TypedSubstitution"
        assert payload.declaration_name == "substitution_comp_core"
        assert payload.group_kind == "mutual"
        assert "private" in payload.modifiers
        assert payload.body_span.start.line == 619

    @pytest.mark.integration
    def test_theorem_span_probe_reports_missing_theorem(self) -> None:
        result = theorem_span(file_path=self.repo_target, theorem="missing_theorem")
        assert result.status == ProbeStatus.MISSING
        assert result.result is None

    def test_theorem_span_from_source_reports_ambiguous_short_name(self) -> None:
        source = """namespace A

theorem dup : True := by
  exact True.intro

end A

namespace B

theorem dup : True := by
  exact True.intro

end B
"""
        result = theorem_span_from_source(file_path=Path("<memory>"), source=source, theorem="dup")
        assert result.status == ProbeStatus.AMBIGUOUS
        assert {candidate.declaration_name for candidate in result.candidates} == {"A.dup", "B.dup"}

    def test_imports_and_namespace_from_source_reports_explicit_source_surface(self) -> None:
        source = """import Mathlib.Data.Nat.Basic
import KanProofs.Algebra.Types

namespace Demo

theorem smoke : True := by
  exact True.intro
"""
        result = imports_and_namespace_from_source(file_path=Path("<memory>"), source=source, theorem="smoke")
        assert result.status == ProbeStatus.OK
        payload = cast(ProofctlImportsAndNamespace, result.result)
        assert payload.declaration_name == "Demo.smoke"
        assert payload.namespace == "Demo"
        assert payload.imports == ["Mathlib.Data.Nat.Basic", "KanProofs.Algebra.Types"]

    @pytest.mark.integration
    def test_semantic_local_context_probe_succeeds_in_sandboxed_downstream_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            real_workspace_root = self._linked_repo_workspace(Path(temp_dir) / "workspace")
            real_workspace = bind_workspace(real_workspace_root)
            sandbox = create_sandbox_workspace(workspace=real_workspace, run_dir=Path(temp_dir) / "run")
            sandbox_target = sandbox.target_file(
                real_workspace_root / "KanProofs" / "Algebra" / "ControlFlowGrades.lean"
            )
            result = local_context(
                tool_paths=self.tool_paths,
                workspace=sandbox.sandbox_workspace,
                file_path=sandbox_target,
                theorem="CFGrade.le_refl",
            )
        assert result.status == ProbeStatus.OK
        payload = cast(ProofctlLocalContext, result.result)
        assert payload.declaration_name == "CFGrade.le_refl"
        assert any("cf" in row and "CFGrade" in row for row in payload.locals)

    @pytest.mark.integration
    def test_semantic_local_context_probe_succeeds_in_real_git_backed_workspace_sandbox(self) -> None:
        real_workspace = bind_workspace(self.repo_root)
        if not real_workspace.git_backed:
            return
        if not (real_workspace.root / ".lake").exists():
            return
        if not shutil.which("lake"):
            return
        with tempfile.TemporaryDirectory() as temp_dir:
            sandbox = create_sandbox_workspace(workspace=real_workspace, run_dir=Path(temp_dir) / "real-git")
            try:
                sandbox_target = sandbox.target_file(self.repo_target)
                local_result = local_context(
                    tool_paths=self.tool_paths,
                    workspace=sandbox.sandbox_workspace,
                    file_path=sandbox_target,
                    theorem="CFGrade.le_refl",
                )
                insertion_result = insertion_point(
                    tool_paths=self.tool_paths,
                    workspace=sandbox.sandbox_workspace,
                    file_path=sandbox_target,
                    theorem="CFGrade.le_refl",
                )
            finally:
                self._cleanup_sandbox_workspace(sandbox)
        assert local_result.status == ProbeStatus.OK
        local_payload = cast(ProofctlLocalContext, local_result.result)
        assert any("cf" in row and "CFGrade" in row for row in local_payload.locals)
        assert insertion_result.status == ProbeStatus.OK
        insertion_payload = cast(ProofctlInsertionPoint, insertion_result.result)
        assert insertion_payload.position.line == self._declaration_anchor_line(
            self.repo_target,
            "theorem le_refl (cf : CFGrade) : cf ≤ cf := by",
        )

    @pytest.mark.integration
    def test_semantic_goal_and_insertion_probes_succeed_in_sandboxed_downstream_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            real_workspace_root = self._linked_repo_workspace(Path(temp_dir) / "workspace")
            real_workspace = bind_workspace(real_workspace_root)
            sandbox = create_sandbox_workspace(workspace=real_workspace, run_dir=Path(temp_dir) / "run")
            sandbox_target = sandbox.target_file(
                real_workspace_root / "KanProofs" / "Algebra" / "ControlFlowGrades.lean"
            )
            goal_result = goal_snapshot(
                tool_paths=self.tool_paths,
                workspace=sandbox.sandbox_workspace,
                file_path=sandbox_target,
                theorem="CFGrade.le_refl",
            )
            insertion_result = insertion_point(
                tool_paths=self.tool_paths,
                workspace=sandbox.sandbox_workspace,
                file_path=sandbox_target,
                theorem="CFGrade.le_refl",
            )
        assert goal_result.status == ProbeStatus.OK
        goal_payload = cast(ProofctlGoalSnapshot, goal_result.result)
        assert any("cf : CFGrade" in goal for goal in goal_payload.goals)
        assert insertion_result.status == ProbeStatus.OK
        insertion_payload = cast(ProofctlInsertionPoint, insertion_result.result)
        assert insertion_payload.kind == "before-theorem"
        assert insertion_payload.position.line == self._declaration_anchor_line(
            self.repo_target,
            "theorem le_refl (cf : CFGrade) : cf ≤ cf := by",
        )

    @pytest.mark.integration
    def test_semantic_proof_state_probe_reports_stable_goal_context_and_insertion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            real_workspace_root = self._linked_repo_workspace(Path(temp_dir) / "workspace")
            real_workspace = bind_workspace(real_workspace_root)
            sandbox = create_sandbox_workspace(workspace=real_workspace, run_dir=Path(temp_dir) / "run")
            sandbox_target = sandbox.target_file(
                real_workspace_root / "KanProofs" / "Algebra" / "ControlFlowGrades.lean"
            )
            result = proof_state(
                tool_paths=self.tool_paths,
                workspace=sandbox.sandbox_workspace,
                file_path=sandbox_target,
                theorem="CFGrade.le_refl",
            )
        assert result.status == ProbeStatus.OK
        payload = cast(ProofctlProofState, result.result)
        assert payload.declaration_name == "CFGrade.le_refl"
        assert any("cf: CFGrade" in row or "cf : CFGrade" in row for row in payload.local_context)
        assert any("cf : CFGrade" in goal for goal in payload.goals)
        assert payload.insertion_point.kind == "before-theorem"
        assert payload.insertion_point.position.line == self._declaration_anchor_line(
            self.repo_target,
            "theorem le_refl (cf : CFGrade) : cf ≤ cf := by",
        )

    @pytest.mark.integration
    def test_semantic_probe_reports_unavailable_without_writable_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            real_workspace_root = self._linked_repo_workspace(Path(temp_dir) / "workspace")
            real_workspace = bind_workspace(real_workspace_root)
            sandbox = create_sandbox_workspace(workspace=real_workspace, run_dir=Path(temp_dir) / "run")
            readonly_workspace = replace(sandbox.sandbox_workspace, writable=False)
            sandbox_target = sandbox.target_file(
                real_workspace_root / "KanProofs" / "Algebra" / "ControlFlowGrades.lean"
            )
            result = local_context(
                tool_paths=self.tool_paths,
                workspace=readonly_workspace,
                file_path=sandbox_target,
                theorem="CFGrade.le_refl",
            )
        assert result.status == ProbeStatus.UNAVAILABLE
        assert result.message is not None
        assert "writable sandboxed workspace" in result.message

    @pytest.mark.integration
    def test_semantic_probe_reports_error_for_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            real_workspace_root = self._linked_repo_workspace(Path(temp_dir) / "workspace")
            real_workspace = bind_workspace(real_workspace_root)
            sandbox = create_sandbox_workspace(workspace=real_workspace, run_dir=Path(temp_dir) / "run")
            missing_file = sandbox.sandbox_workspace.root / "KanProofs" / "Missing.lean"
            result = goal_snapshot(
                tool_paths=self.tool_paths,
                workspace=sandbox.sandbox_workspace,
                file_path=missing_file,
                theorem="Nope.missing",
            )
        assert result.status == ProbeStatus.ERROR
        assert result.message is not None

    def test_locator_uses_workspace_target_contract(self) -> None:
        if not self.repo_target.is_file():
            pytest.skip()
        source = self.repo_target.read_text(encoding="utf-8")
        span = locate_theorem_span_in_file(
            target_file=self.repo_target,
            source=source,
            theorem_name="le_refl",
        )
        assert span.keyword == "theorem"
        assert span.name == "CFGrade.le_refl"
        assert span.body_start > span.declaration_start

    def test_private_mutual_patch_rewrites_only_target_body(self) -> None:
        if not self.repo_mutual_target.is_file():
            pytest.skip()
        source = self.repo_mutual_target.read_text(encoding="utf-8")
        span = locate_theorem_span_in_file(
            target_file=self.repo_mutual_target,
            source=source,
            theorem_name="substitution_comp_core",
        )
        patched = replace_theorem_body(
            source,
            span,
            """by
  exact hM
""",
        )
        assert "private theorem substitution_comp_core" in patched
        assert "exact hM" in patched
        assert "private theorem substitution_valuetype_core" in patched
        assert "exact hM\n\nprivate theorem substitution_valuetype_core" not in patched

    def test_locator_reports_ambiguous_source_names(self) -> None:
        source = """namespace A

theorem dup : True := by
  exact True.intro

end A

namespace B

theorem dup : True := by
  exact True.intro

end B
"""
        with pytest.raises(ValueError, match="multiple theorem or lemma declarations"):
            probe = theorem_span_from_source(file_path=Path("<memory>"), source=source, theorem="dup")
            locate_theorem_span_from_probe(source=source, theorem_name="dup", probe=probe)

    def test_workspace_modules_do_not_carry_source_only_probe_surface(self) -> None:
        engine_root = self.tool_paths.root / "src" / "kan_prover" / "engine"
        proofctl_tree = ast.parse((engine_root / "proofctl.py").read_text(encoding="utf-8"))
        source_probing_tree = ast.parse((engine_root / "source_probing.py").read_text(encoding="utf-8"))
        theorem_locator_tree = ast.parse((engine_root / "theorem_locator.py").read_text(encoding="utf-8"))
        inspection_tree = ast.parse((engine_root / "inspection.py").read_text(encoding="utf-8"))

        proofctl_functions = {
            node.name for node in ast.walk(proofctl_tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert "theorem_span_from_source" not in proofctl_functions
        assert "imports_and_namespace_from_source" not in proofctl_functions
        assert "_syntax_probe" not in proofctl_functions

        source_probing_functions = {
            node.name
            for node in ast.walk(source_probing_tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert "locate_theorem_span" not in source_probing_functions

        for module_name, tree in {
            "theorem_locator.py": theorem_locator_tree,
            "inspection.py": inspection_tree,
        }.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.module != "kan_prover.engine.proofctl":
                    continue
                imported = {alias.name for alias in node.names}
                assert not ({"theorem_span_from_source", "imports_and_namespace_from_source"} & imported), (
                    f"{module_name} still imports source-only helpers from proofctl"
                )

    @pytest.mark.integration
    def test_repair_request_artifact_carries_probe_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            workspace_root = self._linked_demo_workspace(temp_root / "workspace")
            target_file = workspace_root / "Problems" / "RepairTarget.lean"
            target_file.write_text(
                (self.fixtures / "repair_target.lean").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            candidate_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            args = self._repair_args(
                file=target_file,
                workspace=workspace_root,
                theorem="fix_me",
                candidate_files=[candidate_path],
                apply=False,
            )
            with patch(
                "kan_prover.cli.commands.repair.discover_tool_paths",
                return_value=self._temp_paths(temp_artifacts),
            ):
                result, exit_code = repair.run_command(args)
            assert exit_code == 0
            request_path = Path(result.work_dir) / "attempt-001" / "backend" / "request.json"
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            assert payload["target_module_path"] == "Problems/RepairTarget.lean"
            assert payload["helper_module_root"] == "KanProverGenerated.Helpers"
            assert payload["imports"] == []
            assert payload["proof_target"]["declaration_name"] == "fix_me"
            assert payload["proof_target"]["safe_edit"]["kind"] == "replace-body"
            assert payload["proof_target"]["proof_state_available"] is True
            assert "proof_state" in payload
            assert isinstance(payload["proof_state"], dict)
            assert "goals" in payload["proof_state"]
            assert any("⊢ True" in goal for goal in payload["goal_snapshot"])
            assert payload["semantic_probe_results"]["declaration-target"]["status"] == "ok"
            assert payload["semantic_probe_results"]["theorem-span"]["status"] == "ok"
            assert payload["semantic_probe_results"]["proof-state"]["status"] == "ok"
            assert payload["probe_results"]["theorem-span"]["status"] == "ok"
            assert payload["probe_results"]["imports-and-namespace"]["status"] == "ok"
            assert payload["probe_results"]["proof-state"]["status"] == "ok"
            assert not payload["syntax_probe_results"]

    def test_inspection_state_fails_when_semantic_declaration_target_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            workspace_root = self._linked_demo_workspace(temp_root / "workspace")
            target_file = workspace_root / "Problems" / "RepairTarget.lean"
            target_file.write_text(
                (self.fixtures / "repair_target.lean").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            workspace = bind_workspace(workspace_root)
            sandbox = create_sandbox_workspace(workspace=workspace, run_dir=temp_root / "run")
            unavailable = ProbeResult(
                layer=ProbeLayer.SEMANTIC,
                command="declaration-target",
                status=ProbeStatus.UNAVAILABLE,
                file=str(target_file),
                theorem="fix_me",
                message="semantic helper unavailable",
            )
            proof_state_failure = ProbeResult(
                layer=ProbeLayer.SEMANTIC,
                command="proof-state",
                status=ProbeStatus.ERROR,
                file=str(target_file),
                theorem="fix_me",
                message="unused in this failure path",
            )
            with (
                patch(
                    "kan_prover.engine.inspection.SemanticContextProbes.from_workspace",
                    return_value=SemanticContextProbes(
                        declaration_target_result=unavailable,
                        proof_state_result=proof_state_failure,
                    ),
                ),
                pytest.raises(RequiredWorkspaceProbeError) as error,
            ):
                build_inspection_state(
                    tool_paths=self.tool_paths,
                    workspace=workspace,
                    sandbox=sandbox,
                    target_file=target_file,
                    theorem="fix_me",
                    timeout_seconds=60.0,
                    command_name="repair",
                )
            assert error.value.required_for == "declaration-target"
            assert error.value.probe.status == ProbeStatus.UNAVAILABLE
            assert error.value.probe.command == "declaration-target"

    @pytest.mark.integration
    def test_run_inspect_read_only_skips_sandbox_but_keeps_semantic_probe_truth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_artifacts.mkdir(parents=True, exist_ok=True)
            (temp_artifacts / "index.jsonl").write_text("", encoding="utf-8")
            workspace_root = self._linked_demo_workspace(temp_root / "workspace")
            target_file = workspace_root / "Problems" / "RepairTarget.lean"
            target_file.write_text(
                (self.fixtures / "repair_target.lean").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with (
                patch(
                    "kan_prover.engine.inspection.create_sandbox_workspace",
                    side_effect=AssertionError("read-only inspect should not create a sandbox"),
                ),
            ):
                result, exit_code = run_inspect(
                    tool_paths=self._temp_paths(temp_artifacts),
                    workspace=bind_workspace(workspace_root),
                    target_file=target_file,
                    theorem="fix_me",
                    timeout_seconds=60.0,
                    read_only=True,
                )
            assert exit_code == 0
            assert result.read_only is True
            assert result.sandbox is None
            assert result.helper_module_root is None
            assert result.file_state_check.status.value == "skipped"
            assert result.file_state_check.requested is False
            assert result.snapshot.diagnostic_count == 0
            context_bundle = cast(dict[str, JsonValue], result.snapshot.semantic_probe_results["context-bundle"])
            declaration_target = cast(
                dict[str, JsonValue], result.snapshot.semantic_probe_results["declaration-target"]
            )
            proof_state = cast(dict[str, JsonValue], result.snapshot.semantic_probe_results["proof-state"])
            assert context_bundle["status"] == "ok"
            assert declaration_target["status"] == "ok"
            assert proof_state["status"] == "ok"
            assert result.snapshot.goal_snapshot

    @pytest.mark.integration
    def test_run_inspect_default_skips_file_state_verification_but_keeps_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_artifacts.mkdir(parents=True, exist_ok=True)
            (temp_artifacts / "index.jsonl").write_text("", encoding="utf-8")
            workspace_root = self._linked_demo_workspace(temp_root / "workspace")
            target_file = workspace_root / "Problems" / "RepairTarget.lean"
            target_file.write_text(
                (self.fixtures / "repair_target.lean").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with patch(
                "kan_prover.engine.inspection._baseline_diagnostics",
                side_effect=AssertionError("default inspect should skip file-state verification"),
            ):
                result, exit_code = run_inspect(
                    tool_paths=self._temp_paths(temp_artifacts),
                    workspace=bind_workspace(workspace_root),
                    target_file=target_file,
                    theorem="fix_me",
                    timeout_seconds=60.0,
                )
            assert exit_code == 0
            assert result.read_only is False
            assert result.sandbox is not None
            assert result.file_state_check.status.value == "skipped"
            assert result.file_state_check.requested is False
            assert result.snapshot.goal_snapshot

    @pytest.mark.integration
    def test_run_retrieve_default_skips_file_state_verification_but_keeps_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_artifacts.mkdir(parents=True, exist_ok=True)
            (temp_artifacts / "index.jsonl").write_text("", encoding="utf-8")
            workspace_root = self._linked_demo_workspace(temp_root / "workspace")
            target_file = workspace_root / "Problems" / "RepairTarget.lean"
            target_file.write_text(
                (self.fixtures / "repair_target.lean").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with patch(
                "kan_prover.engine.inspection._baseline_diagnostics",
                side_effect=AssertionError("default retrieve should skip file-state verification"),
            ):
                result, exit_code = run_retrieve(
                    tool_paths=self._temp_paths(temp_artifacts),
                    workspace=bind_workspace(workspace_root),
                    target_file=target_file,
                    theorem="fix_me",
                    timeout_seconds=60.0,
                )
            assert exit_code == 0
            assert result.read_only is False
            assert result.sandbox is not None
            assert result.file_state_check.status.value == "skipped"
            assert result.file_state_check.requested is False
            assert result.snapshot.retrieval.hits

    @pytest.mark.integration
    def test_run_inspect_verify_file_state_opt_in_runs_baseline_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_artifacts.mkdir(parents=True, exist_ok=True)
            (temp_artifacts / "index.jsonl").write_text("", encoding="utf-8")
            workspace_root = self._linked_demo_workspace(temp_root / "workspace")
            target_file = workspace_root / "Problems" / "RepairTarget.lean"
            target_file.write_text(
                (self.fixtures / "repair_target.lean").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with patch("kan_prover.engine.inspection._baseline_diagnostics", return_value=[]) as baseline:
                result, exit_code = run_inspect(
                    tool_paths=self._temp_paths(temp_artifacts),
                    workspace=bind_workspace(workspace_root),
                    target_file=target_file,
                    theorem="fix_me",
                    timeout_seconds=60.0,
                    verify_file_state=True,
                )
            assert exit_code == 0
            assert baseline.call_count == 1
            assert result.file_state_check.status.value == "ran"
            assert result.file_state_check.requested is True
            assert result.snapshot.diagnostic_count == 0

    def test_inspect_cli_json_reports_required_semantic_probe_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = self._linked_demo_workspace(temp_root / "workspace")
            target_file = workspace_root / "Problems" / "RepairTarget.lean"
            target_file.write_text(
                (self.fixtures / "repair_target.lean").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            failure = ProbeResult(
                layer=ProbeLayer.SEMANTIC,
                command="declaration-target",
                status=ProbeStatus.ERROR,
                file=str(target_file),
                theorem="fix_me",
                message="Could not prepare semantic probe executable.",
                error_kind=LeanHelperErrorKind.BOOTSTRAP_FAILED,
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--json",
                "inspect",
                "--workspace",
                str(workspace_root),
                str(target_file),
                "--theorem",
                "fix_me",
            ]
            with (
                patch("kan_prover.cli.commands.inspect.discover_tool_paths", return_value=temp_paths),
                patch(
                    "kan_prover.engine.inspection.SemanticContextProbes.from_workspace",
                    return_value=SemanticContextProbes(
                        declaration_target_result=failure,
                        proof_state_result=ProbeResult(
                            layer=ProbeLayer.SEMANTIC,
                            command="proof-state",
                            status=ProbeStatus.ERROR,
                            file=str(target_file),
                            theorem="fix_me",
                            message="unused in this failure path",
                        ),
                    ),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(argv)
            assert exit_code == 4
            payload = json.loads(stdout.getvalue())
            assert payload["status"] == "error"
            assert payload["error_kind"] == "required-semantic-probe-failed"
            assert payload["required_for"] == "declaration-target"
            assert "next_step" not in payload
            probe_result = cast(dict[str, JsonValue], payload["probe_result"])
            assert probe_result["status"] == "error"
            assert probe_result["error_kind"] == "bootstrap-failed"
            assert "semantic `declaration-target` probe data" in payload["message"]
            stderr_text = stderr.getvalue()
            assert "failure kind: required-semantic-probe-failed" in stderr_text
            assert "semantic `declaration-target` probe data is unavailable" in stderr_text
            assert "next step:" in stderr_text

    def test_inspect_cli_plain_reports_required_semantic_probe_failure_with_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = self._linked_demo_workspace(temp_root / "workspace")
            target_file = workspace_root / "Problems" / "RepairTarget.lean"
            target_file.write_text(
                (self.fixtures / "repair_target.lean").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            failure = ProbeResult(
                layer=ProbeLayer.SEMANTIC,
                command="declaration-target",
                status=ProbeStatus.UNAVAILABLE,
                file=str(target_file),
                theorem="fix_me",
                message="Semantic probes require a writable sandboxed workspace.",
                error_kind=LeanHelperErrorKind.EXECUTION_FAILED,
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "inspect",
                "--workspace",
                str(workspace_root),
                str(target_file),
                "--theorem",
                "fix_me",
            ]
            with (
                patch("kan_prover.cli.commands.inspect.discover_tool_paths", return_value=temp_paths),
                patch(
                    "kan_prover.engine.inspection.SemanticContextProbes.from_workspace",
                    return_value=SemanticContextProbes(
                        declaration_target_result=failure,
                        proof_state_result=ProbeResult(
                            layer=ProbeLayer.SEMANTIC,
                            command="proof-state",
                            status=ProbeStatus.ERROR,
                            file=str(target_file),
                            theorem="fix_me",
                            message="unused in this failure path",
                        ),
                    ),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(argv)
            assert exit_code == 4
            assert stdout.getvalue() == ""
            stderr_text = stderr.getvalue()
            assert "failure kind: required-semantic-probe-failed" in stderr_text
            assert "cause: semantic `declaration-target` probe data is unavailable" in stderr_text
            assert "writable sandboxed workspace" in stderr_text
            assert "next step: bind `--workspace` to a writable Lake workspace" in stderr_text

    def test_session_start_cli_plain_reports_unsupported_target_contract(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            "session",
            "start",
            str(self.repo_prop_target),
            "--workspace",
            str(self.repo_root),
            "--theorem",
            "substitution_comp",
        ]
        target = ProofctlDeclarationTarget(
            module_path="KanProofs.Structural.Substitution.ValueSubstitution",
            short_name="substitution_comp",
            declaration_name="substitution_comp",
            namespace=None,
            namespace_stack=[],
            imports=[
                "KanProofs.Structural.Substitution.Exposure",
                "KanProofs.Structural.Substitution.ShallowAdmissibility",
            ],
            keyword="def",
            modifiers=[],
            group=ProofctlGroupMembership(kind="standalone"),
            declaration_span=ProbeSpan(start=ProbePoint(line=13, column=0), end=ProbePoint(line=25, column=0)),
            name_span=ProbeSpan(start=ProbePoint(line=13, column=4), end=ProbePoint(line=13, column=21)),
            body_span=ProbeSpan(start=ProbePoint(line=14, column=2), end=ProbePoint(line=25, column=0)),
            safe_edit=None,
            proof_state_available=False,
        )
        with (
            patch(
                "kan_prover.cli.commands.session.run_start",
                side_effect=UnsupportedTargetError(command="session start", target=target),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = main(argv)
        assert exit_code == 4
        assert stdout.getvalue() == ""
        stderr_text = stderr.getvalue()
        assert "failure kind: unsupported-target-kind" in stderr_text
        assert "cannot use `substitution_comp` as a proof-workflow target" in stderr_text
        assert "`session start`" in stderr_text
        assert "proof-target contract" in stderr_text
        assert "editable body" in stderr_text
        assert "detail: Lean did not expose a safe-edit boundary or proof-state data" in stderr_text

    @pytest.mark.integration
    def test_retrieve_cli_json_surfaces_ranked_hits_and_score_rationale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = self._linked_demo_workspace(temp_root / "workspace")
            target_file = workspace_root / "Problems" / "RepairTarget.lean"
            target_file.write_text(
                (self.fixtures / "repair_target.lean").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--json",
                "retrieve",
                "--workspace",
                str(workspace_root),
                str(target_file),
                "--theorem",
                "fix_me",
            ]
            with (
                patch("kan_prover.cli.commands.retrieve.discover_tool_paths", return_value=temp_paths),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(argv)
            assert exit_code == 0
            payload = json.loads(stdout.getvalue())
            assert payload["command"] == "retrieve"
            file_state_check = cast(dict[str, JsonValue], payload["file_state_check"])
            assert file_state_check["status"] == "skipped"
            snapshot = cast(dict[str, JsonValue], payload["snapshot"])
            retrieval = cast(dict[str, JsonValue], snapshot["retrieval"])
            hits = cast(list[JsonValue], retrieval["hits"])
            assert hits
            first_hit = cast(dict[str, JsonValue], hits[0])
            assert "source_kind" in first_hit
            assert "score_breakdown" in first_hit
            assert "kan-prover retrieve ok" in stderr.getvalue()
            assert "file_state_check=skipped" in stderr.getvalue()

    @pytest.mark.integration
    def test_retrieve_cli_json_explicit_file_state_verification_reports_ran(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = self._linked_demo_workspace(temp_root / "workspace")
            target_file = workspace_root / "Problems" / "RepairTarget.lean"
            target_file.write_text(
                (self.fixtures / "repair_target.lean").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--json",
                "retrieve",
                "--workspace",
                str(workspace_root),
                str(target_file),
                "--theorem",
                "fix_me",
                "--verify-file-state",
            ]
            with (
                patch("kan_prover.cli.commands.retrieve.discover_tool_paths", return_value=temp_paths),
                patch("kan_prover.engine.inspection._baseline_diagnostics", return_value=[]),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(argv)
            assert exit_code == 0
            payload = json.loads(stdout.getvalue())
            file_state_check = cast(dict[str, JsonValue], payload["file_state_check"])
            assert file_state_check["status"] == "ran"
            assert file_state_check["requested"] is True
            assert "file_state_check=ran" in stderr.getvalue()

    @pytest.mark.integration
    def test_retrieve_cli_json_surfaces_embedding_score_component_when_enabled(self) -> None:
        class DeterministicCliEmbeddingRetriever:
            def provider_name(self) -> str:
                return "test-double"

            def model_name(self) -> str | None:
                return "deterministic"

            def embed(self, texts: list[str]) -> list[list[float]]:
                vectors: list[list[float]] = []
                for text in texts:
                    if "fix_me" in text:
                        vectors.append([1.0, 0.0])
                    else:
                        vectors.append([0.4, 0.1])
                return vectors

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = self._linked_demo_workspace(temp_root / "workspace")
            target_file = workspace_root / "Problems" / "RepairTarget.lean"
            target_file.write_text(
                (self.fixtures / "repair_target.lean").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--json",
                "retrieve",
                "--workspace",
                str(workspace_root),
                str(target_file),
                "--theorem",
                "fix_me",
                "--enable-embedding-retrieval",
            ]
            with (
                patch("kan_prover.cli.commands.retrieve.discover_tool_paths", return_value=temp_paths),
                patch(
                    "kan_prover.engine.retrieval._build_embedding_retriever",
                    return_value=DeterministicCliEmbeddingRetriever(),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(argv)
            assert exit_code == 0
            payload = json.loads(stdout.getvalue())
            snapshot = cast(dict[str, JsonValue], payload["snapshot"])
            retrieval = cast(dict[str, JsonValue], snapshot["retrieval"])
            hits = cast(list[JsonValue], retrieval["hits"])
            assert hits
            first_hit = cast(dict[str, JsonValue], hits[0])
            score_breakdown = cast(list[JsonValue], first_hit["score_breakdown"])
            assert any(
                cast(dict[str, JsonValue], component).get("label") == "embedding-similarity"
                for component in score_breakdown
            )
            assert "kan-prover retrieve ok" in stderr.getvalue()

    @pytest.mark.integration
    def test_repair_cli_json_reports_required_semantic_probe_failure_for_proof_state_execution_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = self._linked_demo_workspace(temp_root / "workspace")
            target_file = workspace_root / "Problems" / "RepairTarget.lean"
            target_file.write_text(
                (self.fixtures / "repair_target.lean").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            candidate_path = self._write_text(temp_root / "good.body.lean", GOOD_BODY)
            failure = ProbeResult(
                layer=ProbeLayer.SEMANTIC,
                command="proof-state",
                status=ProbeStatus.ERROR,
                file=str(target_file),
                theorem="fix_me",
                message="Lean helper exited with code 1 while probing proof state.",
                error_kind=LeanHelperErrorKind.EXECUTION_FAILED,
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--json",
                "repair",
                "--workspace",
                str(workspace_root),
                str(target_file),
                "--theorem",
                "fix_me",
                "--candidate-file",
                str(candidate_path),
            ]
            with (
                patch("kan_prover.cli.commands.repair.discover_tool_paths", return_value=temp_paths),
                patch("kan_prover.engine.proofctl.proof_state", return_value=failure),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(argv)
            assert exit_code == 4
            payload = json.loads(stdout.getvalue())
            assert payload["status"] == "error"
            assert payload["error_kind"] == "required-semantic-probe-failed"
            assert payload["required_for"] == "proof-state"
            probe_result = cast(dict[str, JsonValue], payload["probe_result"])
            assert probe_result["command"] == "proof-state"
            assert probe_result["status"] == "error"
            assert probe_result["error_kind"] == "execution-failed"
            assert "semantic `proof-state` probe data" in payload["message"]
            assert "semantic `proof-state` probe data" in stderr.getvalue()

    @pytest.mark.integration
    def test_repair_cli_json_reports_backend_failure_separately_from_semantic_probe_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = self._linked_demo_workspace(temp_root / "workspace")
            target_file = workspace_root / "Problems" / "RepairTarget.lean"
            target_file.write_text(
                (self.fixtures / "repair_target.lean").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--json",
                "repair",
                "--workspace",
                str(workspace_root),
                str(target_file),
                "--theorem",
                "fix_me",
                "--agent",
                "codex",
            ]
            with (
                patch("kan_prover.cli.commands.repair.discover_tool_paths", return_value=temp_paths),
                patch(
                    "kan_prover.adapters.codex.CodexBackend.propose",
                    side_effect=BackendCommandError(
                        backend="codex",
                        backend_error_kind="unavailable",
                        message="Codex CLI is not available on PATH: expected `codex`.",
                    ),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(argv)
            assert exit_code == 4
            payload = json.loads(stdout.getvalue())
            assert payload["status"] == "error"
            assert payload["error_kind"] == "backend-failed"
            assert payload["backend"] == "codex"
            assert payload["backend_error_kind"] == "unavailable"
            assert payload["error_kind"] != "required-semantic-probe-failed"
            assert "Codex CLI is not available on PATH" in payload["message"]
            stderr_text = stderr.getvalue()
            assert "failure kind: backend-failed" in stderr_text
            assert "cause: `codex` returned `unavailable`" in stderr_text
            assert "next step:" in stderr_text

    @pytest.mark.integration
    def test_repair_cli_plain_reports_backend_failure_with_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_artifacts = temp_root / "artifacts"
            temp_paths = self._temp_paths(temp_artifacts)
            workspace_root = self._linked_demo_workspace(temp_root / "workspace")
            target_file = workspace_root / "Problems" / "RepairTarget.lean"
            target_file.write_text(
                (self.fixtures / "repair_target.lean").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "repair",
                "--workspace",
                str(workspace_root),
                str(target_file),
                "--theorem",
                "fix_me",
                "--agent",
                "codex",
            ]
            with (
                patch("kan_prover.cli.commands.repair.discover_tool_paths", return_value=temp_paths),
                patch(
                    "kan_prover.adapters.codex.CodexBackend.propose",
                    side_effect=BackendCommandError(
                        backend="codex",
                        backend_error_kind="unavailable",
                        message="Codex CLI is not available on PATH: expected `codex`.",
                    ),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(argv)
            assert exit_code == 4
            assert stdout.getvalue() == ""
            stderr_text = stderr.getvalue()
            assert "failure kind: backend-failed" in stderr_text
            assert "cause: `codex` returned `unavailable`" in stderr_text
            assert "Codex CLI is not available on PATH" in stderr_text
            assert "next step: install or expose the `codex` CLI on PATH" in stderr_text

    def test_context_bundle_cache_hits_across_fresh_process_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            cache_root = temp_root / "semantic-cache"
            temp_paths = self._temp_tool_paths_with_runtime(temp_root)
            workspace_root = self._cache_workspace(temp_root / "workspace")
            workspace = bind_workspace(workspace_root)
            target_file = self._write_text(
                workspace_root / "Demo.lean",
                "theorem target : True := by\n  exact True.intro\n",
            )
            probe = self._context_bundle_probe_result(file_path=target_file, theorem="target")

            clear_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch("kan_prover.engine.proofctl.run_lean_helper", return_value=probe) as runner,
            ):
                first = semantic_context_bundle(
                    tool_paths=temp_paths,
                    workspace=workspace,
                    file_path=target_file,
                    theorem="target",
                )
            assert first.status is ProbeStatus.OK
            assert runner.call_count == 1

            clear_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch(
                    "kan_prover.engine.proofctl.run_lean_helper",
                    side_effect=AssertionError("cache hit should bypass the helper"),
                ),
            ):
                second = semantic_context_bundle(
                    tool_paths=temp_paths,
                    workspace=workspace,
                    file_path=target_file,
                    theorem="target",
                )
            assert second.status is ProbeStatus.OK
            payload = cast(ProofctlSemanticContextBundle, second.result)
            assert payload.declaration_target.short_name == "target"

    def test_context_bundle_cache_invalidates_on_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            cache_root = temp_root / "semantic-cache"
            temp_paths = self._temp_tool_paths_with_runtime(temp_root)
            workspace_root = self._cache_workspace(temp_root / "workspace")
            workspace = bind_workspace(workspace_root)
            target_file = self._write_text(
                workspace_root / "Demo.lean",
                "theorem target : True := by\n  exact True.intro\n",
            )

            clear_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch(
                    "kan_prover.engine.proofctl.run_lean_helper",
                    return_value=self._context_bundle_probe_result(file_path=target_file, theorem="target"),
                ),
            ):
                semantic_context_bundle(
                    tool_paths=temp_paths,
                    workspace=workspace,
                    file_path=target_file,
                    theorem="target",
                )

            target_file.write_text("theorem target : True := by\n  trivial\n", encoding="utf-8")
            clear_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch(
                    "kan_prover.engine.proofctl.run_lean_helper",
                    return_value=self._context_bundle_probe_result(file_path=target_file, theorem="target"),
                ) as runner,
            ):
                semantic_context_bundle(
                    tool_paths=temp_paths,
                    workspace=workspace,
                    file_path=target_file,
                    theorem="target",
                )
            assert runner.call_count == 1

    def test_context_bundle_cache_invalidates_on_runtime_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            cache_root = temp_root / "semantic-cache"
            temp_paths = self._temp_tool_paths_with_runtime(temp_root)
            workspace_root = self._cache_workspace(temp_root / "workspace")
            workspace = bind_workspace(workspace_root)
            target_file = self._write_text(
                workspace_root / "Demo.lean",
                "theorem target : True := by\n  exact True.intro\n",
            )

            clear_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch(
                    "kan_prover.engine.proofctl.run_lean_helper",
                    return_value=self._context_bundle_probe_result(file_path=target_file, theorem="target"),
                ),
            ):
                semantic_context_bundle(
                    tool_paths=temp_paths,
                    workspace=workspace,
                    file_path=target_file,
                    theorem="target",
                )

            runtime_source = temp_paths.packaged_probe_sources / "SemanticProbe.lean"
            runtime_source.write_text(
                runtime_source.read_text(encoding="utf-8") + "\n-- cache bust\n",
                encoding="utf-8",
            )
            clear_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch(
                    "kan_prover.engine.proofctl.run_lean_helper",
                    return_value=self._context_bundle_probe_result(file_path=target_file, theorem="target"),
                ) as runner,
            ):
                semantic_context_bundle(
                    tool_paths=temp_paths,
                    workspace=workspace,
                    file_path=target_file,
                    theorem="target",
                )
            assert runner.call_count == 1

    def test_context_bundle_cache_invalidates_on_schema_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            cache_root = temp_root / "semantic-cache"
            temp_paths = self._temp_tool_paths_with_runtime(temp_root)
            workspace_root = self._cache_workspace(temp_root / "workspace")
            workspace = bind_workspace(workspace_root)
            target_file = self._write_text(
                workspace_root / "Demo.lean",
                "theorem target : True := by\n  exact True.intro\n",
            )

            clear_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch(
                    "kan_prover.engine.proofctl.run_lean_helper",
                    return_value=self._context_bundle_probe_result(file_path=target_file, theorem="target"),
                ),
            ):
                semantic_context_bundle(
                    tool_paths=temp_paths,
                    workspace=workspace,
                    file_path=target_file,
                    theorem="target",
                )

            record_path = next(cache_root.rglob("*.json"))
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["cache_schema_version"] = "semantic-cache.v0"
            record_path.write_text(json.dumps(record), encoding="utf-8")
            clear_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch(
                    "kan_prover.engine.proofctl.run_lean_helper",
                    return_value=self._context_bundle_probe_result(file_path=target_file, theorem="target"),
                ) as runner,
            ):
                semantic_context_bundle(
                    tool_paths=temp_paths,
                    workspace=workspace,
                    file_path=target_file,
                    theorem="target",
                )
            assert runner.call_count == 1

    def test_batch_declaration_inventory_cache_invalidates_on_frontier_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            cache_root = temp_root / "semantic-cache"
            temp_paths = self._temp_tool_paths_with_runtime(temp_root)
            workspace_root = self._cache_workspace(temp_root / "workspace")
            workspace = bind_workspace(workspace_root)
            target_file = self._write_text(
                workspace_root / "Target.lean",
                "theorem target : True := by\n  exact helper_truth\n",
            )
            helper_file = self._write_text(
                workspace_root / "Support.lean",
                "theorem helper_truth : True := by\n  exact True.intro\n",
            )
            manifest_path = self._write_text(
                temp_root / "frontier.json",
                json.dumps([str(helper_file)]),
            )
            probe = ProbeResult(
                layer=ProbeLayer.SEMANTIC,
                command=LeanHelperCommand.BATCH_DECLARATION_INVENTORY.value,
                status=ProbeStatus.OK,
                file=str(target_file),
                theorem="*",
                result=ProofctlBatchDeclarationInventory(
                    files=[
                        ProofctlBatchDeclarationInventoryFile(
                            file_path=str(helper_file),
                            entries=[
                                ProofctlDeclarationMetadata(
                                    module_path="Support",
                                    short_name="helper_truth",
                                    declaration_name="helper_truth",
                                    namespace=None,
                                    namespace_stack=[],
                                    imports=[],
                                    declaration_kind="theorem",
                                    modifiers=[],
                                    group=ProofctlGroupMembership(kind="standalone"),
                                    declaration_span=ProbeSpan(
                                        start=ProbePoint(line=1, column=0),
                                        end=ProbePoint(line=2, column=0),
                                    ),
                                    name_span=ProbeSpan(
                                        start=ProbePoint(line=1, column=8),
                                        end=ProbePoint(line=1, column=20),
                                    ),
                                    body_span=ProbeSpan(
                                        start=ProbePoint(line=1, column=29),
                                        end=ProbePoint(line=2, column=0),
                                    ),
                                )
                            ],
                        )
                    ]
                ),
            )

            clear_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch("kan_prover.engine.proofctl.run_lean_helper", return_value=probe) as runner,
            ):
                first = batch_declaration_inventory(
                    tool_paths=temp_paths,
                    workspace=workspace,
                    file_path=target_file,
                    manifest_path=manifest_path,
                )
            assert first.status is ProbeStatus.OK
            assert runner.call_count == 1

            clear_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch(
                    "kan_prover.engine.proofctl.run_lean_helper",
                    side_effect=AssertionError("cache hit should bypass the helper"),
                ),
            ):
                second = batch_declaration_inventory(
                    tool_paths=temp_paths,
                    workspace=workspace,
                    file_path=target_file,
                    manifest_path=manifest_path,
                )
            assert second.status is ProbeStatus.OK

            helper_file.write_text("theorem helper_truth : True := by\n  trivial\n", encoding="utf-8")
            clear_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch("kan_prover.engine.proofctl.run_lean_helper", return_value=probe) as runner,
            ):
                third = batch_declaration_inventory(
                    tool_paths=temp_paths,
                    workspace=workspace,
                    file_path=target_file,
                    manifest_path=manifest_path,
                )
            assert third.status is ProbeStatus.OK
            assert runner.call_count == 1

    @pytest.mark.integration
    def test_live_semantic_probe_reuses_unchanged_content_and_rebuilds_on_digest_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            workspace_root = self._linked_demo_workspace(temp_root / "workspace").resolve()
            workspace = bind_workspace(workspace_root)
            target_file = workspace_root / "Problems" / "LiveProbe.lean"
            source_v1 = "theorem target : True := by\n  exact True.intro\n"
            source_v2 = "theorem target : True := by\n  -- changed once\n  exact True.intro\n"
            target_file.write_text(source_v1, encoding="utf-8")

            with LiveSemanticSession(
                tool_paths=self.tool_paths,
                workspace=workspace,
                timeout_seconds=60.0,
            ) as session:
                first = session.context_bundle(
                    SemanticContextRequest(
                        tool_paths=self.tool_paths,
                        workspace=workspace,
                        file_path=target_file,
                        theorem="target",
                        timeout_seconds=60.0,
                        source_text=source_v1,
                    )
                )
                second = session.context_bundle(
                    SemanticContextRequest(
                        tool_paths=self.tool_paths,
                        workspace=workspace,
                        file_path=target_file,
                        theorem="target",
                        timeout_seconds=60.0,
                        source_text=source_v1,
                    )
                )
                third = session.context_bundle(
                    SemanticContextRequest(
                        tool_paths=self.tool_paths,
                        workspace=workspace,
                        file_path=target_file,
                        theorem="target",
                        timeout_seconds=60.0,
                        source_text=source_v2,
                    )
                )
                fourth = session.context_bundle(
                    SemanticContextRequest(
                        tool_paths=self.tool_paths,
                        workspace=workspace,
                        file_path=target_file,
                        theorem="target",
                        timeout_seconds=60.0,
                        source_text=source_v2,
                    )
                )

            assert first.session_facts.reuse == LiveFileSessionReuse.CREATED.value
            assert second.session_facts.reuse == LiveFileSessionReuse.REUSED.value
            assert third.session_facts.reuse == LiveFileSessionReuse.REBUILT.value
            assert fourth.session_facts.reuse == LiveFileSessionReuse.REUSED.value
            assert first.bundle_probe.status is ProbeStatus.OK
            assert second.bundle_probe.status is ProbeStatus.OK
            assert third.bundle_probe.status is ProbeStatus.OK
            assert fourth.bundle_probe.status is ProbeStatus.OK
            first_bundle = cast(ProofctlSemanticContextBundle, first.bundle_probe.result)
            second_bundle = cast(ProofctlSemanticContextBundle, second.bundle_probe.result)
            third_bundle = cast(ProofctlSemanticContextBundle, third.bundle_probe.result)
            assert first_bundle.declaration_target.declaration_name == "target"
            assert second_bundle.declaration_target.declaration_name == "target"
            assert third_bundle.declaration_target.declaration_name == "target"
            assert first_bundle.proof_state is not None
            assert second_bundle.proof_state is not None
            assert first_bundle.proof_state.goals == second_bundle.proof_state.goals
            assert third.session_facts.content_digest != first.session_facts.content_digest

    def _linked_repo_workspace(self, workspace_root: Path) -> Path:
        workspace_root.mkdir(parents=True, exist_ok=True)
        for name in ["lakefile.toml", "lake-manifest.json", "lean-toolchain", "KanProofs.lean"]:
            source = self.repo_root / name
            if source.is_file():
                (workspace_root / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        (workspace_root / ".lake").symlink_to(self.repo_root / ".lake", target_is_directory=True)
        shutil.copytree(self.repo_root / "KanProofs", workspace_root / "KanProofs", symlinks=True)
        return workspace_root

    def _linked_demo_workspace(self, workspace_root: Path) -> Path:
        demo_root = self.tool_paths.bundled_demo_runtime
        workspace_root.mkdir(parents=True, exist_ok=True)
        for name in ["lakefile.toml", "lake-manifest.json", "lean-toolchain", "KanRuntime.lean"]:
            (workspace_root / name).symlink_to(demo_root / name)
        (workspace_root / ".lake").symlink_to(demo_root / ".lake", target_is_directory=True)
        (workspace_root / "Tools").symlink_to(demo_root / "Tools", target_is_directory=True)
        (workspace_root / "Problems").mkdir(parents=True, exist_ok=True)
        return workspace_root

    def _write_text(self, path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        return path

    def _helper_request(self, *, command: LeanHelperCommand) -> LeanHelperRequest:
        return LeanHelperRequest(
            tool_paths=self.tool_paths,
            workspace=bind_workspace(self.repo_root),
            file_path=self.repo_mutual_target,
            theorem="substitution_comp_core",
            command=command,
            requires_writable=False,
        )

    def _helper_payload(
        self,
        *,
        command: LeanHelperCommand,
        status: str,
        result: JsonObject | None = None,
        message: str | None = None,
    ) -> JsonObject:
        payload: JsonObject = {
            "schema_version": "probe.v2",
            "layer": "semantic",
            "command": command.value,
            "status": status,
            "file": str(self.repo_mutual_target),
            "theorem": "substitution_comp_core",
        }
        if result is not None:
            payload["result"] = result
        if message is not None:
            payload["message"] = message
        return payload

    def _span_payload(self, start_line: int, start_column: int, end_line: int, end_column: int) -> JsonObject:
        return {
            "start": {"line": start_line, "column": start_column},
            "end": {"line": end_line, "column": end_column},
        }

    def _temp_paths(self, temp_artifacts: Path) -> ToolPaths:
        return ToolPaths(
            root=self.tool_paths.root,
            prompts=self.tool_paths.prompts,
            artifacts=temp_artifacts,
            artifact_runs=temp_artifacts / "runs",
            artifact_index=temp_artifacts / "index.jsonl",
            artifact_sessions=temp_artifacts / "sessions",
            bundled_demo_runtime=self.tool_paths.bundled_demo_runtime,
            packaged_probe_sources=self.tool_paths.packaged_probe_sources,
        )

    def _temp_tool_paths_with_runtime(self, temp_root: Path) -> ToolPaths:
        runtime_root = temp_root / "tool-root" / "lean-runtime"
        tools_dir = runtime_root / "Tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        (runtime_root / "lakefile.toml").write_text('name = "KanRuntime"\n', encoding="utf-8")
        (runtime_root / "lake-manifest.json").write_text('{"packages":[]}', encoding="utf-8")
        (runtime_root / "lean-toolchain").write_text("leanprover/lean4:stable\n", encoding="utf-8")
        (tools_dir / "SemanticProbe.lean").write_text("def main : IO Unit := pure ()\n", encoding="utf-8")
        return ToolPaths(
            root=temp_root / "tool-root",
            prompts=self.tool_paths.prompts,
            artifacts=temp_root / "artifacts",
            artifact_runs=temp_root / "artifacts" / "runs",
            artifact_index=temp_root / "artifacts" / "index.jsonl",
            artifact_sessions=temp_root / "artifacts" / "sessions",
            bundled_demo_runtime=runtime_root,
            packaged_probe_sources=tools_dir,
        )

    def _cache_workspace(self, workspace_root: Path) -> Path:
        workspace_root.mkdir(parents=True, exist_ok=True)
        (workspace_root / "lakefile.toml").write_text('name = "Demo"\n', encoding="utf-8")
        (workspace_root / "lake-manifest.json").write_text('{"packages":[]}', encoding="utf-8")
        (workspace_root / "lean-toolchain").write_text("leanprover/lean4:stable\n", encoding="utf-8")
        return workspace_root

    def _context_bundle_probe_result(self, *, file_path: Path, theorem: str) -> ProbeResult:
        declaration_span = ProbeSpan(start=ProbePoint(line=1, column=0), end=ProbePoint(line=2, column=0))
        body_span = ProbeSpan(start=ProbePoint(line=1, column=22), end=ProbePoint(line=2, column=0))
        return ProbeResult(
            layer=ProbeLayer.SEMANTIC,
            command=LeanHelperCommand.CONTEXT_BUNDLE.value,
            status=ProbeStatus.OK,
            file=str(file_path.resolve()),
            theorem=theorem,
            result=ProofctlSemanticContextBundle(
                declaration_target=ProofctlDeclarationTarget(
                    module_path="Demo",
                    short_name="target",
                    declaration_name=theorem,
                    namespace=None,
                    namespace_stack=[],
                    imports=[],
                    keyword="theorem",
                    modifiers=[],
                    group=ProofctlGroupMembership(kind="standalone"),
                    declaration_span=declaration_span,
                    name_span=ProbeSpan(start=ProbePoint(line=1, column=8), end=ProbePoint(line=1, column=14)),
                    body_span=body_span,
                    safe_edit=ProofctlSafeEditBoundary(
                        kind="replace-body",
                        declaration_span=declaration_span,
                        body_span=body_span,
                    ),
                    proof_state_available=True,
                ),
                proof_state=ProofctlProofState(
                    source="semantic",
                    declaration_name=theorem,
                    namespace=None,
                    locals=[],
                    local_context=[],
                    goals=["⊢ True"],
                    insertion_point=ProofctlInsertionPoint(
                        kind="replace-body",
                        declaration_name=theorem,
                        namespace=None,
                        position=ProbePoint(line=1, column=22),
                    ),
                ),
            ),
        )

    def _line_number(self, path: Path, needle: str) -> int:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if needle in line:
                return line_number
        msg = f"Could not find `{needle}` in `{path}`."
        raise AssertionError(msg)

    def _declaration_anchor_line(self, path: Path, theorem_line: str) -> int:
        lines = path.read_text(encoding="utf-8").splitlines()
        theorem_index = self._line_number(path, theorem_line) - 1
        for index in range(theorem_index, -1, -1):
            if lines[index].strip().startswith("/--"):
                return index + 1
        return theorem_index + 1

    def _cleanup_sandbox_workspace(self, sandbox: object) -> None:
        strategy = getattr(sandbox, "strategy", None)
        sandbox_root = getattr(sandbox, "sandbox_root", None)
        real_workspace = getattr(sandbox, "real_workspace", None)
        if not isinstance(sandbox_root, Path):
            return
        if strategy is None or real_workspace is None:
            return
        if str(strategy) == "git-worktree":
            git_root = getattr(real_workspace, "git_root", None)
            if isinstance(git_root, Path):
                subprocess.run(
                    ["git", "-C", str(git_root), "worktree", "remove", "--force", str(sandbox_root)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            return
        shutil.rmtree(sandbox_root, ignore_errors=True)

    def _repair_args(
        self,
        *,
        file: Path,
        workspace: Path,
        theorem: str,
        candidate_files: list[Path],
        apply: bool,
    ) -> Namespace:
        return Namespace(
            file=str(file),
            workspace=str(workspace),
            theorem=theorem,
            agent="none",
            candidate_file=[str(path) for path in candidate_files],
            replay_attempt=[],
            decomposition_file=[],
            timeout_seconds=60.0,
            apply=apply,
        )
