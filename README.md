# lean-proofctl

`lean-proofctl` provides the `proofctl` command, a verifier-first proof assistant for arbitrary Lake workspaces.

- Lean and Lake are the only proof authority.
- Python owns orchestration, artifacts, replay, and backend calls.
- The target is a real workspace selected with `--workspace` or inferred from the target file.
- Verification happens in a sandbox rooted at that workspace.
- `apply` is explicit. The tool does not write back to the real workspace until a branch has already verified in the
    sandbox.

## Setup

From this repository root:

```sh
uv python pin 3.14
uv sync --group dev
```

Use `--demo` only for the bundled demo workspace under `lean-runtime/`:

```sh
uv run proofctl doctor --demo
uv run proofctl init --demo --skip-build
```

For a real Lake workspace, bind to that workspace directly:

```sh
uv run proofctl doctor --workspace /path/to/workspace
uv run proofctl init --workspace /path/to/workspace
```

You do not need to copy a target workspace under this repository.

## Workflow Map

The command family is grouped by job:

- workspace bootstrap: `init`, `doctor`
- theorem context and planning: `check`, `inspect`, `retrieve`, `plan`, `decompose`
- bounded repair: `repair`, `solve`
- theorem sessions: `session start`, `session step`, `session goals`, `session checkpoint`, `session finish`
- graph search: `prove`, `resume`, `apply`
- artifacts and audit: `trace`, `replay`, `show`, `export`
- unattended evaluation: `bench`

The usual operator path is:

1. Run `doctor` if workspace health is not already known.
1. Run `inspect` first.
1. Run `retrieve` when premise choice is part of the question.
1. Choose the narrowest proving workflow that fits the theorem.
1. Read `show`, `trace`, `replay`, or `export` instead of guessing what happened.
1. Use `apply` only after a verified branch exists.

## Which Command When

- `check`: run authoritative Lean verification for one file without proof search.
- `inspect`: read theorem context, proof state, retrieval hits, and helper inventory for one theorem.
- `retrieve`: read ranked retrieval hits, score reasons, and near misses for one theorem.
- `plan`: ask for one structured theorem plan before a larger edit or search run.
- `decompose`: ask for a bounded helper plan when the theorem likely needs helper lemmas first.
- `repair`: verify one candidate whole theorem body in a sandbox.
- `solve`: retry bounded theorem-body repair when direct repair is still the right shape but one candidate is not
    enough.
- `session ...`: iterate on one theorem body inside one sandbox when you want updated proof state between full-body
    replacements.
- `prove`: run graph-backed search when direct repair is too weak or when you need branching and helper work.
- `resume`: continue a stored prove run instead of starting over.
- `apply`: transplant one verified theorem branch back to the real workspace.
- `trace`: summarize one stored run from artifacts only.
- `replay`: rerun one stored run with an explicit replay mode.
- `show run|session`: inspect stored run and session artifacts without opening raw JSON.
- `export`: write a self-contained theorem-context bundle from a stored run.
- `bench`: run a local benchmark manifest and keep rows linked to concrete run ids and artifact dirs.

## Common Workflows

### Inspect First

`inspect` is semantic-first by default. Use `--read-only` for the cheap non-mutating path. Add `--verify-file-state`
only when you want one extra full-file `lake lean` check before the command returns.

```sh
uv run proofctl --json inspect \
  --workspace /Users/jcreinhold/Code/kan \
  /Users/jcreinhold/Code/kan/KanProofs/Algebra/ControlFlowGrades.lean \
  --theorem CFGrade.le_refl \
  --read-only
```

Read the result before editing:

- theorem statement
- editable span
- diagnostics from the semantic read path
- retrieval hits
- active helpers
- `semantic_probe_results`

To ask for the optional extra full-file check:

```sh
uv run proofctl --json inspect \
  --workspace /Users/jcreinhold/Code/kan \
  /Users/jcreinhold/Code/kan/KanProofs/Algebra/ControlFlowGrades.lean \
  --theorem CFGrade.le_refl \
  --verify-file-state
```

### Retrieve Theorem Context

`retrieve` is also semantic-first by default. Use `--read-only` for the normal cheap retrieval view. Add
`--verify-file-state` only when you want the extra full-file sandbox check.

```sh
uv run proofctl --json retrieve \
  --workspace /Users/jcreinhold/Code/kan \
  /Users/jcreinhold/Code/kan/KanProofs/Algebra/ControlFlowGrades.lean \
  --theorem CFGrade.le_refl \
  --read-only
```

Use `--enable-embedding-retrieval` only when semantic similarity is worth the extra dependency and latency.

### Verify One Candidate Body

Use `repair` when you already have a plausible full theorem body:

```sh
uv run proofctl --json repair \
  --workspace /Users/jcreinhold/Code/kan \
  /Users/jcreinhold/Code/kan/KanProofs/Algebra/ControlFlowGrades.lean \
  --theorem CFGrade.le_refl \
  --agent none \
  --candidate-file /Users/jcreinhold/Code/lean-proofctl/tests/acceptance/repo_root_cfgrade_le_refl.body.lean
```

`--agent none` means the outer agent supplies the proof text and `proofctl` only verifies it.

### Iterate In One Sandbox

