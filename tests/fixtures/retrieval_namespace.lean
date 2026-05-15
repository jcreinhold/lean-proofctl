namespace Demo

theorem helper_one : True := by
  exact True.intro

theorem target : True := by
  exact helper_one

end Demo

theorem global_helper : True := by
  exact True.intro
