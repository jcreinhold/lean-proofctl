"""Benchmark and prototype harness for tactic-session investigation work."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import yaml

PLACEHOLDER_BODY = "by\n  exact False.elim ?missing"
MAX_TACTIC_DEPTH = 1
LEAN_TIMEOUT_SECONDS = 5.0
PROBE_TIMEOUT_SECONDS = 20.0

WHOLE_BODY_BANK: tuple[tuple[str, str], ...] = (
    ("norm-num", "by\n  norm_num\n"),
    ("omega", "by\n  omega\n"),
    ("native-decide", "by\n  native_decide\n"),
)

TACTIC_BANK: tuple[tuple[str, str], ...] = (
    ("norm-num", "norm_num"),
    ("omega", "omega"),
)

PROBE_EXAMPLE_TARGET = "mathd_numbertheory_1124"
PROBE_EXAMPLE_STEPS = ("omega",)
JsonDict = dict[str, Any]


class AttemptStatus(StrEnum):
    """Normalized theorem-check outcomes for the investigation harness."""

    VERIFIED = "verified"
    FRONTIER = "frontier"
    FAILED = "failed"


@dataclass(frozen=True)
class BenchmarkTarget:
    """One theorem row from the standard benchmark manifests."""

    target_id: str
    suite: str
    workspace: Path
    file: Path
    theorem: str


@dataclass(frozen=True)
class LeanRunResult:
    """One Lean verification outcome over one temporary theorem body."""

    status: AttemptStatus
    wall_time_seconds: float
    return_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class SearchAttempt:
    """One whole-body or tactic-prefix verification attempt."""

    candidate_id: str
    body: str
    status: AttemptStatus
    wall_time_seconds: float


@dataclass(frozen=True)
class SearchOutcome:
    """Aggregate result for one search condition on one theorem."""

    status: AttemptStatus
    verifier_calls: int
    wall_time_seconds: float
    winning_candidate_id: str | None
    winning_body: str | None
    attempts: tuple[SearchAttempt, ...]


@dataclass(frozen=True)
class ProbeExample:
    """One recorded post-last-tactic probe observation."""

    target_id: str
    command: str
    status: str
    source: str | None
    goal_count: int | None
    first_goal: str | None
    wall_time_seconds: float
    stderr: str


@dataclass(frozen=True)
class TargetComparison:
    """Benchmark comparison row for one theorem."""

    target_id: str
    suite: str
    theorem: str
    whole_body: SearchOutcome
    tactic_search: SearchOutcome | None


def tool_root() -> Path:
    """Return the `lean-proofctl` root from this module path."""
    return Path(__file__).resolve().parents[3]


def benchmark_root() -> Path:
    """Return the checked-in benchmark root."""
    return tool_root() / "benchmarks"


def docs_data_path() -> Path:
    """Return the default JSON output path for recorded measurements."""
    return tool_root() / "docs" / "data" / "tactic-session-investigation.json"


def _decoded_timeout_text(value: str | bytes | None) -> str:
    """Normalize timeout stdout/stderr payloads into text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def classify_lean_run(*, return_code: int, stderr: str) -> AttemptStatus:
    """Map one Lean process result into verified, frontier, or failed."""
    if return_code == 0:
        return AttemptStatus.VERIFIED
    error_lines = [line for line in stderr.splitlines() if ": error:" in line]
    if error_lines and all("unsolved goals" in line for line in error_lines):
        return AttemptStatus.FRONTIER
    return AttemptStatus.FAILED


def replace_placeholder_body(*, source_text: str, body: str) -> str:
    """Swap the shipped failing placeholder body for one candidate body."""
    if PLACEHOLDER_BODY not in source_text:
        msg = "Benchmark theorem does not contain the expected placeholder body."
        raise ValueError(msg)
    return source_text.replace(PLACEHOLDER_BODY, body.rstrip(), 1)


def render_tactic_body(steps: tuple[str, ...]) -> str:
    """Render one tactic prefix into a theorem body."""
    if not steps:
        return "by\n"
    lines = "\n".join(f"  {step}" for step in steps)
    return f"by\n{lines}\n"


