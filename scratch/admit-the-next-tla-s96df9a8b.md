# Jeong Jeong's Deserters Vertical Slice

> Reconstruction record, 2026-07-18. This document is derived from the
> durably approved parent review `ir_0fee3edab57b4de5807d7c0601ab7744` after
> its predecessor's uncommitted design-only worktree was removed. It is not a
> recovered artifact or a new review gate.

## Problem

The Playable Curated World needs terminal evidence for a creator-selected TLA
increment after the completed UR Lessons versus GW Allies world. The increment
must exercise the existing typed runtime and normal Etude consumers while
leaving the frozen RUL-9 evidence and default matchup identity unchanged.

## The demo

Run `./scripts/verify-tla-jeong-increment`. Literal seeds 1 and 3 cast Jeong
Jeong's Deserters, submit its mandatory target as the next structured Command,
observe the chosen creature gain one +1/+1 counter, and reach terminal with
identical states and ordered semantic events through production WebSocket,
direct headless, and persisted canonical replay paths.

## Approach

Add one immutable semantic pack, `tla-jeong-increment-v1`, by replacing one
Water Tribe Rallier with one Jeong in a same-slot `gw_allies_jeong` deck. Jeong
uses the admitted triggered-program, creature-selector, and `put_counters`
vocabulary. An additive exact-match pack catalog selects either the original
two-deck pack or this increment from the complete unordered pair of decklists;
no match or an ambiguous match fails closed. Etude selects an oriented asset
manifest and binds session content/replay identity to the compiled pack.

The evidence runner freezes the first two qualifying terminal seeds once,
replays their normal Commands across all three consumers, retains canonical
replays, and measures prompt/offer families, fallbacks, command latency,
throughput, RSS, and program-token pressure. Jeong's exact `Rebel` subtype is
kept in rules IR. Because the approved scope excludes a learning-schema
migration, full learning-definition projection is explicitly not claimed;
program tokens are measured against the unchanged v1 opcode/choice vocabulary.

## De-risking

| Question | Finding | Impact on design |
|---|---|---|
| Does Jeong require a new rules primitive? | No. Its ETB ability compiles to the existing target role, creature selector, and `put_counters` opcode. | Classify card semantics as content-only and reject any new opcode. |
| Can a second authored pack coexist without changing the default world? | Yes, if selection is exact over both complete decklists. | Use an additive immutable catalog; keep all original constants and assets as defaults. |
| Can pack selection silently choose the wrong semantics? | Exact zero-match selection falls back to the general registry and duplicate matches fail closed. | Test both seat orders, a tampered deck, and an intentionally ambiguous catalog. |
| Do fixed seeds prove the card actually played? | Seeds 1 and 3 both reach terminal and contain adjacent cast/target revisions with an observed counter delta. | Freeze only those literal tapes and retain their normal Commands and events. |
| Can token evidence reuse the frozen learning schema exactly? | The program can; the exact `Rebel` characteristic cannot be encoded by the frozen v1 subtype vocabulary. | Measure admitted program tokens and visible program pressure, preserve the exact rules subtype, and do not claim or add a learning schema in this Task. |
| Are inherited product budgets already known to miss? | RUL-9 missed live outer p95 and complete-game throughput while inner semantic, headless, replay, capacity, and RSS gates passed. | Keep the two live product budgets report-only; exactness, inner latency, engine throughput, RSS, fallback, and program capacity are required. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|---|---|---|
| Mutate the original two-deck pack and default deck | Smallest code diff, but rewrites the shipped world and invalidates frozen evidence. | Violates the approved immutable-addition and RUL-9 preservation requirements. |
| Dispatch packs by a requested string or card name | Easy selection but creates a second authority and permits deck/pack drift. | Exact deck content must select semantics fail-closed. |
| Add `Rebel` to the v1 learning schema | Enables full definition tokenization but changes checkpoint/schema identity. | Schema and Intelligence migration are explicitly out of scope. |
| Admit several nearby TLA cards | More breadth per release. | Fails the one-vertical-slice constraint and weakens terminal attribution. |

## Key decisions

- Preserve the original `ur-lessons-vs-gw-allies` pack, default named decks,
  compatibility content hash, asset hash, and frozen RUL-9 bytes.
- Orient the new Etude pack as `GW Allies — Jeong` versus `UR Lessons`, while
  Rust catalog matching remains seat-order independent.
- Keep Jeong exact as a 1/2 Human Rebel Ally with mandatory targeted ETB
  counter semantics.
- Treat the immutable catalog as the sole reusable kernel extension.
- Preserve complete raw evidence even when the two live product observations
  miss; never convert those misses into hidden fallback or a broader optimizer.

## Scope

- In scope: one Jeong card, one same-slot GW deck variant, one additive exact
  immutable pack catalog, focused rules/catalog/session tests, two literal
  terminal seeds, live/headless/replay parity, replay artifacts, and workload
  evidence.
- Out of scope: new opcode, learning schema, frontend interaction, Intelligence
  work, broad TLA completion, RUL-9 mutation, and live-release optimization.

## Done when

`./scripts/verify-tla-jeong-increment` passes debug Rust catalog/card tests,
checks both compiled semantic packs, rebuilds the Python extension, passes the
focused Etude tests, and verifies the immutable receipt and canonical replays
with zero required-gate failures.

## Measure

Before/after comparison uses the RUL-9 release budgets without rerunning or
rewriting its artifacts: live outer Command p95 100 ms and 1.0 games/s are
reported product observations; inner p95 ≤10 ms, headless/replay ≥500 steps/s,
RSS ≤512 MiB, program catalog ≤4096 tokens, individual program ≤160 tokens,
visible references ≤128, exactness mismatches zero, and all fallback/overflow
counters zero are required.
