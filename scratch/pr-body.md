## Try it!

Run the focused construction suite:

```bash
uv run pytest tests/sim/test_teacher1_evidence.py \
  tests/sim/test_teacher1_pilot.py \
  tests/sim/test_mcts.py -q
```

Expected result: `24 passed`.

Inspect the runtime fingerprint that must match the frozen contract:

```bash
uv run experiments/runners/run_teacher1_pilot.py --print-runtime
```

On the declared machine, the host detector resolves `Apple M4 Max`, and the
printed `pilot_source_sha256` is
`40af43d7eeea333ce7a20792774487abcfacff7137f1f30ef9afe1e3a3844f21`.

The admission command intentionally fails before creating output while the
control lock is absent:

```bash
uv run experiments/runners/run_teacher1_pilot.py \
  --contract experiments/contracts/w2-234-teacher1-pilot-v1.json \
  --control-lock experiments/contracts/w2-234-teacher1-control-lock-v1.json \
  --stage teacher-gate \
  --out-dir .runs/w2-234-teacher1-pilot-v1
```

## Intent

Harden the Teacher-1 admission boundary so only same-host, internally
consistent calibration evidence can unlock the preregistered 8/32/128 pilot.
This keeps foreign-host or malformed latency receipts from influencing the
experiment before any search work begins.

## Assumptions

- The evaluation surface is the contract-declared Apple M4 Max host with the
  pinned macOS, Python, Torch, machine, and MPS tuple.
- Teacher-0 simulations per legal action and Teacher-1 total tree traversals
  are comparable only through same-host realized p50 latency.
- Admission remains blocked until the terminal Teacher-0 manifest, exact
  `policy_value` checkpoint, calibration artifact, and reviewed control lock
  exist.

## Key decisions

- Bind contract, live runtime, control lock, and calibration to one explicit
  host tuple including chip identity.
- Discover the chip with the repository's established `sysctl` plus processor
  fallback pattern.
- Derive `abs(flat_p50 - teacher_p50) / teacher_p50` from raw finite values,
  require a positive Teacher-1 denominator, and reject a conflicting reported
  gap.
- Apply the contract-owned `max_realized_p50_gap` and validate all admission
  prerequisites before creating the output directory.

## Not included

This PR creates no calibration, control lock, Teacher-1 result, or student
artifact. It does not run admission or distillation, alter the preregistered
experiment, or supply Search Project KR 2-5 evidence.