def run_lean_body(*, workspace: Path, source_text: str, body: str) -> LeanRunResult:
    """Verify one theorem body by compiling a temporary Lean file inside one workspace."""
    with tempfile.TemporaryDirectory(prefix="proofctl-tactic-investigation-") as temp_dir:
        temp_file = Path(temp_dir) / "Investigation.lean"
        temp_file.write_text(replace_placeholder_body(source_text=source_text, body=body), encoding="utf-8")
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                ["lake", "env", "lean", str(temp_file)],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
                timeout=LEAN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            finished = time.perf_counter()
            stderr = _decoded_timeout_text(error.stderr).strip()
            stdout = _decoded_timeout_text(error.stdout).strip()
            return LeanRunResult(
                status=AttemptStatus.FAILED,
                wall_time_seconds=finished - started,
                return_code=124,
                stdout=stdout,
                stderr=f"lean-timeout: {stderr}".strip(),
            )
        finished = time.perf_counter()
    return LeanRunResult(
        status=classify_lean_run(return_code=completed.returncode, stderr=completed.stderr),
        wall_time_seconds=finished - started,
        return_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def run_whole_body_search(
    *,
    candidate_bank: tuple[tuple[str, str], ...],
    runner: Callable[[str], LeanRunResult],
) -> SearchOutcome:
    """Try the whole-body candidate bank in order and stop at the first verified body."""
    attempts: list[SearchAttempt] = []
    total_time = 0.0
    for candidate_id, body in candidate_bank:
        result = runner(body)
        total_time += result.wall_time_seconds
        attempts.append(
            SearchAttempt(
                candidate_id=candidate_id,
                body=body,
                status=result.status,
                wall_time_seconds=result.wall_time_seconds,
            )
        )
        if result.status is AttemptStatus.VERIFIED:
            return SearchOutcome(
                status=AttemptStatus.VERIFIED,
                verifier_calls=len(attempts),
                wall_time_seconds=total_time,
                winning_candidate_id=candidate_id,
                winning_body=body,
                attempts=tuple(attempts),
            )
    return SearchOutcome(
        status=attempts[-1].status if attempts else AttemptStatus.FAILED,
        verifier_calls=len(attempts),
        wall_time_seconds=total_time,
        winning_candidate_id=None,
        winning_body=None,
        attempts=tuple(attempts),
    )


def run_tactic_search(
    *,
    tactic_bank: tuple[tuple[str, str], ...],
    runner: Callable[[str], LeanRunResult],
    max_depth: int = MAX_TACTIC_DEPTH,
) -> SearchOutcome:
    """Search tactic prefixes to a bounded depth using Lean verification as the oracle."""
    attempts: list[SearchAttempt] = []
    total_time = 0.0
    frontier: list[tuple[str, ...]] = [()]
    seen: set[tuple[str, ...]] = {()}
    for depth in range(1, max_depth + 1):
        next_frontier: list[tuple[str, ...]] = []
        for prefix in frontier:
            for candidate_id, tactic in tactic_bank:
                steps = (*prefix, tactic)
                if steps in seen:
                    continue
                seen.add(steps)
                body = render_tactic_body(steps)
                result = runner(body)
                total_time += result.wall_time_seconds
                attempts.append(
                    SearchAttempt(
                        candidate_id="+".join([*(part.replace(" ", "_") for part in prefix), candidate_id]),
                        body=body,
                        status=result.status,
                        wall_time_seconds=result.wall_time_seconds,
                    )
                )
                if result.status is AttemptStatus.VERIFIED:
                    return SearchOutcome(
                        status=AttemptStatus.VERIFIED,
                        verifier_calls=len(attempts),
                        wall_time_seconds=total_time,
                        winning_candidate_id=attempts[-1].candidate_id,
                        winning_body=body,
                        attempts=tuple(attempts),
                    )
                if result.status is AttemptStatus.FRONTIER and depth < max_depth:
                    next_frontier.append(steps)
        frontier = next_frontier
    return SearchOutcome(
        status=attempts[-1].status if attempts else AttemptStatus.FAILED,
        verifier_calls=len(attempts),
        wall_time_seconds=total_time,
        winning_candidate_id=None,
        winning_body=None,
        attempts=tuple(attempts),
    )


def load_standard_targets() -> tuple[BenchmarkTarget, ...]:
    """Load the ten standard benchmark rows from the checked-in manifests."""
    manifests = (
        benchmark_root() / "manifests" / "minif2f-test-subset.yaml",
        benchmark_root() / "manifests" / "putnambench-subset.yaml",
    )
    targets: list[BenchmarkTarget] = []
    for manifest in manifests:
        payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            msg = f"Manifest is not a mapping: {manifest}"
            raise ValueError(msg)
        benchmark = payload.get("benchmark")
        if not isinstance(benchmark, dict):
            msg = f"Manifest is missing `benchmark`: {manifest}"
            raise ValueError(msg)
        workspace_value = payload.get("workspace")
        if not isinstance(workspace_value, str):
            msg = f"Manifest is missing string `workspace`: {manifest}"
            raise ValueError(msg)
        rows = payload.get("targets")
        if not isinstance(rows, list):
            msg = f"Manifest is missing list `targets`: {manifest}"
            raise ValueError(msg)
        workspace = (manifest.parent / workspace_value).resolve()
        suite = str(benchmark.get("suite") or benchmark.get("family") or manifest.stem)
        for row in rows:
            if not isinstance(row, dict):
                msg = f"Manifest target is not a mapping: {manifest}"
                raise ValueError(msg)
            target_id = row.get("id")
            file_value = row.get("file")
            theorem = row.get("theorem")
            if not isinstance(target_id, str) or not isinstance(file_value, str) or not isinstance(theorem, str):
                msg = f"Manifest target is missing required fields: {manifest}"
                raise ValueError(msg)
            targets.append(
                BenchmarkTarget(
                    target_id=target_id,
                    suite=suite,
                    workspace=workspace,
                    file=(workspace / file_value).resolve(),
                    theorem=theorem,
                )
            )
    return tuple(targets)


def capture_probe_example(*, target: BenchmarkTarget) -> ProbeExample | None:
    """Record one post-last-tactic probe example on the miniF2F surface."""
    if target.target_id != PROBE_EXAMPLE_TARGET:
        return None
    source_text = target.file.read_text(encoding="utf-8")
    body = render_tactic_body(PROBE_EXAMPLE_STEPS)
    with tempfile.TemporaryDirectory(prefix="proofctl-tactic-probe-") as temp_dir:
        temp_file = Path(temp_dir) / "Investigation.lean"
        temp_file.write_text(replace_placeholder_body(source_text=source_text, body=body), encoding="utf-8")
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [
                    "lake",
                    "env",
                    "lean",
                    "--run",
                    str(tool_root() / "lean-runtime" / "Tools" / "SemanticProbe.lean"),
                    "proof-state-after-last-tactic",
                    str(target.workspace),
                    str(temp_file),
                    target.theorem,
                ],
                cwd=target.workspace,
                capture_output=True,
                text=True,
                check=False,
                timeout=PROBE_TIMEOUT_SECONDS,
            )
            finished = time.perf_counter()
        except subprocess.TimeoutExpired as error:
            finished = time.perf_counter()
            return ProbeExample(
                target_id=target.target_id,
                command="proof-state-after-last-tactic",
                status="timeout",
                source=None,
                goal_count=None,
                first_goal=None,
                wall_time_seconds=finished - started,
                stderr=_decoded_timeout_text(error.stderr).strip(),
            )
    payload = _decode_json_from_stdout(completed.stdout)
    if not isinstance(payload, dict):
        return ProbeExample(
            target_id=target.target_id,
            command="proof-state-after-last-tactic",
            status="decode-error",
            source=None,
            goal_count=None,
            first_goal=None,
            wall_time_seconds=finished - started,
            stderr=completed.stderr.strip(),
        )
    payload_dict = cast(JsonDict, payload)
    result = payload_dict.get("result")
    result_dict = result if isinstance(result, dict) else None
    goals = result_dict.get("goals") if result_dict is not None else None
    goal_list = goals if isinstance(goals, list) else []
    first_goal = goal_list[0] if goal_list and isinstance(goal_list[0], str) else None
    source_value = result_dict.get("source") if result_dict is not None else None
    source = source_value if isinstance(source_value, str) else None
    return ProbeExample(
        target_id=target.target_id,
        command="proof-state-after-last-tactic",
        status=str(payload_dict.get("status")),
        source=source,
        goal_count=len(goal_list) if isinstance(goal_list, list) else None,
        first_goal=first_goal,
        wall_time_seconds=finished - started,
        stderr=completed.stderr.strip(),
    )


