# INT-8 assumptions and decisions

- Durable input decision (2026-07-18): the recovered source payload contains
  exactly 13 regular files. Its canonical sorted-file-table SHA-256 is
  `13868767846b7004f140cfade3652909347bdbb6708b69cb8c10b36ec2756eb0`.
  Freeze the byte-identical payload under
  `experiments/data/int-8-retained-int-4-smoke-v1/sha256/13868767846b7004f140cfade3652909347bdbb6708b69cb8c10b36ec2756eb0/payload`.
- Retention boundary (2026-07-18): `.runs/` is gitignored and may be used only
  for staging and run output. Check in the payload, the Task-owned
  `input-manifest.json`, and an exact byte copy of the separately hashed INT-4
  contract under the same content-addressed directory before experiment code.
  Bind the contract's raw-byte SHA-256
  `bbbba03856b74047a8f8cec44f23a3c28b92558bff0001c602e4211c75d260a7`
  separately from its parsed canonical identity
  `9c3f0f600b70ca4fef7131086f6d9b350f9612e690cbb9d621e961a5de27d03c`.
  The runner must pass from a fresh canonical checkout without reading the
  sibling INT-4 worktree after the initial copy.
- Scope correction (2026-07-18): INT-8 may modify the existing INT-6 arena
  implementation. It may not create a second arena authority or modify INT-9,
  managym, or reusable search/authority kernels.
- Required implementation (2026-07-18): add a checkpoint-bound PUCT candidate
  that uses the checkpoint only for legal-action priors and delegates leaf
  values to the existing random terminal evaluator. The generic `agent_puct`
  path remains excluded because it consumes checkpoint value output.
- Required execution (2026-07-18): add a smoke-only complete diagnostic cohort
  path inside INT-6 for uniform, chosen-policy-only, and visit-policy-only
  priors, using common comparison seeds and one combined payoff matrix.
- Frozen boundary (2026-07-18): the INT-6 `ArenaKey`, rating prior, five anchor
  registration payloads and identities, anchor cohort digest, schedules,
  selected matchup, and scale remain exact. The arena implementation-source and
  checked contract digests may advance to bind the additive capability.
- Fail-closed boundary (2026-07-18): only exact retained byte, manifest, or
  current-loader incompatibility stops the Task before experiment execution.
  Missing candidate or multi-challenger support is implementation work.
- Evidence boundary (2026-07-18): one 507-label training seed remains
  `engineering_smoke_only_no_admission_claim`; no outcome can grant admission,
  promotion, or a general chosen-versus-visit method claim.
