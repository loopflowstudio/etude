# Open Work — 2026-09-13

## Evidence boundary

- Human-review scan from `/Users/jack/src/etude.agent` on 2026-09-13.
- Worktrees: `lf wt list --format json`.
- Current authored PRs: live GitHub query; missing GitHub data would be
  `unknown`, but the query succeeded.
- Branch ancestry uses the checkout's current `origin/main`; GitHub PR state
  is authoritative for squash-merged branches.
- Wave/task state: `lf ls --json`, `lf roadmap --json`, and focused
  `lf status` checks after the repository PM migration.
- No ship, abandon, prune, or wave mutation has been dispatched yet.

## Pass 1: Clear the decks

| Item | Wave | Kind | Age | Status | Recommendation | Why |
|---|---|---|---:|---|---|---|
| ~~`jack-heart/search-learning-architecture-2` / PR #179~~ | Intelligence | worktree + PR | 24d | `lf ship` dispatched 2026-09-23 | **ship — dispatched** | The only open authored PR was complete and its Task explicitly awaited settlement. |
| `jack-heart/materialize-possible-world-batches-without` / ETU-55 | Rules | dirty worktree, no PR | 25d since edit | 767 tracked additions plus provider/tests/receipts; one-construction RUL-13 evidence exists, but the untracked INT-17 v2 run preceded the required commit | **ship-partial** | Land the prepared materializer and valid provider evidence; exclude/defer the inadmissible calibration run rather than losing the provider fix or laundering the result. |
| `jack-heart/run-the-production-visit-teacher` / ETU-31 | Intelligence | dirty worktree, no PR | 25d since edit | Contract/coordinator/tests exist; review says not publishable until clean-host run and retained verify-only result. The former uv/PM inspection blockers no longer reproduce. | **ship** | Resume the full Task through its required production result; do not publish an infrastructure-only slice. |
| `jack-heart/resolve-live-advice-against-the` / ETU-21 | Intelligence | dirty worktree, no PR | 25d since edit | Alternate `edr1`/neutral-likelihood implementation, 2 commits plus 532 tracked additions | **abandon** | Superseded by merged PR #177, which shipped canonical `ed2`, the pinned tracked posterior, authenticated background advice, and live/Study parity. |
| `jack-heart/ai-assisted-play-north-star` | — | dirty worktree, no PR | 57d | Seven modified charter files plus scratch design on a branch 37 commits behind | **abandon** | Its product outcome landed in PR #147 and was subsequently refined in current Wave memory. |
| `ops/open-work` | — | dirty worktree, no PR | 57d | Only two untracked July review artifacts; branch is 37 commits behind | **abandon** | Historical review snapshot is superseded by this scan. |
| `jack-heart/agent` | — | current worktree + remote, no PR | 0d | Six commits ahead of main: PM migration and current Wave memory/goal corrections | **ship** | These are current repository authority and durable learnings, not scratch-only work. Dispatch only after this review artifact is checkpointed. |
| 109 merged local branch-only refs | mixed | local branches, no worktree | 57–66d | Each has a merged PR, is an ancestor of main, or is patch-equivalent to main | **abandon** | Local lifecycle residue; no unique unmerged outcome is identified by the scan. Exact set is below. |
| 23 stale remote refs (excluding `jack-heart/d1`) | mixed | remote refs, no open PR | 64–66d | 21 merged PRs and 2 closed early PRs whose tips are ancestors of main | **prune** | Older than 60 days, no worktree dependency, and no open PR. Exact set is below. |
| Four unpublished/superseded local refs | mixed | branch-only | 57–59d | Backups or closed predecessor PRs; no worktree | **abandon** | PR #80 landed, current Wave memory supersedes the backup, PR #160 was closed, and PR #90 was superseded by later structured-choice work. |
| `jack-heart/d1` | Intelligence | local + remote branch, no PR | 64d | Unique 8,191-line exp-12 result; report says complete and records a non-additive information × continuation diagnosis | **discuss** | Decide whether this frozen research result should be salvaged and shipped or deliberately abandoned; pruning now would erase its only remote copy. |
| `proto/dense-board-rig` | — | local-only branch | 58d | Nine unique commits; dense-board E2E plus visual-system work | **discuss** | Valuable-looking but unreviewed prototype with no PR or worktree: ship it intentionally or abandon it. |
| `proto/semantic-utilities` | — | local-only branch | 58d | Eleven unique commits; semantic colors, contrast validator, docs, and visual work | **discuss** | Valuable-looking but unreviewed prototype with no PR or worktree: ship it intentionally or abandon it. |

