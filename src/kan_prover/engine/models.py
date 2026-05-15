"""Typed models for CLI requests, results, diagnostics, and artifacts."""

from dataclasses import dataclass, field, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

type JsonPrimitive = None | bool | int | float | str
type JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


class ExitCode(StrEnum):
    """Stable exit code names for machine consumers."""

    SUCCESS = "success"
    CHECK_FAILED = "check-failed"
    BAD_USAGE = "bad-usage"
    TIMEOUT = "timeout"
    INTERNAL_ERROR = "internal-error"


class Severity(StrEnum):
    """Normalized Lean diagnostic severities."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class DiagnosticClass(StrEnum):
    """Stable diagnostic classes used by the verifier boundary."""

    SYNTAX_ERROR = "syntax-error"
    UNKNOWN_IDENTIFIER = "unknown-identifier"
    TYPE_MISMATCH = "type-mismatch"
    TACTIC_FAILURE = "tactic-failure"
    RESOURCE_EXHAUSTION = "resource-exhaustion"
    PARSER = "parser"
    IMPORT = "import"
    UNKNOWN_NAME = "unknown-name"
    ELABORATION = "elaboration"
    TACTIC = "tactic"
    UNSOLVED_GOALS = "unsolved-goals"
    TIMEOUT = "timeout"
    OTHER = "other"


class CheckStatus(StrEnum):
    """Top-level result state for a verifier run."""

    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


class LoopStatus(StrEnum):
    """Top-level result state for repair and solve loops."""

    VERIFIED = "verified"
    FAILED = "failed"
    NO_CANDIDATE = "no-candidate"
    ERROR = "error"


class SandboxStrategy(StrEnum):
    """Stable sandbox materialization strategies."""

    GIT_WORKTREE = "git-worktree"
    COPIED_WORKSPACE = "copied-workspace"


class InspectionStatus(StrEnum):
    """Top-level result state for inspection and export commands."""

    OK = "ok"
    ERROR = "error"


class FileStateCheckStatus(StrEnum):
    """Whether the extra full-file Lean check ran for one read command."""

    SKIPPED = "skipped"
    RAN = "ran"


class PlanStatus(StrEnum):
    """Top-level result state for theorem planning commands."""

    PLANNED = "planned"
    NO_PLAN = "no-plan"
    ERROR = "error"


class ProveStatus(StrEnum):
    """Top-level result state for graph-backed prove runs."""

    VERIFIED = "verified"
    UNSOLVED = "unsolved"
    ERROR = "error"


class ResumeStatus(StrEnum):
    """Top-level result state for stored-graph resume runs."""

    VERIFIED = "verified"
    UNSOLVED = "unsolved"
    ERROR = "error"


class HelperStatus(StrEnum):
    """Stable lifecycle states for managed helper obligations."""

    PROPOSED = "proposed"
    ACTIVE = "active"
    VERIFIED = "verified"
    DISCARDED = "discarded"


class DoctorStatus(StrEnum):
    """Top-level result state for workspace readiness checks."""

    READY = "ready"
    NOT_READY = "not-ready"
    ERROR = "error"


class DoctorCheckStatus(StrEnum):
    """Stable per-check status values for `kan-prover doctor`."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


class BatchStatus(StrEnum):
    """Top-level result state for benchmark batches."""

    COMPLETED = "completed"
    FAILED = "failed"


class ApplyStatus(StrEnum):
    """Top-level result state for verified graph transplants."""

    APPLIED = "applied"
    ERROR = "error"


class ApplyErrorCode(StrEnum):
    """Machine-readable apply failure categories."""

    NO_VERIFIED_NODE = "no-verified-node"
    AMBIGUOUS_VERIFIED_NODE = "ambiguous-verified-node"
    NODE_NOT_FOUND = "node-not-found"
    NODE_NOT_THEOREM_STATE = "node-not-theorem-state"
    NODE_NOT_VERIFIED = "node-not-verified"
    HELPER_CONFLICT = "helper-conflict"
    HELPER_OUTSIDE_MANAGED_ROOT = "helper-outside-managed-root"
    HELPER_SOURCE_MISSING = "helper-source-missing"
    INVALID_SOURCE_RUN = "invalid-source-run"


class SessionStatus(StrEnum):
    """Top-level result state for theorem session commands."""

    ACTIVE = "active"
    FINISHED_VERIFIED = "finished-verified"
    FINISHED_FAILED = "finished-failed"
    ERROR = "error"


class SessionActionKind(StrEnum):
    """Stable theorem session action names."""

    START = "start"
    STEP = "step"
    GOALS = "goals"
    CHECKPOINT = "checkpoint"
    FINISH = "finish"


class EvaluationWorkflow(StrEnum):
    """Stable workflow kinds for paired benchmark conditions."""

    PLAIN = "plain"
    TOOL = "tool"


class TaskKind(StrEnum):
    """Stable backend task kinds."""

    SOLVE = "solve"
    REPAIR = "repair"
    DECOMPOSE = "decompose"
    PLAN = "plan"
    THEOREM_STEP = "theorem_step"
    MINIMIZE = "minimize"


class RepairStrategy(StrEnum):
    """Stable theorem-body repair strategies chosen by the engine."""

    REGENERATE = "regenerate"
    TARGETED_CORRECTION = "targeted-correction"
    SYNTAX_FIX = "syntax-fix"


