# Train the First Belief-Conditioned Student — Immutable Conditional Shard Contract + Smallest Ablation

> Kickoff for INT-14 (wave: intelligence; project: search-teacher-and-student-arena).
> Directive version 1. Resumes the prior kickoff epoch without expanding scope:
> immutable conditional shard/manifest contract + the smallest belief-conditioned
> policy/value student ablation against the existing Teacher-0 arena path. **No
> learned belief head. No strength claim.**

## Problem

The intelligence wave already has a runnable **Teacher-0** path: flat-MC data →
policy/value student → seat-balanced arena, frozen as a 512-game immutable
snapshot (`experiments/runners/run_teacher0_partial_snapshot.py`,
`wave/intelligence/MEMORY.md:31-34`). The public-belief-state (PBS) design
(`wave/intelligence/02-beliefs-design.md`) is the eventual destination, but it is
explicitly **"Status: dormant, trigger-armed"** (`02-beliefs-design.md:22`),
activated only on the Exit-1 tripwire. A learned belief tracker, range nets, and
per-hand value vectors are not justified yet and are out of scope here.

What is missing and cheap is the **plumbing and measurement contract** that sits
between a conditional search and a conditioned student:

1. an **immutable on-disk shard/manifest format** for per-condition strategy
   labels, so a snapshot can be frozen, verified, and consumed fail-closed the
   way Teacher-0's is;
2. an **adapter** that converts a conditional search's per-condition strategy
   result into shard rows, pinning the shape the search must produce; and
3. a **smallest matched ablation** proving the conditioning path does not break
   the arena and is ready to measure the uniform-vs-weighted gap when real labels
   arrive — without a learned belief head and without claiming toy strength.

INT-13 will produce a `ConditionalStrategyResult` (per-condition strategy
distributions). **It does not exist yet** (zero source matches for
`ConditionalStrategyResult`/`INT-13`; the closest shape is `PuctResult`,
`manabot/sim/mcts.py:47-61`, whose `world_*` arrays already add a per-world
axis). This task defines the contract INT-13 must conform to and builds the
smallest student ablation against the existing arena, using a **synthetic
uniform-determinization toy fixture** that conforms to the same pinned shape. Who
benefits: the search wave gets a frozen label contract to target; the student
wave gets a measurement harness ready for real labels; reviewers get an honest
receipt that separates plumbing from strength.

## The demo

Two commands, both honest about what they prove:

```bash
uv run pytest tests/sim/test_conditional_snapshot.py -q
uv run python experiments/runners/run_belief_conditioned_ablation.py --frozen
```

The first round-trips `freeze_conditional_snapshot`/`verify_conditional_snapshot`
on a synthetic conditional shard and fails closed on digest, condition-schema,
`CLAIM_BOUNDARY`, missing-condition-key, and file-set drift — the exact
`test_teacher0_partial_snapshot.py` discipline applied to the new schema.

The second prints four matched arms — `policy_only`, `policy_value`,
`belief_conditioned_policy_only`, `belief_conditioned_policy_value` — with
seat-balanced win rates + Wilson 95% CIs versus random and versus the frozen
Teacher-0 controls, at the same compute budget across multiple seeds, and an
explicit **pre-registered statement** that the toy (uniform-determinization)
condition label is expected to show **~0 strength gap**, so the output is a
plumbing/measurement receipt, not a strength claim. The conditioned student is
non-inferior to the non-conditioned baseline within noise; a non-zero gap is
flagged as a surprising falsification candidate, not a victory.

## Approach

### 1. Immutable conditional shard/manifest contract (schema_version 2)

Mirror `run_teacher0_partial_snapshot.py` exactly; bump the snapshot manifest
`schema_version` to `2` and add a condition axis. Reuse `SnapshotError`,
`_atomic_json`, `_file_sha256`, `_json_sha256`, `_identity`,
`_trainer_identity`/`_dataset_identity`, `_snapshot_identity`, and the
atomic-publish + fail-closed-verify pattern verbatim.

