# Expose a Canonical Replay Decision Index

## Problem

Completed games currently have two incompatible histories. The live protocol
publishes a viewer-safe `ExperienceFrame`, exact `InteractionOffer`s, a bound
`Command`, and ordered `PresentationEvent`s. The persisted trace instead keeps
pre-action legacy observations, raw action lists, and raw action indices. The
replay page reconstructs frames from adjacent trace observations, and Study's
`decision_id` is an unchecked string. There is therefore no durable address
that means "the exact choice this player saw here," and restoring a guessed
trace offset can drift in perspective, prompt, offer, presentation position,
or private information.

W2-275 gives Game one canonical, viewer-scoped decision history for the pinned
UR Lessons versus GW Allies replay. Every deliberate command by the historical
viewer receives a chronological address bound to the immutable replay, match,
viewer, revision, prompt, selected offer, command, and presentation cursor.
Restoration returns the exact stored viewer-safe frame and offer; it never
reconstructs either from a later engine state. Opponent policy activity,
configured auto-pass/F6 activity, and rules resolution stay in the ordered
semantic continuation between player decisions and never become fake choices.

This directly advances the Semantic Table and Decision Inspector KR that
"Direct play, replay, and the decision inspector show the same canonical state
and event sequence for a recorded curated match." It also protects the KR that
semantic beats remain ordered and are never inferred from snapshot diffs.

## The demo

Run
`uv run --extra dev python -m gui.replay_index protocol/fixtures/canonical-curated-replay-v1.json --list`,
copy any emitted `erd1.` address, then run the same command with
`--restore <address>`. The tool prints the pinned viewer, revision, prompt,
played offer, frame hash, and semantic continuation; repeating the restore
returns byte-identical frame and offer JSON, while a changed or missing address
is rejected.

## Approach

### 1. Define a closed canonical replay artifact

Add a Rust-owned `canonical-replay-v1` schema beside the experience protocol,
with matching explicit Python and TypeScript models. The safe artifact is a
projection for exactly one historical viewer, not an authority snapshot and
not a container of both players' private views:

```text
CanonicalReplayV1
  version: 1
  replay_id: string
  match_id: MatchId
  viewer: PlayerId
  content_hash: ContentHash
  asset_manifest_hash: AssetManifestHash
  presentation_head: PresentationSeq
  presentation: PresentationEvent[]
  decisions: ReplayDecision[]

ReplayDecision
  ordinal: u64
  revision: Revision
  prompt_id: PromptId
  offer_id: OfferId
  command_id: CommandId
  presentation_cursor: PresentationSeq
  frame: ExperienceFrame
  offer: InteractionOffer
  command: Command
```

The canonical document excludes its own digest to avoid circular identity.
Its identity is `(replay_id, sha256(canonical document bytes))`. Production and
fixtures use one canonical JSON encoder: UTF-8, sorted object keys, compact
separators, and no insignificant whitespace. The source digest therefore pins
semantic bytes rather than a filesystem path or pretty-printing accident.

`ReplayDecisionAddress` adds that source identity to the decision's complete
binding:

```text
version, replay_id, replay_sha256, match_id, viewer, ordinal, revision,
prompt_id, offer_id, command_id, presentation_cursor
```

Its deep-link representation is `erd1.` plus unpadded base64url of a fixed
JSON array. All `u64` values are decimal strings inside the address payload so
Rust, Python, and JavaScript round-trip them without crossing JavaScript's safe
integer boundary. The array order is part of v1 and has cross-language golden
tests. This is a serialization seam, not a URL route or Study UI.

### 2. Capture decisions at the authority boundary

Refactor the protocol frame builder so it is pure and cacheable for a published
revision. Transient compatibility fields such as drained log lines and
`auto_passed` counts remain on the legacy socket wrapper rather than changing a
canonical frame on repeated recovery calls. The exact frame sent to the player
is retained until that prompt is consumed.

When an explicit player `Command` is accepted, record before stepping the
engine:

- the cached `ExperienceFrame` the player saw;
- the selected `InteractionOffer` from that frame;
- the validated command with its match/revision/prompt/offer bindings;
- the historical viewer;
- the next authority `PresentationEvent.seq` as `presentation_cursor`; and
- the next viewer-local chronological ordinal.

Persist this closed decision record on the trace event. The compatibility
`action` socket path must lower its selected raw index through the current
published offer and the same internal command application function, using an
authority-generated stable command ID, so it cannot create a hole. Duplicate
command retries reuse the prior receipt and never append another decision.

Do not create decision records for `auto=True` priority passes, `pass_turn`
expansion, stop changes, opponent-policy steps in this viewer projection, or
individual `PresentationEvent`s. Those transitions continue to be stepped and
traced normally. Old traces without the new records remain loadable in the
legacy replay viewer, but the canonical index API reports
`canonical_replay_unavailable`; it never backfills offers from raw action
positions.

### 3. Preserve one durable semantic ledger

Build the canonical artifact from the decision records plus the exact
`TraceEvent.presentation` arrays already emitted by the authority. Flatten the
arrays in trace order and validate contiguous `seq` values. Persist or derive
`presentation_head` even when the final decision emitted no semantic event.

For decision `i`, its continuation is the half-open cursor interval
`[decision[i].presentation_cursor, decision[i + 1].presentation_cursor)`, or
`[cursor, presentation_head)` for the final decision. Equal cursors are valid:
two genuine decisions may have no semantic beat between them. Cursors may not
move backward or point outside the ledger. This preserves every automatic
semantic event between choices while making the decision count independent of
animation vocabulary and event batching.

The first pinned artifact must be generated from a complete deterministic
UR Lessons versus GW Allies game with fixed seed, fixed player policy, fixed
opponent policy, injected match ID/clock, and protocol commands rather than raw
indices. Generate it twice in the same test and require byte-for-byte equality.
Every frame in the checked artifact must have an empty opponent hand and must
pass the existing historical-viewer privacy checks.

### 4. Resolve addresses strictly

`CanonicalReplayIndex.restore(address)` performs all checks before returning
data:

1. parse the closed `erd1` payload and reject bad versions, padding, field
   counts, numeric forms, or unknown data;
2. recompute the canonical replay SHA-256 and match replay/match/viewer
   identity;
3. look up the ordinal and compare every redundant address field with the
   stored decision;
4. revalidate frame/prompt/offer/command bindings and viewer privacy; and
5. return a defensive copy of the exact frame, selected offer, played command,
   cursor, and semantic continuation interval.

Construction rejects non-contiguous ordinals, duplicate addresses, duplicate
command IDs, duplicate `(revision, prompt_id)` identities, absent selected
offers, missing decision records, cursor gaps, presentation gaps, and any
frame/offer/command drift. Resolution never falls back from a stale full
address to "whatever is now at this ordinal."

Expose the pure resolver through the Python API/CLI and a TypeScript consumer.
The trace HTTP surface may return the viewer-0 index and resolve its address,
but it must not accept `reveal_hidden`, raw trace action lists, or another
viewer as parameters. A future authenticated viewer-1 projection is a separate
artifact with a separate digest.

### 5. Make Study consume the address without owning replay truth

Keep Study artifact v1's closed `decision_id: string`, but require that string
to be a canonical `erd1` serialization. Rust, Python, and TypeScript Study
validators parse it and compare replay ID/digest, match, viewer, frame
revision, prompt, offer, and command identities with the landmark's existing
fields. The Study conformance fixture points at the new canonical curated
replay fixture, and an integration test resolves the address through Game and
asserts that the returned frame, offer, and played command exactly equal the
landmark copies.