class StrategyFamily(StrEnum):
    """Stable proof-graph strategy families."""

    WHOLE_BODY = "whole-body"
    STATE_SEARCH = "state-search"
    REPAIR_FROM_DIAGNOSTIC = "repair-from-diagnostic"


class BackendResultKind(StrEnum):
    """Strict backend result kinds."""

    THEOREM_BODY_REPLACEMENTS = "theorem_body_replacements"
    DECOMPOSITION = "decomposition"
    THEOREM_PLAN = "theorem_plan"
    THEOREM_SESSION_ACTIONS = "theorem_session_actions"
    NO_PROGRESS = "no_progress"


class TheoremBodyResponseClass(StrEnum):
    """Stable classification for theorem-body backend response shapes."""

    NO_PROGRESS = "no_progress"
    SINGLE_USABLE_CANDIDATE = "single-usable-candidate"
    NO_USABLE_CANDIDATE = "no-usable-candidate"
    MULTIPLE_USABLE_CANDIDATES = "multiple-usable-candidates"
    UNUSABLE_RESPONSE = "unusable-response"


class TheoremSessionActionKind(StrEnum):
    """Strict bounded action kinds for theorem session guidance."""

    APPEND_TACTIC_TEXT = "append_tactic_text"
    REPLACE_THEOREM_DRAFT = "replace_theorem_draft"
    PROPOSE_HELPER_LEMMA = "propose_helper_lemma"
    SUGGEST_CHECKPOINT_LABEL = "suggest_checkpoint_label"
    RECOMMEND_STRATEGY_SHIFT = "recommend_strategy_shift"


class RetrievalSourceKind(StrEnum):
    """Stable retrieval source kinds."""

    SAME_FILE = "same-file"
    SAME_NAMESPACE = "same-namespace"
    GOAL_DECLARATION = "goal-declaration"
    ACCEPTED_PATCH = "accepted-patch"
    PROVED_HELPER = "proved-helper"
    PROOF_FRAGMENT = "proof-fragment"
    GRAPH_STATE = "graph-state"
    TACTIC_FRAGMENT = "tactic-fragment"
    FAILURE_SUMMARY = "failure-summary"


@dataclass(frozen=True)
class WorkspaceMetadata:
    """Stable workspace metadata persisted with verifier artifacts."""

    root: str
    package: str
    toolchain: str


@dataclass(frozen=True)
class SandboxMetadata:
    """Stable sandbox metadata persisted with verifier artifacts."""

    real_workspace_root: str
    sandbox_root: str
    sandbox_workspace_root: str
    strategy: SandboxStrategy
    helper_root_dir: str
    helper_module_root: str


@dataclass(frozen=True)
class Position:
    """A 1-based line and 0-based column pair from Lean."""

    line: int
    column: int


@dataclass(frozen=True)
class Range:
    """A normalized Lean source range."""

    startLine: int
    startCol: int
    endLine: int
    endCol: int


@dataclass(frozen=True)
class RelatedDiagnostic:
    """A related diagnostic entry."""

    file: str
    range: Range
    message: str


@dataclass(frozen=True)
class Diagnostic:
    """Stable diagnostic schema exposed by the CLI and ledger."""

    file: str
    range: Range
    severity: Severity
    message: str
    related: list[RelatedDiagnostic] = field(default_factory=list)
    class_name: DiagnosticClass = DiagnosticClass.OTHER
    kind: str | None = None
    caption: str | None = None
    identifier: str | None = None
    subexpression: str | None = None

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize with the external `class` field name."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["class"] = data.pop("class_name")
        return data


@dataclass(frozen=True)
class RunRequest:
    """Machine-readable input for one verifier run."""

    command: str
    target_file: str
    cwd: str
    tool_root: str
    workspace_root: str
    workspace_package: str
    workspace_toolchain: str
    target_module_path: str
    timeout_seconds: float
    json_output: bool
    jsonl_output: bool
    started_at: datetime
    sandbox_strategy: SandboxStrategy | None = None
    sandbox_root: str | None = None
    sandbox_workspace_root: str | None = None
    helper_module_root: str | None = None
    helper_root_dir: str | None = None
    read_only: bool | None = None
    verify_file_state: bool | None = None
    proof_target: dict[str, JsonValue] | None = None
    syntax_probe_results: dict[str, JsonValue] = field(default_factory=dict)
    semantic_probe_results: dict[str, JsonValue] = field(default_factory=dict)
    probe_results: dict[str, JsonValue] = field(default_factory=dict)
    theorem: str | None = None
    backend: str | None = None
    apply: bool | None = None
    max_attempts: int | None = None
    max_decomposition_depth: int | None = None


@dataclass(frozen=True)
class LeanInvocation:
    """The exact command passed through Lake."""

    argv: list[str]
    cwd: str
    timeout_seconds: float


@dataclass(frozen=True)
class LeanRunResult:
    """Raw subprocess output from Lake."""

    invocation: LeanInvocation
    returncode: int | None
    wall_time_seconds: float
    stdout: str
    stderr: str
    timed_out: bool
    diagnostics: list[Diagnostic]
    parse_errors: list[str]


@dataclass(frozen=True)
class FileStateCheck:
    """Summary of the optional full-file Lean check on a read command."""

    status: FileStateCheckStatus
    requested: bool
    diagnostic_count: int


