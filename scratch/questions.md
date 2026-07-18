# Open Review Assumptions

- 2026-07-17: The interactive reviewer had not answered the first scheduling
  question when the Task process restarted into headless mode. Following the
  instruction to choose the simpler path, the design now assumes arena v1
  freezes the complete anchor round robin once and content-addresses it;
  subsequent candidates play only challenger-versus-anchor cells on the same
  deal blocks. This is not recorded as human confirmation and remains subject
  to the review disposition.
- 2026-07-17: The kickoff's remaining numerical choices are design assumptions
  pending explicit human correction: flat-MC budgets 4/16/64, a 400-Elo
  Gaussian prior, a +25 Elo promotion margin, 10% relative compute tolerances,
  and a 10-percentage-point competency noninferiority margin. The schedule is
  now evidence-backed at 24 paired deal blocks/48 games per matchup, matching
  the established preregistered cell size without doubling it.
