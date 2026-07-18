# Close Selected-Match Public-Commitment Identity Gaps

## Problem

The fixed 132-Command UR Lessons versus GW Allies match is already one exact
managym authority across Etude live play, headless execution, and persisted
canonical replay. Exact-range belief tracking still cannot consume that entire
trace: one selected public action changes the opponent's canonical unseen pool
without a managym-owned `PublicCommitment`, so `BeliefTracker` fails closed
before terminal.

This blocks the Playable Curated World provider-closure KR and Intelligence's
later live-belief/calibration work. The fix must preserve the authority split:
`DecisionFrame` identifies the action before commitment,
`TransitionReceipt` publishes the accepted public identity, and manabot only
conditions on that identity. Etude labels, action positions, hidden authority
hands, and replay adapters must not reconstruct it.

The pre-change audit ran the exact-range tracker and the tracker transport
contract against the immutable fixed source
`conformance/authored-match-v1/release-stack-ur-vs-gw-seed-0.json`:

- 132 Commands reach terminal.
- 60 selected offers already carry commitments: 23 `cast`, 16 `play_land`,
  and 21 `pass_priority`. The other 72 are deliberately unadmitted prompt
  actions.
- The pending-cast mechanism correctly carries the earlier `cast` identity
  across all seven delayed target commitments at Etude revisions 17, 22, 28,
  39, 67, 111, and 121.
- The sole exercised `RulesProviderGap` is Etude revision 29, actor 0,
  `DISCARD_THEN_DRAW/select_card`: `Island` leaves the unseen pool, the public
  hand size changes from 3 to 4 after the discard and two hidden draws, and the
  receipt has no commitment.
- The full exact-range state machine completes 29 updates and then raises
  `hidden-pool exit has no canonical public commitment identity` at that
  revision.
- The adjacent discard root has 484 compatible worlds. A normal materialized
  world preserves the source viewer Observation, but the likelihood mode fails
  with `cannot refresh the acting non-priority prompt`; without regeneration,
  its cloned offers still name the actual hand rather than the hypothetical
  hand.