**Conditional shard NPZ** (extension of `manabot/sim/distill.py:36-45`): keep
every existing per-decision key (`OBS_KEYS`, `META_KEYS`, `SCORE_KEY`,
`VISIT_COUNT_KEY`, `ROOT_VALUE_KEY`) scoped to the public decision (one row per
decision). Add:

- `condition_count` — int16 `(D,)`, the number of conditions K for the decision.
- `condition_index` — int16 `(D, K_max)`, per-condition id; `-1` padding.
- `condition_weight` — float32 `(D, K_max)`, per-condition belief weight,
  non-negative and summing to 1 per decision; `0` padding.
- `condition_scores` — float32 `(D, K_max, max_actions)`, the per-condition
  strategy distribution (the `ConditionalStrategyResult` payload); `-1` padding
  on invalid actions. For the K=1 toy this equals `scores`.
- `condition_root_value` — float32 `(D, K_max)` (optional), per-condition value.

The per-shard `provenance` tag (`distill.py:232-247`) gains
`condition_label_format` (e.g. `"uniform_determinization_world_index_v1"`) and
`condition_schema_digest` (SHA-256 over the pinned `ConditionalStrategyResult`
shape JSON). The loader expands to per-(decision, condition) training rows,
repeating the public obs and attaching `condition_index`/`condition_weight` as
obs fields for the conditioned arm.

**Conditional snapshot manifest** (`schema_version: 2`): copy the Teacher-0
field set (`run_teacher0_partial_snapshot.py:300-330`) and add a `condition_schema`
block — `{condition_label_format, K_max, condition_strategy_result_shape_digest,
condition_source}` — plus a revised `CLAIM_BOUNDARY`:

```python
CLAIM_BOUNDARY = {
    "claim": "belief_conditioned_plumbing_ablation",
    "learned_belief_head": False,
    "pbs_range_net": False,
    "per_hand_value_vector": False,
    "condition_source": "synthetic_uniform_determinization_toy",
    "strength_claim": False,
    "policy_target_kind": "score_softmax_not_mcts_visits",
    "value_target_kind": "terminal_outcome",
    "teacher_algorithm": "flat_determinized_monte_carlo",
}
```

`freeze_conditional_snapshot()` / `verify_conditional_snapshot()` assert the
`condition_schema` block, re-derive `snapshot_identity_sha256`, re-hash every
NPZ/JSON, and enforce the exact file set — all fail-closed via `SnapshotError`.

### 2. The `ConditionalStrategyResult` shape contract + adapter

Define a frozen, digest-pinned shape for `ConditionalStrategyResult` (a small
JSON schema: per-decision `condition_index[]`, `condition_weight[]`,
`condition_scores[K][max_actions]`, optional `condition_root_value[]`, plus the
public decision anchor). The **adapter** converts any object conforming to this
shape into conditional shard rows. Because INT-13 has not shipped, the adapter is
exercised by a **synthetic toy producer** that conforms to the same shape:
uniform determinization via the existing `determinized_puct` per-world arrays
(`manabot/sim/mcts.py:451-460`) or a trivial K=1 producer that copies the
existing flat-MC `scores` into `condition_scores` with `condition_weight=[1.0]`.
The shape digest is asserted at ingest, so when INT-13's real
`ConditionalStrategyResult` arrives it is ingested with **no contract change**.

### 3. Smallest belief-conditioned student ablation (no learned belief head)

Reuse `train_search_supervised` (`manabot/sim/search_supervised.py:297-432`)
unchanged. The condition enters as a **provided side input**, not a prediction:

- Add a condition object at `Agent._gather_object_embeddings`
  (`manabot/model/agent.py:169-206`): an extra row built from
  `condition_index`/`condition_weight`, appended to the concatenated object
  sequence, with a neutral slot in the static `is_agent_template` (`:92-101`).
- The shared encoder, policy head (`:73-77`), and **scalar** value head
  (`:78-85`) are unchanged. There is **no belief head, no range net, no per-hand
  value vector** — the value head stays scalar, exactly as today. The condition
  is a tag the student is conditioned on, not a hidden state it predicts.