Study does not choose which addresses are landmarks, rank them, run policy or
search analysis, infer legality, or own the resolver. It only carries and
verifies Game's address.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|-----------------|
| Can current trace offsets serve as decision addresses? | No. `TraceEvent` stores a legacy pre-action observation, all raw actions, and a positional action index; it stores no authoritative frame, prompt, offer, command, viewer, revision, or decision cursor. The frontend builds replay frames from adjacent observations. | Capture the exact protocol objects before engine mutation. Legacy offsets are never promoted to canonical addresses. |
| Are repeated protocol frame reads byte-stable today? | No. `_experience_frame()` drains pending villain log lines and auto-pass counts while building a frame, and optional compatibility fields are outside the current frame hash. | Make canonical frame publication pure/cacheable and move transient compatibility data out of the indexed frame. Source digest plus exact storage, not `frame_hash` alone, proves equality. |
| Are semantic events already authoritative and durable enough to reuse? | Yes. `_commit_presentation()` drains the authority projector, assigns monotonic `seq` values, returns the same dictionaries live, and persists the batch on the transition's final trace step. The in-memory recovery ledger is bounded to 256, but the trace retains all persisted batches. | Flatten persisted event batches; do not infer events from observations or use the bounded reconnect ledger as durable replay truth. |
| How are automatic actions distinguishable from player decisions? | Priority stops/F6 already mark generated passes with `TraceEvent.auto=True`; opponent activity is separately labelled `actor="villain"`. | A viewer index includes only that viewer's explicit, non-auto commands. All intervening presentation stays in cursor intervals. |
| Can the existing trace API safely expose raw records as an index? | No. `prepare_trace_payload()` normalizes/redacts observations, but raw `actions` and action descriptions remain separate fields and can encode acting-player choices. | The canonical API emits only captured viewer-safe frames/offers/commands and semantic events, never raw trace rows or a hidden-info toggle. |
| Does Study already bind a landmark to replay truth? | No. `StudyLandmark.decision_id` is a free string. Validators bind its embedded frame/offer/command to each other but do not resolve the ID against a replay. | Give `decision_id` the closed `erd1` grammar and add cross-artifact resolution/equality tests while leaving landmark selection in Study. |
| Can `protocol/fixtures/bolt-target.json` become the canonical replay fixture? | No. It is a recovery-plus-next-command conformance bundle, not a chronological match. Its presentation tail spans revision 1 to 2 while the bundled command expects revision 2, so treating the tail as that command's replay continuation would manufacture history. | Add a separate complete canonical replay fixture generated from one deterministic curated match. Keep the bolt fixture for its existing recovery contract. |
| Will a numeric address round-trip through TypeScript? | Existing protocol `u64`s are TypeScript `number`s and the docs already flag the `Number.MAX_SAFE_INTEGER` limit. | Serialize address counters as canonical decimal strings and reject unsafe values at the current TypeScript runtime boundary; do not silently round. |
| Are the current cross-language foundations healthy? | The focused experience/study conformance baseline passes: `uv run --extra dev pytest -q tests/gui/test_experience_protocol.py tests/gui/test_study_protocol.py` reports 18 passing tests. | Extend the existing Rust-schema/Python/TypeScript golden-fixture pattern instead of adding a Python-only replay contract. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Use trace-event or frontend frame indices | Smallest change and naturally chronological. | Counts opponent/auto steps as choices, reconstructs state from legacy observations, has no prompt/offer/command binding, and changes meaning when trace internals change. |
| Address only by `frame_hash` or a hash of frame plus command | Compact and content-oriented. | Hashes do not enumerate chronology, repeated equivalent frames are possible, the current frame hash omits compatibility fields, and no presentation cursor or source replay identity is recoverable without scanning. |
| Build the index in the Svelte replay client | Avoids backend/schema work. | Gives the client replay authority, repeats private-data and legality logic, cannot certify the command the authority accepted, and violates Game/Study ownership. |
| Replay raw engine actions from the seed whenever an address is opened | Stores less frame data. | Engine/content changes can alter history, restoration would depend on private state and current code, and exact viewer-safe equality would become an execution claim rather than an immutable artifact property. |
| Put both players' indexed frames in one public artifact | Gives one globally contiguous decision list. | Combining separately viewer-safe private projections creates an omniscient artifact and violates the historical-knowledge boundary. Viewer-scoped artifacts preserve chronology without cross-view leakage. |

## Key decisions

1. **Canonical means captured, not reconstructed.** The indexed frame and offer
   are the exact objects published before the accepted command.
