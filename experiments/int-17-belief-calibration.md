# INT-17: first belief-calibration curves

**World:** w2  
**Evidence class:** one-seed selected-trace calibration only  
**Status:** preregistered, unrun  
**Contract:**
[`contracts/int-17-belief-calibration-v1.json`](contracts/int-17-belief-calibration-v1.json)

## Question

Across the retained seed-0 UR-Lessons-versus-GW-Allies decision sequence, does
the frozen INT-7 action-likelihood policy assign more mass to the actual hidden
opponent hand than the contemporaneous compatible-deal prior does?

## Prediction

For each fixed viewer, over opponent public-commitment points:

- mean `prior_log_loss_nats - posterior_log_loss_nats` is greater than zero;
- posterior true-hand mass exceeds prior true-hand mass on more than 55% of
  points.

This checkpoint was trained on the interactive mirror rather than the selected
curated matchup. Refutation is therefore a first-class result and will be
retained without choosing another checkpoint.

## Frozen inputs

- Authority trace: seed 0, 132 Commands, SHA-256 `57f5e2d1...`.
- RUL-11 provider receipt: 62 provider commitments, zero exercised gaps,
  SHA-256 `45227941...`.
- Likelihood checkpoint: INT-7 `visit_policy_only` seed 197, SHA-256
  `1673a237...`.
- Tracker: exact compatible-deal support, `epsilon=0.05`, counterfactual seed
  907, batch size 256, CPU.
- Cohort: both viewers, one initial point plus every transition, 266 total
  points.

Truth is read only after each tracker update by the authority audit. It is not
an input to the Observation, likelihood model, tracker, or command tape.

## Cost cap

The checked preflight contains 903,063 counterfactual world rows: 694,187 for
viewer 0 and 208,876 for viewer 1. Generation is limited to one worker,
1,000,000 rows, six wall/core hours, 2 GiB peak RSS, and 64 MiB of retained
bytes. Crossing any cap yields no calibration result.

## Run

```bash
uv run --extra dev python experiments/runners/run_belief_calibration.py \
  --contract experiments/contracts/int-17-belief-calibration-v1.json \
  --out-dir .runs/int-17-belief-calibration-v1
```

## Interpretation boundary

The points are serially correlated observations from one fixed trace. The
result can describe this checkpoint's selected-trace belief updates; it cannot
establish general calibration, a method effect, or gameplay strength.

R4 will join this result to the exact-range arena by world, matchup,
checkpoint, epsilon, and player identity. Better calibration without an arena
gain points to planner use or matched-cost effects; worse calibration alongside
an arena loss points first to the likelihood model. Crossed results remain
ambiguous and do not support a causal strength claim.

## Result

Pending the bounded frozen run.