Matched arms from the same frozen conditional fixture, mirroring the Teacher-0
matched-controls block (`run_teacher0_partial_snapshot.py:598-611`):

- `policy_only` / `policy_value` — existing non-conditioned arms (condition
  object absent / zeroed).
- `belief_conditioned_policy_only` / `belief_conditioned_policy_value` — same
  fixture, same capacity init, same train/val split, same optimizer, same seed;
  the only difference is the condition side input is present.

Evaluate through the existing arena path — `_matchup` / `_student_vs_random`
(`experiments/runners/run_search_supervised.py:231-263`), `play_games` +
`aggregate_records` (`manabot/sim/flat_mc.py:394-512`), seat-balanced with
Wilson CIs — versus random and versus the frozen Teacher-0 controls. Reuse the
`joint_policy_noninferior`-style gate (`run_search_supervised.py:736-746`).
Multiple seeds; pinned content/engine/observation/action/model/opponent/compute
identities. No new rating system, no new arena.

### 4. Honest framing (do not overclaim toy strength)

The toy condition is a **uniform determinization** — an uninformative belief
(`02-beliefs-design.md:22-30`: "uniform determinization is exactly correct
against uninformative opponents"; `:192-194`: uniform-vs-weighted gap ~0 against
random). The pre-registered prediction is therefore **~0 strength gap** vs the
non-conditioned baseline. The deliverable is a **plumbing + measurement-integrity
receipt**: the immutable conditional shard/manifest contract is built and
verified; the adapter ingests the pinned `ConditionalStrategyResult` shape; the
conditioned student trains and is non-inferior within noise; the harness is ready
for INT-13's likelihood-weighted labels. The `CLAIM_BOUNDARY` forbids strength
claims; the arena report names the toy condition source and the ~0 prediction.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|------------------|
| Does `ConditionalStrategyResult` exist? | No. Zero `*.py`/`*.rs` matches for `ConditionalStrategyResult`/`INT-13`; closest shape is `PuctResult` (`manabot/sim/mcts.py:47-61`) whose `world_*` arrays already add a per-world axis. | Pin a frozen, digest-bound shape contract now; exercise the adapter with a synthetic toy producer that conforms to it. INT-13 must conform; no contract change at ingest. |
| Is there a learned belief head to remove? | No. Zero belief modules in `manabot/model`, `manabot/env`, `manabot/sim`, or `managym/src/agent`. `02-beliefs-design.md:22` is dormant/trigger-armed; `manabot/sim/value.py:11` only documents the scalar-value type-error caveat. | "No learned belief head" is satisfied by construction: the condition is a provided side input at `_gather_object_embeddings`, not a predicted belief. |
| Does the snapshot contract support a schema bump? | Yes. `schema_version` is a manifest field; `freeze_snapshot`/`verify_snapshot` already assert `policy_target_kind`/`value_target_kind` and re-hash every file (`run_teacher0_partial_snapshot.py:203-407`). | Mirror the pattern; bump to `schema_version: 2`; add `condition_schema` assertions and condition-key presence checks to freeze/verify. |
| Is the frozen 512-game snapshot on disk in this worktree? | No. `.runs/` is gitignored (`.gitignore:22`) and absent here; the snapshot is reproducible evidence, not a checked-in artifact. | The toy fixture is a **new small frozen conditional shard** generated and frozen in this PR. Do not depend on the 512-game snapshot being present. |
| Does the existing arena accept a conditioned student? | Yes. `make_player`/`play_games` accept a `checkpoint` spec (`manabot/sim/flat_mc.py:193-362, 394-482`); the conditioned student is just an `Agent` with the condition object in its obs dict. | No new arena/rating. Reuse `_matchup`/`_student_vs_random`, `aggregate_records`, Wilson CIs, and the `joint_policy_noninferior` gate. |
| Will the toy condition show strength? | No. Uniform determinization is an uninformative belief; the beliefs design pre-registers ~0 gap against uninformative opponents (`02-beliefs-design.md:192-194`). | Pre-register ~0; the receipt is plumbing/measurement integrity, not strength. A non-zero gap is a flagged falsification candidate. |
| Does the condition label leak hidden information? | For the toy, no. Uniform-determinization worlds are sampled from the opponent's unseen pool under public constraints; `condition_index` is a public tag, not the opponent's true hand. `resample_hidden` (`managym/src/state/zone.rs:246-254`) is the same uniform draw search already uses. | Information-set honesty is preserved (`MEMORY.md:54-56`). Label cost (K × decisions) is reported per the open tension on search label cost. |
| Is the condition object's attention slot safe? | The `is_agent_template` is a static buffer over the 6-object layout (`agent.py:92-101`); adding a row needs a neutral (non-agent) slot so ownership sign-flip (`:294`) does not mis-attribute the condition. | Add one neutral slot; validity is a constant `1` so the condition is never padding-masked out. Minimal, localized change. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Full PBS student (range net + per-hand value vector) | The dormant `02-beliefs-design.md` end-state. | Trigger-armed, not justified; violates "no learned belief head"; the Exit-1 tripwire has not fired. Out of scope. |
| Wait for INT-13 before defining the contract | Avoids pinning a shape that might shift. | The contract is the cheap de-risking prerequisite; pinning it now lets INT-13 target a frozen shape and lets the student ablation be built and measured in parallel against a toy fixture. |
| Condition on a public feature (e.g. land count) instead of a belief label | Reuses the obs pipeline with no new shard. | Not "belief-conditioned"; does not exercise the conditional-search label path; measures a semantic ablation, not the conditioning plumbing this task owns. |
| Hash the condition into the `events` window | No model change. | `events` is a fixed recent-event buffer (`observation.py:480-512`), not a conditioning channel; the clean, minimal attach point is `_gather_object_embeddings`. |
| Per-row (decision,condition) NPZ without a condition axis on labels | Simplest loader. | Either duplicates the public obs K times on disk (label-cost waste) or breaks the existing per-row `scores` shape. The chosen per-decision-obs + `(D,K_max,*)` label arrays keep the NPZ compact and extend `scores`'s existing `(D,max_actions)` shape mechanically. |

