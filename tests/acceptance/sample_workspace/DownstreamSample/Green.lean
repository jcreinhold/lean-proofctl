namespace DownstreamSample

theorem smoke_true : True := by
  exact True.intro

theorem smoke_pair : True /\ True := by
  exact And.intro True.intro True.intro

end DownstreamSample
