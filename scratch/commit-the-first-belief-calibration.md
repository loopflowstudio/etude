# Commit the first belief-calibration curves

## Problem

R3 of the Intelligence results ladder needs the first frozen answer to a
simple question: as public actions arrive, does the exact-range tracker put
more or less probability on the opponent hand that authority says is true
than the compatible-deal prior does at the same state?

The machinery already exists, but its retained evidence stops one step short.
`BeliefTracker` maintains the posterior and its matched compatible prior,
`FrozenPolicyLikelihood` evaluates public actions counterfactually over the
canonical managym world space, and the known-truth audit scores the posterior
without exposing truth to the acting path. INT-9 did not run because its
likelihood artifact was unresolved. RUL-11 has since closed the selected
UR-Lessons-versus-GW-Allies provider path, and INT-7 has retained compatible
world-w2 checkpoint bytes. INT-17 must join those exact artifacts and commit a
result, positive or negative. It must not substitute the test-only likelihood,
train a belief head, modify Rules, or make a gameplay-strength claim.

This advances the wave measure:

> R3 — a committed repro script emits posterior mass on the actual hidden hand
> versus prior mass across the decision sequence, with a byte-locked real
> likelihood model and no `RulesProviderGap` on the exercised path.

## The demo

From the repository root, a developer runs:

```bash
uv run --extra dev python experiments/runners/run_belief_calibration.py \
  --contract experiments/contracts/int-17-belief-calibration-v1.json \
  --out-dir .runs/int-17-belief-calibration-v1
```

The command replays the retained seed-0 selected match for both fixed viewers,
prints the paired posterior/prior truth-mass and log-loss summary, and writes a
content-addressed result containing all 266 curve points, tracker receipts,
identity receipts, and the resource ledger. The checked result is then
reproducible without generation through the same runner's `--verify-only`
mode.

## Approach

Create a dedicated, versioned INT-17 calibration contract and runner rather
than mutating the frozen unresolved INT-9 contract. The contract binds:

- world `w2`, the selected `ur-lessons-vs-gw-allies` matchup, and the retained
  seed-0 authority command tape;
- the RUL-11 public-commitment receipt and its exact identity stream;
- INT-7 `visit_policy_only` seed 197 as the action-likelihood policy;
- `epsilon=0.05`, batch size 256, CPU inference, counterfactual seed 907, and
  policy temperature 1.0;
- observation/action/content/semantic-decision identities and an explicit
  preregistration commit;
- both viewers, every one of the 132 transitions, and an initial point, for
  133 points per viewer;
- one worker, at most 1,000,000 counterfactual world rows, six wall/core hours,
  2 GiB peak RSS, and 64 MiB of retained result bytes.

Replay the authority tape once with two independent `BeliefTracker` instances
sharing one frozen likelihood evaluator. Before each semantic command, clone
the exact root. After applying the command, feed the provider-owned transition
and retained root to both trackers. Only after both updates complete may the
authority-only audit read each viewer's actual opponent hand. This preserves
the key information boundary: truth scores the result but never enters the
tracker, likelihood model, sampling path, or command selection.

At the initial state and after every transition, emit one row per viewer with
the game/trace/seed/viewer/sequence/revision identities; command and public
commitment identities; actual-hand authority witness; world-space, posterior,
and prior digests; support diagnostics; and paired metrics. Extend the reusable
audit layer with a paired point rather than adding truth fields to
`BeliefTracker`.

Retain the result under
`experiments/data/int-17-belief-calibration-v1/sha256/<manifest-sha256>/` with:

- `curves.jsonl`: the complete 266 authority-only points;
- `summary.json`: per-viewer and combined descriptive summaries;
- `tracker-receipts.json`: exact transition histories and history digests;
- `generation-receipt.json`: command, runtime, source, checkpoint, trace, and
  host identities;
- `resource-ledger.jsonl`: append-only preflight/run/verification cost events;
- `manifest.json`: every retained file's bytes and SHA-256 plus an artifact
  digest;
- `verification.json`: replay equality, zero-gap/integrity counts, and
  `no_generation=true`.

Add an experiment report that states the result as one selected-trace
calibration baseline, not model or gameplay evidence. It must preserve a
negative result unchanged. It also supplies the precommitted interpretation
for R4: better calibration with no arena gain points at planner/use-of-belief
or matched-cost effects; worse calibration with an arena loss points first at
the likelihood model; any other crossing is ambiguity, not permission to
claim that beliefs caused strength.

## Exact retained-byte and provider audit

The retained compatible checkpoint exists at:

`experiments/data/int-7-value-target-comparison-v1/sha256/3f7a00e179fe49fe111ff8a361501ca8080e501fa7189abeaacb66194a5de5bf/result/checkpoints/visit_policy_only-seed-197.pt`

Its SHA-256 is
`1673a237ef2460d0e699667987c29fe6b42c28711bdb2041989f37692edbd1e6`,
exactly matching the retained INT-7 manifest. `torch.load` exposes the expected
world-w2 `model_state_dict`, training hypers, and `visit_policy_only` BC
metadata. `FrozenPolicyLikelihood` loaded it through
`load_checkpoint_agent`, so this is loader compatibility, not merely a file
extension or digest match.

The checkpoint was then evaluated at RUL-11 transition ordinal 29, viewer 1,
against the provider identity `{"kind":"discard","card":"Island"}`. The
exact inline audit command was `uv run --extra dev python -`; it reconstructed
the first 30 commands from
`conformance/authored-match-v1/release-stack-ur-vs-gw-seed-0.json`, built the
canonical prior, and called `FrozenPolicyLikelihood.evaluate`. It completed in
3.5 wall seconds overall and 0.459665709 seconds inside the evaluator. All 484
worlds were legal, 100 contained a matching authoritative offer, and matching
likelihoods were nonconstant: minimum 0.2430262715, maximum 0.7604032159,
standard deviation 0.0975911979. The loaded checkpoint digest remained the
expected `1673a237...`.

RUL-11's checked selected trace is seed 0, 132 Commands, and 62 provider-owned
commitments: 33 observed by viewer 0 and 29 by viewer 1. It includes
`play_land`, `cast`, `pass_priority`, `discard`, and `decline_discard`
identities. Its full neutral identity replay records 132 tracker transitions
per viewer, 62 consumed commitments, zero exercised `RulesProviderGap`, and
atomic rejection of unsupported or card-mismatched identities. The authority
receipt SHA-256 is
`57f5e2d1a482831e4e1edeb69632c0b34030d2e8ee701eab9b13b484c6937147`;
the RUL-11 command-tape SHA-256 is
`f26862d55368c18137baafb19c36f2943efcedf913917c31e543537ebfd4a127`.

A no-inference preflight replay measured the exact expensive workload in 5.2
wall seconds. Viewer 0 consumes 694,187 counterfactual rows across 33 action
updates with maximum support 121,485. Viewer 1 consumes 208,876 rows across 29
action updates with maximum support 41,806. The two-viewer total is 903,063
rows. The audit then attempted a full viewer-1 frozen-checkpoint replay using
`uv run --extra dev python -`. It remained CPU-bound at about one core and
1,025,200 KiB RSS and was deliberately interrupted after 28:01 before
completion because the exploratory command had no resource ledger or hard
cap. It had not emitted a result and therefore establishes cost, not
full-trace checkpoint/provider closure. The bounded repro must still prove
all later identities with the real checkpoint and fail closed if any does not.

The checkpoint was trained on the interactive mirror rather than this curated
selected matchup. Its ABI and loader compatibility are exact, but its action
likelihoods are out-of-distribution. That is the strongest confound and part of
the result: INT-17 measures whether this first retained real policy improves or
damages the selected-trace belief. It must not substitute another checkpoint
after seeing the curves.

## Metric definitions

For viewer `v` at point `t`, let `h(v,t)` be the authority-only actual hidden
opponent hand, `B(v,t)` the tracked posterior, and `P(v,t)` the canonical
compatible-deal prior at the same public state.

- `posterior_true_hand_mass = B(v,t)[h(v,t)]`.
- `prior_true_hand_mass = P(v,t)[h(v,t)]`.
- `posterior_log_loss_nats = -ln(posterior_true_hand_mass)`.
- `prior_log_loss_nats = -ln(prior_true_hand_mass)`.
- `log_loss_improvement_nats = prior_log_loss_nats -
  posterior_log_loss_nats`; positive favors the action-updated belief.
- `truth_mass_ratio = posterior_true_hand_mass / prior_true_hand_mass`.
- `posterior_true_hand_rank` and `prior_true_hand_rank` use descending mass
  with the existing deterministic tie behavior.
- Per-card inclusion probability, Brier score, and ten-bin ECE are computed
  separately for posterior and prior through the existing audit definitions.
- Top-hand mass and effective range size are emitted for both arms.

All summaries are descriptive over a serially correlated single trace. They
include combined and per-viewer means/medians, final-point deltas, the fraction
of points where the posterior beats the prior, and the same summaries limited
to opponent public-commitment points. They are not confidence intervals,
independent samples, general calibration, or a method claim.

