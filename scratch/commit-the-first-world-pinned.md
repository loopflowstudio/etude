# Commit the First World-Pinned Arena Rating Run

## Problem

Etude has a reviewed world-pinned arena contract but no production result on
its scale. INT-18 must turn the INT-6 instrument into retained evidence: the
five frozen code-only anchors and the frozen dPUCT-32 challenger play the full
declared production schedule, every Command trace replays, and the output
contains ratings, every payoff cell, graph connectivity, and paired-deal
uncertainty. This advances the Intelligence wave's R4 arena result and the
Search Teacher and Student Arena KR requiring the first committed production
rating run.

The result must preserve historical identity, not merely reproduce similar
algorithms under current source. Current main has moved beyond the INT-6
release: the frozen contract fails closed against both current arena/player
source and the current native extension. The production run therefore executes
the authenticated frozen release in a temporary non-worktree overlay while the
current branch owns orchestration, verification, derivation, and retention.

The requested exact-range versus uniform-determinization comparison is not
silently substituted. Retained INT-7 checkpoints now load as real w2 policy
bytes, but the registered INT-9 contract still names its likelihood checkpoint
as `unresolved_required`, INT-6 has no exact-range registration or stateful
semantic lifecycle, and the exact likelihood path is outside the arena cap at
its present cost. INT-18 retains that precise evidence wait unless an already
registered, byte-identical, cap-admissible seam appears.

## The demo

From the authenticated frozen overlay, run the historical anchor and challenger
commands, then envelope their verified outputs from this branch:

```bash
uv run python experiments/runners/run_skill_arena.py freeze-anchors \
  --contract experiments/contracts/int-6-skill-arena-v1.json \
  --out-dir <run>/anchor --profile smoke
uv run python experiments/runners/run_skill_arena.py challenge \
  --contract experiments/contracts/int-6-skill-arena-v1.json \
  --anchor-artifact <run>/anchor/manifest.json \
  --candidate experiments/candidates/int-6-dpuct-32-w4-v1.json \
  --out-dir <run>/challenge --profile smoke --verify
uv run python <current-worktree>/experiments/runners/run_int18_arena_rating.py \
  --stage smoke --out-dir <run>
```

Together they play all 15 cells of the six-player smoke cohort on both seat
legs of both frozen deals, verify every retained Command trace, and print the
manifest identity plus the paths to `rating.json`, `payoff-matrix.json`,
`connectivity.json`, and `paired-deal-uncertainty.json`. Replacing `smoke` with
`production` runs the same closed path over the 24-deal production schedule.

## Approach

Add one INT-18 orchestration and verification runner. It does not modify the
frozen INT-6 contract, registrations, arena key, rating prior, schedules, or
player code.

1. Authenticate the exact frozen inputs in the repository object database:
   contract `experiments/contracts/int-6-skill-arena-v1.json` at file SHA-256
   `fc9cb76c0d80ad64951455ac6fede94b1355f383dde2f2964d0e578f62671a71`,
   five anchor identities, challenger identity
   `6b1eb7855864bd81cfe1995fa98ab104b2c2e18b721ff37989df7873183d3904`,
   and frozen execution commit
   `76d0834797316c3b6e153ed10e5fadd146a8980a`. At that commit the arena source
   bundle equals the contract's
   `b722c8119ebb31fce137231e1434a379059cfd782c734d34b506db0e5ceefe76`
   and the dPUCT source bundle equals the registration's
   `7236414edec8be6d1013cf14d87098a501cd7e504964a27a8dcc58e60972d7fe`.
2. Materialize that commit with `git archive` into a fresh temporary directory,
   never a git worktree. Build its cp312 native extension through the pinned
   root `uv` environment and require the contract's extension SHA-256
   `18d04fe651eddf958da9ebbe0024fca762ffee91dd17c08c6b17f41d1b065504`
   before any game starts. The temporary overlay is execution isolation, not a
   new branch or source of truth.
3. Run the historical INT-6 CLI unchanged: first `freeze-anchors`, then
   `challenge` with `int-6-dpuct-32-w4-v1.json`, using the same profile for both.
   Run both historical `--verify` paths after generation. Copy only the closed
   verified artifacts back to the requested output directory.
4. Derive an INT-18 envelope without changing game evidence. It binds the
   frozen contract, execution commit, exact source files, built extension,
   anchor/challenger identities, the anchor and challenge manifests, closed
   file receipts, and the current derivation source. It emits:
   - the historical rating and complete 15-cell payoff matrix;
   - explicit graph connectivity: six nodes, 15 observed edges, one component,
     random anchor reachable from every player, no missing expected cell;
   - paired-deal uncertainty copied and indexed from the registered global
     deal-block bootstrap, including every player rating and every rating
     difference interval, plus per-cell paired-block counts;
   - legality, replay, registration, runtime, profile, competency, latency,
     throughput, memory, and resource-cap receipts;
   - an interpretation that reports the dPUCT rating but preserves INT-6's
     `rated_not_promotion_eligible / incumbent_not_in_cohort` disposition.
