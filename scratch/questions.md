# Remaining assumptions

- W2-214 assumes that learning five known structural relation classes on
  nuisance-disjoint synthetic variants is enough to choose a candidate for
  W2-213. It is not evidence that the candidate handles unseen compositions,
  card identities, or runtime bindings; W2-213 must falsify those separately.
- Literal-erased family skeletons intentionally appear in train, validation,
  and test. The anti-template gate removes duplicate programs, nuisance
  configurations, pair templates, and label-correlated generator fields, but
  does not claim a novel-AST holdout.
- Definition-reference programs are excluded because the current symbolic
  payload contains a semantic key. Local target/choice declaration-reference
  edges are sufficient for W2-214's link kata, but referenced-definition
  semantics require a separately specified identity-safe input before they can
  enter W2-213 or a production encoder contract.
- The 2.5x p95 and 40% throughput thresholds are relative CPU selection gates
  for this bounded probe. Any later production candidate must remeasure cached
  catalog encoding in the actual policy inference path rather than port these
  absolute timings.
