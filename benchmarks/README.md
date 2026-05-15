Standard benchmark fixtures for `kan-prover bench`.

These fixtures keep the benchmark surface reproducible and checked in. They do not claim full leaderboard coverage. Each
suite here is a documented subset that is small enough to vendor and stable enough to reuse across later search work.

Current suites:

- `miniF2F-test-subset-v1` MiniF2F test-split theorem statements adapted from `facebookresearch/miniF2F` at commit
    `e4f113090ad82d64f8ce064d2f55b613a9b6bded`.
- `putnambench-subset-v1` PutnamBench Lean 4 theorem statements adapted from `trishullab/PutnamBench` at commit
    `b391f48b645c5b6101ed89aca96230226d654b3e`.

Both upstream sources are Apache 2.0 licensed. The checked-in Lean files keep the original theorem names and problem
text where practical, but replace proofs with failing placeholders so `kan-prover` still has a real theorem target to
repair or prove.

Run the checked-in standard manifests with:

```sh
make bench-standard
```

Benchmark rows are expected to point back to the concrete `run_id` and `artifact_dir` for each executed target. That
link is part of the audit trail.

`pass_k` semantics:

- `pass_k` is declared in the manifest benchmark block.
- Rows are grouped by manifest target id.
- A target counts as a pass if one of its first `k` rows succeeds.
- The shipped standard manifests use `pass_k = 1`, so `pass_rate` is the same as one-run solve rate.

The production manifests under `benchmarks/manifests/` do not ship proof answers. The smoke manifest under
`tests/acceptance/` exists only to prove that the standard benchmark path runs end to end inside the test suite.