## Key decisions

- **schema_version 2 conditional snapshot**, mirroring schema_version 1 exactly
  (`CLAIM_BOUNDARY`, `SnapshotError`, atomic write, `snapshot_identity_sha256`,
  freeze/verify fail-closed). The contract is the deliverable; the toy fixture
  proves it.
- **The condition enters as a provided object row + weight scalar at
  `_gather_object_embeddings`**, with one neutral `is_agent` slot. The policy and
  scalar value heads are unchanged. This is the smallest change that makes the
  student condition-aware without a belief head, range net, or per-hand value
  vector.
- **The toy fixture is a synthetic uniform-determinization conditional shard**
  conforming to the pinned `ConditionalStrategyResult` shape. No real conditional
  search runs in this slice; the toy producer reuses existing `determinized_puct`
  per-world arrays or a trivial K=1 copy of flat-MC `scores`.
- **Pre-registered ~0 strength gap**; `CLAIM_BOUNDARY` forbids strength claims.
  The receipt is plumbing + measurement integrity, consistent with the beliefs
  design's own ~0 prediction for uninformative opponents.
- **Matched controls verbatim from Teacher-0**: same fixture, same capacity init,
  same split/optimizer/seed; the only difference is conditioning on/off.
- **No Rust, no arena, no rating, no Study changes.** Conditioning is a Python
  obs-field + shard-key extension; the arena is reused as-is.

## Scope

- **In scope:** conditional shard NPZ keys (`condition_count`,
  `condition_index`, `condition_weight`, `condition_scores`,
  `condition_root_value`) + `condition_label_format`/`condition_schema_digest`
  provenance fields; frozen `ConditionalStrategyResult` shape contract + digest;
  synthetic toy fixture producer; `freeze_conditional_snapshot` /
  `verify_conditional_snapshot` + fail-closed tests; condition side-input in
  `Agent` (one object row, no new head); matched ablation runner reusing
  `train_search_supervised` + `_matchup`/`_student_vs_random`; pre-registered ~0
  statement; multi-seed seat-balanced arena with Wilson CIs and label-cost
  reporting.
