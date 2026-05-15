# LLM-Based Proof Search and Repair for Lean: Literature Review

**Date:** 2026-03-28 **Scope:** Recent developments (2023-03 through 2026-03) in LLM-based proof search, proof repair,
and verifier-first proving for Lean, compared against `kan-prover`.

______________________________________________________________________

## 1. Executive Summary

The field of LLM-based theorem proving in Lean has moved from tactic-by-tactic generation with fine-tuned small models
(2023) to whole-proof generation with chain-of-thought reasoning, recursive subgoal decomposition, and reinforcement
learning from verifier feedback (2025-2026). The strongest systems now achieve 88-99% on miniF2F-test --- a benchmark
that was at ~30% two years ago. Three architectural patterns dominate: (1) whole-proof generation with RL-trained models
and optional self-correction loops (DeepSeek-Prover-V2, Goedel-Prover-V2, Kimina-Prover); (2) agentic orchestration that
decomposes theorems, delegates subgoals to specialized provers, and repairs failures using compiler feedback (APOLLO,
Hilbert, Prover Agent); and (3) retrieval-augmented tactic-level search with progress estimation (LeanDojo/ReProver,
LeanProgress, Lean Copilot).

The biggest implications for `kan-prover`:

1. **Subgoal decomposition with verifier feedback is the highest-leverage proving strategy.** `kan-prover` already has
    decomposition and helper-lemma workflows; the literature confirms this is the right shape but shows that recursive
    decomposition with per-subgoal repair loops substantially outperforms flat decomposition.
1. **Whole-proof generation now dominates tactic-by-tactic search for standard benchmarks.** `kan-prover`'s session-step
    model replaces whole theorem bodies, which aligns well. But the literature shows that chain-of-thought reasoning
    interleaved with formal steps (Lean-STaR, Kimina-Prover) and self-correction loops (Goedel-Prover-V2) add
    significant value on top of whole-proof generation.
1. **Lean-side infrastructure has consolidated around a few key interfaces** --- Pantograph for machine-to-machine
    tactic interaction, LeanDojo for data extraction and training, and LeanInteract/lean4-repl for lightweight
    Python-Lean bridges. `kan-prover`'s custom SemanticProbe surface is more tightly workspace-integrated than any of
    these, but it lacks tactic-level granularity.
1. **Retrieval-augmented proving matters, but the strongest retrieval is over semantic embeddings of proof states, not
    just declaration names.** `kan-prover`'s token-overlap retrieval is functional but behind the state of the art.
1. **Proof repair as a distinct task now has its own dataset (APRIL) and training methodology.** This is directly
    relevant to `kan-prover`'s repair loop.

______________________________________________________________________

## 2. Current Landscape

### 2.1 Whole-Proof Generation with RL

**What it is.** Train a large language model to produce complete Lean proof scripts in one pass, using reinforcement
learning from verifier feedback (binary reward: verified or not) to improve generation quality. The model emits a full
proof body; the Lean compiler checks it.

**Strongest representatives:**