### Exact stale remote prune batch

`jack-heart/d1` is deliberately excluded pending its own decision.

```text
c0-decision-profile
c1-interactive-deck
competency-suite
conformance-audit
exp-07-expert-iteration
gui-effective-pt
gui-reactivity-fix
gui-stops
jack-heart/paper
paper-update
rules-stage-1
rules-stage-2
rules-stage-3
rules-stage-4
search-wave-roadmap
advisor-keep-doings
d1-launch-spec
exp-06-newworld
exp-10-value-gate
exp-11-curriculum
index-correction
restore-restructure
understanding-2026-07-10
```

### Exact merged local branch abandon batch

```text
advisor-keep-doings
avatar-team-sealed-north-star
c0-decision-profile
c1-interactive-deck
competency-suite
conformance-audit
d1-launch-spec
exp-06-newworld
exp-10-value-gate
exp-11-curriculum
gui-effective-pt
gui-reactivity-fix
gui-stops
index-correction
jack-heart/add-deterministic-matchstate-hash-and
jack-heart/add-semantic-kernel-conformance-oracle
jack-heart/add-structured-attacker-offers-and
jack-heart/add-structured-attacker-offers-and-2
jack-heart/add-versioned-visual-references-to
jack-heart/build-a-semantic-policy-prototype
jack-heart/build-retry-before-reveal-study
jack-heart/build-the-first-exact-range
jack-heart/build-whole-rollout-branching-benchmark
jack-heart/build-whole-rollout-branching-benchmark-2
jack-heart/certify-protocol-v1-across-rust
jack-heart/certify-protocol-v1-across-rust-2
jack-heart/compile-two-deck-card-semantics
jack-heart/compile-two-deck-card-semantics-2
jack-heart/complete-two-deck-typed-ir
jack-heart/default-manabot-loopflow-tasks-to
jack-heart/define-viewer-safe-study-artifact
jack-heart/disambiguate-structural-encoder-optimization-capacity
jack-heart/document-metta-observation-robustness
jack-heart/establish-the-game-experience-proof
jack-heart/establish-the-game-experience-proof-2
jack-heart/expose-a-versioned-belief-conditioned
jack-heart/expose-viewer-safe-semantic-program
jack-heart/freeze-one-exact-matchup-asset
jack-heart/generate-coverage-gaps-and-kernel
jack-heart/generate-coverage-gaps-and-kernel-2
jack-heart/implement-compact-clone-plus-undo
jack-heart/implement-dense-page-cow-driver
jack-heart/implement-objectref-incarnation-and-lki
jack-heart/intelligence-overnight
jack-heart/land-protocol-v1-envelope-through
jack-heart/make-trigger-and-sba-stabilization
jack-heart/materialize-viewer-safe-policy-and
jack-heart/mcts-teacher-prototype
jack-heart/measure-and-stress-study-fork
jack-heart/measure-and-stress-study-fork-2
jack-heart/paper
jack-heart/prototype-belief-to-strategy-comparison
jack-heart/prototype-belief-to-strategy-comparison-2
jack-heart/prototype-structured-offers-for-priority
jack-heart/prototype-structured-offers-for-priority-2
jack-heart/prototype-structured-policy-decoder-and-2
jack-heart/prove-contentpack-sharing-and-clone
jack-heart/prove-one-command-clean-machine
jack-heart/prove-release-matrix-accessibility-across
jack-heart/prove-search-state-fork-and
jack-heart/prove-snapshot-plus-event-recovery
jack-heart/prove-structural-semantic-representations-on
jack-heart/prove-terminal-release-stack-prompt
jack-heart/provide-exact-study-fork-and
jack-heart/provide-exact-study-fork-and-2
jack-heart/put-a-pilot-and-watcher
jack-heart/re-run-contentpack-allocation-receipt
jack-heart/re-run-contentpack-allocation-receipt-2
jack-heart/refresh-w2-208-allocation-receipt
jack-heart/render-combat-and-turn-transitions
jack-heart/render-one-spell-sequence-from
jack-heart/render-one-spell-sequence-from-2
jack-heart/reorganize-intelligence-wave
jack-heart/replace-canonical-snapshot-with-witness
jack-heart/replace-the-advice-fixture-with
jack-heart/replace-w2-179-local-diagnostics
jack-heart/reproducible-branching-receipt-digest
jack-heart/resilient-search-datagen
jack-heart/rewrite-etude-around-ai-assisted
jack-heart/route-curated-mutations-through-proposed
jack-heart/route-curated-mutations-through-proposed-2
jack-heart/route-etude-managed-workers-to
jack-heart/run-a-bounded-search-teacher
jack-heart/run-a-bounded-search-teacher-4
jack-heart/run-a-bounded-search-teacher-teacher1-evidence
jack-heart/run-a-bounded-search-teacher-teacher1-run
jack-heart/run-four-arm-semantic-transfer
jack-heart/run-one-complete-authored-match
jack-heart/run-one-complete-authored-match-2
jack-heart/run-the-first-visit-based
jack-heart/run-the-first-visit-based-complete-the-visit-teacher-iteration
jack-heart/run-the-first-visit-based-run-the-registered-production-iteration
jack-heart/search-learning-architecture
jack-heart/separate-contentpack-and-measure-clone
jack-heart/train-the-first-belief-conditioned
paper-update
recovery/rules-memory-wave-mutate-20260715
rename-etude
rescue-set-seed
restore-restructure
rules-stage-1
rules-stage-2
rules-stage-3
rules-stage-4
search-wave-roadmap
study-wave
understanding-2026-07-10
worktree-agent-a363df8210ef05456
worktree-agent-aa323507a808e4181
```