The preregistered directional prediction is that each viewer's mean
`log_loss_improvement_nats` at opponent-commitment points is positive and that
the posterior true-hand mass exceeds the prior on more than 55% of those
points. Refutation closes R3 honestly; it does not trigger checkpoint
substitution.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|------------------|
| Do real compatible checkpoint bytes exist? | Yes. INT-7 retained all twelve checkpoints; selected visit-policy seed 197 matches SHA-256 `1673a237...`. | Lock this one checkpoint and forbid fallback or post-result selection. |
| Can the production likelihood loader consume them? | Yes. The exact checkpoint loads through `load_checkpoint_agent` and produces nonconstant grouped likelihoods on a real RUL-11 discard root. | Proceed to a real repro rather than byte-availability closure. |
| Is provider identity closed on the selected path? | RUL-11 replays all 132 transitions for both viewers with 62 canonical commitments and zero gaps. The real checkpoint refresh path is directly proven at the 484-world discard root; later real-model roots remain a run gate. | Bind both RUL-11 and authority receipts; any runtime gap aborts without result. |
| Does existing audit code expose the required comparison? | It scores posterior truth mass, rank, Brier, and ECE after every transition, while the tracker already retains a contemporaneous compatible prior. It does not emit paired prior points. | Add a paired authority-only audit structure; do not change tracker semantics. |
| Is a complete run cheap enough? | Preflight is 903,063 world rows. An unbounded viewer-1 probe exceeded 28 minutes and about 1 GiB RSS. | One selected seed, both viewers, one worker, 1M-row/6h/2GiB hard caps; no search or new gameplay. |
| Can the result explain later arena evidence? | Calibration and arena identity can share world, matchup, checkpoint, epsilon, and player provenance, but strength has not run. | Commit an interpretation rubric and join identities; make no current strength claim. |
| Could a positive result be overstated? | Yes. One deterministic trace is serially correlated, and the likelihood policy is selected-match OOD. | Report a selected-trace baseline only; retain raw curves and both positive and negative deltas. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Unlock and reuse the unresolved INT-9 arena contract | Reuses one runner but mixes calibration with search/gameplay and mutates a frozen unresolved artifact definition. | INT-17 needs a result-only contract on the landed provider path. |
| Generate fresh games with `play_games` | Produces more seeds but adds policy/search decisions, new command tapes, and provider paths RUL-11 did not freeze. | The first result should consume the exact retained selected trace. New cohorts can be versioned later. |
| Use `IdentityAuditLikelihood` or `test-only-likelihood/v1` | Fast and already closes provider identity. | It cannot answer whether a real policy updates beliefs and is explicitly forbidden. |
| Optimize or approximate world evaluation before running | Could reduce hours, but changes the instrument and risks altering exact Bayes semantics. | The measured one-trace run fits the cap; produce the result before building more machinery. |
| Retain only aggregate log loss | Small artifact and easy review. | It cannot show when or why beliefs move, and cannot explain a later arena result. |

## Key decisions

1. **One frozen game, both viewers.** RUL-11 proves exactly one retained seed-0
   command tape. Both viewers cover both asymmetric decks and yield complete
   curves without inventing a new cohort. The schema is per-game and supports
   future versioned cohorts, but additional seeds are not smuggled into v1.
2. **Latest selected policy-only checkpoint.** INT-7's decision was
   `continue_visit_policy_only`; seed 197 is the first manifest-listed retained
   arm. Fix it before results. Value heads are irrelevant to action likelihood.
3. **Truth remains audit-only.** Score after the tracker update and never add
   truth to a BeliefState, Observation, likelihood call, player, or receipt
   that can reach Etude.
4. **Every decision is a point.** Include the initial state and all transitions,
   including own actions and unadmitted commands. This makes transport, flat
   intervals, and action updates visible instead of cherry-picking informative
   moments.
5. **Prior is contemporaneous.** Compare against the canonical compatible
   prior after the same public transport, not only the opening prior. The delta
   then isolates retained action-history likelihood information.
6. **Negative is complete.** Worse log loss, a truth-mass collapse, or a failed
   directional prediction remains the frozen result. Integrity failures do not
   become results; they abort with exact typed evidence.
7. **Generation provenance and live verification are separate.** The result
   binds an explicit preregistration commit and authenticates its contract and
   source bytes at that revision. Verify-only replays against the declared
   compatible runtime without relabeling generation.

## Success and failure modes

Wild success is not merely a downward log-loss line. It is a curve where
specific public actions visibly multiply the actual hand's mass relative to
the same-state prior, the effect persists through later public transport, both
viewers remain normalized and truth-supported, and the receipt gives R4 an
exact reason to expect or not expect an arena difference.