- **Out of scope:** learned belief head / range net / PBS; per-hand value vector;
  likelihood-weighted determinization (INT-13 / the real beliefs wave); real
  conditional search runs; MCTS visit-distillation; Teacher-1 admission; any
  strength or superhuman claim; new arena/rating system; Rust or PyO3 changes;
  frontend/Study/protocol changes; replay-schema migration; activating the
  `02-beliefs-design.md` wave.

## Done when

- `freeze_conditional_snapshot` / `verify_conditional_snapshot` round-trip a
  synthetic conditional shard; fail-closed tests cover digest mismatch,
  `condition_schema` mismatch, missing `condition_index`/`condition_weight`/
  `condition_scores`, `CLAIM_BOUNDARY` drift, and file-set change (mirroring
  `tests/sim/test_teacher0_partial_snapshot.py`).
- The adapter ingests a `ConditionalStrategyResult`-shaped object (toy producer)
  into shard rows with no contract change; the shape digest is asserted at
  ingest.
- The conditioned student trains via `train_search_supervised` with the condition
  side input and is non-inferior (within Wilson-CI noise) to the non-conditioned
  baseline on `_student_vs_random` / `_matchup` across seeds; the matched-controls
  block holds (same fixture/init/split/optimizer/seed; only conditioning differs).
- The arena report explicitly states the toy condition source and the
  pre-registered ~0 prediction; **no strength claim is emitted**.
- `uv run pytest tests/sim/test_conditional_snapshot.py` passes; the ablation
  runner passes; `cargo` is untouched (no Rust changes).

## Measure

- **Pre:** the frozen Teacher-0 `policy_only`/`policy_value` seat-balanced win
  rates vs random (the existing arena baseline), re-captured at this PR's compute
  budget and seeds for a matched pre/post.
- **Post:** the four matched arms' win rates + Wilson 95% CIs vs random and vs
  the frozen Teacher-0 controls, plus label cost (conditions × decisions) and
  legality, at the same compute budget across multiple seeds.
- **"Better" is not the bar.** The pre-registered expectation is **~0 gap**
  (conditioning on an uninformative toy label). The measured quantity is the
  gap + its uncertainty, reported as a plumbing/measurement receipt. A non-zero
  gap would be a surprising, flagged falsification candidate, not a win. Per
  `wave/intelligence/GOAL.md`: pin content/engine/observation/action/model/
  opponent/compute identities; report seat-balanced strength with paired-deal
  uncertainty; win rate alone is insufficient (legality, label cost, and
  calibration reported alongside).

## Wave alignment

- **GOAL.md** — "Ablations remove ... search at the boundary of a working
  prototype so their effects on learning, transfer, strength, and systems cost
  are directly measurable." This is exactly such an ablation at the Teacher-0
  boundary. "Search teachers and students are compared in actual selected
  matchups at explicit compute budgets, with legality, competencies, seat-balanced
  strength, calibration, latency, throughput, label cost, and uncertainty." —
  reused verbatim. Evidence discipline (matched controls, multiple seeds, pinned
  identities, no claim until earned) is honored; the toy fixture is explicitly
  labeled and makes no superhuman claim.
- **MEMORY.md** — "Information-set honesty is part of the executable system"
  (`:54-56`): the toy condition is a public tag / uniform determinization, no
  hidden-info leakage. "Search can improve labels while making data expensive or
  information-set inconsistent; cost and honesty must be measured" (`:74-76`):
  label cost (K × decisions) is reported.
- **02-beliefs-design.md** — dormant/trigger-armed (`:22`); this slice does
  **not** activate it. The pre-registered ~0 prediction is consistent with the
  design's own "uniform-vs-weighted gap ~0 against random opponents" (`:192-194`).
  New risk introduced and named: pinning the `ConditionalStrategyResult` shape
  before INT-13 ships — mitigated by a digest-bound contract and a toy producer
  that exercises the real shape; if INT-13's shape diverges, the contract
  revision is a visible, reviewable schema bump, not a silent break.