@dataclass(frozen=True)
class RunManifest:
    """High-level run metadata stored beside artifacts."""

    run_id: str
    command: str
    status: (
        CheckStatus
        | LoopStatus
        | InspectionStatus
        | PlanStatus
        | ProveStatus
        | ResumeStatus
        | BatchStatus
        | SessionStatus
        | ApplyStatus
    )
    exit_code: ExitCode
    created_at: datetime
    artifact_dir: str


@dataclass(frozen=True)
class CheckResult:
    """Final machine result for `kan-prover check`."""

    run_id: str
    command: str
    status: CheckStatus
    exit_code: ExitCode
    target_file: str
    tool_root: str
    workspace: WorkspaceMetadata
    artifact_dir: str
    wall_time_seconds: float
    diagnostic_count: int
    diagnostics: list[Diagnostic]
    lean: LeanRunResult
    started_at: datetime
    finished_at: datetime

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize with normalized diagnostic output."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["diagnostics"] = [diagnostic.to_json() for diagnostic in self.diagnostics]
        lean = cast(dict[str, JsonValue], data["lean"])
        lean["diagnostics"] = [diagnostic.to_json() for diagnostic in self.lean.diagnostics]
        return data


@dataclass(frozen=True)
class InitResult:
    """Final machine result for `kan-prover init`."""

    command: str
    status: CheckStatus
    exit_code: ExitCode
    tool_root: str
    workspace_root: str
    workspace_package: str
    workspace_toolchain: str
    workspace_role: str
    selected_via: str
    artifacts_root: str
    build_ran: bool
    started_at: datetime
    finished_at: datetime
    wall_time_seconds: float
    build_stdout: str = ""
    build_stderr: str = ""

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the init response."""
        return cast(dict[str, JsonValue], dataclass_to_jsonable(self))


@dataclass(frozen=True)
class DoctorCheck:
    """One actionable workspace readiness check."""

    name: str
    status: DoctorCheckStatus
    summary: str
    detail: str | None = None


@dataclass(frozen=True)
class DoctorResult:
    """Final machine result for `kan-prover doctor`."""

    command: str
    status: DoctorStatus
    exit_code: ExitCode
    tool_root: str
    inspected_path: str
    workspace_root: str | None
    workspace_package: str | None
    workspace_toolchain: str | None
    workspace_role: str
    selected_via: str
    ready: bool
    checks: list[DoctorCheck]
    started_at: datetime
    finished_at: datetime
    wall_time_seconds: float

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the doctor response."""
        return cast(dict[str, JsonValue], dataclass_to_jsonable(self))


@dataclass(frozen=True)
class FileSpan:
    """A 1-based inclusive source span in one file."""

    start_line: int
    end_line: int


@dataclass(frozen=True)
class SourceWindow:
    """A small source window around the target theorem."""

    file: str
    start_line: int
    end_line: int
    text: str


@dataclass(frozen=True)
class InsertionPointSummary:
    """A stable insertion point surfaced by semantic probes."""

    kind: str
    position: Position


@dataclass(frozen=True)
class RetrievalScoreComponent:
    """One additive retrieval score component."""

    label: str
    points: int
    detail: str | None = None


@dataclass(frozen=True)
class RetrievalHit:
    """One ranked retrieval item passed to the backend."""

    hit_id: str
    source_kind: RetrievalSourceKind
    title: str
    summary: str
    content: str
    score: int
    path: str | None = None
    theorem: str | None = None
    source_run_id: str | None = None
    match_reasons: list[str] = field(default_factory=list)
    score_breakdown: list[RetrievalScoreComponent] = field(default_factory=list)


@dataclass(frozen=True)
class RetrievalNearMiss:
    """One declaration-shaped retrieval candidate that ranked below the returned hits."""

    title: str
    source_kind: RetrievalSourceKind
    summary: str
    score: int
    omission_reason: str
    path: str | None = None
    theorem: str | None = None
    match_reasons: list[str] = field(default_factory=list)
    score_breakdown: list[RetrievalScoreComponent] = field(default_factory=list)


@dataclass(frozen=True)
class RetrievalExplanation:
    """Stable retrieval explanation surfaced to operators and artifacts."""

    hit_limit: int
    near_miss_limit: int
    hits: list[RetrievalHit]
    near_misses: list[RetrievalNearMiss]
    active_helpers: list[HelperRef]


@dataclass(frozen=True)
class HelperRef:
    """A proved helper module that can be imported into the target file."""

    helper_name: str
    module_name: str
    file_path: str
    theorem: str
    statement: str
    workspace_root: str | None = None
    target_module_path: str | None = None
    helper_module_root: str | None = None


@dataclass(frozen=True)
class PriorAttemptSummary:
    """Compact summary of one earlier repair attempt."""

    attempt_index: int
    candidate_id: str
    source: str
    verified: bool
    diagnostic_count: int
    diagnostics: list[Diagnostic]


@dataclass(frozen=True)
class RepairFailure:
    """Structured failure details used to select one repair strategy."""

    diagnostic_index: int
    diagnostic_class: DiagnosticClass
    severity: Severity
    file: str
    range: Range
    message: str
    kind: str | None = None
    identifier: str | None = None
    subexpression: str | None = None


