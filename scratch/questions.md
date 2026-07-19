# INT-17 parent demo evidence

Review: `ir_7dc791772c524adbac77c5bab5cc01ac`

This matrix preserves the original `Done when` claims from
`scratch/commit-the-first-belief-calibration.md`. It does not relabel the
fail-closed systems result as calibration completion. R3 remains open.

## Original Done When matrix

| Original claim | Direct proof surface / action | Observed pass or gap |
| --- | --- | --- |
| The contract is committed before generation and binds the exact trace, checkpoint, viewers, hyperparameters, caps, identities, and directional prediction. | Code/test: `uv run pytest -q tests/sim/test_belief_calibration_runner.py tests/belief/test_audit.py`; inspect the frozen contract and the live raw/canonical hash guard in `test_rul13_requires_a_new_execution_contract_with_identical_science`. | **PASS.** 10 tests passed. The live v1 file matches raw SHA-256 `221ef4979ae0e4421efb5953473276b125cb5fdaf91983f4a66660b065d44473` and canonical SHA-256 `5b764e76820c2794c31aa57f7a8246478e8038721a1b74b93120b5832a246e75`; the real checkpoint is `1673a237…`, the cohort is one seed-0 game/two viewers/132 commands/62 commitments, fallback is forbidden, and the prediction/caps remain frozen. |
| The bounded demo command completes under the caps and two fresh runs produce byte-identical canonical result payloads. | Log/metric: inspect retained `failure.json` from the exact frozen command; do not rerun it. | **GAP.** The command did not complete. It was stopped fail-closed after 5,876 wall seconds / 5,078.04 CPU seconds and three completed commitment updates once static and measured evidence proved the full run would exceed the 21,600-second cap. No first result or second byte-identical result exists. |
| The retained result contains exactly 266 curve points, with zero integrity/provider failures and finite paired truth masses everywhere. | Receipt: inspect `failure.json` integrity and retained envelope. | **GAP.** `curves_emitted=0` and `result_retained=false`. Provider gaps observed were zero, but there are no truth-mass curve points to validate. |
| `summary.json` reports posterior/prior log loss and calibration, paired deltas, and selected-trace/OOD limitations. | Retained artifact tree / manifest validation. | **GAP.** No scientific result completed, so no `summary.json` exists. The retained four-file envelope is systems evidence only. |
| The experiment report records prediction versus result and the R4 interpretation rubric without claiming strength. | Report: inspect `experiments/int-17-belief-calibration.md`. | **GAP for the original scientific claim.** The report preserves the preregistered prediction, R4 rubric, and no-strength boundary, and records the measured systems failure. It cannot record prediction versus calibration result because no curves were produced. |
| `./scripts/verify-belief-calibration` builds the pinned extension, runs focused tests/provider verification, and completes scientific verify-only replay with `no_generation=true` and an unchanged retained tree. | Code/tests/preflight: focused pytest and ruff; `uv run --extra dev python experiments/runners/run_belief_calibration.py --contract experiments/contracts/int-17-belief-calibration-v1.json --out-dir .runs/int-17-belief-calibration-preflight --preflight-only`; inspect `verification.json`. | **GAP.** Focused tests and preflight pass, but scientific verify-only is explicitly `not_applicable` because no retained scientific result exists. Replaying it would repeat the proven over-cap O(S^2) workload. No `no_generation=true` scientific replay claim is made. |
| No Rust is changed; if Rust is touched, run the full debug suite. | Task diff state: `lf task changes INT-17 --json`. | **PASS.** The shipping diff contains no file under `managym/src`; the conditional full-debug requirement was not triggered. |

## Direct closure evidence

| Evidence | Action | Observed result |
| --- | --- | --- |
| Live contract identity and v2 continuity guard | Focused pytest command above | 10 passed in 5.35s. The guard checks both live raw and canonical v1 identities and requires v2 to copy `trace`, `likelihood_checkpoint`, `algorithm`, `cohort`, `preflight`, `metrics`, `prediction`, `caps`, `arena_interpretation`, and `exclusions` exactly. Only `provider_receipt` and `expected_runtime` may refresh after RUL-13. |
| Python lint/format | `uv run ruff check tests/sim/test_belief_calibration_runner.py tests/belief/test_audit.py`; `uv run ruff format --check ...` | All checks passed; both files already formatted. |
| Selected-trace preflight | Runner `--preflight-only` command above | `preflight_passed`: 132 commands, 62 commitments, 903,063 rows, viewer rows 694,187/208,876, action updates 33/29, maximum supports 121,485/41,806, identity stream SHA-256 `8d9f46fa86742f323f915ba69bc0007e225415e8fc5504792eac4b49ebae66b6`. |
| Content-addressed retained envelope | Validate every manifest entry with `wc -c` and `shasum -a 256`, then validate receipt identity and total bytes | Passed: 4 files, 9,308 bytes, failure receipt SHA-256 `78bde491e16957b743a59cebe6f87fd519dc982793d5d6f7dbb649a98d57e027`. |
| Measured/static root cause | Inspect the likelihood row loop, Python-to-engine materialization call, `Env.materialize_possible_world`, `PossibleWorldSpace::for_viewer/from_parts`, and retained `failure.json` | `FrozenPolicyLikelihood` materializes every support row; each call reconstructs and recursively enumerates the whole support. Worst update: `121,485^2 = 14,758,605,225` row-enumeration operations. Observed schedule: `sum(S^2) = 51,506,080,901`; even the optimistic measured projection is 41,575.37 seconds (11.55 hours), above the frozen 6-hour cap. |

## Review boundary

The shippable result is the immutable v1 contract and content-addressed failure
receipt, the retained clean stop with no partial scientific result, the
measured O(S^2) systems proof, and the computable post-RUL-13 v2 path. It is not
a calibration demo, a threshold result, a belief-quality claim, or a strength
claim.

The 266 curves, `summary.json`, paired calibration metrics, and scientific
verify-only replay remain unachieved. R3 therefore remains `evidence_wait` and
open. Do not rerun v1. After RUL-13 lands, preregister v2 with the exact copied
scientific sections and refreshed provider/runtime identities before executing
the same science.
