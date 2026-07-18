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