2. **Addresses are redundant on purpose.** Replay digest plus chronological
   ordinal locates the row; match/viewer/revision/prompt/offer/command/cursor
   fields prevent a stale or corrupted ordinal from resolving to different
   history.
3. **Replay identity is immutable content identity.** Any canonical replay
   byte change invalidates old addresses visibly.
4. **The index is viewer-scoped.** It is complete for one historical player
   and can be regenerated separately for another authorized viewer. It never
   merges private perspectives.
5. **Automation is continuation, not choice.** Opponent policy activity,
   configured passes, and rules events remain in the replay timeline but do
   not inflate the historical viewer's decision index.
6. **Event ranges are cursor-based, not ownership guesses.** A continuation
   may include events with `caused_by=None`; chronology, not heuristic cause
   attribution, determines what plays before the next decision.
7. **Old traces fail closed for canonical restore.** Supporting legacy viewing
   does not justify inventing missing authority objects.
8. **Study stores Game's address string.** Keeping `decision_id` as a string
   avoids an unnecessary Study schema version break while its now-closed
   grammar and resolver integration eliminate free-form IDs.

Wild success is that any future replay, inspector, bookmark, or Study artifact
can carry one short address and land on the exact table, prompt, legal offer,
and next semantic beat the player experienced, even after client UI changes.
The player never notices the index; they notice that review links simply cannot
open the wrong decision.

Wild failure would be an index derived after the fact from raw actions, stable
only until a renderer or engine revision changed, or an artifact that became
omniscient by combining both players' projections. The strict source digest,
authority-time capture, viewer scope, and fail-closed legacy behavior are the
guards against that outcome.

## Scope

- In scope: a closed canonical replay/index/address contract in Rust, Python,
  and TypeScript; authority-time capture of exact viewer frames/offers/commands;
  one complete deterministic curated-match fixture; semantic cursor intervals;
  strict construction and restore validation; viewer-safe trace API and CLI
  seams; Study `decision_id` parsing/binding; duplicate, missing, privacy, and
  replay-equivalence tests.
- Out of scope: landmark ranking or selection; policy/value/search execution;
  alternate-line simulation; private/hindsight facts; client-side legality;
  Study UI; replay-page redesign; share/bookmark routing; legacy-trace
  reconstruction; generic arbitrary-deck fixtures.

## Done when

- The deterministic pinned UR Lessons versus GW Allies generator produces the
  same canonical JSON bytes and SHA-256 twice, with contiguous decision
  ordinals covering every explicit command made by its historical viewer.
- Restoring every emitted address reproduces byte-identical `ExperienceFrame`,
  selected `InteractionOffer`, and played `Command`, with the same viewer,
  revision, prompt, frame hash, and presentation cursor recorded live.
- Concatenating decision continuation intervals preserves the complete ordered
  viewer-safe `PresentationEvent` ledger through `presentation_head`; automatic
  steps add no decision address.
- Rust, Python, and TypeScript reject duplicate/gapped ordinals, duplicate
  command or prompt identities, missing addresses, stale replay digests,
  identity drift, cursor/presentation gaps, and opponent-private facts.
- The Study curated landmark's `decision_id` parses as `erd1`, resolves through
  Game's fixture, and equals its embedded frame, offer, and command exactly.
- Verification is green in the same modes CI uses:

  ```bash
  cargo test --locked --manifest-path managym/Cargo.toml
  uv run --extra dev pytest -q tests/gui
  npm --prefix frontend test -- --run
  npm --prefix frontend run check
  npm --prefix frontend run build
  ```

## Measure

- **Decision coverage:** `indexed explicit viewer commands / recorded explicit
  viewer commands = 100%` for the pinned match.
- **Restore equivalence:** `restored exact frame+offer+command / indexed
  decisions = 100%` in Rust, Python, and TypeScript fixture tests.
- **Semantic coverage:** every persisted presentation sequence number appears
  exactly once in the ordered replay ledger and every event between decisions
  remains reachable from a cursor interval; no presentation event becomes a
  decision row.
- **Privacy:** zero opponent-hand identities and zero raw trace action lists in
  the canonical viewer artifact and restore responses.
