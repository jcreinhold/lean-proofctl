import Mathlib

theorem mathd_numbertheory_3 :
    Finset.sum (Finset.range 10) (fun x => (x + 1) ^ 2) % 10 = 5 := by
  exact False.elim ?missing
