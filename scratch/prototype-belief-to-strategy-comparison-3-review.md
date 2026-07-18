# GAM-6 Conditional Evidence Review

## What was implemented

The existing fixture-backed belief-to-strategy surface now consumes real
conditional determinized-PUCT evidence at its existing canonical decision.
One generator run holds the replay root, viewer, advisor, compute class,
paired seed plan, action vocabulary, and producer fixed while comparing a
creature-dense opponent-hand belief with its interaction-heavy complement.

The `AdviceArtifact` wrapper now enforces those comparison invariants in both
Python and TypeScript. `DecisionEvidence` robustness and uncertainty use real
per-world q-values rather than an aggregate favorable-world proxy and a scalar
uncertainty broadcast. The checked fixture, identity expectations, and truthful
README wording were updated. The component, routes, endpoint shape, and
fixture-backed runtime remain unchanged.

## Key choices

- Used the exact existing `erd1` decision and Study root. No second replay,
  protocol, or UI path was introduced.
- Used one four-world prior and complementary conditions with seed 197 and 16
  simulations per condition. Both displayed beliefs have support two.
- Matched the conditional planner to the player surface by ordered offer
  `(id, actor, verb)`. Diagnostic planner labels are intentionally not treated
  as player-facing labels.
- Accepted the measured 50/50 interaction policy instead of tuning the seed or
  budget for presentation. The curve policy is 43.75/56.25, so the conditional
  policy delta is real and visible.
- Kept runtime static. GAM-7 still owns live provider/checkpoint integration and
  its unavailable states.

## How it fits together

`generate_advice_fixture.py` restores the canonical decision, proves four
hidden-hand worlds preserve the viewer observation and offers, and runs one
paired conditional search twice. It maps the two complementary
`ConditionResult` values to the existing `DecisionEvidence` shape and writes
the same two-landmark `StudyArtifact` wrapper. The server continues to load
that checked artifact once and serve the same `GET/POST /api/advice` contract;
the shared Svelte component renders it in live and Study.

## Risks and bottlenecks

- Regeneration takes about one minute locally because it performs five aligned
  conditions twice for deterministic proof. Runtime requests remain static and
  do not pay this cost.
- The four exact worlds are a bounded curated prototype, not an inferred or
  exhaustive range. Labels state beliefs, not hidden truth.
- The advisor is determinized PUCT with a uniform random leaf evaluator. It
  makes no ISMCTS, equilibrium, strength, or latency claim.
- The installed local managym extension predates source-declared semantic JSON
  bindings. `tests/etude/test_semantic_boundary.py` therefore cannot pass until
  that extension is rebuilt; this branch changes no Rust or extension binary.

## What's not included

No runtime search, live provider, checkpoint loading, new belief-entry control,
component redesign, protocol schema, Retry/return behavior, watcher role,
generic range tool, chat, alternate decision, or Avatar content was added.

## Validation

- Generator: byte-identical fixture SHA-256 before/after regeneration:
  `9a6fc3cd845fe17c8f8a6b9406fec1a125907c944f14621e60dd11cc6c108225`.
- `uv run pytest tests/etude/test_advice.py -q`: 18 passed.
- `uv run python -m pytest tests/etude --ignore=tests/etude/test_semantic_boundary.py -q`:
  136 passed.
- `cd frontend && npm test -- --run`: 82 passed.
- `cd frontend && npm run check`: 0 errors and 0 warnings.
- `cd frontend && npm run build`: passed.
- `cd frontend && ETUDE_API_PORT=8027 ETUDE_FRONTEND_PORT=5197 npx playwright test advice.spec.ts`:
  5 passed.
- `uv run ruff check ...` and `uv run ruff format --check ...`: passed for
  changed Python files.
- Unavailable full Etude check: the installed `.so` lacks
  `semantic_decision_frame_json`, `semantic_observation_json`, and
  `execute_semantic_command_json`; isolated semantic-boundary tests fail on
  those missing methods before reaching GAM-6 code.