@dataclass(frozen=True)
class AgentRequest:
    """Stable strict request passed to one backend attempt."""

    task_kind: TaskKind
    attempt_index: int
    candidate_limit: int
    theorem_name: str
    target_file: str
    workspace_root: str
    workspace_package: str
    sandbox_workspace_root: str
    target_module_path: str
    helper_module_root: str
    helper_root_dir: str
    theorem_statement: str | None
    editable_span: FileSpan | None
    source_window: SourceWindow
    diagnostics: list[Diagnostic]
    prior_attempts: list[PriorAttemptSummary]
    retrieval_hits: list[RetrievalHit]
    active_helpers: list[HelperRef]
    allowed_output_files: list[str]
    schema_path: str
    repair_strategy: RepairStrategy | None = None
    selected_failure: RepairFailure | None = None
    imports: list[str] = field(default_factory=list)
    namespace: str | None = None
    proof_target: dict[str, JsonValue] | None = None
    proof_state: dict[str, JsonValue] | None = None
    local_context: list[str] = field(default_factory=list)
    goal_snapshot: list[str] = field(default_factory=list)
    session_id: str | None = None
    current_draft: str | None = None
    checkpoint_labels: list[str] = field(default_factory=list)
    prompt_template_name: str | None = None
    syntax_probe_results: dict[str, JsonValue] = field(default_factory=dict)
    semantic_probe_results: dict[str, JsonValue] = field(default_factory=dict)
    probe_results: dict[str, JsonValue] = field(default_factory=dict)

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize with normalized diagnostics."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["diagnostics"] = [diagnostic.to_json() for diagnostic in self.diagnostics]
        data["prior_attempts"] = [
            {
                **cast(dict[str, JsonValue], dataclass_to_jsonable(attempt)),
                "diagnostics": [diagnostic.to_json() for diagnostic in attempt.diagnostics],
            }
            for attempt in self.prior_attempts
        ]
        return data


@dataclass(frozen=True)
class BackendInvocation:
    """Exact backend command and working directory for one strict call."""

    argv: list[str]
    cwd: str


@dataclass(frozen=True)
class PatchCandidate:
    """One bounded theorem-body replacement candidate."""

    candidate_id: str
    theorem: str
    body: str
    source: str
    replay_from: str | None = None


class DecompositionLayer(StrEnum):
    """The failure layer a decomposition step addresses."""

    MISSING_LEMMA = "missing-lemma"
    LOCAL_PATCH = "local-patch"


@dataclass(frozen=True)
class DecompositionStep:
    """One bounded decomposition or helper planning step."""

    step_id: str
    title: str
    goal: str
    layer: DecompositionLayer
    helper_name: str | None = None
    helper_statement: str | None = None
    helper_proof: str | None = None
    helper_imports: list[str] = field(default_factory=list)
    file_hint: str | None = None


@dataclass(frozen=True)
class TheoremPlanStep:
    """One explicit theorem planning step from a backend."""

    step_id: str
    title: str
    goal: str
    rationale: str
    depends_on: list[str] = field(default_factory=list)
    helper_suggestions: list[str] = field(default_factory=list)
    retrieval_refs: list[str] = field(default_factory=list)
    file_actions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TheoremPlan:
    """A strict structured theorem plan."""

    summary: str
    strategy: str
    steps: list[TheoremPlanStep]
    helper_gaps: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AppendTacticTextAction:
    """Append bounded tactic text to the current theorem draft."""

    action_id: str
    kind: TheoremSessionActionKind
    tactic_text: str
    rationale: str | None = None


@dataclass(frozen=True)
class ReplaceTheoremDraftAction:
    """Replace the current theorem draft with one bounded candidate."""

    action_id: str
    kind: TheoremSessionActionKind
    theorem_name: str
    body: str
    rationale: str | None = None


@dataclass(frozen=True)
class ProposedHelperLemma:
    """One helper lemma proposal for the current theorem session."""

    helper_name: str
    helper_statement: str
    proof_stub: str
    helper_imports: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProposeHelperLemmaAction:
    """Propose one helper lemma to unblock the current proof state."""

    action_id: str
    kind: TheoremSessionActionKind
    helper: ProposedHelperLemma
    rationale: str | None = None


@dataclass(frozen=True)
class SuggestCheckpointLabelAction:
    """Suggest one checkpoint label for the current theorem session node."""

    action_id: str
    kind: TheoremSessionActionKind
    label: str
    rationale: str | None = None


@dataclass(frozen=True)
class RecommendStrategyShiftAction:
    """Recommend a bounded strategy change for the current proof attempt."""

    action_id: str
    kind: TheoremSessionActionKind
    strategy: str
    rationale: str


type TheoremSessionAction = (
    AppendTacticTextAction
    | ReplaceTheoremDraftAction
    | ProposeHelperLemmaAction
    | SuggestCheckpointLabelAction
    | RecommendStrategyShiftAction
)


@dataclass(frozen=True)
class VerifiedHelper:
    """A helper lemma that passed Lean verification and was persisted."""

    helper_name: str
    module_name: str
    file_path: str
    theorem: str
    statement: str
    source_step_id: str
    decomposition_depth: int = 1


@dataclass(frozen=True)
class HelperState:
    """One managed helper obligation tracked across proving states."""

    helper_id: str
    source_theorem: str
    statement: str
    module_name: str
    file_path: str
    dependencies: list[str]
    status: HelperStatus
    consumer_theorem_states: list[str]
    decomposition_depth: int
    helper_name: str | None = None
    source_node_id: str | None = None
    verification_artifact_dir: str | None = None
    proof_stub: str | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize nested diagnostics with normalized output."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["diagnostics"] = [diagnostic.to_json() for diagnostic in self.diagnostics]
        return data


