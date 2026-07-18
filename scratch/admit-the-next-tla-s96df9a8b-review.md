# Jeong Vertical Slice Review Guide

## What was implemented

RUL-10 admits `Jeong Jeong's Deserters` through a new immutable
`tla-jeong-increment-v1` semantic pack and a `gw_allies_jeong` deck that swaps
one Water Tribe Rallier for one Jeong. The original authored pack, default
matchup, and four frozen RUL-9 files remain byte-identical.

The sole reusable kernel extension is an additive pack catalog. managym selects
one compiled pack only when the complete unordered pair of decklists matches
exactly; no match retains the general registry and ambiguity fails closed.
Etude independently selects the oriented asset manifest and verifies that it
matches managym's compiled pack before exposing session/replay content identity.

## Key choices

- Jeong is content-only rules semantics: its mandatory ETB target and +1/+1
  counter use the existing typed trigger, selector, target role, and
  `put_counters` opcode.
- The new deck is a separate named variant. No existing deck constant, pack,
  compatibility content hash, or default selection was mutated.
- Literal seeds 1 and 3 were frozen after one qualifying scan. Each contains
  adjacent cast and target Commands plus a directly observed counter delta.
- Replay measurement consumes Commands deserialized from the retained canonical
  replay artifacts, not a parallel reconstructed action tape.
- Jeong's exact `Rebel` subtype is preserved in rules IR. The frozen v1 learning
  vocabulary cannot encode that new characteristic, so this Task measures the
  admitted program tokens and explicitly does not claim full learning-definition
  projection or migrate a schema.

## How it fits together

The semantic compiler produces a checked-in Jeong IR snapshot. managym's exact
deck catalog binds that IR to the match's immutable ContentPack; Etude binds the
corresponding asset manifest and drives normal structured Commands. The evidence
runner records one authority per seed, executes it through production WebSocket
and direct headless paths, then executes the persisted canonical replay Commands
and compares every revision witness and ordered semantic event group.

## Risks and bottlenecks

- Exact catalog selection is intentionally strict. A deck-content change must
  produce a new immutable catalog entry or it falls back to the general pack.
- Full semantic learning projection remains fail-closed on `Rebel`; resolving
  that requires a separately reviewed schema/checkpoint migration.
- On this host, live outer Command p95 is 106.653 ms versus the inherited 100 ms
  product budget and live completion is 0.324 games/s versus 1.0/s. Inner p95
  is 2.477 ms and headless/replay exceed 1,039 steps/s, so optimization remains
  outside this content-admission Task.

## What's not included

No new opcode, learning schema, frontend interaction code, Intelligence work,
broad TLA completion, RUL-9 evidence mutation, or live-release optimization.

## Validation

- `./scripts/verify-tla-jeong-increment`: pass.
- Full debug `cargo test --manifest-path managym/Cargo.toml --no-fail-fast`:
  pass.
- `cargo clippy --manifest-path managym/Cargo.toml --all-targets --all-features -- -D warnings`:
  pass.
- Focused Etude authority/parity/pack/session tests: 25 pass.
- Rust formatting and changed-Python Ruff checks: pass.
- Evidence: two terminal seeds, 10 measured games per surface, zero exactness
  mismatches, fallbacks, overflows, or unadmitted visible definitions.