5. Run smoke first. Production may start only if smoke is byte-closed,
   complete, connected, replay-clean, identity-clean, and under the same
   cumulative cap logic. Production is 15 cells x 24 deal blocks x two seat
   legs = 720 games, with 2,000 global-deal bootstrap replicates and the frozen
   100-seed competency schedule.
6. Retain the verified production result content-addressed under
   `experiments/data/int-18-first-world-pinned-arena-v1/sha256/<manifest>/` and
   write the timeless result report. Verification must be generation-free and
   leave the retained tree byte-identical.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|------------------|
| Can the frozen INT-6 CLI run directly on current main? | No. Preflight reports arena source `90ec2488...` instead of frozen `b722c811...`; random/flat-MC source is `c9272652...` instead of `f40b3d54...`; dPUCT source is `42fba442...` instead of `7236414e...`; current engine source/extension are `3ca84515...`/`c95a85bb...` instead of `6acc1f17...`/`18d04fe6...`. | Execute an authenticated frozen overlay. Never rewrite the frozen contract or relabel current source as the frozen players. |
| Is an exact source closure still recoverable? | Yes. Remote branch history identifies commit `76d08347...`; committed-source replay matches the frozen arena and every anchor source digest, and uniquely matches the dPUCT registration source. Contract and candidate bytes are available at that commit. | Pin `76d08347...` as the execution release and verify each committed file before building. |
| What is the smallest identity-preserving end-to-end proof? | The frozen smoke profile over all six players is 15 complete cells x two deals x two seat legs = 60 games. A candidate-only pair would not prove anchor connectivity or the complete-matrix path. | Smoke uses the entire six-player cohort at the smaller frozen schedule; no development pair is promoted into evidence. |
| Does the existing estimator retain the required uncertainty? | Yes. It fits seat-aware Gaussian-MAP Bradley-Terry and bootstraps whole global deal blocks. The rating artifact contains intervals for every player and every pairwise rating difference; payoff cells retain paired sweep/split counts. It does not emit an explicit connectivity artifact. | Re-index existing uncertainty without refitting under a new model, and add a deterministic connectivity derivation. |
| Are exact-range likelihood bytes now present? | Real retained w2 checkpoints exist, including `visit_teacher_root-seed-197.pt` at SHA-256 `06794769...` and 428,679 bytes, and load through `FrozenPolicyLikelihood`. The INT-9 production contract does not select or lock any of them; its required artifact remains unresolved. | A loadable byte is not a registered player. Do not choose a convenient checkpoint after the fact. Retain the exact unresolved registration evidence. |
| Could exact-range still fit the frozen arena cap if a checkpoint were selected? | Not on the current path. A 128-world sample projects about 60.4 seconds for one 10,832-world likelihood update; a real one-game, two-simulation probe was interrupted after six minutes inside the first per-world update. INT-6 also lacks the player's semantic lifecycle hooks and registration schema. | Exclude exact-range/uniform from admission with typed `evidence_wait`; record the load/hash and capacity receipts, with no neutral-likelihood or authored-range substitute. |
| Is production compute bounded? | Prior retained runs executed 112 current-arena smoke games in 0.120 wall hours and 544 larger-cohort games in 0.672 wall hours. INT-18 has 720 games and the frozen INT-6 cap is 16 wall hours / 64 core hours / four workers / 4 GiB. | Preserve pre-stage cap checks, four workers, and append-only accounting. Abort before a stage whose conservative projection exceeds the cap. |
| Can later code changes verify the frozen evidence honestly? | Yes, if generation provenance and current verifier provenance are separate and the verifier never regenerates games. This is the established INT-7 pattern. | Manifest both closures; verify historical artifacts under the release overlay and the retained envelope under current derivation code. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Edit the INT-6 contract and registrations to current hashes | Simplest local execution. | It creates new players and a new runtime while falsely keeping the frozen arena-v1 identity. Frozen evidence is never renamed or mutated. |
| Run current arena code under an additive contract | Preserves the rating model and schedule and follows the INT-8 diagnostic pattern. | Appropriate for non-admission diagnostics, but INT-18 specifically owes the first production result for the frozen players. Current random, flat-MC, dPUCT, and engine bytes have all drifted. |
| Run only dPUCT versus one anchor as the smoke | Fastest gameplay probe. | It does not exercise the complete matrix, population connectivity, or global-block rating path and remains a development pair. |
| Select the retained INT-7 Teacher-root seed-197 checkpoint for exact-range | Makes the player constructible. | INT-9 did not preregister that selection, and present likelihood materialization violates the production cap. This would turn availability into post-hoc authority. |
| Use neutral likelihood or a static authored belief | Cheap and already testable. | It erases the measured belief treatment and is explicitly forbidden substitution. |

## Key decisions

- Frozen player identity wins over implementation convenience. The generation
  process runs the authenticated historical bytes; current code only
  orchestrates and verifies.