@dataclass(frozen=True)
class AgentResponse:
    """Structured result plus raw backend I/O for one strict call."""

    backend: str
    result_kind: BackendResultKind
    invocation: BackendInvocation
    prompt_path: str
    prompt_text: str
    schema_path: str
    raw_stdout: str
    raw_stderr: str
    raw_output: str
    parsed_payload: dict[str, JsonValue] | None
    candidates: list[PatchCandidate] = field(default_factory=list)
    candidate_parse_errors: list[str] = field(default_factory=list)
    decomposition: list[DecompositionStep] = field(default_factory=list)
    theorem_plan: TheoremPlan | None = None
    theorem_session_actions: list[TheoremSessionAction] = field(default_factory=list)
    message: str | None = None
    theorem_body_classification: TheoremBodyClassification | None = None

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize nested candidates for durable artifacts."""
        return cast(dict[str, JsonValue], dataclass_to_jsonable(self))


@dataclass(frozen=True)
class TheoremBodyClassification:
    """Auditable classification of one theorem-body backend response."""

    response_class: TheoremBodyResponseClass
    usable_candidate_count: int
    rejected_candidate_count: int
    invalid_candidate_count: int
    selected_candidate_id: str | None = None
    message: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PatchAttempt:
    """One sandboxed patch attempt and its verification result."""

    attempt_index: int
    candidate: PatchCandidate
    workspace_root: str
    workspace_package: str
    target_module_path: str
    sandbox_root: str
    sandbox_workspace_root: str
    helper_module_root: str
    sandbox_file: str
    verification_file: str
    candidate_body_path: str
    diff_path: str
    backend_request_path: str
    backend_response_path: str
    backend_stdout_path: str
    backend_stderr_path: str
    backend_command_path: str
    verified: bool
    proof_target: dict[str, JsonValue] | None
    syntax_probe_results: dict[str, JsonValue]
    semantic_probe_results: dict[str, JsonValue]
    backend_response: AgentResponse
    verification: CheckResult
    repair_strategy: RepairStrategy | None = None
    selected_failure: RepairFailure | None = None

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize nested verification results with normalized diagnostics."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["backend_response"] = self.backend_response.to_json()
        data["verification"] = self.verification.to_json()
        return data


@dataclass(frozen=True)
class LoopResult:
    """Final machine result for `kan-prover repair` and `kan-prover solve`."""

    run_id: str
    command: str
    status: LoopStatus
    exit_code: ExitCode
    target_file: str
    target_module_path: str
    theorem: str
    tool_root: str
    workspace: WorkspaceMetadata
    sandbox: SandboxMetadata
    helper_module_root: str
    artifact_dir: str
    work_dir: str
    backend: str
    wall_time_seconds: float
    backend_call_count: int
    attempt_count: int
    verified_attempt: int | None
    applied: bool
    max_decomposition_depth: int
    retrieval_hits: list[RetrievalHit]
    proved_helpers: list[VerifiedHelper]
    decomposition_count: int
    baseline: CheckResult
    attempts: list[PatchAttempt]
    started_at: datetime
    finished_at: datetime

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize nested attempt and verifier payloads."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["baseline"] = self.baseline.to_json()
        data["attempts"] = [attempt.to_json() for attempt in self.attempts]
        return data


@dataclass(frozen=True)
class DecomposeResult:
    """Final machine result for `kan-prover decompose`."""

    run_id: str
    command: str
    status: LoopStatus
    exit_code: ExitCode
    target_file: str
    target_module_path: str
    theorem: str
    tool_root: str
    workspace: WorkspaceMetadata
    sandbox: SandboxMetadata
    helper_module_root: str
    artifact_dir: str
    backend: str
    wall_time_seconds: float
    backend_call_count: int
    max_decomposition_depth: int
    retrieval_hits: list[RetrievalHit]
    steps: list[DecompositionStep]
    started_at: datetime
    finished_at: datetime

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the decompose result payload."""
        return cast(dict[str, JsonValue], dataclass_to_jsonable(self))


@dataclass(frozen=True)
class InspectionSnapshot:
    """One theorem-local inspection snapshot used by inspect and plan."""

    theorem_statement: str | None
    editable_span: FileSpan | None
    imports: list[str]
    namespace: str | None
    proof_target: dict[str, JsonValue] | None
    proof_state: dict[str, JsonValue] | None
    local_context: list[str]
    goal_snapshot: list[str]
    insertion_point: InsertionPointSummary | None
    diagnostic_count: int
    diagnostics: list[Diagnostic]
    retrieval_hits: list[RetrievalHit]
    active_helpers: list[HelperRef]
    retrieval_explanation: RetrievalExplanation | None = None
    syntax_probe_results: dict[str, JsonValue] = field(default_factory=dict)
    semantic_probe_results: dict[str, JsonValue] = field(default_factory=dict)
    probe_results: dict[str, JsonValue] = field(default_factory=dict)

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize nested diagnostics with normalized output."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["diagnostics"] = [diagnostic.to_json() for diagnostic in self.diagnostics]
        return data


@dataclass(frozen=True)
class RetrieveSnapshot:
    """One retrieval-focused theorem snapshot."""

    theorem_statement: str | None
    imports: list[str]
    namespace: str | None
    goal_snapshot: list[str]
    diagnostic_count: int
    diagnostics: list[Diagnostic]
    retrieval: RetrievalExplanation

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize nested diagnostics with normalized output."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["diagnostics"] = [diagnostic.to_json() for diagnostic in self.diagnostics]
        return data