- **DeepSeek-Prover-V2** (Apr 2025, arXiv [2504.21801](https://arxiv.org/abs/2504.21801)). 671B MoE model. Recursive
    decomposition: DeepSeek-V3 produces a proof sketch with subgoals; a 7B model solves each subgoal. Chain-of-thought
    cold-start data feeds GRPO reinforcement learning. **88.9% on miniF2F-test, 49/658 on PutnamBench.** Open-source.
    Lean 4.
- **Goedel-Prover-V2** (Aug 2025, arXiv [2508.03613](https://arxiv.org/abs/2508.03613)). 32B model. Scaffolded data
    synthesis (synthetic tasks of increasing difficulty), verifier-guided self-correction (iterative proof revision
    using Lean compiler feedback), model averaging. **88.1% miniF2F pass@32; 90.4% with self-correction.** Open-source.
    Lean 4.
- **Kimina-Prover** (Apr 2025, arXiv [2504.11354](https://arxiv.org/abs/2504.11354)). 72B model from Moonshot AI.
    RL-trained from Qwen2.5-72B with a "formal reasoning pattern" that emulates human problem-solving in Lean. No
    external search algorithm; relies on the model's internal reasoning. **80.7% miniF2F pass@8192.** Open-source (1.5B,
    7B distilled versions). Lean 4.

**Why it matters.** These systems show that whole-proof generation, combined with RL from verifier feedback, is now the
dominant paradigm for benchmark performance. `kan-prover`'s `repair` and `session step` commands already consume whole
theorem bodies, so the architectural fit is good. The key gap is that `kan-prover` does not yet expose a self-correction
loop where verifier diagnostics are fed back to the model for iterative refinement within a single attempt.

### 2.2 Agentic Orchestration with Decomposition

**What it is.** An outer agent coordinates an informal reasoning LLM, a specialized formal prover, and Lean's verifier.
The agent decomposes theorems into subgoals, delegates each to a prover, collects verifier feedback, repairs failures,
and reassembles verified sub-proofs.

**Strongest representatives:**

- **APOLLO** (May 2025, arXiv [2505.05758](https://arxiv.org/abs/2505.05758), NeurIPS 2025). Model-agnostic agentic
    framework. LLM generates proofs; agents fix syntax errors, identify failing sub-lemmas using Lean, invoke solvers,
    and retry with low top-K budget. **84.9% miniF2F with sub-8B models; raises Goedel-Prover-SFT from ~40% to 65.6%
    while cutting sample complexity from 25,600 to a few hundred.** Lean 4.
- **Hilbert** (Sep 2025, arXiv [2509.22819](https://arxiv.org/abs/2509.22819), NeurIPS 2025 Workshop). From Apple.
    Recursive decomposition with four components: informal LLM (Gemini 2.5 Pro/Flash), specialized prover
    (DeepSeek-Prover-V2-7B or Goedel-Prover-V2-32B), formal verifier, semantic theorem retriever. **94.7-99.2% miniF2F;
    70.0% PutnamBench.** Open-source. Lean 4.
- **Prover Agent** (Jun 2025, arXiv [2506.19923](https://arxiv.org/abs/2506.19923), ICML 2025). Multi-agent system:
    informal reasoning LLM, formal prover, Lean feedback. Generates auxiliary lemmas (not just subgoals but also special
    cases and useful facts). **88.1% miniF2F; 25/658 PutnamBench with small models.** Lean 4.

**Why it matters.** This is the closest architectural match to `kan-prover`'s `prove` command (graph-backed search with
planning, decomposition, helper-lemma management, and session-based subgoal proving). The literature confirms that the
decompose-delegate-repair pattern works. Key differences: the literature systems use fine-tuned specialized provers for
subgoals (not general-purpose LLM backends), and they do recursive decomposition (subgoals can themselves be
decomposed). `kan-prover` currently supports one level of decomposition.

### 2.3 Retrieval-Augmented Theorem Proving

**What it is.** Augment the LLM with retrieved premises, declarations, or proof fragments selected based on the current
proof state or theorem statement.

**Strongest representatives:**

- **LeanDojo / ReProver** (Jun 2023, arXiv [2306.15626](https://arxiv.org/abs/2306.15626), NeurIPS 2023). Foundational
    work. Extracts fine-grained data from Lean repos. ReProver retrieves premises based on encoded proof state, then
    generates the next tactic. Retriever uses program analysis to identify accessible premises and hard negatives.
    **Benchmark: 98,734 theorems from mathlib.** Lean 4.
- **Lean Copilot** (Apr 2024, arXiv [2404.12534](https://arxiv.org/abs/2404.12534)). Runs LLM inference natively inside
    Lean. Three tools: `suggest_tactics`, `search_proofs`, `select_premises`. Uses ReProver for premise retrieval.
    **Automates 74.2% of proof steps on average.** Lean 4.
- **LeanProgress** (Feb 2025, arXiv [2502.17925](https://arxiv.org/abs/2502.17925), ICLR 2025). Predicts remaining proof
    steps from the current proof state. Uses this as a search heuristic in best-first search. **75.1% prediction
    accuracy; 3.8% improvement on Mathlib4 over ReProver baseline.** Lean 4.

**Why it matters.** `kan-prover`'s retrieval system (`retrieval.py`) collects hits from 9+ source families: same-file
declarations, same-namespace declarations, goal-mentioned declarations, accepted patches, proved helpers, proof
fragments, graph run artifacts, and failure summaries. This is broader in source diversity than any system in the
literature. However, `kan-prover` scores hits using token overlap and heuristic weights, while the literature uses
learned dense embeddings over proof states. The scoring gap is the main retrieval weakness.

### 2.4 Tactic-Level Search

**What it is.** Generate one tactic at a time, verify it against Lean, and use search algorithms (best-first, beam,
MCTS) to explore the proof tree.

**Strongest representatives:**

- **COPRA** (Oct 2023, arXiv [2310.04353](https://arxiv.org/abs/2310.04353), COLM 2024). In-context learning agent.
    GPT-4 proposes tactics within a stateful backtracking search. Execution feedback builds the prompt for the next
    query. Tracks search history to reduce hallucinations.
- **Lean-STaR** (Jul 2024, arXiv [2407.10040](https://arxiv.org/abs/2407.10040), NeurIPS 2024). Trains models to produce
    informal "thoughts" before each formal tactic step. Expert iteration with multi-step success as reward. **43.4% ->
    46.3% miniF2F pass@64.**
- **InternLM2.5-StepProver** (Oct 2024, arXiv [2410.15700](https://arxiv.org/abs/2410.15700)). Expert iteration on
    large-scale Lean problems. Scales tactic-level proving to harder problems through curriculum.

**Why it matters.** `kan-prover`'s session model replaces entire theorem bodies, not individual tactics. The literature
shows that tactic-level search with verifier feedback at each step enables finer-grained error localization and
backtracking. However, whole-proof approaches have caught up or surpassed tactic-level search on standard benchmarks.
The main advantage of tactic-level interaction is for longer, more complex proofs where whole-proof generation fails.
`kan-prover` could benefit from optional tactic-level granularity in sessions, but this is not the highest priority.

### 2.5 Proof Repair

**What it is.** Given a broken proof and compiler diagnostics, produce a repaired proof. Distinct from proof search in
that the input includes a plausible but incorrect attempt.

**Strongest representatives:**

- **APRIL dataset** (Feb 2026, arXiv [2602.02990](https://arxiv.org/abs/2602.02990)). 260,000 supervised tuples pairing
    proof failures with compiler diagnostics, repair targets, and natural-language diagnoses. First large-scale dataset
    specifically for Lean proof repair. Demonstrates that training on repair data improves both repair accuracy and
    feedback-conditioned reasoning.
- **Goedel-Prover-V2 self-correction mode** (Aug 2025). Iterative proof revision using Lean compiler feedback. Raises
    miniF2F from 88.1% to 90.4%.
- **APOLLO repair agents** (May 2025). Agents analyze proofs, fix syntax errors, identify failing sub-lemmas using Lean
    diagnostics, and retry.

**Why it matters.** `kan-prover`'s `repair` and `solve` commands are already structured as repair loops: verify a
candidate, collect diagnostics, pass them to the backend, get a revised candidate. The APRIL dataset demonstrates that
repair-specific training data matters. The self-correction patterns in Goedel-Prover-V2 and APOLLO show that feeding
structured diagnostics (not just "it failed") to the model significantly improves repair success. `kan-prover` already
passes diagnostics to backends, which is the right shape.

### 2.6 Informal-to-Formal and Decomposition

**What it is.** Use an informal reasoning model to produce a proof sketch or plan, then formalize each step.

**Strongest representatives:**

- **Draft, Sketch, and Prove (DSP)** (Oct 2022, arXiv [2210.12283](https://arxiv.org/abs/2210.12283), ICLR 2023). Maps
    informal proofs to formal proof sketches; uses sketches to guide automated provers. **miniF2F 20.9% -> 39.3%.**
- **DeepSeek-Prover-V2 recursive decomposition** (Apr 2025). DeepSeek-V3 produces informal reasoning + formal subgoals;
    7B model solves each.
- **Hilbert recursive decomposition** (Sep 2025). Splits problems recursively; each level can invoke informal reasoning
    or formal proving.

**Why it matters.** `kan-prover`'s `plan` and `decompose` commands implement a version of this pattern. The literature
shows that recursive decomposition (subgoals of subgoals) and informal reasoning interleaved with formal steps are the
key ingredients. `kan-prover`'s decomposition is currently single-level. Making it recursive is a clear opportunity.

### 2.7 Lean-Side Infrastructure

**What it is.** Tooling that exposes Lean's internal state to external programs for proof search, data extraction, and
verification.

**Strongest representatives:**

- **Pantograph** (Oct 2024, arXiv [2410.16429](https://arxiv.org/abs/2410.16429), TACAS 2025). Machine-to-machine
    interface for Lean 4. Provides proof state inspection, tactic execution, expression construction, environment
    examination. Python bindings (PyPantograph). C FFI. Supports MCTS-style search.
- **LeanDojo** (Jun 2023). Extracts fine-grained interaction traces from Lean repos. Provides training data, benchmark
    splits, and a proof search environment.
- **LeanInteract** (2025). Python package for interacting with Lean 4 via lean4-repl. Lightweight. Supports all Lean
    versions 4.8.0-4.29.0.
- **ntp-toolkit** (CMU, 2024). Extracts next-tactic and full-proof training data from arbitrary Lean 4 repos. Produces
    JSONL datasets.
- **lean4-repl** (2024). Lean 4 REPL that can execute commands and tactics, returning proof state.

**Why it matters.** `kan-prover`'s SemanticProbe.lean is a custom Lean helper surface that provides declaration
targeting, proof state, local context, goal snapshots, and insertion points. This is more tightly integrated with a
specific workspace than Pantograph (which is environment-level, not workspace-level). However, Pantograph exposes
tactic-level execution and MCTS-compatible state transitions, which `kan-prover` lacks. The gap is tactic-level
interaction, not workspace awareness.

______________________________________________________________________

## 3. Comparison to kan-prover

### Ahead

- **Workspace-aware verification.** No system in the literature binds to a real Lake workspace the way `kan-prover` does
    with `--workspace`. Most systems operate on isolated files or standardized benchmark environments. `kan-prover`
    verifies against the actual project dependencies, imports, and build configuration.
- **Durable, replayable artifacts.** `kan-prover` persists every probe result, backend request/response, retrieval hit,
    and verification outcome in structured artifact bundles that support `replay`, `export`, and `trace`. No comparable
    system in the literature provides this level of audit trail.
- **Sandbox-then-apply workflow.** `kan-prover` verifies in a sandbox copy of the workspace before any write-back. The
    `apply` command requires an explicit verified branch. Most literature systems either write directly or operate in
    ephemeral environments.
- **Multi-source retrieval with near-miss explanation.** `kan-prover`'s retrieval collects from 9+ source families and
    reports near-misses with omission reasons. This is richer in source diversity than any published system (most use
    only premise selection from mathlib or the local file).
- **Helper-lemma management.** `kan-prover` materializes helpers under a managed namespace
    (`KanProverGenerated.Helpers`), tracks their verification status, and includes them in retrieval for subsequent
    proving. This is comparable to Prover Agent and Hilbert but more explicit in namespace management.
- **Graph-backed search with session reuse.** The `prove`/`resume` workflow builds a proof graph where each node is a
    full session snapshot with verification status. This is more structured than the tree search in most literature
    systems, and supports resumption from stored frontier states.

### Aligned

- **Whole-body replacement model.** `kan-prover`'s `repair`, `solve`, and `session step` replace entire theorem bodies,
    not individual tactics. This aligns with the dominant whole-proof generation paradigm (DeepSeek-Prover-V2,
    Goedel-Prover-V2, Kimina-Prover).
- **Diagnostic-driven repair loops.** `kan-prover` passes Lean diagnostics to backends for generating revised
    candidates. This matches APOLLO's pattern and Goedel-Prover-V2's self-correction mode.
- **Planning before search.** `kan-prover`'s `plan` command asks for a structured theorem plan before search, similar to
    DSP's sketch and DeepSeek-Prover-V2's decomposition phase.
- **Decomposition into helpers.** `kan-prover`'s `decompose` command proposes and verifies helper lemmas, matching the
    decomposition patterns in APOLLO, Hilbert, and Prover Agent.
- **Lean-backed declaration targeting.** `kan-prover`'s SemanticProbe provides Lean-authoritative declaration spans,
    namespace data, and proof state, similar in spirit to Pantograph's declaration inspection but workspace-scoped.

### Behind

- **Retrieval scoring.** `kan-prover` scores retrieval hits using token overlap and heuristic weights (`retrieval.py`
    lines 92-109: `theorem_tokens`, `constants`, `type_heads`). The literature uses learned dense embeddings (ReProver)
    or semantic search (Hilbert). Token overlap is functional but will miss semantically similar declarations with
    different surface forms.
- **No recursive decomposition.** `kan-prover`'s decomposition is single-level: it proposes helpers for the target
    theorem. Hilbert and DeepSeek-Prover-V2 recursively decompose subgoals. This matters for harder theorems.
- **No self-correction within a single attempt.** `kan-prover`'s repair loop generates a new candidate per attempt.
    Goedel-Prover-V2's self-correction mode and APOLLO's repair agents refine within a single attempt using structured
    diagnostic feedback. The distinction: `kan-prover` retries with fresh backend calls; the literature shows that
    conditioning on the specific failure (not just the diagnostics) improves repair quality.
- **No tactic-level interaction.** `kan-prover` sessions operate at whole-body granularity. Pantograph and COPRA expose
    tactic-level state transitions, enabling finer-grained search and error localization. This is less critical given
    that whole-proof generation dominates benchmarks, but matters for complex proofs.
- **No proof progress estimation.** LeanProgress predicts remaining proof steps to guide search. `kan-prover` has no
    analogous heuristic for prioritizing which graph nodes to expand.

### Missing

- **No model fine-tuning or RL pipeline.** `kan-prover` is backend-agnostic (it calls an LLM API) and does not train or
    fine-tune models. All the strongest benchmark results come from models trained with RL from verifier feedback (GRPO
    on binary verification reward). This is a deliberate architectural choice (Python orchestration, not model
    training), but it means `kan-prover` depends entirely on the quality of the backend model.
- **No informal reasoning integration.** Hilbert and DeepSeek-Prover-V2 use an informal reasoning model (GPT, Gemini,
    DeepSeek-V3) to produce high-level proof sketches before formalization. `kan-prover`'s `plan` command delegates this
    to the backend, but there is no explicit informal-then-formal pipeline.
- **No proof-state embedding for retrieval.** The strongest retrieval systems (ReProver) encode proof states as dense
    vectors and retrieve premises by similarity. `kan-prover` has no embedding model.
- **No synthetic data or training data extraction pipeline.** ntp-toolkit and LeanDojo extract training data from Lean
    repos. `kan-prover` does not produce training data from its verification runs, though its artifact structure could
    support this.
- **No benchmark suite beyond internal acceptance.** The literature uses miniF2F, PutnamBench, ProofNet, and Mathlib4 as
    standard evaluation. `kan-prover`'s `bench` command runs local manifests but there is no standardized cross-system
    benchmark integration.

______________________________________________________________________

## 4. High-Leverage Opportunities

Ranked by expected impact for `kan-prover` given its current verifier-first, workspace-aware, artifact-heavy design.

### 1. Recursive Decomposition

**What to build.** Extend the decomposition engine so that helper lemmas produced by `decompose` can themselves be
decomposed. Allow the `prove` graph to recurse: a node that needs a helper spawns a sub-prove for that helper.

**Why it matters.** Hilbert (99.2% miniF2F) and DeepSeek-Prover-V2 (88.9%) both show that recursive decomposition is the
single largest contributor to solving hard theorems. `kan-prover` already has single-level decomposition; making it
recursive is a natural extension.

**Side:** Mostly Python (prove.py, loop.py). Some Lean-side work if helper verification needs chained Lake builds.

**Category:** Search.

### 2. Structured Diagnostic Feedback for Self-Correction

**What to build.** In the repair loop, instead of passing raw diagnostics to the backend, parse Lean's error messages
into structured failure categories (type mismatch, unknown identifier, tactic failure, timeout, etc.) and include the
specific failing subexpression. Allow the backend to attempt in-place correction conditioned on the structured failure
before generating a wholly new candidate.

**Why it matters.** APOLLO's 84.9% with sub-8B models comes largely from structured error analysis and targeted repair.
Goedel-Prover-V2's self-correction adds 2.3 percentage points. `kan-prover` already classifies diagnostics
(`diagnostics.py`) but does not use this classification to guide repair strategy.

**Side:** Python (loop.py, inspection.py, diagnostics.py). No Lean changes needed.

**Category:** Search / evaluation.

### 3. Embedding-Based Retrieval

**What to build.** Add an optional dense retrieval path alongside the existing token-overlap scorer. Encode theorem
statements and proof states using a sentence embedding model (e.g., a ReProver-style encoder or a general-purpose
embedding model). Store embeddings for workspace declarations. Rank by cosine similarity, then merge with the existing
heuristic scores.

**Why it matters.** Token overlap misses semantically related declarations with different surface forms. ReProver
demonstrates that learned retrieval significantly improves premise selection quality. The embedding index can be built
once per workspace and cached.

**Side:** Python (retrieval.py). Optional: a small embedding model or API call.

**Category:** Retrieval.

### 4. Tactic-Level Probe (Optional Granularity in Sessions)

**What to build.** Extend SemanticProbe.lean (or integrate Pantograph) to support executing a single tactic against the
current proof state and returning the resulting goals. Expose this as an optional session action (`session tactic`)
alongside the existing whole-body `session step`.

**Why it matters.** For complex proofs where whole-body replacement fails, tactic-level interaction enables finer error
localization and incremental construction. COPRA and Lean-STaR show that tactic-level feedback is valuable, especially
for longer proofs. This does not replace whole-body generation; it supplements it.

**Side:** Both. Lean-side: tactic execution in SemanticProbe or Pantograph integration. Python-side: session.py,
prove.py.

**Category:** Infrastructure.

### 5. Standard Benchmark Integration

**What to build.** Add miniF2F-Lean4 and a subset of PutnamBench as `bench` manifests. Produce results in a format
comparable to the literature (pass@k, solve rate, backend calls per theorem, wall time). This enables objective
measurement of backend quality and search strategy improvements.

**Why it matters.** Without standard benchmarks, improvements to `kan-prover`'s search, retrieval, or decomposition
cannot be compared to the literature. The `bench` command already exists; what's missing is the benchmark data and the
standardized evaluation protocol.

**Side:** Python (bench.py, new manifest files). Lean-side: import the benchmark theorem files into a test workspace.

**Category:** Evaluation.

______________________________________________________________________

## 5. Evidence Table

| System / Paper                                                 | Year | Lean | Task                                 | Key Idea                                                           | Evaluation                         | kan-prover Relevance             |
| -------------------------------------------------------------- | ---- | ---- | ------------------------------------ | ------------------------------------------------------------------ | ---------------------------------- | -------------------------------- |
| [LeanDojo / ReProver](https://arxiv.org/abs/2306.15626)        | 2023 | 4    | Retrieval-augmented tactic search    | Dense premise retrieval + encoder-decoder tactic generation        | 98K theorems from mathlib; miniF2F | Retrieval architecture reference |
| [COPRA](https://arxiv.org/abs/2310.04353)                      | 2023 | 3/4  | Stateful tactic search               | GPT-4 in backtracking search with execution feedback               | miniF2F                            | Session-style proving reference  |
| [Draft, Sketch, Prove](https://arxiv.org/abs/2210.12283)       | 2022 | 4    | Informal-to-formal                   | LLM sketch -> formal proof skeleton -> automation fills gaps       | miniF2F 39.3%                      | Plan/decompose pattern           |
| [DeepSeek-Prover-V1.5](https://arxiv.org/abs/2408.15839)       | 2024 | 4    | Whole-proof RL                       | GRPO training; Monte Carlo tree search at inference                | miniF2F 63.5%                      | RL from verifier feedback        |
| [Lean Copilot](https://arxiv.org/abs/2404.12534)               | 2024 | 4    | Interactive copilot                  | LLM inference native in Lean; suggest/search/select                | 74.2% step automation              | In-editor integration model      |
| [Lean-STaR](https://arxiv.org/abs/2407.10040)                  | 2024 | 4    | Thought-augmented tactics            | Interleave informal thoughts with tactic steps; expert iteration   | miniF2F 46.3%                      | Chain-of-thought for tactics     |
| [LeanAgent](https://arxiv.org/abs/2410.06209)                  | 2024 | 4    | Lifelong learning                    | Curriculum learning across repos; progressive training             | 155 new proofs across 23 repos     | Multi-repo proving               |
| [Pantograph](https://arxiv.org/abs/2410.16429)                 | 2024 | 4    | Machine-to-machine API               | REPL for proof interaction; tactic exec; Python/C FFI              | TACAS 2025                         | Lean-side infrastructure         |
| [Kimina-Prover](https://arxiv.org/abs/2504.11354)              | 2025 | 4    | Whole-proof RL                       | Formal reasoning pattern; no external search; RL from Qwen2.5      | miniF2F 80.7% pass@8192            | Internal reasoning scaling       |
| [DeepSeek-Prover-V2](https://arxiv.org/abs/2504.21801)         | 2025 | 4    | Recursive decomposition + RL         | V3 decomposes; 7B solves subgoals; GRPO training                   | miniF2F 88.9%; PutnamBench 49/658  | Recursive decomp reference       |
| [APOLLO](https://arxiv.org/abs/2505.05758)                     | 2025 | 4    | Agentic repair                       | Multi-agent: syntax fix, error isolation, solver dispatch, retry   | miniF2F 84.9% (sub-8B)             | Repair loop architecture         |
| [Prover Agent](https://arxiv.org/abs/2506.19923)               | 2025 | 4    | Multi-agent proving                  | Informal LLM + formal prover + Lean; auxiliary lemma gen           | miniF2F 88.1%; PutnamBench 25/658  | Helper-lemma workflow            |
| [Goedel-Prover-V2](https://arxiv.org/abs/2508.03613)           | 2025 | 4    | Self-correction + scaffolded data    | Verifier-guided self-correction; model averaging                   | miniF2F 90.4% (self-correct)       | Self-correction pattern          |
| [Hilbert](https://arxiv.org/abs/2509.22819)                    | 2025 | 4    | Recursive decomposition              | Informal + formal + verifier + retriever; recursive subgoals       | miniF2F 99.2%; PutnamBench 70%     | Recursive decomp + retrieval     |
| [LeanProgress](https://arxiv.org/abs/2502.17925)               | 2025 | 4    | Search guidance                      | Predict remaining proof steps; guide best-first search             | +3.8% on Mathlib4                  | Search heuristic                 |
| [APRIL](https://arxiv.org/abs/2602.02990)                      | 2026 | 4    | Proof repair dataset                 | 260K failure/diagnostic/repair tuples for Lean                     | Repair accuracy improvement        | Repair-specific training data    |
| [LEGO-Prover](https://arxiv.org/abs/2310.00656)                | 2023 | 4    | Modular proof + growing library      | Constructs proofs block-by-block; maintains verified lemma library | miniF2F 50.0%                      | Helper-lemma library design      |
| [BFS-Prover](https://arxiv.org/abs/2502.03438)                 | 2025 | 4    | Best-first search + expert iteration | Strategic data filtering, DPO from errors, length normalization    | miniF2F 72.95%                     | Search heuristic design          |
| [Aristotle](https://arxiv.org/abs/2510.01346)                  | 2025 | 4    | Multi-component IMO-level proving    | MCTS on Lean sketches + informal reasoning + geometry solver       | IMO 2025 gold (5/6 problems)       | Multi-strategy orchestration     |
| [MA-LoT](https://arxiv.org/abs/2503.03205)                     | 2025 | 4    | Model-collaboration CoT repair       | Separate prover and corrector models with iterative refinement     | miniF2F 61.07%                     | Prover/corrector split           |
| [LeanHammer](https://arxiv.org/abs/2506.07477)                 | 2025 | 4    | Neural premise selection for ATP     | LeanPremise retriever + lean-auto DTT-to-HOL translation           | +21% over baselines                | Premise selection for retrieval  |
| [Lean Finder](https://arxiv.org/abs/2510.15940)                | 2025 | 4    | Semantic search for Mathlib          | Intent-aware embeddings; 1.4M query-code pairs                     | 81.6% upvoted vs 56.9% baseline    | Semantic retrieval reference     |
| [Leanabell-Prover-V2](https://arxiv.org/abs/2507.08649)        | 2025 | 4    | Verifier-integrated RL               | 7B model with Lean 4 verifier feedback in RL loop                  | miniF2F 78.2% pass@128             | RL from verifier feedback        |
| [Ax-Prover](https://arxiv.org/abs/2510.12787)                  | 2025 | 4    | MCP-based agent proving              | LLM with Lean tools via Model Context Protocol                     | Competitive on public benchmarks   | MCP integration pattern          |
| [Leanstral](https://mistral.ai/news/leanstral)                 | 2026 | 4    | Code + proof agent                   | MoE 120B/6B active; generates code + proofs for verification       | FLTEval 26.3 pass@2                | Software verification prover     |
| [LeanInteract](https://github.com/augustepoiroux/LeanInteract) | 2025 | 4    | Python-Lean bridge                   | Lightweight Python interface via lean4-repl                        | Supports Lean 4.8-4.29             | Alternative Lean bridge          |
| [ntp-toolkit](https://github.com/cmu-l3/ntp-toolkit)           | 2024 | 4    | Training data extraction             | Extract next-tactic and full-proof data from Lean repos            | miniCTX benchmark                  | Data pipeline reference          |

______________________________________________________________________

## 6. Recommended Issue Stack

### Parent Tracker

**Title:** `kan-prover proof search: literature-informed improvements`

**Body:** Implement the highest-leverage improvements identified in the 2026-03-28 literature review of LLM-based proof
search and repair for Lean. Focus areas: recursive decomposition, structured diagnostic repair, embedding-based
retrieval, and standard benchmark integration.

### Child Issues (Sequenced)

**1. Add recursive decomposition to `prove` graph search**

- Extend `prove.py` to allow helper-lemma nodes to spawn sub-decompositions
- Allow `decompose` to recurse up to a configurable depth
- Track decomposition depth in graph nodes and artifacts
- Acceptance: a theorem that requires a helper which itself requires a helper is solved end-to-end
- Depends on: nothing (builds on existing decompose + prove infrastructure)

**2. Structure diagnostic feedback for repair strategy selection**

- Parse Lean diagnostics into machine-readable failure categories beyond the current `DiagnosticClass`
- Include the specific failing subexpression when available
- Add a repair-strategy selector that chooses between whole-body regeneration and targeted in-place correction based on
    failure category
- Acceptance: repair loop uses structured failure categories to guide backend requests; `--agent none` path still works
- Depends on: nothing (builds on existing diagnostics.py and loop.py)

**3. Add embedding-based retrieval scoring**

- Add an optional dense retrieval path in `retrieval.py`
- Encode theorem statements and proof-state goals using a sentence embedding model
- Build and cache a per-workspace embedding index for declarations
- Merge embedding similarity scores with existing heuristic scores
- Acceptance: `retrieve` command shows embedding-based scores when enabled; retrieval quality improves on at least one
    acceptance theorem
- Depends on: nothing (additive to existing retrieval)

**4. Add miniF2F and PutnamBench benchmark manifests**

- Import miniF2F-Lean4 theorems into a benchmark workspace
- Add a PutnamBench subset
- Create `bench` manifests that produce pass@k, solve rate, and backend-call-per-theorem metrics
- Produce results in a format comparable to the literature evidence table
- Acceptance: `make -C tools/kan-prover bench` runs the standard benchmarks and produces a comparable results table
- Depends on: nothing (builds on existing bench infrastructure)

**5. Investigate tactic-level session actions**

- Evaluate whether Pantograph integration or a SemanticProbe extension can provide tactic-level state transitions
- Prototype a `session tactic` action that executes one tactic and returns updated goals
- Assess the value for `kan-prover`'s proving workflows on hard theorems
- Acceptance: design document with prototype results and a go/no-go recommendation
- Depends on: #4 (need benchmark data to evaluate whether tactic-level interaction helps)

______________________________________________________________________

## Appendix

### Sources Consulted

**Primary papers (arXiv):**

- [DeepSeek-Prover-V2 (2504.21801)](https://arxiv.org/abs/2504.21801)
- [Goedel-Prover-V2 (2508.03613)](https://arxiv.org/abs/2508.03613)
- [Kimina-Prover (2504.11354)](https://arxiv.org/abs/2504.11354)
- [APOLLO (2505.05758)](https://arxiv.org/abs/2505.05758)
- [Hilbert (2509.22819)](https://arxiv.org/abs/2509.22819)
- [Prover Agent (2506.19923)](https://arxiv.org/abs/2506.19923)
- [LeanDojo / ReProver (2306.15626)](https://arxiv.org/abs/2306.15626)
- [COPRA (2310.04353)](https://arxiv.org/abs/2310.04353)
- [Lean Copilot (2404.12534)](https://arxiv.org/abs/2404.12534)
- [Lean-STaR (2407.10040)](https://arxiv.org/abs/2407.10040)
- [LeanAgent (2410.06209)](https://arxiv.org/abs/2410.06209)
- [Pantograph (2410.16429)](https://arxiv.org/abs/2410.16429)
- [LeanProgress (2502.17925)](https://arxiv.org/abs/2502.17925)
- [APRIL (2602.02990)](https://arxiv.org/abs/2602.02990)
- [Draft, Sketch, and Prove (2210.12283)](https://arxiv.org/abs/2210.12283)
- [PutnamBench (2407.11214)](https://arxiv.org/abs/2407.11214)
- [InternLM2.5-StepProver (2410.15700)](https://arxiv.org/abs/2410.15700)
- [LEGO-Prover (2310.00656)](https://arxiv.org/abs/2310.00656)
- [BFS-Prover (2502.03438)](https://arxiv.org/abs/2502.03438)
- [Aristotle (2510.01346)](https://arxiv.org/abs/2510.01346)
- [MA-LoT (2503.03205)](https://arxiv.org/abs/2503.03205)
- [LeanHammer / LeanPremise (2506.07477)](https://arxiv.org/abs/2506.07477)
- [Lean Finder (2510.15940)](https://arxiv.org/abs/2510.15940)
- [Leanabell-Prover-V2 (2507.08649)](https://arxiv.org/abs/2507.08649)
- [Ax-Prover (2510.12787)](https://arxiv.org/abs/2510.12787)
- [AlphaProof (Nature 2025)](https://www.nature.com/articles/s41586-025-09833-y)

**Project repos and official pages:**

- [LeanDojo](https://leandojo.org/)
- [Pantograph (GitHub)](https://github.com/leanprover/Pantograph)
- [PyPantograph](https://github.com/stanford-centaur/PyPantograph)
- [DeepSeek-Prover-V2 (GitHub)](https://github.com/deepseek-ai/DeepSeek-Prover-V2)
- [Goedel-Prover (GitHub)](https://github.com/Goedel-LM/Goedel-Prover)
- [Goedel-Prover-V2 (GitHub)](https://github.com/Goedel-LM/Goedel-Prover-V2)
- [Kimina-Prover (GitHub)](https://github.com/MoonshotAI/Kimina-Prover-Preview)
- [LeanCopilot (GitHub)](https://github.com/lean-dojo/LeanCopilot)
- [LeanAgent (GitHub)](https://github.com/lean-dojo/LeanAgent)
- [ntp-toolkit (GitHub)](https://github.com/cmu-l3/ntp-toolkit)
- [LeanInteract (GitHub)](https://github.com/augustepoiroux/LeanInteract)
- [Hilbert (GitHub)](https://github.com/apple/ml-hilbert)
- [PutnamBench (GitHub)](https://github.com/trishullab/PutnamBench)

**Local code reviewed:**

- `tools/kan-prover/README.md`
- `tools/kan-prover/src/kan_prover/engine/proofctl.py`
- `tools/kan-prover/src/kan_prover/engine/lean_helpers.py`
- `tools/kan-prover/src/kan_prover/engine/inspection.py`
- `tools/kan-prover/src/kan_prover/engine/retrieval.py`
- `tools/kan-prover/src/kan_prover/engine/loop.py`
- `tools/kan-prover/src/kan_prover/engine/session.py`
- `tools/kan-prover/src/kan_prover/engine/prove.py`
- `tools/kan-prover/lean-runtime/Tools/SemanticProbe.lean`
- GitHub issues #228, #246, #258

### Search Gaps and Uncertainty

- **AlphaProof.** Google DeepMind's AlphaProof (IMO 2024) has no published paper or architecture details. Media reports
    suggest it uses AlphaZero-style search over Lean proofs. Without primary sources, I cannot compare it meaningfully
    to `kan-prover`. The IMO 2024 results (4 gold-level problems) are impressive but not reproducible.
- **Proprietary systems.** OpenAI, Anthropic, and Google likely have internal theorem-proving capabilities that are not
    published. Benchmark contamination (training on benchmark theorems) is a known concern for published results.
- **miniF2F saturation.** With Hilbert at 99.2%, miniF2F is nearly saturated as a benchmark. The field is moving toward
    PutnamBench, FormalMATH, and domain-specific benchmarks. Claims above ~90% on miniF2F should be interpreted with
    caution.
- **Lean version fragmentation.** Some results are on Lean 3, some on Lean 4. Lean 4 results are not directly comparable
    to Lean 3 results. I have focused on Lean 4 systems where possible.
- **Reproducibility.** Many benchmark results use large compute budgets (pass@8192 for Kimina-Prover, pass@32 for
    Goedel-Prover-V2). `kan-prover` operates in a much tighter budget regime (typically 1-10 backend calls per theorem).
    Comparison requires normalizing for compute budget.
- **AlphaProof.** Google DeepMind published full details in Nature (Nov 2025). Architecture: encoder-decoder transformer
    with AlphaZero-style RL, tactic-level best-first search with AND/OR nodes, and test-time reinforcement learning on
    problem variants. Solved 3 IMO 2024 problems including P6 (solved by only 5 human contestants). Not open-source; not
    reproducible. Trained on ~80M auto-formalized statements. The AND-node search structure (requiring all branches
    proved) is relevant to `kan-prover`'s graph-backed search.
- **LEGO-Prover** (ICLR 2024 Oral). Constructs proofs modularly using a growing library of verified lemmas. During
    proving, retrieves existing "skills" and creates new ones. miniF2F 50.0%. Directly relevant to `kan-prover`'s
    helper-lemma management under `KanProverGenerated.Helpers`. Now included in the evidence table.

### What Would Most Improve Confidence

1. **Running miniF2F through `kan-prover bench`** with a strong backend (DeepSeek-Prover-V2-7B or Goedel-Prover-V2-32B)
    to establish a concrete baseline.
1. **Profiling `kan-prover`'s retrieval quality** against ReProver-style dense retrieval on the same workspace.
1. **Testing recursive decomposition** on a set of hard `KanProofs` theorems to see whether single-level vs. recursive
    decomposition makes a practical difference for this project's proof targets.
1. **Reading the APRIL paper in full** to assess whether its repair-training methodology could produce training data
    from `kan-prover`'s existing artifact bundles.
