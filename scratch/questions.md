# RUL-12 Execution Assumptions

- The first independently bound RUL-12 run (`71641a8c...`) is an honest
  contention miss and must remain available. It regressed every compute-bound
  control at once—live inner p95 16.969 ms, headless 239.2 steps/s, replay
  193.0 steps/s, and training 218.7 traversals/s—while exactness, capacity,
  RSS, and all fallback counters passed. At characterization time the 16-logical-
  CPU host reported load averages 155.49/232.35/169.88 and concurrent search
  and calibration workloads.
- Proceed without another production optimization. Preserve the contended
  receipt under an explicit diagnostic name, then rerun the exact unchanged
  RUL-12 contract only after a one-game live/headless probe returns to the
  already observed non-contended range. This treats host capacity as the next
  measurement decision and does not weaken, trim, or reinterpret any budget.
- A 2026-07-18 control recovered to live p95 41.035 ms, inner p95 3.176 ms,
  1.063 games/s, and 501.2 headless steps/s at one-minute host load 8.48.
  During the immediately following unchanged full measurement, load rose to
  37.52/83.36/87.39 and every independent compute control regressed again:
  live p95 131.500 ms, inner p95 12.886 ms, headless/replay 255.5/280.5
  steps/s, and training 246.8 traversals/s. Preserve artifact `c14f3a43...`
  as `control-recovered-then-contended`; it is not admission evidence.
- Two subsequent 2026-07-18 controls correctly rejected another full run even
  after the one-minute load average fell: at load 7.07, live p95 was 90.013 ms
  but completion was 0.616 games/s and headless was 410.4 steps/s; at load
  5.62, live p95 was 85.920 ms but completion was 0.614 games/s and headless
  was 445.7 steps/s. Both retained zero authority fallbacks. `lf top` and the
  process table named the active confounders: the INT-17 belief-calibration
  runner remained near 100% CPU while release Rust/Xcode builds repeatedly
  entered and left the host. A fresh check at 16:59 local still showed 72
  Loopflow/provider processes, belief calibration at 99.5% CPU, and a release
  Rust compile at 96.2% CPU. The bounded next check is to rerun `lf top` after
  those named jobs clear, then rerun exactly one live/headless control; only a
  control satisfying live p95 <= 100 ms, inner p95 <= 10 ms, live completion
  >= 1.0 games/s, headless >= 500 steps/s, and zero fallbacks admits the full
  unchanged measurement.
- A bounded 2026-07-18 17:05 local recheck still rejected even the one-game
  control before execution. `lf top` reported 70 Loopflow/provider processes;
  after a 55-second `lf task wait INT-17`, that Task remained active and its
  belief-calibration runner (PID 88729) still consumed 92.7-100% CPU. A
  concurrent Loopflow release Rust link (PID 60550) consumed 300.2% CPU, and
  host load was 13.19/13.50/25.93. Per the pre-registered two-stage boundary,
  no control, default receipt, or full workload was started. The next bounded
  check remains `lf top` after INT-17 and the release build clear, followed by
  exactly one live/headless control before any formal run.
- The 2026-07-18 17:13 local changes-requested retry also failed host admission
  before the control. INT-17 remained active after a bounded 55-second wait,
  an unrelated Kata Xcode build was still spawning many parallel clang jobs,
  and load reached 157.56/66.21/40.50. The completed review still requires a
  clean-host control, passing unchanged full run, default receipt/report,
  `RUL12_RELEASE_STACK_OK`, and fresh PR CI; none can be claimed from this
  rejected admission check, and no evidence path was written or relabeled.

## Passing host admission and formal run

- On 2026-07-18 at 18:21 America/Los_Angeles, the immediately preceding
  process-level monitor reached `host_check_7=4 active_busy=0` at load averages
  6.89/10.79/10.10. A fresh `lf top` named no active search, training,
  calibration, Rust/Xcode build, or test workload; the accompanying `uptime`
  snapshot was 6.37/10.55/10.03. The exact one-game control command was:

  ```bash
  uv run python - <<'PY'
  import json
  from experiments.runners import run_rul9_played_workloads as rul9

  authority = rul9._authority()
  live = rul9._measure_live_game(authority)
  headless = rul9._measure_engine_game(authority, "headless")

  live_ms = [float(row["duration_ms"]) for row in live["protocol_commands"]]
  inner_ms = [float(row["duration_ms"]) for row in live["inner_commands"]]
  result = {
      "live_command_p95_ms": rul9.percentile(live_ms, 0.95),
      "inner_command_p95_ms": rul9.percentile(inner_ms, 0.95),
      "live_games_per_second": 1.0 / float(live["game_seconds"]),
      "headless_steps_per_second": float(headless["commands"])
      / float(headless["game_seconds"]),
      "live_game_seconds": float(live["game_seconds"]),
      "headless_game_seconds": float(headless["game_seconds"]),
      "live_fallback_counters": live["fallback_counters"],
      "headless_fallback_counters": headless["fallback_counters"],
      "terminal_state_sha256": live["terminal_state_sha256"],
      "logical_trace_sha256": live["logical_trace_sha256"],
  }
  result["admitted"] = (
      result["live_command_p95_ms"] <= 100.0
      and result["inner_command_p95_ms"] <= 10.0
      and result["live_games_per_second"] >= 1.0
      and result["headless_steps_per_second"] >= 500.0
      and all(value == 0 for value in result["live_fallback_counters"].values())
      and all(
          value == 0 for value in result["headless_fallback_counters"].values()
      )
  )
  print(json.dumps(result, indent=2, sort_keys=True))
  raise SystemExit(0 if result["admitted"] else 2)
  PY
  ```

- That control exited 0 with live Command p95 29.4016373 ms, inner Command
  p95 1.93127905 ms, 1.69964991 complete games/s, and 1093.8694 direct
  headless steps/s. All four live and all four headless authority fallbacks
  (`legacy_fixed_action`, `card_name_dispatch`, `candidate_cap`, and
  `client_legality`) were zero. Its live/headless durations were
  0.588356459/0.120672541 seconds, terminal witness was `e48de247...de7`, and
  ordered logical trace was `d326bb05...da2`.
- Only after that control passed all five thresholds did
  `uv run python experiments/runners/run_rul12_release_stack_budget.py` run
  the unchanged warmup, ten-game live/headless/persisted-replay release cell,
  and the unchanged `full_clone/current_game_v1` four-worker x 128-simulation
  training cell. It exited 0 and wrote the default receipt/report with artifact
  identity `052a588f...0176` and `RUL12_RELEASE_STACK_OK`: live p50/p95
  4.969/30.659 ms, 1.649 games/s, headless/replay 1046.3/1047.1 steps/s,
  training 1394.8 traversals/s, and zero fallbacks, overflow, or provider gaps.
