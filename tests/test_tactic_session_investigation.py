"""Focused tests for the tactic-session investigation harness."""

from pathlib import Path

from _pytest.monkeypatch import MonkeyPatch

from kan_prover.investigation.tactic_session_investigation import (
    AttemptStatus,
    LeanRunResult,
    classify_lean_run,
    evaluate_targets,
    render_tactic_body,
    replace_placeholder_body,
    run_tactic_search,
    run_whole_body_search,
)


def test_classify_lean_run_marks_unsolved_goals_as_frontier() -> None:
    stderr = "/tmp/Test.lean:7:2: error: unsolved goals\n⊢ True"
    assert classify_lean_run(return_code=1, stderr=stderr) is AttemptStatus.FRONTIER


def test_replace_placeholder_body_swaps_the_expected_stub() -> None:
    source = "import Mathlib\n\ntheorem demo : True := by\n  exact False.elim ?missing\n"
    replaced = replace_placeholder_body(source_text=source, body="by\n  exact True.intro\n")
    assert "False.elim" not in replaced
    assert "exact True.intro" in replaced


def test_whole_body_search_stops_at_first_verified_candidate() -> None:
    outcomes = iter(
        [
            LeanRunResult(AttemptStatus.FAILED, 1.0, 1, "", "bad"),
            LeanRunResult(AttemptStatus.VERIFIED, 2.0, 0, "", ""),
        ]
    )

    def runner(_body: str) -> LeanRunResult:
        return next(outcomes)

    result = run_whole_body_search(
        candidate_bank=(("bad", "by\n  aesop\n"), ("good", "by\n  omega\n")),
        runner=runner,
    )
    assert result.status is AttemptStatus.VERIFIED
    assert result.verifier_calls == 2
    assert result.winning_candidate_id == "good"


def test_tactic_search_expands_frontier_prefixes() -> None:
    results = {
        render_tactic_body(("setup",)): LeanRunResult(AttemptStatus.FRONTIER, 1.0, 1, "", "unsolved"),
        render_tactic_body(("finish",)): LeanRunResult(AttemptStatus.FAILED, 1.0, 1, "", "bad"),
        render_tactic_body(("other",)): LeanRunResult(AttemptStatus.FAILED, 1.0, 1, "", "bad"),
        render_tactic_body(("setup", "setup")): LeanRunResult(AttemptStatus.FAILED, 1.0, 1, "", "bad"),
        render_tactic_body(("setup", "finish")): LeanRunResult(AttemptStatus.VERIFIED, 1.0, 0, "", ""),
        render_tactic_body(("setup", "other")): LeanRunResult(AttemptStatus.FAILED, 1.0, 1, "", "bad"),
    }

    def runner(body: str) -> LeanRunResult:
        return results[body]

    result = run_tactic_search(
        tactic_bank=(("setup", "setup"), ("finish", "finish"), ("other", "other")),
        runner=runner,
        max_depth=2,
    )
    assert result.status is AttemptStatus.VERIFIED
    assert result.winning_candidate_id == "setup+finish"


def test_evaluate_targets_skips_tactic_search_after_whole_body_success(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    source = "import Mathlib\n\ntheorem demo : True := by\n  exact False.elim ?missing\n"
    target_file = tmp_path / "Demo.lean"
    target_file.write_text(source, encoding="utf-8")

    from kan_prover.investigation import tactic_session_investigation as mod

    calls: list[str] = []

    def fake_run_lean_body(*, workspace: Path, source_text: str, body: str) -> LeanRunResult:
        del workspace
        del source_text
        calls.append(body)
        if "native_decide" in body:
            return LeanRunResult(AttemptStatus.VERIFIED, 1.0, 0, "", "")
        return LeanRunResult(AttemptStatus.FAILED, 1.0, 1, "", "bad")

    monkeypatch.setattr(mod, "run_lean_body", fake_run_lean_body)
    monkeypatch.setattr(mod, "capture_probe_example", lambda *, target: None)

    payload = evaluate_targets(
        (
            mod.BenchmarkTarget(
                target_id="demo",
                suite="suite",
                workspace=tmp_path,
                file=target_file,
                theorem="demo",
            ),
        )
    )

    assert payload["summary"]["whole_body_solved"] == 1
    assert payload["summary"]["tactic_target_count"] == 0
    assert payload["targets"][0]["tactic_search"] is None
    assert any("native_decide" in body for body in calls)
