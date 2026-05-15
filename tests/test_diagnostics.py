"""Unit tests for diagnostic classification and repair strategy selection."""

from pathlib import Path

from lean_proofctl.engine.diagnostics import normalize_diagnostic, timeout_diagnostic
from lean_proofctl.engine.loop import _select_repair_strategy
from lean_proofctl.engine.models import DiagnosticClass, RepairStrategy, Severity


class TestDiagnostics:
    """Focused coverage for structured failure extraction."""

    def test_normalize_syntax_error(self) -> None:
        diagnostic = normalize_diagnostic(
            {
                "fileName": "/tmp/project/Demo.lean",
                "pos": {"line": 3, "column": 2},
                "endPos": {"line": 3, "column": 5},
                "severity": "error",
                "kind": "parser",
                "data": "unexpected token 'end'",
            },
            Path("/tmp/project"),
        )
        assert diagnostic.class_name is DiagnosticClass.SYNTAX_ERROR
        assert diagnostic.subexpression == "end"

    def test_normalize_unknown_identifier(self) -> None:
        diagnostic = normalize_diagnostic(
            {
                "fileName": "/tmp/project/Demo.lean",
                "pos": {"line": 5, "column": 8},
                "severity": "error",
                "kind": "lean.unknownConstant",
                "data": "unknown constant `helper_truth`",
            },
            Path("/tmp/project"),
        )
        assert diagnostic.class_name is DiagnosticClass.UNKNOWN_IDENTIFIER
        assert diagnostic.identifier == "helper_truth"

    def test_normalize_type_mismatch(self) -> None:
        diagnostic = normalize_diagnostic(
            {
                "fileName": "/tmp/project/Demo.lean",
                "pos": {"line": 6, "column": 2},
                "severity": "error",
                "kind": "lean.applicationTypeMismatch",
                "data": (
                    "application type mismatch\n"
                    "  Nat.succ\n"
                    "argument\n"
                    '  "x"\n'
                    "has type\n"
                    "  String\n"
                    "but is expected to have type\n"
                    "  Nat"
                ),
            },
            Path("/tmp/project"),
        )
        assert diagnostic.class_name is DiagnosticClass.TYPE_MISMATCH
        assert diagnostic.subexpression == "Nat.succ"

    def test_normalize_unsolved_goals(self) -> None:
        diagnostic = normalize_diagnostic(
            {
                "fileName": "/tmp/project/Demo.lean",
                "pos": {"line": 9, "column": 2},
                "severity": "error",
                "kind": "tactic.unsolvedGoals",
                "data": "unsolved goals\n⊢ True",
            },
            Path("/tmp/project"),
        )
        assert diagnostic.class_name is DiagnosticClass.UNSOLVED_GOALS
        assert diagnostic.subexpression == "⊢ True"

    def test_normalize_tactic_failure(self) -> None:
        diagnostic = normalize_diagnostic(
            {
                "fileName": "/tmp/project/Demo.lean",
                "pos": {"line": 10, "column": 2},
                "severity": "error",
                "kind": "tactic.exact",
                "data": "tactic 'exact' failed",
            },
            Path("/tmp/project"),
        )
        assert diagnostic.class_name is DiagnosticClass.TACTIC_FAILURE

    def test_normalize_resource_exhaustion(self) -> None:
        diagnostic = normalize_diagnostic(
            {
                "fileName": "/tmp/project/Demo.lean",
                "pos": {"line": 12, "column": 2},
                "severity": "error",
                "kind": "lean.elab",
                "data": "maximum number of heartbeats (200000) has been reached",
            },
            Path("/tmp/project"),
        )
        assert diagnostic.class_name is DiagnosticClass.RESOURCE_EXHAUSTION

    def test_timeout_diagnostic_classifies_timeout(self) -> None:
        diagnostic = timeout_diagnostic("Demo.lean", 60.0)
        assert diagnostic.class_name is DiagnosticClass.TIMEOUT
        assert diagnostic.severity is Severity.ERROR

    def test_select_repair_strategy_prefers_syntax_fix(self) -> None:
        syntax = normalize_diagnostic(
            {
                "fileName": "/tmp/project/Demo.lean",
                "pos": {"line": 3, "column": 2},
                "severity": "error",
                "kind": "parser",
                "data": "unexpected token 'end'",
            },
            Path("/tmp/project"),
        )
        unknown = normalize_diagnostic(
            {
                "fileName": "/tmp/project/Demo.lean",
                "pos": {"line": 5, "column": 8},
                "severity": "error",
                "kind": "lean.unknownConstant",
                "data": "unknown constant `helper_truth`",
            },
            Path("/tmp/project"),
        )
        strategy, selected = _select_repair_strategy(diagnostics=[unknown, syntax])
        assert strategy is RepairStrategy.SYNTAX_FIX
        assert selected is not None
        assert selected.diagnostic_class is DiagnosticClass.SYNTAX_ERROR

    def test_select_repair_strategy_uses_targeted_correction(self) -> None:
        diagnostic = normalize_diagnostic(
            {
                "fileName": "/tmp/project/Demo.lean",
                "pos": {"line": 5, "column": 8},
                "severity": "error",
                "kind": "lean.unknownConstant",
                "data": "unknown constant `helper_truth`",
            },
            Path("/tmp/project"),
        )
        strategy, selected = _select_repair_strategy(diagnostics=[diagnostic])
        assert strategy is RepairStrategy.TARGETED_CORRECTION
        assert selected is not None
        assert selected.identifier == "helper_truth"

    def test_select_repair_strategy_uses_regeneration_for_resource_exhaustion(self) -> None:
        diagnostic = normalize_diagnostic(
            {
                "fileName": "/tmp/project/Demo.lean",
                "pos": {"line": 12, "column": 2},
                "severity": "error",
                "kind": "lean.elab",
                "data": "maximum number of heartbeats (200000) has been reached",
            },
            Path("/tmp/project"),
        )
        strategy, selected = _select_repair_strategy(diagnostics=[diagnostic])
        assert strategy is RepairStrategy.REGENERATE
        assert selected is not None
        assert selected.diagnostic_class is DiagnosticClass.RESOURCE_EXHAUSTION
