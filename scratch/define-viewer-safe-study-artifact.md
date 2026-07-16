# W2-222 — viewer-safe study artifact v1

## User-visible outcome

A Study consumer can open one immutable artifact from the curated UR Lessons
versus GW Allies matchup and restore the exact historical `ExperienceFrame`,
the exact selected `InteractionOffer`, and the played `Command` as that player
saw them. Policy mass, search value, visits, sampled-world robustness,
uncertainty, and provenance remain separately labelled evidence rather than a
single confidence score.

## Source of truth

Rust wire types in `managym/src/study.rs` own the closed, versioned JSON
contract and generate `protocol/study-v1.schema.json`. The checked-in
`protocol/fixtures/study-curated-decision.json` is the executable shared record.
Python Pydantic and TypeScript declarations are explicit consumers of that
schema and fixture. The artifact embeds, rather than reconstructs, the
historical `ExperienceFrame` and chosen `InteractionOffer`.

## End-to-end proof

The shared fixture pins the content/asset pack, engine build, model checkpoint,
match/state/viewer/decision/prompt/offer, and analysis budget. Rust, Python, and
TypeScript each validate and round-trip the same fixture, assert that the
embedded frame/offer/played command identities agree, and reject mutations
that add an opponent hand card or an RNG seed. Rust and Python also compare
their concrete field shapes to the Rust-generated schema.

Verification:

```bash
cargo test --locked --manifest-path managym/Cargo.toml study_protocol
uv run pytest -q tests/gui/test_study_protocol.py
npm --prefix frontend test -- --run src/lib/study-protocol.test.ts
```

## Affected surfaces and consumers

- Rust: new study authority types, validation, schema exporter, and fixture
  conformance tests.
- Python: closed Pydantic study models reusing the existing experience models.
- TypeScript: wire declarations and a fail-closed viewer-safety assertion.
- Protocol artifacts: one generated schema, one shared fixture, and precise
  documentation of the certification boundary.
- Existing Match, Replay, learning observations, experience protocol v1, and
  their fixtures remain byte-for-byte compatible.

## Absent and error states

- V1 requires one or more landmarks and one or more alternatives; missing
  evidence is not represented as a fabricated zero.
- Every metric refers to an admitted alternative and every binding must agree
  with the embedded historical frame and offer.
- Opponent hand identities are forbidden even though the transitional
  `ExperienceFrame` type can represent them for a player's own hand.
- Unknown fields—including RNG seeds—fail at all three typed boundaries.
- Hindsight evidence is absent from v1; it cannot be smuggled into default
  evidence as an optional sidecar.

## Operational boundary

The artifact is portable JSON and validation is deterministic and offline.
This task sets no search, storage, latency, or branch-execution budget beyond
the identities already recorded in the artifact.

## Exclusions

No landmark-selection algorithm, search execution, branch/fork lifecycle, UI,
client-side legality, annotations, sharing service, or hindsight lens. Those
consume this contract in later Study tasks.
