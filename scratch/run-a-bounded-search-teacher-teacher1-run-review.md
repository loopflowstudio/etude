# PR #114 review guide

## What was implemented

The Teacher-1 admission runner now treats the declared evaluation host and raw
latency measurements as authoritative inputs to the control lock. Contract,
live runtime, control lock, and calibration must agree on the complete host
identity, including the Apple chip. The runner derives the relative p50 gap
from the measured Teacher-1 and Teacher-0 p50 values, rejects inconsistent
reported gaps, and applies the threshold owned by the frozen contract.

The contract's pinned runner hash and the pre-run experiment note were updated,
and focused regressions cover foreign chips, invalid latency values, a
nonpositive Teacher-1 denominator, inconsistent reported gaps, and a changed
contract threshold.

## Key choices

- Chip discovery follows `scripts/bench_branching.py`: query
  `machdep.cpu.brand_string`, then fall back to `platform.processor()`.
  Architecture alone cannot distinguish the declared M4 Max from another
  arm64 Mac.
- `teacher_p50_decision_ms` and `flat_p50_decision_ms` are authoritative. The
  redundant `relative_p50_gap` remains in the artifact for readability but
  must match the derived value within a strict floating-point tolerance.
- The runner validates runtime fingerprints before loading the control lock,
  and validates the lock before creating the output directory. Missing or
  inconsistent prerequisites therefore fail without starting search work or
  leaving a partial run artifact.
- The contract owns `max_realized_p50_gap`; the runner does not duplicate the
  threshold.

## How it fits together

`w2-234-teacher1-pilot-v1.json` pins the host, source hash, budgets, and gate.
At admission, the runner projects the contract, current runtime, control lock,
and calibration onto one host tuple, verifies the frozen recovery/checkpoint
receipts, derives each latency gap, and only then creates the Teacher-1 run
directory. The tests construct valid receipts and perturb one boundary at a
time to prove fail-closed behavior.

## Validation

- `uv run pytest tests/sim/test_teacher1_evidence.py tests/sim/test_teacher1_pilot.py tests/sim/test_mcts.py -q` — 24 passed.
- `uv run ruff format --check experiments/runners/run_teacher1_pilot.py tests/sim/test_teacher1_pilot.py` — passed.
- `uv run ruff check experiments/runners/run_teacher1_pilot.py tests/sim/test_teacher1_pilot.py` — passed.
- GitHub CI on `d03a717c3fc9b1c84cac0ad7a1301ab805f3bcbb` — all checks passed, including debug Rust tests, Python unit tests, both integration platforms, protocol conformance, clean-machine play, and semantic gates.
- Runtime and contract `pilot_source_sha256` — both
  `40af43d7eeea333ce7a20792774487abcfacff7137f1f30ef9afe1e3a3844f21`.

## Risks and bottlenecks

- Host matching is intentionally strict. An OS, Python, Torch, MPS, machine,
  or chip change requires a new reviewed contract/calibration rather than
  silently reusing old latency evidence.
- The reported gap remains redundant data. Rejecting disagreement prevents it
  from becoming a second source of truth.
- Teacher-1 admission is still blocked on the terminal Teacher-0 manifest,
  exact `policy_value` checkpoint, same-host calibration, and checked-in
  control lock. This PR does not remove that operational dependency.

## What's not included

- No calibration or control-lock artifact.
- No Teacher-1 8/32/128 admission run or result.
- No student training or distillation.
- No claim toward Search Project KRs 2-5; the Project remains KR1-only.
- No change to matchup, seeds, budgets, predictions, quality gates, or branch
  decisions.