Use theorem sessions when you want to replace the full body step by step and inspect updated goals in the same sandbox:

```sh
uv run proofctl session start \
  --workspace /Users/jcreinhold/Code/kan \
  /Users/jcreinhold/Code/kan/KanProofs/Algebra/ControlFlowGrades.lean \
  --theorem CFGrade.le_refl
```

Then use:

- `uv run proofctl session goals SESSION_ID`
- `uv run proofctl session step SESSION_ID --body-file /tmp/CFGrade.le_refl.body.lean`
- `uv run proofctl session checkpoint SESSION_ID --label after-case-split`
- `uv run proofctl session finish SESSION_ID`

`session start` does not eagerly start a live Lean worker. It creates the session and records the first theorem context
on the cheap one-shot path. Repeated same-process follow-up work may promote later semantic requests to the live
file-session runtime and reuse the live file state automatically.

`session step` replaces the whole theorem body. It does not take a tactic fragment unless that fragment is the whole
body.

### Switch To Graph Search

Use `prove` when the theorem needs branching, helper work, or repeated session search:

```sh
uv run proofctl --json prove \
  --workspace /Users/jcreinhold/Code/kan \
  /Users/jcreinhold/Code/kan/KanProofs/Algebra/ControlFlowGrades.lean \
  --theorem CFGrade.le_refl \
  --agent codex \
  --max-nodes 12
```

`prove` has an explicit semantic runner policy. It does not imply live mode for every request. The Python engine chooses
between:

- a one-shot compiled `SemanticProbe` executable for cold or single requests
- a live semantic runner for repeated same-process follow-up work

The Lean runtime owns reusable file-session state. Python owns the policy that decides when to stay one-shot and when to
promote a repeated file lane to live mode.

After a prove run:

- use `resume RUN_ID` to keep going from the stored frontier
- use `show run RUN_ID` for the quick stored summary
- use `trace RUN_ID` when you need the fuller audit view
- use `export RUN_ID` to save a portable theorem bundle
- use `apply RUN_ID --node-id VERIFIED_NODE_ID` only after a verified node exists

## Semantic Probe Boundary

`inspect` and `retrieve` use Lean-backed semantic probes by default. The canonical theorem-context contract is
`context-bundle`, which packages the two main semantic views together:

- `declaration-target`
- `proof-state`

Other Lean-backed probe payloads still appear in stored artifacts and remain useful operator surfaces:

- `declaration-inventory`
- `local-context`
- `goal-snapshot`
- `insertion-point`

Retrieval also uses a compiled workspace declaration index over the workspace frontier. That index replaces the older
per-file semantic inventory fan-out as the main retrieval backbone.

The bundled Lean runtime is now split across modules:

- `lean-runtime/Tools/SemanticProbe/Main.lean`
- `lean-runtime/Tools/SemanticProbe/LoadedFile.lean`
- `lean-runtime/Tools/SemanticProbe/LiveFileSession.lean`
- `lean-runtime/Tools/SemanticProbe/LiveProtocol.lean`

`Main.lean` still exposes one-shot commands such as `context-bundle`, `declaration-target`, `declaration-inventory`,
`proof-state`, `local-context`, `goal-snapshot`, and `insertion-point`. It also exposes `serve`, which runs the live
file-session protocol.

## Artifact Contract

Runs persist probe data in three related fields:

- `semantic_probe_results`: authoritative Lean-backed probe payloads for normal workspace-backed runs
- `syntax_probe_results`: source-only probe payloads
- `probe_results`: the combined probe map stored with the run

Healthy workspace-backed runs should put theorem-targeting and proof-state data under `semantic_probe_results`. In
particular, expect to see:

- `context-bundle`
- `declaration-target`
- `proof-state`
- often `declaration-inventory` in retrieval-heavy flows

`syntax_probe_results` is not the normal happy path for workspace-backed theorem runs. Treat it as source-only output or
fallback output, not as equivalent authority.

## Tests

The test suite is `pytest`-native and split by marker:

- default `uv run pytest -q` runs the fast unit suite only
- integration `uv run pytest -q -m integration` runs workspace-backed Lean or Lake tests
- acceptance `uv run pytest -q -m acceptance` runs the downstream product path over real workspaces

The default `pytest` config excludes `integration`, `slow`, and `acceptance` tests so unit feedback stays fast.

## Verification

From this repository root:

```sh
uv run proofctl --help
uv run proofctl inspect --help
uv run proofctl retrieve --help
uv run proofctl session --help
uv run proofctl show --help
uv run proofctl show run --help
uv run proofctl show session --help
```

Repo checks for this tool:

```sh
make python-fmt
make python-lint
make python-typecheck
make accept
```

## Downstream Acceptance

The trusted downstream acceptance gate is:

```sh
make accept
```

It covers the real product workflow, including theorem inspection, retrieval, repair, sessions, graph search, replay,
apply, show, and benchmark plumbing.

## Standard Benchmarks

Checked-in standard benchmark subsets live under `benchmarks/`:

- `benchmarks/manifests/minif2f-test-subset.yaml`
- `benchmarks/manifests/putnambench-subset.yaml`

Run the checked-in standard manifests with:

```sh
make bench-standard
```