def evaluate_targets(targets: tuple[BenchmarkTarget, ...]) -> dict[str, Any]:
    """Run the whole-body and tactic-search comparison over the target set."""
    comparisons: list[TargetComparison] = []
    probe_examples: list[ProbeExample] = []
    tactic_targets = 0
    for target in targets:
        print(f"[investigation] target {target.target_id}")
        source_text = target.file.read_text(encoding="utf-8")

        workspace = target.workspace

        def runner(body: str, *, workspace: Path = workspace, source_text: str = source_text) -> LeanRunResult:
            return run_lean_body(workspace=workspace, source_text=source_text, body=body)

        whole_body = run_whole_body_search(candidate_bank=WHOLE_BODY_BANK, runner=runner)
        tactic_search: SearchOutcome | None = None
        if whole_body.status is not AttemptStatus.VERIFIED:
            tactic_targets += 1
            tactic_search = run_tactic_search(tactic_bank=TACTIC_BANK, runner=runner)
        comparisons.append(
            TargetComparison(
                target_id=target.target_id,
                suite=target.suite,
                theorem=target.theorem,
                whole_body=whole_body,
                tactic_search=tactic_search,
            )
        )
        probe_example = capture_probe_example(target=target)
        if probe_example is not None:
            probe_examples.append(probe_example)
        print(
            "[investigation] done"
            f" {target.target_id}"
            f" whole_body={whole_body.status}"
            f" tactic_search={tactic_search.status if tactic_search is not None else 'skipped'}"
        )
    whole_body_solved = sum(1 for row in comparisons if row.whole_body.status is AttemptStatus.VERIFIED)
    tactic_solved = sum(
        1 for row in comparisons if row.tactic_search is not None and row.tactic_search.status is AttemptStatus.VERIFIED
    )
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "comparison_method": {
            "evaluation_shape": (
                "run whole-body over all targets, then run tactic search only on targets that whole-body did not verify"
            ),
            "whole_body_candidate_bank": [{"id": key, "body": body} for key, body in WHOLE_BODY_BANK],
            "tactic_bank": [{"id": key, "tactic": tactic} for key, tactic in TACTIC_BANK],
            "max_tactic_depth": MAX_TACTIC_DEPTH,
            "lean_timeout_seconds": LEAN_TIMEOUT_SECONDS,
            "classification_rule": "verified on exit 0; frontier on unsolved-goals-only diagnostics; failed otherwise",
        },
        "summary": {
            "target_count": len(comparisons),
            "whole_body_solved": whole_body_solved,
            "whole_body_unsolved": len(comparisons) - whole_body_solved,
            "tactic_target_count": tactic_targets,
            "tactic_search_solved": tactic_solved,
            "whole_body_total_calls": sum(row.whole_body.verifier_calls for row in comparisons),
            "tactic_search_total_calls": sum(
                row.tactic_search.verifier_calls for row in comparisons if row.tactic_search is not None
            ),
            "whole_body_total_wall_time_seconds": round(
                sum(row.whole_body.wall_time_seconds for row in comparisons), 3
            ),
            "tactic_search_total_wall_time_seconds": round(
                sum(row.tactic_search.wall_time_seconds for row in comparisons if row.tactic_search is not None), 3
            ),
        },
        "targets": [asdict(row) for row in comparisons],
        "probe_examples": [asdict(example) for example in probe_examples],
    }


def _decode_json_from_stdout(stdout: str) -> object | None:
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            return json.loads(stdout[index:])
        except json.JSONDecodeError:
            continue
    return None


def parse_args() -> argparse.Namespace:
    """Parse CLI flags for the investigation harness."""
    parser = argparse.ArgumentParser(
        description="Run the tactic-session investigation comparison over the standard benchmark manifests."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=docs_data_path(),
        help="Path to the output JSON file.",
    )
    return parser.parse_args()


def main() -> int:
    """Run the investigation harness and write its JSON result."""
    args = parse_args()
    payload = evaluate_targets(load_standard_targets())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