### Exact four-branch superseded local batch

```text
backup/pr80-local-pre-reconcile
backup/wave-mutate-17ff49f
jack-heart/measure-played-release-and-training
jack-heart/prototype-structured-policy-decoder-and
```

The scan originally grouped five refs here; evidence review moved
`jack-heart/d1` into its own discussion row. The executable batch is therefore
the four refs above.

## Pass 2: Wave audit

The Loopflow inventory reports many “active” task records (Game 19,
Intelligence 20, Rules 34, Study 2), but the current Linear roadmap has only
five incomplete tasks: Game 1, Intelligence 3, Rules 1, Study 0. Most apparent
capacity is settled lifecycle residue with missing historical worktrees.

| Wave | Vision progress | Recent activity | Recommendation |
|---|---|---|---|
| Game | 9/16 current Project KRs hold. Shared pilot/watcher play, canonical live advice, and Retry/Study exist; the next observed player-facing gap is mana display. | Product path shipped through PR #177; current ETU-14 duplicates that merged outcome. No new Game implementation since July, only current PM/memory correction. | **continue narrowly** — close the duplicate ETU-14 lifecycle, then take one observed player gap; do not recreate generic advice or Retry work. |
| Intelligence | 12/19 KRs hold. The belief/search substrate is strong, but the results ladder still lacks the production visit-teacher result and has one green unlanded architecture PR. | PR #179 is green but idle for 24d; ETU-31 and the superseded ETU-21 worktrees last moved 25d ago. | **continue, with lack of action called out** — land #179, retire ETU-21, and run ETU-31 before opening another lane. |
| Rules | 21/21 current Project KRs hold, but the prepared materializer needed by the live belief workload remains unshipped. | Valid prototype/provider evidence was produced by 2026-08-20; no later implementation activity. | **continue only through ETU-55, then reassess** — this is a narrow provider handoff, not permission for more benchmark substrate. |
| Study | Explicitly folded into Game; no current Projects or incomplete Tasks. Existing unavailable Project records are historical lifecycle residue. | No independent delivery expected. | **archive** — repository convention already uses `wave/archive/`; preserve memory and move no new work into Study. |

## Current punch list

- Confirm or reject each Pass 1 row; shipping dispatches are fire-and-forget.
- Decide whether `jack-heart/d1` is a result worth salvaging.
- Decide whether the two local visual prototypes should ship or be abandoned.
- Confirm the four Wave dispositions; Wave files will not be mutated by this
  review.

## Dispatched ships

- 2026-09-23: `(cd /Users/jack/src/etude.search-learning-architecture &&
  nohup lf ship >
  /Users/jack/src/etude/scratch/ship-logs/jack-heart/search-learning-architecture-2.log
  2>&1 &)`

  Log: `/Users/jack/src/etude/scratch/ship-logs/jack-heart/search-learning-architecture-2.log`