Wild failure is a polished average hiding a broken trace: truth enters the
model path, a later commitment silently groups by offer index, zero mass is
serialized as infinity, the selected checkpoint changes after inspection, or
an aggregate built from correlated points becomes a general calibration or
strength claim. The contract, raw curves, fail-closed gates, and narrow report
are chosen specifically to prevent those failures.

## Scope

- In scope: a new INT-17 contract and replay runner; paired posterior/prior
  authority-only audit points; the frozen seed-0 two-viewer result; exact
  identities, resource and generation receipts; a report; verify-only replay;
  focused regression and tamper tests.
- Out of scope: Rules or Game changes, new provider identities, generated
  gameplay or arena matches, search, checkpoint training or selection,
  supervised belief heads, new likelihood models, multiple matchup seeds,
  content widening, UI/Study presentation, and any gameplay-strength claim.

## Fail-closed conditions

Abort before retaining a result if any of the following occurs:

- checkpoint path missing, SHA-256 mismatch, loader incompatibility, ABI/world
  mismatch, or model id equal to `test-only-likelihood/v1`;
- authority tape, RUL-11 receipt, command stream, matchup/content, contract,
  preregistration source, or runtime fingerprint drift;
- any `RulesProviderGap`, missing opponent likelihood root, unsupported public
  commitment, semantic command rejection, state/revision mismatch, replay
  mismatch, or source-root mutation;
- actual hand outside canonical support, zero/non-finite posterior or prior
  truth mass, normalization error above the existing tolerance, private truth
  observed before the audit boundary, or private cards in viewer-safe receipts;
- anything other than 132 tracker records and 133 curve points per viewer, 62
  total action updates, or the preflight total of 903,063 counterfactual rows;
- row, wall/core time, RSS, retained-byte, or one-worker cap exceeded;
- noncanonical JSON/JSONL, missing digest, artifact digest mismatch, tampered
  retained file, nondeterministic rerun, or any write/generation in
  `--verify-only` mode.

## Done when

- The contract is committed before generation and binds the exact trace,
  checkpoint, viewers, hyperparameters, caps, identities, and directional
  prediction.
- The bounded command in **The demo** completes under the caps and two fresh
  runs produce byte-identical canonical result payloads.
- The retained result contains exactly 266 curve points, 264 post-transition
  points plus two initial points, with zero integrity/provider failures and
  finite paired truth masses everywhere.
- `summary.json` reports posterior and prior log loss/calibration separately,
  their paired deltas, and the selected-trace/OOD limitations.
- The experiment report records prediction versus result and the R4
  interpretation rubric without claiming strength.
- `./scripts/verify-belief-calibration` builds the pinned extension, runs the
  focused Python tests and RUL-11 provider verification, and completes
  verify-only replay with `no_generation=true` and an unchanged retained tree.
- No Rust is changed. If implementation unexpectedly touches Rust, the full
  debug `cargo test --manifest-path managym/Cargo.toml` suite becomes mandatory
  before landing.

## Measure

Before generation, the runner records the preflight baseline: 132 commands,
62 provider commitments, two viewers, 903,063 counterfactual rows, zero
provider gaps under the RUL-11 identity audit, and checkpoint SHA-256
`1673a237...`.

The primary result is the paired time series of true-hand mass and
`log_loss_improvement_nats`. Secondary descriptive measurements are
posterior/prior hand rank, top mass, effective support, per-card Brier/ECE,
normalization, likelihood/update time, world rows, peak RSS, and exact replay
integrity. "Better" means positive mean log-loss improvement for each viewer
and posterior mass above prior on more than 55% of opponent-commitment points;
failure of that prediction is retained as the first honest calibration result.

## Execution closure (2026-07-18)

The exact frozen command was stopped cleanly after 1:37:56 wall / 84:38 CPU
with three completed commitment receipts, empty stderr, and no curves. A static
audit proved that `FrozenPolicyLikelihood` materializes each support row through
an engine method that reconstructs and recursively enumerates the entire
`PossibleWorldSpace` for every row. The frozen trace has
`sum(S^2) = 51,506,080,901`; even the most optimistic coefficient from the
three completed updates projects 41,575 seconds, exceeding the 21,600-second
cap. This is a retained systems failure, not a calibration result.

Execution now depends on a Rules-owned identity-bound materializer that
validates/enumerates one space once and materializes many indices without
re-enumeration. Because v1 correctly byte-locks the original runner, engine
source, and extension, RUL-13 must be followed by a new preregistered execution
contract that copies the cohort, checkpoint, algorithm, metrics, prediction,
and caps exactly while binding the repaired provider and runtime identities.
The v1 contract and failure receipt remain unchanged historical evidence.