@dataclass(frozen=True)
class InspectResult:
    """Final machine result for `kan-prover inspect`."""

    run_id: str
    command: str
    status: InspectionStatus
    exit_code: ExitCode
    target_file: str
    target_module_path: str
    theorem: str
    tool_root: str
    workspace: WorkspaceMetadata
    sandbox: SandboxMetadata | None
    helper_module_root: str | None
    read_only: bool
    file_state_check: FileStateCheck
    artifact_dir: str
    wall_time_seconds: float
    snapshot: InspectionSnapshot
    started_at: datetime
    finished_at: datetime

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the inspect payload."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["snapshot"] = self.snapshot.to_json()
        return data


@dataclass(frozen=True)
class RetrieveResult:
    """Final machine result for `kan-prover retrieve`."""

    run_id: str
    command: str
    status: InspectionStatus
    exit_code: ExitCode
    target_file: str
    target_module_path: str
    theorem: str
    tool_root: str
    workspace: WorkspaceMetadata
    sandbox: SandboxMetadata | None
    helper_module_root: str | None
    read_only: bool
    file_state_check: FileStateCheck
    artifact_dir: str
    wall_time_seconds: float
    snapshot: RetrieveSnapshot
    started_at: datetime
    finished_at: datetime

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the retrieve payload."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["snapshot"] = self.snapshot.to_json()
        return data


@dataclass(frozen=True)
class PlanResult:
    """Final machine result for `kan-prover plan`."""

    run_id: str
    command: str
    status: PlanStatus
    exit_code: ExitCode
    target_file: str
    target_module_path: str
    theorem: str
    tool_root: str
    workspace: WorkspaceMetadata
    sandbox: SandboxMetadata
    helper_module_root: str
    artifact_dir: str
    backend: str
    wall_time_seconds: float
    backend_call_count: int
    inspection: InspectionSnapshot
    plan: TheoremPlan | None
    backend_response: AgentResponse
    started_at: datetime
    finished_at: datetime

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize nested plan and backend payloads."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["inspection"] = self.inspection.to_json()
        data["backend_response"] = self.backend_response.to_json()
        return data


@dataclass(frozen=True)
class ProofGraphNode:
    """One persisted node in a graph-backed proof search run."""

    graph_index: int
    node_id: str
    node_kind: str
    parent_node_id: str | None
    strategy_family: StrategyFamily | None
    task_kind: TaskKind | None
    session_id: str | None
    session_artifact_dir: str | None
    session_node_id: str | None
    action_payload: dict[str, JsonValue]
    theorem_draft: str
    diagnostics: list[Diagnostic]
    goals: list[str]
    probe_snapshot: dict[str, JsonValue]
    score: int
    status: str
    dedup_signature: str
    decomposition_depth: int
    active_helper_ids: list[str] = field(default_factory=list)
    helper_id: str | None = None
    backend_request_path: str | None = None
    backend_response_path: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize nested diagnostics with normalized output."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["diagnostics"] = [diagnostic.to_json() for diagnostic in self.diagnostics]
        return data


@dataclass(frozen=True)
class ProofGraphSummary:
    """Small summary of one prove run's stored graph."""

    root_node_id: str
    verified_node_id: str | None
    frontier_node_ids: list[str]
    node_count: int
    edge_count: int
    branch_node_ids: list[str]


@dataclass(frozen=True)
class ProveResult:
    """Final machine result for `kan-prover prove`."""

    run_id: str
    command: str
    status: ProveStatus
    exit_code: ExitCode
    target_file: str
    target_module_path: str
    theorem: str
    tool_root: str
    workspace: WorkspaceMetadata
    artifact_dir: str
    backend: str
    wall_time_seconds: float
    backend_call_count: int
    max_decomposition_depth: int
    plan: TheoremPlan | None
    graph: ProofGraphSummary
    helpers: list[HelperState]
    nodes: list[ProofGraphNode]
    started_at: datetime
    finished_at: datetime

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize nested plan and proof-graph payloads."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["helpers"] = [helper.to_json() for helper in self.helpers]
        data["nodes"] = [node.to_json() for node in self.nodes]
        return data


@dataclass(frozen=True)
class ResumeResult:
    """Final machine result for `kan-prover resume`."""

    run_id: str
    command: str
    status: ResumeStatus
    exit_code: ExitCode
    source_run_id: str
    source_node_id: str | None
    target_file: str
    target_module_path: str
    theorem: str
    tool_root: str
    workspace: WorkspaceMetadata
    artifact_dir: str
    backend: str
    wall_time_seconds: float
    backend_call_count: int
    max_decomposition_depth: int
    plan: TheoremPlan | None
    graph: ProofGraphSummary
    helpers: list[HelperState]
    nodes: list[ProofGraphNode]
    started_at: datetime
    finished_at: datetime
    error_message: str | None = None

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize nested plan and proof-graph payloads."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["helpers"] = [helper.to_json() for helper in self.helpers]
        data["nodes"] = [node.to_json() for node in self.nodes]
        return data


class ReplayMode(StrEnum):
    """Stable replay mode names surfaced by the CLI."""

    RECHECK_ONLY = "recheck-only"
    REUSE_AGENT_OUTPUT = "reuse-agent-output"
    REQUERY_AGENT = "requery-agent"