- Smoke is a complete cohort at the frozen smoke schedule, not a pairwise
  shortcut. It proves the same matrix and graph that production will scale.
- Connectivity is an explicit artifact and hard gate, not an inference from a
  successful fit.
- Paired-deal uncertainty remains the preregistered global-deal-block
  bootstrap. No post-result confidence method is introduced.
- dPUCT receives a rating but cannot be promoted because arena v1 has no
  same-compute incumbent in its frozen cohort. The report says so prominently.
- Exact-range/uniform is a fail-closed evidence wait in this Task unless the
  already-registered byte and execution path both reproduce within cap. The
  current retained checkpoints are recorded as candidates, never silently
  adopted.
- The successful outcome is a boringly trustworthy first scale: a developer
  can inspect any rating, traverse to its payoff cells and deal blocks, and
  replay the Commands that produced them.
- The failure condition is catching identity drift before play or any replay,
  connectivity, cap, or closed-set mismatch after play. Partial output is not
  a rating result.

## Scope

- In scope: exact frozen INT-6 anchors and dPUCT-32; smoke then production;
  temporary release overlay; ratings; complete payoff matrix; connectivity;
  paired-deal uncertainty; competencies; matched-root systems profiles;
  legality, viewer-safety, replay, identity, cap, and closed-set receipts;
  content-addressed retention; exact-range/uniform typed evidence wait.
- Out of scope: a new planner family, learned priors or values, a new arena
  scale, Rules changes, optimized possible-world materialization, selecting or
  training a new likelihood checkpoint, supervised beliefs, content widening,
  adaptive opponents, promotion without an incumbent, and any strength claim
  from development probes.

## Done when

The smoke command exits successfully with 60 verified games, 15 payoff cells,
one six-node connected component, zero missing cells, zero integrity/replay
failures, and frozen source/runtime/registration identities. The production
command then exits successfully with 720 verified games under the frozen cap,
2,000 successful global-block bootstrap replicates, the same 15 complete cells
and connected graph, all required evidence files, and a generation-free
verification receipt.

The production manifest and report are committed under the content-addressed
retention path. They advance the wave measure: "R4 — first strength results"
and the Project KR: "The world-pinned arena commits its first production
rating run over the frozen anchors and the dPUCT-32 challenger, with ratings,
the full payoff matrix, connectivity, and paired-deal uncertainty retained."
The manifest separately states whether the Belief-Aware Play arena KR remains
open and why.

## Measure

Before generation, record contract/candidate file hashes, committed source
receipts, native extension hash, arena key, all six registration identities,
deal/competency/bootstrap seed digests, and projected resource use.

For smoke and production, record:

- games, cells, deal blocks, both seat legs, decisions, and trace bytes;
- zero illegal actions, private exposures, root mutations, offer-binding or
  Command-fabrication failures, truncations, and replay mismatches;
- Bradley-Terry ratings, seat effect, residuals, fit convergence, log loss,
  Hessian condition, and 2.5/50/97.5 percentiles from global-block bootstrap;
- all 15 observed payoff cells, per-seat results, paired sweeps/splits, and no
  missing expected cell;
- connected components and random-anchor reachability;
- S1-S5 competencies and matched-root p50/p95 latency, nodes/second,
  decisions/second, playout caps, and peak RSS;
- wall/core hours, workers, artifact bytes, and cap margins;
- exact-range candidate checkpoint load/hash evidence, registered-artifact
  status, measured likelihood materialization cost, and the typed inclusion or
  evidence-wait decision.

## Review correction: authenticate current verification

The frozen execution closure remains exactly unchanged: release commit
`76d0834797316c3b6e153ed10e5fadd146a8980a`, anchor manifest
`cc83abb7...`, challenge manifest `998ad75d...`, and their complete gameplay
tree. The correction is confined to the current, non-generating INT-18
envelope.

`experiments/runners/run_int18_arena_rating.py` will derive a
`current_verifier` receipt containing its repository-relative path, exact file
SHA-256, role, and authority boundary. Both `int18-result.json` and
`int18-manifest.json` bind that receipt. `--verify-only` recomputes the live
runner hash and requires exact equality with both retained copies while
continuing to perform no replay and no game generation. The content-addressed
retention directory and report move to the new envelope manifest identity;
the historical anchor/challenge files are byte-identical before and after.
The result-time exact-range evidence wait remains a closed, hashed artifact;
verification authenticates its retained receipt and status rather than
re-deriving it from a later mutable INT-9 contract.

The implemented runner receipt is SHA-256 `47976d1c...`; the corrected
envelope identity is `af0c3f56...`. These identify only current derivation and
verification. They do not replace or relabel the frozen execution commit,
contract, extension, player registrations, anchor manifest, or challenge
manifest.

Done when focused verification reports the authenticated current verifier
SHA-256 and `no_generation=true`, the anchor/challenge manifests and aggregate
gameplay-tree digest remain unchanged, and the retention/report closure cites
the new envelope identity.
