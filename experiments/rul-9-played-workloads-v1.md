# RUL-9: Played Release and Training Workloads

**Run:** 2026-07-18  
**Matchup:** UR Lessons versus GW Allies  
**Verdict:** **PASS** — release pass, training pass, capacity pass, fallbacks pass.

## Result

The fixed release tape and the saturated selected BranchDriver teacher both stayed within their pre-registered budgets. All authority, search, cap, projection, and overflow counters remained zero. The selected representation remains `full_clone/current_game_v1`.

| Workload | Command p50 / p95 | Step throughput | Complete games | Peak RSS |
|---|---:|---:|---:|---:|
| Live release | 2.683 / 28.250 ms | 236.2/s | 1.789/s | 319.2 MiB |
| Headless release | 0.535 / 0.960 ms | 804.1/s | 6.092/s | shared release cell |
| Persisted replay | 0.539 / 0.923 ms | 811.1/s | 6.145/s | shared release cell |
| 4×128 training | 0.037 / 0.067 ms | 5.921 roots/s; 757.9 traversals/s | 0.0563/s | 1022.6 MiB |

Training PUCT decision latency was 479.1 ms p50 and 1181.2 ms p95 across 421 root Commands and 53888 traversals. All 4 games reached terminal.

## Semantic capacity and miss diagnosis

The compiled catalog contains 2088 active tokens. Tokens per admitted definition were 70 p50, 97 p95, and 148 maximum. Acting-view decisions reached 42 visible references with zero projection failure, unadmitted definition, truncation, or overflow.

The final workload did not reproduce the exploratory expanded-token pressure: its counterfactual maximum was 2493, below the 4,096 diagnostic frontier. The selected shared catalog remained 2088 active tokens with definition-row references and zero overflow. At the maximum, zone attribution was `{"battlefield": 851, "exile": 148, "graveyard": 1273, "hand": 221}`. The pre-registered capacity gates pass without clipping; retain `full_clone/current_game_v1` and the shared catalog/reference representation.

## Integrity

- Authority receipt: `57f5e2d1a482831e4e1edeb69632c0b34030d2e8ee701eab9b13b484c6937147`.
- Parity receipt: `af198bb3dcac542a6a34f1cbd8250938e4bfe0e79df7d284de4928873cc0f60b`.
- Source closure: `3af31e03f43e06f500e1328d7015a2b74b82f10899a4540b17849b2c41880705` over 99 files.
- Native extension: `cddf332ca1101584a406991ef7136c6713322b51b17df1634488bfa07e76e6f1` (_managym.cpython-312-darwin.so, release profile).
- Contract: `9593f3ff7e0eed8fe0316ec66bda96296815480fc0d37441831586cdf3555477`.
- Evidence artifact: `3b9a2dbd025edcd16f193b86e40ad24150f4861b83e37658aa45ab0437019ba6`.
- Release RSS samples: 281; training RSS samples: 660.
- Raw evidence retains every Command/PUCT duration, game duration, RSS sample, semantic census row, terminal hash, outcome hash, and fallback/cap counter.

## Strongest confound

These absolute performance gates were measured on one Apple Silicon host. Exact workload, source, toolchain, worker topology, and binary identities are pinned, but another host may move latency and RSS without changing the representation. The fail-closed verifier treats such drift as new evidence, not as a continuation of this run.

## Reproduce

```bash
uv run experiments/runners/run_rul9_played_workloads.py \
  --contract experiments/contracts/rul-9-played-workloads-v1.json \
  --out experiments/data/rul-9-played-workloads-v1.json \
  --report experiments/rul-9-played-workloads-v1.md
./scripts/verify-rul9-played-workloads
```
