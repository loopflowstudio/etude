# RUL-9: Played Release and Training Workloads

**Run:** 2026-07-18  
**Matchup:** UR Lessons versus GW Allies  
**Verdict:** **MISS** — release miss, training pass, capacity pass, fallbacks pass.

## Result

The current source reproduced one exact 132-Command semantic trace across live, headless, and persisted replay execution, and all authority, search, cap, projection, and overflow counters remained zero. Headless release, persisted replay, and the saturated selected BranchDriver teacher stayed within every pre-registered budget. Etude live play did **not**: WebSocket Command p95 and complete-game throughput missed, so the fail-closed verifier exits nonzero. The selected representation remains `full_clone/current_game_v1`; the isolated miss is in live presentation/protocol work around native Command apply.

| Workload | Command p50 / p95 | Step throughput | Complete games | Peak RSS |
|---|---:|---:|---:|---:|
| Live release | 31.590 / 150.492 ms | 45.7/s | 0.347/s | 322.2 MiB |
| Headless release | 0.598 / 1.403 ms | 588.5/s | 4.458/s | shared release cell |
| Persisted replay | 0.596 / 1.380 ms | 599.1/s | 4.538/s | shared release cell |
| 4×128 training | 0.047 / 0.093 ms | 4.520 roots/s; 578.5 traversals/s | 0.0429/s | 1026.2 MiB |

Training PUCT decision latency was 648.3 ms p50 and 1398.1 ms p95 across 421 root Commands and 53888 traversals. All 4 games reached terminal.

## Budget-miss attribution

The live miss is outside native semantic Command apply. Live inner Command p95 was 4.291 ms against the 10 ms budget, while the complete WebSocket round trip was 150.492 ms p95 against 100 ms. Live completion was 0.347 games/s against 1.0. Direct headless and persisted replay passed at 588.5 and 599.1 steps/s, and the selected training cell passed at 4.520 root steps/s, 578.5 traversals/s, 0.04294 complete games/s, 1,398.1 ms PUCT p95, and 1,026.2 MiB peak RSS.

The immediately preceding pre-rebase source/binary receipt had passed live play, but publication changed eight shared Command/search/provider files and the native extension identity. A post-rebase one-repetition calibration and two full exact runs all reproduced the live protocol/completion miss while native apply remained within budget; the final clean run restored every headless and training gate. This isolates the decision-bearing regression to the current Etude live consumer path rather than branch representation, semantic capacity, or host-wide engine throughput. RUL-9 does not modify those shared providers or tune its precommitted contract.

## Semantic capacity diagnosis

The compiled catalog contains 2088 active tokens. Tokens per admitted definition were 70 p50, 97 p95, and 148 maximum. Acting-view decisions reached 42 visible references with zero projection failure, unadmitted definition, truncation, or overflow.

The final workload did not reproduce the exploratory expanded-token pressure: its counterfactual maximum was 2493, below the 4,096 diagnostic frontier. The selected shared catalog remained 2088 active tokens with definition-row references and zero overflow. At the maximum, zone attribution was `{"battlefield": 851, "exile": 148, "graveyard": 1273, "hand": 221}`. The pre-registered capacity gates pass without clipping; retain `full_clone/current_game_v1` and the shared catalog/reference representation.

## Integrity

- Authority receipt: `57f5e2d1a482831e4e1edeb69632c0b34030d2e8ee701eab9b13b484c6937147`.
- Frozen PR #153 parity provenance: `af198bb3dcac542a6a34f1cbd8250938e4bfe0e79df7d284de4928873cc0f60b`; current parity is re-executed and retained by this RUL-9 receipt.
- Source closure: `3f612831df0179a6ce0c1f85e2def15cec6c68ae880ebea5d60d75e8bae13ac2` over 99 files.
- Native extension: `c95a85bba1128e6c3afdade5b5cf59dfeb3b1ec464fdd403cdf155a5cf834f8e` (_managym.cpython-312-darwin.so, release profile).
- Contract: `9593f3ff7e0eed8fe0316ec66bda96296815480fc0d37441831586cdf3555477`.
- Evidence artifact: `498df1eda031f1d6ea68f72f792dfd68195bb10eab884b006aa7772b586564da`.
- Release RSS samples: 842; training RSS samples: 782.
- Raw evidence retains every Command/PUCT duration, game duration, RSS sample, semantic census row, terminal hash, outcome hash, and fallback/cap counter.

## Strongest confound

These absolute performance gates were measured on one Apple Silicon host with many Loopflow/provider processes resident. Exact workload, source, toolchain, worker topology, and binary identities are pinned, but another host may move latency and RSS without changing the representation. A contended full run also slowed headless and training cells; it was rejected. After a quiescent interval, the checked run restored those cells while repeating only the live miss. The fail-closed verifier retains that miss instead of tuning the contract or silently carrying forward the pre-rebase pass.

## Reproduce

```bash
uv run experiments/runners/run_rul9_played_workloads.py \
  --contract experiments/contracts/rul-9-played-workloads-v1.json \
  --out experiments/data/rul-9-played-workloads-v1.json \
  --report experiments/rul-9-played-workloads-v1.md
./scripts/verify-rul9-played-workloads
```