@dataclass(frozen=True)
class ArtifactProbeSummary:
    """Compact status line for one stored semantic probe payload."""

    probe: str
    status: str
    summary: str | None = None


@dataclass(frozen=True)
class ArtifactRetrievalHitSummary:
    """Compact retrieval hit summary surfaced by artifact readers."""

    title: str
    source_kind: str
    score: int
    theorem: str | None = None
    path: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class RunSelectionSummary:
    """Focused stored attempt or graph-node view for `kan-prover show run`."""

    selection_kind: str
    index: int
    label: str
    node_id: str | None
    action_kind: str | None
    candidate_id: str | None
    status: str | None
    verified: bool
    diagnostic_count: int
    goal_count: int | None = None
    response_class: str | None = None
    message: str | None = None
    session_id: str | None = None
    session_node_id: str | None = None
    pretty_proof_state: str | None = None
    semantic_session: dict[str, JsonValue] | None = None
    semantic_probes: list[ArtifactProbeSummary] = field(default_factory=list)


@dataclass(frozen=True)
class RunGraphSummary:
    """Stored proof-graph summary exposed by `kan-prover show run`."""

    root_node_id: str
    verified_node_id: str | None
    frontier_node_ids: list[str]
    branch_node_ids: list[str]
    node_count: int
    edge_count: int


@dataclass(frozen=True)
class ShowRunResult:
    """Artifact-only inspection result for one stored run bundle."""

    run_id: str
    command: str
    status: InspectionStatus
    exit_code: ExitCode
    source_run_id: str
    stored_command: str
    stored_status: str
    target_file: str
    target_module_path: str | None
    theorem: str | None
    workspace_root: str | None
    workspace_package: str | None
    backend: str | None
    artifact_dir: str
    source_artifact_dir: str
    retrieval_hits: list[ArtifactRetrievalHitSummary]
    proof_target: dict[str, JsonValue] | None
    semantic_probes: list[ArtifactProbeSummary]
    selected: RunSelectionSummary | None
    graph: RunGraphSummary | None
    started_at: datetime
    finished_at: datetime

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the stored-run inspection payload."""
        return cast(dict[str, JsonValue], dataclass_to_jsonable(self))


@dataclass(frozen=True)
class SessionNodeSummary:
    """Compact node history row for one stored theorem session."""

    index: int
    node_id: str
    action: str
    parent_node_id: str | None
    status: str | None
    diagnostic_count: int
    goal_count: int


@dataclass(frozen=True)
class SessionNodeDetail:
    """Focused stored node view for `kan-prover show session`."""

    index: int
    node_id: str
    action: str
    parent_node_id: str | None
    status: str | None
    theorem_draft: str
    diagnostic_count: int
    goal_count: int
    proof_target: dict[str, JsonValue] | None
    pretty_proof_state: str | None
    semantic_session: dict[str, JsonValue] | None = None
    semantic_probes: list[ArtifactProbeSummary] = field(default_factory=list)


@dataclass(frozen=True)
class ShowSessionResult:
    """Artifact-only inspection result for one stored theorem session."""

    run_id: str
    command: str
    status: InspectionStatus
    exit_code: ExitCode
    source_session_id: str
    session_status: str
    target_file: str
    target_module_path: str | None
    theorem: str
    artifact_dir: str
    session_artifact_dir: str
    current_node_id: str | None
    checkpoints: list[str]
    nodes: list[SessionNodeSummary]
    selected: SessionNodeDetail
    started_at: datetime
    finished_at: datetime

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the stored-session inspection payload."""
        return cast(dict[str, JsonValue], dataclass_to_jsonable(self))


@dataclass(frozen=True)
class TraceAttemptSummary:
    """Compact attempt summary exposed by `kan-prover trace`."""

    attempt_index: int
    verified: bool
    diagnostic_count: int
    candidate_id: str | None = None
    backend_request_path: str | None = None
    backend_response_path: str | None = None
    candidate_body_path: str | None = None
    diff_path: str | None = None


@dataclass(frozen=True)
class TraceResult:
    """Artifact-only summary for one stored run bundle."""

    run_id: str
    command: str
    status: InspectionStatus
    exit_code: ExitCode
    source_run_id: str
    source_command: str
    source_status: str
    target_file: str
    theorem: str | None
    backend: str | None
    artifact_dir: str
    attempt_count: int
    attempts: list[TraceAttemptSummary]
    artifact_paths: list[str]
    started_at: datetime
    finished_at: datetime

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the trace payload."""
        return cast(dict[str, JsonValue], dataclass_to_jsonable(self))


@dataclass(frozen=True)
class ReplayResult:
    """Replay result that points at a concrete replayed run bundle."""

    run_id: str
    command: str
    status: InspectionStatus
    exit_code: ExitCode
    mode: ReplayMode
    source_run_id: str
    source_command: str
    source_attempt_index: int | None
    target_file: str
    target_module_path: str | None
    workspace_root: str | None
    workspace_package: str | None
    sandbox_strategy: str | None
    theorem: str | None
    backend: str | None
    artifact_dir: str
    replayed_run_id: str
    replayed_command: str
    replayed_status: str
    replayed_artifact_dir: str
    machine_label: str
    started_at: datetime
    finished_at: datetime
    child_result: dict[str, JsonValue]

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the replay payload."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["child_result"] = self.child_result
        return data


@dataclass(frozen=True)
class ExportResult:
    """Self-contained export bundle for one theorem context."""

    run_id: str
    command: str
    status: InspectionStatus
    exit_code: ExitCode
    source_run_id: str
    source_command: str
    source_attempt_index: int | None
    target_file: str
    target_module_path: str | None
    workspace_root: str | None
    workspace_package: str | None
    theorem: str | None
    artifact_dir: str
    bundle_path: str
    format: str
    started_at: datetime
    finished_at: datetime
    bundle: dict[str, JsonValue]

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the export payload."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["bundle"] = self.bundle
        return data


@dataclass(frozen=True)
class ApplyResult:
    """Final machine result for `kan-prover apply`."""

    run_id: str
    command: str
    status: ApplyStatus
    exit_code: ExitCode
    source_run_id: str
    source_node_id: str | None
    target_file: str
    target_module_path: str | None
    theorem: str | None
    workspace_root: str | None
    workspace_package: str | None
    artifact_dir: str
    applied_helper_modules: list[str]
    started_at: datetime
    finished_at: datetime
    error_code: ApplyErrorCode | None = None
    error_message: str | None = None

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the apply payload."""
        return cast(dict[str, JsonValue], dataclass_to_jsonable(self))


