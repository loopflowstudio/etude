# Rules memory

## Durable evidence and scheduling

- Runtime foundation evidence resolved 2026-07-15: W2-208/PR #81 proves independently reset environments, exact root/sibling forks, and retained RolloutPool slots share one versioned `Arc<ContentPack>` while mutable branch facts remain isolated; the allocation gate retains 4,096 pack references at 0 allocations/0 bytes and shows immutable-definition bytes do not increase GameState/Game clone allocations. W2-216/PR #89 merged at d8a92aad729adabf82867c19b021510c8d473165 and corrected the receipt to the canonical measured revision f7d5878b6be4b2276ac5f42f50dc5f390a6982ad with passing thresholds. Runtime KRs 1 and 2 hold; KR3 remains open. For revision-bound evidence, verify `measurement_code_revision` after the final rebase and before landing; merged status alone is not proof of freshness.
- Semantic choice evidence verified 2026-07-15: W2-181 and W2-188 make Semantic KRs 2 and 3 hold; priority, Lightning Bolt targeting, and declare attackers use revision/prompt-bound structured offers and atomic commands without the 32-choice cap, with legacy-adapter legal-outcome and deterministic-trace differential coverage. W2-189/PR #88 merged at dd0032697bcffc0dd03d374910772879e8d923b6 and makes KR4 hold with the structured-decoder benchmark. Semantic KRs 1, 5, and 6 remain open.
- Semantic learning order established 2026-07-15: W2-215 owns the viewer-safe typed-program projection for KR5. W2-223 owns KR1's complete two-deck offline-compiler admission and no-card-name-dispatch proof and stays dormant while W2-215 is active. W2-214 follows verified W2-215 evidence; W2-213 follows W2-214. Filed Tasks, PR publication, and training activity are not KR evidence.
- Identity and event evidence verified 2026-07-15: W2-180, W2-190, and W2-191 make all Identity and Event Semantics KRs hold. The admitted slice has incarnation-changing leave/re-enter with stale `ObjectRef` rejection and LKI, typed proposed-event replacement/prevention before commit for damage, life, destruction, counters, and zone movement, and deterministic trigger/SBA fixpoint tests with incarnation-safe public rules boundaries. Treat further breadth as gap-driven follow-up.
- Search verification state 2026-07-15: Search KR4 holds; KRs 1–3 remain open. W2-200 owns conformance-oracle CI for KR3. W2-197 owns the executable fork/rollback contract and has a queued repair for its failing CI check; do not duplicate the restart. W2-207's runtime regression gate remains sequenced after the branching contract and matched design evidence.

## Decisions

- The destination remains the creator's curated cube/decks; comprehensive Magic or Commander coverage is not the objective.
- Steal Phase's proven invariants, not its repository shapes: exact object incarnation and LKI, typed card meaning, proposed-event replacement flow, explicit legal interaction, and viewer-safe projections.
- Use immutable, versioned `ContentPack` definitions plus compact dense mutable `MatchState` facts.
- Prefer offline compilation into checked-in typed IR over runtime natural-language parsing.
- Replace flat enumerate-then-clone-and-validate action lists with structured offers that are legal by construction and admit structured policy decoders.
- The search contract likely combines safe snapshot forks outside a rollout with dense transactional execution and mark/rollback inside it.
- Benchmark compact full clone, compact clone plus undo, and dense page-COW fork plus undo at realistic worker x actor x rollout load. Decide on rollout throughput and peak RSS, not clone latency alone.
- Use a readable reference reducer and optimized executor as differential oracles. Explore Phase as a pinned conformance oracle and opponent pool, not as the primary training backend.
- Verification should become mechanical: conformance CI, gap-analysis worklists, property/metamorphic/differential/fuzz testing, and a content-change to kernel-change ratio that can trigger a redesign kill decision.

## Evidence

- `docs/research/semantic-kernel.md`
- `docs/research/manabot-vs-phase.md`
- Phase comparison pinned to phase-rs commit 553b97bd
- Existing capability and card-pool audits under `wave/rules/`

## Open tensions

- Prove the acceptance-slice offline compiler boundary without card-name dispatch.
- Preserve dense-state rollout speed while making branching exact and safe.
- Keep human experience, learning observations, and search snapshots as separate viewer-safe projections of one authoritative match.
- Keep revision-bound performance and allocation receipts aligned with the exact post-rebase source they measure.
