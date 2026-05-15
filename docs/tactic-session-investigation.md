# Tactic-Level Session Investigation For `kan-prover`

## Scope

Issue [#271](https://github.com/jcreinhold/kan/issues/271) asks whether `kan-prover` should add tactic-level session
actions. This write-up stays an investigation note, not a feature spec.

The question is now:

- Does tactic-level stepping buy enough to justify more product surface?
- Does the current live semantic substrate support honest tactic-state stepping?
- Or do we still need a deeper tactic-specific state model?

This document does not propose a production tactic engine. It records the small prototype, the bounded benchmark
comparison, the main costs, and what changed in the runtime since the first version of this question.

## Current Session Surface

`kan-prover` sessions are still whole-body oriented.

- [`session.py`](/Users/jcreinhold/Code/kan/tools/kan-prover/src/kan_prover/engine/session.py) replaces a theorem body,
    checks the file, and records the resulting theorem state.
- [`prove.py`](/Users/jcreinhold/Code/kan/tools/kan-prover/src/kan_prover/engine/prove.py) builds graph search on top of
    those theorem sessions.
- `session step` is still full-body replacement, not tactic stepping.

The cheap path is still the same unattractive path as before: rewrite the file, rerun Lean, and read back the resulting
state. That path still pays the cost of file checking and elaboration on each step.

## Runtime Surface Today

The repository no longer has the old single-file runtime story.

The current Lean runtime is split across:

- [`Main.lean`](/Users/jcreinhold/Code/kan/tools/kan-prover/lean-runtime/Tools/SemanticProbe/Main.lean)
- [`LoadedFile.lean`](/Users/jcreinhold/Code/kan/tools/kan-prover/lean-runtime/Tools/SemanticProbe/LoadedFile.lean)
- [`LiveFileSession.lean`](/Users/jcreinhold/Code/kan/tools/kan-prover/lean-runtime/Tools/SemanticProbe/LiveFileSession.lean)
- [`LiveProtocol.lean`](/Users/jcreinhold/Code/kan/tools/kan-prover/lean-runtime/Tools/SemanticProbe/LiveProtocol.lean)

The current Python runtime split is explicit too:

- [`semantic_runner.py`](/Users/jcreinhold/Code/kan/tools/kan-prover/src/kan_prover/engine/semantic_runner.py) defines
    the one-shot semantic runner contract
- [`semantic_live.py`](/Users/jcreinhold/Code/kan/tools/kan-prover/src/kan_prover/engine/semantic_live.py) defines the
    live same-process semantic runner
- [`semantic_probe_runtime.py`](/Users/jcreinhold/Code/kan/tools/kan-prover/src/kan_prover/engine/semantic_probe_runtime.py)
    boots the compiled `SemanticProbe` executable and the live `serve` process

That means two old claims are no longer true:

- there is now a persistent Lean-side live file-session runtime
- there is now explicit Python policy for one-shot versus live semantic calls

What has not changed is the session contract. The live runtime reuses file state for repeated theorem-context requests.
It does not yet expose a durable tactic-state machine with tactic actions, tactic diagnostics, and replayable
tactic-state transitions.

## Prototype Path

I prototyped the smaller semantic-probe path first.

### Lean helper extension

The runtime still has a narrow helper for post-last-tactic inspection:

- `proof-state-after-last-tactic`

That helper asks Lean for the goals after the last tactic in a rewritten theorem body. It is useful as evidence about
feasibility. It is not the same thing as persistent tactic stepping.

### Python investigation harness

[`tactic_session_investigation.py`](/Users/jcreinhold/Code/kan/tools/kan-prover/src/kan_prover/investigation/tactic_session_investigation.py)
provides a bounded comparison harness over the standard benchmark manifests.

The harness compares:

- whole-body mode: replace a theorem body and rerun Lean
- tactic mode: replace a theorem body with one more tactic and rerun Lean

This stays verifier-first. Lean remains the only proof authority.

## Benchmark Method

The benchmark surface is the same standard ten-row slice:

- `miniF2F-test-subset.yaml`: 6 targets
- `putnambench-subset.yaml`: 4 targets

The completed comparison run used this tiny-budget configuration:

- whole-body candidate bank:
    - `by norm_num`
    - `by omega`
    - `by native_decide`
- tactic bank:
    - `norm_num`
    - `omega`
- tactic depth: `1`
- verifier timeout per attempt: `5.0s`
- evaluation shape:
    - run whole-body on all 10 targets
    - run tactic search only on targets that whole-body does not verify
- solved means Lean exits `0`
- frontier means Lean reports only unsolved goals

The JSON artifact is checked in at
[`tactic-session-investigation.json`](/Users/jcreinhold/Code/kan/tools/kan-prover/docs/data/tactic-session-investigation.json).

## Results

### Tiny-budget benchmark run

The ten-target comparison finished with:

- whole-body solved: `0 / 10`
- tactic solved: `0 / 10`
- whole-body verifier calls: `30`
- tactic verifier calls: `20`
- whole-body wall time: `150.619s`
- tactic wall time: `100.393s`

That is still the main signal. Even with a tiny candidate bank, the dominant cost is repeated Lean checking, not
meaningful search.

Each whole-body attempt landed near the `5.0s` timeout. Each tactic attempt did the same. The cheap tactic path
therefore still spends most of its time paying for verifier startup and theorem elaboration.

### Manual whole-body spot checks

The tiny-budget harness is too small to show what whole-body can do when the candidate is actually good. To separate
candidate quality from verifier cost, I reran five manually discovered whole-body proofs on the same benchmark surface
with a `20s` timeout.

Those checks all verified:

| Target                    | Proof sketch                          | Wall time |
| ------------------------- | ------------------------------------- | --------: |
| `mathd_numbertheory_3`    | `native_decide`                       | `10.010s` |
| `mathd_algebra_33`        | `field_simp [h₀]; nlinarith [h₁, h₂]` | `10.338s` |
| `mathd_algebra_419`       | `nlinarith [h₀, h₁]`                  |  `9.979s` |
| `mathd_numbertheory_299`  | `norm_num`                            | `11.365s` |
| `mathd_numbertheory_1124` | `omega`                               | `10.691s` |

This still matters. The current recompilation path is not useless in principle. It can verify good whole-body
candidates. The problem is search breadth: each candidate still costs about ten seconds on this surface.

### Semantic runtime observations

The prototype still produced one positive and one negative result.

Positive:

- `proof-state-after-last-tactic` succeeded on `mathd_numbertheory_1124` with body `by omega`
- it returned `source = "tactic-goals-after-last"` and `goal_count = 0`
- wall time was about `12.85s`

Negative:

- the same probe path crashed on `mathd_algebra_33` with body `by field_simp [h₀]; nlinarith [h₁, h₂]`
- the concrete failure was:
    - `cannot evaluate [init] declaration 'Mathlib.Meta.NormNum.normNumExt' in the same module`

That failure still matters. It shows that the narrow post-last-tactic probe is not yet a stable base for general tactic
stepping.

## What Changed In The Architecture

The old question was too broad: "should we build any persistent backend at all?"

That is no longer the right question. The repo now has:

- a compiled one-shot `SemanticProbe` runtime
- split Lean runtime modules
- snapshot-backed live file sessions in Lean
- explicit Python one-shot versus live semantic runners

So the real question is narrower:

- does this live semantic substrate already support honest tactic-state stepping?
- or do we still need a deeper tactic-specific state model on top of it?

The evidence in this document still points to the second answer.

## Complexity Costs

Adding tactic-level session actions still has three cost buckets.

### If we keep the recompilation architecture

- every tactic action still triggers theorem elaboration and file checking
- search cost still grows roughly with Lean runs
- operator latency stays too high for interactive use
- the product surface would grow without delivering the main expected gain

This path is still unattractive.

### If we deepen the current semantic runtime

- we would need a stable way to step tactics and recover goal states
- we would need a stronger contract for tactic actions, diagnostics, and state serialization
- we would need replay semantics for tactic-state transitions, not just theorem-body transitions

This is now more plausible than before because the live file-session substrate exists. It is still real work.

### If we adopt a deeper tactic-specific backend

- we would need explicit tactic-state handles and lifecycle rules
- we would need failure recovery semantics
- we would need integration with current `trace`, `replay`, `show`, `export`, and `bench` surfaces
- we would still need to measure whether tactic stepping improves solve rate enough to justify the added complexity

This remains the first path that could plausibly make production tactic actions worthwhile, but it is a larger project.

## Limitations

The investigation still has real limits:

- the benchmark run is intentionally tiny-budget
- the tactic comparison is not a fair proxy for a real tactic-state backend
- the whole-body side is also budget-limited enough to miss proofs that verify with better candidates and more time
- the probe prototype inspects goals after the last tactic in a rewritten file; it does not provide a full tactic-state
    machine
- PutnamBench still does not fit the direct probe workflow as cleanly as the miniF2F slice

Because of those limits, this document is not proof that tactic-level interaction can never help. It is evidence about
what the current cheap path can and cannot justify.

## Recommendation

Do not add production tactic-level session actions on the strength of the cheap recompilation path.

That is not a blanket no. It is a narrower recommendation:

- keep the current whole-body session model if the planned tactic path still rewrites files and reruns Lean
- if tactic sessions come back onto the roadmap, evaluate them against the current live semantic substrate, not against
    the old "no persistent backend" picture
- before adding product surface, require a second investigation that measures solve-rate gain against the current
    whole-body workflow under the same model, node budget, and wall-clock budget

In short:

- cheap recompilation path: still not justified
- current live semantic substrate: promising base layer, but not yet a tactic engine
- deeper tactic-state model: still likely required

## Verification Run

The following checks and experiments were run for this investigation:

- `uv run pytest tests/test_tactic_session_investigation.py`
- `uv run python -m kan_prover.investigation.tactic_session_investigation`
- direct manual whole-body verification checks on five miniF2F targets with a `20s` timeout
- direct `SemanticProbe` success case on `mathd_numbertheory_1124`
- direct `SemanticProbe` failure case on `mathd_algebra_33`

No broader `tools/kan-prover` test suite was needed for this issue because the work is investigation-only and scoped to
the helper command, harness, and this design note.