@dataclass(frozen=True)
class BenchTargetResult:
    """One benchmark row pointing to the concrete child run bundle."""

    target_file: str
    theorem: str
    command: str
    backend: str
    status: str
    exit_code: str
    run_id: str
    artifact_dir: str
    target_module_path: str
    workspace_root: str
    workspace_package: str
    wall_time_seconds: float
    backend_call_count: int | None = None
    time_to_first_verified_seconds: float | None = None
    workflow: str | None = None
    condition_id: str | None = None
    condition_label: str | None = None
    difficulty: str | None = None
    theorem_id: str | None = None
    token_budget: int | None = None
    attempt_count: int | None = None
    verified_attempt: int | None = None
    retrieval_hits: int | None = None
    plan_step_count: int | None = None
    node_count: int | None = None
    edge_count: int | None = None
    branch_count: int | None = None
    frontier_count: int | None = None
    verified_node_id: str | None = None
    helper_count: int | None = None
    source_run_id: str | None = None
    source_node_id: str | None = None
    target_id: str | None = None
    benchmark_family: str | None = None
    benchmark_suite: str | None = None
    benchmark_split: str | None = None
    benchmark_subset: str | None = None


@dataclass(frozen=True)
class BenchBenchmarkSummary:
    """Aggregate metrics for one named benchmark suite."""

    family: str
    suite: str
    split: str | None
    subset: str | None
    theorem_count: int
    solved_count: int
    failed_count: int
    solve_rate: float
    source_repo: str | None = None
    source_commit: str | None = None
    pass_k: int | None = None
    pass_count: int | None = None
    pass_rate: float | None = None


@dataclass(frozen=True)
class BenchConditionSummary:
    """Aggregate metrics for one paired benchmark condition."""

    condition_id: str
    condition_label: str
    workflow: EvaluationWorkflow
    backend: str
    theorem_count: int
    solved_count: int
    failed_count: int
    solve_rate: float
    average_time_to_first_verified_seconds: float | None = None
    average_backend_calls_per_solved_theorem: float | None = None
    token_budget: int | None = None


@dataclass(frozen=True)
class BenchPairSummary:
    """One theorem-local plain-vs-tool comparison inside the paired harness."""

    backend: str
    theorem_id: str
    theorem: str
    difficulty: str
    plain_condition_id: str
    tool_condition_id: str
    plain_solved: bool
    tool_solved: bool
    hard_win: bool
    easy_guardrail_passed: bool


@dataclass(frozen=True)
class BenchResult:
    """Aggregate result for one unattended benchmark batch."""

    run_id: str
    command: str
    status: BatchStatus
    exit_code: ExitCode
    spec_path: str
    artifact_dir: str
    output_path: str
    shared_workspace_root: str | None
    target_count: int
    success_count: int
    failure_count: int
    total_wall_time_seconds: float
    rows: list[BenchTargetResult]
    started_at: datetime
    finished_at: datetime
    benchmark_summary: BenchBenchmarkSummary | None = None
    condition_summaries: list[BenchConditionSummary] = field(default_factory=list)
    pair_summaries: list[BenchPairSummary] = field(default_factory=list)

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the batch payload."""
        return cast(dict[str, JsonValue], dataclass_to_jsonable(self))


@dataclass(frozen=True)
class SessionActionResult:
    """Final machine result for `kan-prover session ...` commands."""

    run_id: str
    command: str
    action: SessionActionKind
    status: SessionStatus
    exit_code: ExitCode
    session_id: str
    target_file: str
    target_module_path: str
    theorem: str
    artifact_dir: str
    session_artifact_dir: str
    current_node_id: str | None
    diagnostic_count: int
    started_at: datetime
    finished_at: datetime
    session: dict[str, JsonValue]

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the theorem session payload."""
        data = cast(dict[str, JsonValue], dataclass_to_jsonable(self))
        data["session"] = self.session
        return data


def dataclass_to_jsonable(value: object) -> JsonValue:
    """Convert nested dataclasses and paths into JSON-safe values."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {str(key): dataclass_to_jsonable(item) for key, item in vars(value).items()}
    if isinstance(value, dict):
        return {str(key): dataclass_to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [dataclass_to_jsonable(item) for item in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)
