import Mathlib

abbrev putnam_2020_a1_solution : ℕ := 508536

/--
Find the number of positive integers $N$ satisfying:

1. $N$ is divisible by $2020$,
2. $N$ has at most $2020$ decimal digits,
3. The decimal digits of $N$ are a string of consecutive ones followed by a
   string of consecutive zeros.
-/
theorem putnam_2020_a1 :
    Set.ncard
        {x : ℕ |
          2020 ∣ x
            ∧ Nat.log 10 x + 1 ≤ 2020
            ∧ ∃ k l, k ≥ l ∧ x = ∑ i ∈ Finset.range (k - l + 1), 10 ^ (i + l)} =
      putnam_2020_a1_solution := by
  exact False.elim ?missing
