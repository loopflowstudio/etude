# W2-223 — Complete two-deck typed-IR admission and interpreter proof

## Directive (child v1, operative)
Close Semantic Programs and Choice ABI KR1 as the sole active Semantic successor
after W2-215. Start from current fetched main containing PR #92.

1. Audit every distinct UR Lessons vs GW Allies acceptance-slice card against the
   checked-in versioned typed IR; extend reviewed declarative inputs + offline
   compiler until the exact slice is fully admitted.
2. Generate a machine-checked coverage report.
3. Make the generic interpreter consume admitted programs WITHOUT card-name dispatch.
4. Add source checks + deterministic happy-path and interaction traces that FAIL
   on missing admission or name-based branches.
5. Preserve ContentPack binding, rules behavior, learning projection, and
   legacy/structured choice ABIs.

Exclude: runtime NL parsing, policy training, transfer experiments, general Magic
breadth. uv for all Python; rebuild CPython 3.12 extension after Rust changes.
Headless: validate proportionally, finish with `lf pr land -c`.
Do NOT start W2-214 or W2-213.

## Serial gate: W2-215 completed → clear to proceed.

## Findings
(filled in as I audit)
