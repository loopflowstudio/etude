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
