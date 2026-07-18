## Try it!

Regenerate the checked conditional evidence, then exercise the adapter and
surface:

```bash
uv run --extra dev python scripts/generate_advice_fixture.py
uv run pytest tests/etude/test_advice.py -q
cd frontend
npm test -- --run advice
ETUDE_API_PORT=8027 ETUDE_FRONTEND_PORT=5197 npx playwright test advice.spec.ts
```

The generator writes the same fixture SHA-256 on repeated runs:
`9a6fc3cd845fe17c8f8a6b9406fec1a125907c944f14621e60dd11cc6c108225`.
At the pinned “Play Mountain” / “Pass priority” decision, the creature-dense
belief produces policy 43.75% / 56.25%; the interaction-heavy belief produces
50% / 50%, with explicit value and per-world uncertainty deltas.

## Intent

Finish GAM-6’s existing fixture-backed comparison with evidence whose visible
delta is attributable to belief conditioning. The old fixture compared two
flat-MC seed families; this one uses a single paired conditional-PUCT plan at
the same canonical decision while keeping the player surface and request shape
unchanged.

## Assumptions

- Four exact ten-card GW Allies hands are a bounded, meaningful prototype world
  space, not an inferred production range.
- Determinized PUCT with a uniform leaf evaluator is advisory evidence, not a
  strength, ISMCTS, or equilibrium claim.
- The installed extension’s missing semantic JSON bindings are an unrelated
  local build mismatch; the 136 unaffected Etude tests pass.

## Key decisions

- One advisor (`conditional-determinized-puct-v1`), compute class
  (`2w-16s-paired-seed-197`), seed plan (`paired-seed-197`), and producer are
  shared across both beliefs.
- The generator rejects viewer-observation drift, offer identity/order drift,
  non-determinism, identical policies, and cross-scenario producer drift.
- Robustness and uncertainty are calculated from real per-world q-values.
- The measured 50/50 policy is preserved; no seed tuning for visual effect.

## Not included

Live provider integration (GAM-7), new belief inputs, UI redesign, protocol
changes, Retry/return work, watcher roles, chat, or generic range tooling.