The public identity is rules-safe. The current Comprehensive Rules define
discard as moving a card from hand to graveyard (CR 701.9a), graveyards as
public zones (CR 400.2), and learn as the optional discard-then-draw action
(CR 701.48a). The card name becomes public only when the command commits; it
does not belong in the non-acting viewer's pre-command Observation. Source:
[Magic Comprehensive Rules, 2026-06-19](https://media.wizards.com/2026/downloads/MagicCompRules%2020260619.pdf).

## The demo

Run `./scripts/verify-public-commitment-parity`. It prints
`RUL11_PUBLIC_COMMITMENT_OK commands=132 commitments=62 gaps=0` and confirms
that the live, persisted-replay, and materialized-hypothesis identity-stream
digests match, with atomic negative proofs and the immutable RUL-9 artifact
`498df1…` named as reused evidence.

## Approach

Land one complete provider slice with four connected changes.

1. Extend managym's existing `PublicCommitment` vocabulary with exactly two
   variants:

   - `Discard { card: String }`
   - `DeclineDiscard`

   `structured_search_offers` emits them only when the current
   `ActionSpaceKind` is `DiscardThenDraw`: `Action::SelectCard` maps to
   `Discard` using the authoritative `CardId` and managym card definition;
   `Action::Decline` maps to `DeclineDiscard`. The identically shaped
   `SelectCard` action in `LookAndSelect` remains unadmitted. The non-acting
   viewer still receives no DecisionFrame, and the accepted receipt exposes
   only the canonical kind and card name—never a `CardId`, object reference,
   offer/action position, hand contents, or world index.

2. Generalize the likelihood materialization mode from “refresh opponent
   priority” to “refresh opponent commitment decision,” with no compatibility
   adapter. It continues to rebuild `Priority` from the hypothetical hand and
   additionally rebuilds `DiscardThenDraw` through
   `suspended_decision_action_space()` after hidden-zone reassignment. Every
   other acting prompt still returns typed `UnsupportedActingPrompt`. A world
   without the observed discarded card has no matching commitment offer and
   therefore receives zero likelihood; a world containing it exposes a stable
   `Discard { card }` group. The retained source remains immutable.

3. Make `BeliefTracker.observe()` transactional. Parse and validate the
   provider identity, re-read the next canonical Observation/space, derive a
   local pending commitment, and validate all public transport facts before
   calling the likelihood model or assigning tracker state. Compute the
   conditioned posterior, transported posterior, compatible prior, stats, and
   record as locals; publish them together only after every fallible step
   succeeds. `discard` is a valid named exit and `decline_discard` is a valid
   likelihood-only commitment. Unsupported kinds and card/pool mismatches
   raise `RulesProviderGap` with posterior/prior digests, pending identity,
   stats, and records unchanged. Tracker observation is read-only, so the
   supplied match witness and event cursor also remain unchanged.

4. Add a checked provider-closure receipt and verifier. Re-execute the fixed
   Commands through the Etude live WebSocket path, load the persisted canonical
   replay produced by that run, and execute its persisted Commands through
   managym. Canonically hash rows of
   `(ordinal, actor, command_id, semantic revisions, public_commitment)` and
   require exact stream equality. Run exact-range trackers for both viewers
   with an explicitly non-calibration identity-audit likelihood, producing 132
   records per viewer and zero gaps. At the revision-29 retained root,
   materialize the first canonical hypothesis containing `Island`, regenerate
   the discard decision, execute its normal semantic Command, and require the
   same `Discard { card: "Island" }` receipt while the source witness, event
   cursor, and viewer Observation remain unchanged. The receipt binds the
   immutable authority-source hash, command-tape hash, identity-stream hash,
   semantic schema, and RUL-9 measurement artifact hash; it does not rerun or
   rewrite RUL-9 measurements.

Adding tagged commitment variants changes the semantic contract vocabulary,
so increment `SEMANTIC_DECISION_VERSION` to 4 in Rust and Python and update the
active, still-unlocked exact-range contract fingerprint. Frozen RUL-9
measurement and derivation files remain byte-for-byte unchanged. Persisted
replay remains a Command source, not a receipt authority: replayed managym
execution must reproduce the commitments.

## Cross-wave sequencing

RUL-11 and INT-15 may implement in parallel, but their evidence cannot land in
either order. RUL-11 changes `managym/decision.py` and
`managym/possible_worlds.py`, two members of `ADVISOR_SOURCE_PATHS`, and
therefore changes both their individual runtime ABI hashes and the aggregate
`CodeSourceArtifact.source_bundle_sha256`. An INT-15 exact-head fixture hashed
before those provider changes land would immediately fail closed as
`advisor_artifact_mismatch` after RUL-11.

The required integration order is:

1. RUL-11 implements, validates, and lands its semantic/provider changes
   first. Parallel INT-15 code work may continue, but INT-15 must not perform
   final exact-head fixture generation or source hashing yet.
2. INT-15 rebases onto the landed RUL-11 commit, then regenerates and validates
   only its new additive exact-head fixture from that post-RUL-11 source tree.
3. INT-15 lands only after the shared integration gate below is green. Neither
   Task regenerates, rewrites, or relabels the frozen INT-12 fixture
   `protocol/fixtures/advice-belief-conditioned-v1.json`, its measurement
   `experiments/data/int-12-belief-strategy-advisor-measurement-v1.json`, the
   RUL-9 evidence, or any other frozen evidence.

The ordered integration gate binds these shared contracts:

| Contract or fingerprint | Required integrated state | Gate |
|-------------------------|---------------------------|------|
| Semantic decision ABI | Rust and Python both report `SEMANTIC_DECISION_VERSION = 4`; `DecisionFrame` and `TransitionReceipt` remain schema-parallel. | `tests/semantic/test_decision_contract.py` and the focused debug Rust public-commitment tests pass. |
| Possible-world ABI | `POSSIBLE_WORLD_SPACE_VERSION` remains `1` because enumeration/query identity is unchanged, while the runtime SHA-256 of `managym/possible_worlds.py` reflects the new bounded materialization behavior. | `tests/belief/test_range.py`, `tests/belief/test_tracker.py`, and `tests/sim/test_conditional_search.py` pass. |
| Exact-range contract | The active INT-9 `semantic_decision_version`, `possible_world_space_version`, `engine_source_sha256`, and `int9_source_sha256` fingerprints match the RUL-11 runtime. | `tests/sim/test_exact_range_runner.py` passes. |
| Advice source artifact | INT-15's new fixture binds the post-RUL-11 ten-file `ADVISOR_SOURCE_PATHS` bundle, including `managym/decision.py`, `managym/possible_worlds.py`, belief range, conditional search, search runtime, Study, and advice provider sources. Deliberate source-hash perturbation still returns only `advisor_artifact_mismatch`. | `tests/etude/test_belief_advisor.py` passes, including `test_compute_seed_and_artifact_perturbations_fail_closed`. |
| Frozen evidence boundary | Existing INT-12 and RUL-9 fixture, measurement, and receipt bytes are unchanged; runtime drift is explicit rather than repaired by rewriting historical evidence. | `tests/sim/test_conditional_search.py::test_frozen_fixture_payload_is_stable_and_runtime_drift_is_explicit` passes. |

This is an ordering constraint only; it adds no RUL-11 commitment variants,
consumer behavior, fixtures, or feature scope.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|------------------|
| Which commitments are actually exercised? | The fixed trace has 23 casts, 16 land plays, 21 priority passes, and 72 unadmitted actions. Only two `DiscardThenDraw` actions share the missing boundary: discard at revision 29 and decline at revision 40. | Add only `Discard` and `DeclineDiscard`; do not publish combat, target, scry, waterbend, look/select, or activated-ability identities. |
| Are delayed targeted casts additional gaps? | No. Seven cards leave the hidden pool on the later target Command, and the existing pending `cast` identity explains each exactly. | Retain pending-cast transport; do not duplicate the cast identity on target choices. |
| Is naming the discarded card viewer-safe? | Yes. CR 701.9a moves it to the public graveyard defined by CR 400.2. The identity is emitted after the accepted transition, while the non-actor's pre-command Observation has no DecisionFrame or hand identity. | Derive the name inside managym from the selected `CardId`; allowlist the receipt payload and test pre-command privacy. |
| Can current possible-world materialization evaluate a discard choice? | No. At the 484-world revision-29 root, `refresh_opponent_priority=True` rejects the non-priority prompt. Preserving the cloned prompt leaves real-hand action bindings on a hypothetical hand. | Rebuild only `Priority` and `DiscardThenDraw` from branch state; all other prompt kinds still fail closed. |
| Does the current tracker fail atomically for every bad identity? | The observed missing-identity gap happens before assignment, so its belief digest and records remain unchanged. A non-null mismatched identity is conditioned before transport validation, however, and can change tracker state first. | Validate receipt identity and transport before likelihood; stage all updates locally and commit once. Add unsupported and mismatched negative proofs. |
| Must a real policy checkpoint be used? | No locked selected-match likelihood checkpoint exists, and calibration is explicitly out of scope. Provider closure concerns semantic identity and exact transport, not policy quality. | Use a labeled identity-audit likelihood for the terminal verifier and exercise real materialization/grouping at the new discard boundary. Do not claim calibration evidence. |
| Will the evidence become a second replay authority? | The immutable tape already persists Commands. Re-executing those Commands through current managym reproduces receipts; storing inferred commitments beside replay decisions would duplicate authority. | Hash reproduced TransitionReceipt identities only. Do not add commitment reconstruction to Etude replay models. |
| Does this require rerunning RUL-9 workloads? | No. RUL-9's immutable origin artifact `498df1eda031f1d6ea68f72f792dfd68195bb10eab884b006aa7772b586564da` already binds the same 132-Command tape and its measurements. | Reference its hashes in the new correctness receipt; leave release/training measurement files and budgets untouched. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Publish identities for all nine selected-match prompt families | Would make every visible action conditionable immediately. | The audit found one provider gap, and seven delayed target exits already have authoritative cast identity. A broad action ontology would force target/object/combat privacy and cross-world semantics not required by this KR. |
| Infer a discarded card in manabot from pool deltas or viewer events | Avoids a Rust enum and discard-prompt regeneration. | It makes manabot reconstruct rules meaning after mutation, cannot evaluate counterfactual action likelihoods before transport, and risks hidden-truth or event-shape dependence. |
| Infer commitments from Etude offer labels or persisted replay rows | Reuses labels such as “Discard Island, then draw a card.” | Labels are presentation, replay rows are not semantic authority, and both violate the ban on client/card-name reconstruction and positional identity. |
| Emit only `Discard { card }`, leaving decline unadmitted | Closes the literal hidden-pool exit with one variant. | Once the discard decision is admitted for likelihood grouping, the observed decline at revision 40 is the other outcome of the same public choice. Skipping it creates asymmetric evidence and an avoidable future gap. |
| Preserve the actual discard ActionSpace on materialized worlds | Requires no new materializer mode. | The retained actions contain physical cards from the actual hand and are illegal or truth-leaking on hypotheses with different hands. |

## Key decisions

- Public commitment identity remains a semantic outcome, not a generic action
  serialization. The two new variants are deliberately prompt-specific.
- The `Discard` name is minted from managym's authoritative selected card and
  published only in the accepted `TransitionReceipt`; no consumer derives it.
- Both discard and decline are admitted because likelihood normalization must
  cover the complete observed binary family once that family is supported.
- Counterfactual legality is rebuilt from each materialized hand. A missing
  discard card means zero matching offers, not fallback to the actual hand.
- Belief updates use validate/compute/commit ordering. No snapshot-and-restore
  mutation path is necessary because `BeliefState` is immutable and every new
  state can be computed locally.
- The semantic decision contract advances to v4; frozen evidence stays frozen,
  while current compatibility fingerprints and parity receipts advance with
  the implementation.
- The verifier's likelihood is an identity/transport instrument only. It is
  explicitly barred from producing calibration or strength claims.
- Wild success means future selected content either runs through the same
  bounded identity cases or fails at one typed provider boundary with enough
  evidence to justify the next variant. Wild failure would be a generic
  action dump that leaks physical identity or lets stale real-hand offers into
  hypotheses; allowlists, branch regeneration, and negative tests prevent
  that outcome.

## Scope

- In scope: audit evidence for the fixed tape; managym `Discard` and
  `DeclineDiscard` identities; discard-decision hypothesis regeneration;
  transactional belief updates; live/replay/materialized identity parity;
  focused Rust debug and uv-managed Python tests; a checked correctness
  receipt/verifier; necessary semantic-version and active-contract fingerprint
  updates.
- Out of scope: identities for other unobserved/unrequired prompt families;
  new cards or mechanics; release-budget work; RUL-9 measurement reruns;
  calibration checkpoints/models/curves; strength evaluation; arena or UI;
  replay-side semantic inference; a general public-action ontology.

## Done when

- `DecisionFrame` and accepted `TransitionReceipt` publish `Discard { card:
  "Island" }` at fixed revision 29 and `DeclineDiscard` at revision 40, with
  no private card/object/position identity.
- The complete fixed tape reaches terminal with two exact-range trackers, 132
  records per viewer, 62 consumed opponent commitment identities in total,
  and zero `RulesProviderGap`.
- Etude live execution and persisted canonical replay reproduce byte-identical
  132-row public-commitment streams.
- One materialized revision-29 hypothesis regenerates legal discard offers and
  reproduces the observed discard identity through a normal semantic Command;
  the retained source is unchanged.
- Unsupported and card-mismatched commitment tests preserve match witness,
  semantic event cursor, posterior/prior digests, pending identity, stats, and
  records.
- Focused debug Rust tests pass with
  `cargo test --manifest-path managym/Cargo.toml public_commitment --no-fail-fast`.
- Focused Python tests pass through uv, and
  `./scripts/verify-public-commitment-parity` recomputes the checked receipt
  successfully.
- The checked receipt names authority receipt SHA-256
  `57f5e2d1a482831e4e1edeb69632c0b34030d2e8ee701eab9b13b484c6937147`
  and RUL-9 measurement artifact SHA-256
  `498df1eda031f1d6ea68f72f792dfd68195bb10eab884b006aa7772b586564da`;
  the RUL-9 measurement bytes are unchanged.

This advances the wave measures that “live play, deterministic replay,
Intelligence search, and Study branching consume projections of the same
authoritative match” and that “viewer-private information … remain[s] exact
across play, replay, search forks, and shared Study artifacts.”

## Measure

| Fixed-trace signal | Before | Required after |
|--------------------|--------|----------------|
| Commands to terminal | 132 | 132 |
| Non-null public commitments | 60 | 62 |
| Unadmitted commands | 72 | 70 |
| Exercised `RulesProviderGap` | 1 at revision 29 | 0 |
| Viewer tracker records | stops at viewer 1 record 29 | 132 per viewer |
| Live/replay identity-stream mismatches | not measured | 0 |
| Materialized discard-decision support | typed unsupported error | regenerated, exact identity |
| Negative-proof witness/cursor/belief/record mutations | mismatched path not atomic | 0 |
| RUL-9 measurement reruns or byte changes | 0 | 0 |
