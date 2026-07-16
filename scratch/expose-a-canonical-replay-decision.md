# Expose a Canonical Replay Decision Index

## Problem

Completed games currently have two incompatible histories. The live protocol
publishes a viewer-safe `ExperienceFrame`, exact `InteractionOffer`s, a bound
`Command`, and ordered `PresentationEvent`s. The persisted trace instead keeps
pre-action legacy observations, raw action lists, and raw action indices. The
replay page reconstructs frames from adjacent observations, and Study's
`decision_id` is an unchecked string. There is no durable address that means
"the exact choice this player saw here," and restoring a guessed trace offset
can drift in actor perspective, prompt, offer, presentation position, or
private information.

W2-275 gives Game one complete chronological decision index for the pinned UR
Lessons versus GW Allies match. Every deliberate choice by either player—the
human and the opponent policy—gets one global ordinal and one stable address.
Each row carries the acting viewer and the exact frame, selected offer, command,
revision, and presentation cursor safe for that viewer. Configured auto-passes,
F6 expansion, and rules resolution remain ordered semantic continuation and
never become fake choices.

The complete mixed-view index is authority-private replay truth. Retrieval
requires an authorized viewer and returns only that viewer's decision rows and
presentation track, or one authorized row. No HTTP, TypeScript, Study, fixture,
or other client artifact may combine private perspectives. Study remains a
filtered historical-viewer consumer of Game's complete index.

This directly advances the Semantic Table and Decision Inspector KR that
"Direct play, replay, and the decision inspector show the same canonical state
and event sequence for a recorded curated match." It also protects the KR that
semantic beats remain ordered and are never inferred from snapshot diffs.

## The demo

Run
`uv run --extra dev python -m gui.replay_index --pinned-match --list-authority`.
The metadata-only authority view shows one contiguous timeline containing both
player 0 and player 1 decisions but no frames or private cards. Then run
the command with `--pinned-match --viewer 0 --list`, restore one emitted
`erd1.` address with `--viewer 0`, and see the exact frame, offer, command, and
continuation; the same address with `--viewer 1` is rejected, and no viewer
output contains the other player's decision frames.

## Approach

### 1. Define complete authority truth and safe viewer projections

Add a Rust-owned `canonical-replay-v1` schema beside the experience protocol,
with an explicit Python authority model and a separate TypeScript-safe viewer
projection. The authority artifact is never a client wire payload:

```text
CanonicalReplayV1                         # authority-private
  version: 1
  replay_id: string                       # immutable, server-assigned
  match_id: MatchId
  content_hash: ContentHash
  asset_manifest_hash: AssetManifestHash
  decisions: ReplayDecision[]             # global chronological order
  presentation_tracks: ViewerPresentationTrack[]

ReplayDecision
  ordinal: u64                            # contiguous across both players
  viewer: PlayerId                        # the acting player
  source: client | policy
  revision: Revision
  prompt_id: PromptId
  offer_id: OfferId
  command_id: CommandId
  presentation_cursor: PresentationSeq    # cursor in viewer's track
  frame: ExperienceFrame                  # safe for `viewer`
  offer: InteractionOffer                 # selected from `frame.offers`
  command: Command

ViewerPresentationTrack
  viewer: PlayerId
  head: PresentationSeq
  events: PresentationEvent[]             # already filtered for viewer
```

The globally contiguous `ordinal` answers "which deliberate decision happened
next?" independently of actor. Each row's `presentation_cursor` addresses the
acting viewer's semantic track at the instant that choice was offered. A
filtered viewer projection contains only rows where `row.viewer == viewer` and
only that viewer's presentation track:

```text
CanonicalReplayProjectionV1              # client/Study-safe
  version, replay_id, match_id, content/asset identities
  viewer: PlayerId
  decisions: ReplayDecision[]             # global ordinals may have gaps
  presentation_head: PresentationSeq
  presentation: PresentationEvent[]
```

Projection validation rejects any row whose viewer differs from the envelope
viewer and any second presentation track. A viewer's semantic continuation for
one decision is the half-open interval from its cursor to the next decision by
that same viewer, or to that viewer track's head for the final row. This keeps
all opponent and rules activity the viewer observed between their choices,
even though the complete authority index contains intervening rows for the
other player.

### 2. Give every row a deep-link-ready exact address

`ReplayDecisionAddress` binds the immutable replay and the row's redundant
identity:

```text
version, replay_id, match_id, ordinal, viewer, revision, prompt_id, offer_id,
command_id, presentation_cursor, decision_sha256
```

`decision_sha256` hashes only the exact viewer-safe row
`{frame, offer, command, presentation_cursor}`. It is safe to reveal to the
authorized row viewer and proves that a stored ordinal did not silently change;
the address never exposes a digest over both private perspectives or an
authority hidden-state hash. Authority storage may maintain a private whole-
artifact integrity digest, but that digest is neither serialized in addresses
nor returned to clients.

The deep-link representation is `erd1.` plus unpadded base64url of a fixed JSON
array. All `u64` values are decimal strings in the address payload so Rust,
Python, and JavaScript round-trip them without crossing JavaScript's safe
integer boundary. Array order is part of v1 and has cross-language golden
tests. This is a serialization seam, not a URL route or Study UI.

### 3. Build one authority decision context for humans and policies

Refactor `_publish_current_prompt()` into a pure
`_build_decision_context(obs, viewer, revision)` that creates together:

- the viewer projection of `obs`;
- one prompt and its complete legal `InteractionOffer`s;
- the offer-to-engine-action lowering map; and
- the resulting cacheable `ExperienceFrame`.

The builder takes the actor explicitly. It no longer hard-codes player 0 in
prompt/offer actor fields or calls `hero_view()`. The engine observation is
already oriented to its acting player; the projection layer redacts its
opponent before hashing or persistence. Rename the Python-only
`LegacyHeroObservation` model to viewer-oriented terminology without changing
its wire shape: `projection.agent.player_index == viewer`, and
`projection.opponent` contains no hidden hand identities. Prompt IDs are
authority-global and monotonic across both actors. Transient compatibility
fields such as drained villain log lines and `auto_passed` counts stay on the
legacy socket wrapper, so repeated reads of a decision frame are
byte-identical.

The human path caches this context as the client-visible published prompt.
`hero_command()` validates the submitted command against it. The compatibility
`action` message first maps its positional index back to the cached offer and
constructs an authority-namespaced command ID, then calls the same internal
command application function. Duplicate client commands reuse the original
receipt and never append a row.

The current villain path is lowered without inventing a client-visible prompt:

1. `_auto_play_villain()` calls `_build_decision_context()` against the
   villain-oriented observation and current revision. The resulting frame,
   prompt, offers, and lowering map stay local to the authority; they are never
   assigned to the human's `published_prompt`, sent on the WebSocket, placed in
   hero recovery, or included in hero logs.
2. The existing policy still evaluates `(env, obs)` and returns a raw legal
   action index. The adapter looks up the exact offer whose lowering target is
   that action. An absent or ambiguous mapping is an authority error, not a
   guessed offer.
3. The authority constructs a `Command` with the match, current revision,
   internal prompt ID, selected offer ID, empty adapter answers, and an
   authority-generated policy command ID. Production IDs come from an injected
   collision-free factory; the deterministic fixture injects fixed IDs. The
   authority namespace cannot collide with client IDs.
4. `_apply_bound_command(context, command, source="policy")` performs the same
   match/revision/prompt/offer checks and lowering used for human commands,
   records the exact villain-safe row, and only then steps the engine.

This internal frame is not a fictional UI prompt: it is the authoritative
legal decision boundary the policy actually consumed, captured for replay.
Nothing makes it visible to player 0. It becomes retrievable only through an
authorized player-1 projection or trusted authority tooling.

### 4. Advance revisions and semantic tracks at every authority step

The current adapter increments its protocol revision once after a whole
human-to-human-surface batch, so intermediate villain decisions have no unique
frame revision. Change the authority revision to advance after every engine
action, including unindexed auto-passes. Every deliberate row therefore stores
the exact pre-command revision; the next decision, regardless of actor, has a
later revision. Auto-pass revisions remain real state transitions but consume
no decision ordinal.

Drain the `PresentationProjector` after every step and project its committed
facts into each authorized viewer track with exact per-step `from_revision` and
`to_revision`. Each track has its own contiguous sequence space. Public facts
may serialize identically in both tracks; a future private reveal belongs only
in its authorized track. `caused_by` retains a command ID only in the track
authorized for that command's viewer and is `None` in the other viewer's
track. The trace persists the same dictionaries returned live. No consumer
derives semantic meaning from snapshots.

The human client can still receive one batched `FrameUpdate` whose
`base_revision` is its last surfaced frame and whose final frame revision may
jump across policy and auto-pass steps. Relax `validatePresentationUpdate()`
from "every event spans the whole batch" to "events are sequence-contiguous,
ordered by nondecreasing transition, and each transition lies within
`(base_revision, final_revision]`." Gaps are valid revisions with no semantic
event. This preserves live batching while making historical intermediate
frames canonical.

Configured auto-pass/F6 actions use the same bound lowering path so revision
and event truth remain complete, but are marked `source="automatic"` in the
private trace transition and are excluded before decision ordinals are
assigned. Stop changes are configuration, not commands. Rules resolution
events remain events only.

### 5. Capture, validate, and restore the complete index

Immediately before a deliberate bound command steps the engine, record:

- the next global decision ordinal;
- the acting viewer and source (`client` or `policy`);
- the exact authority decision context frame and selected offer;
- the validated command; and
- the next sequence in that viewer's presentation track.

Construction validates one complete replay before it is saved:

- global ordinals are exactly `0..N-1`, regardless of actor;
- both configured players occur when the deterministic match contains their
  deliberate decisions;
- every non-auto human or policy trace transition has exactly one row, and no
  automatic transition has a row;
- command IDs and `(revision, prompt_id)` pairs are unique;
- frame prompt, selected offer, command, actor, and revision bindings agree;
- every frame is safe for its declared viewer;
- each viewer track is sequence-contiguous, cursors are monotonic within that
  viewer's filtered rows, and cursors fall within that track; and
- authority-private storage never crosses the client serialization boundary.

`CanonicalReplayIndex.restore(address, authorized_viewer)` parses the closed
address, verifies the caller is authorized for `address.viewer`, looks up the
global ordinal, compares every redundant identity field, recomputes the row's
viewer-safe `decision_sha256`, and returns a defensive copy of the exact frame,
offer, command, cursor, and same-viewer continuation. It rejects malformed,
missing, stale, cross-viewer, duplicate, or drifted addresses and never falls
back to whatever is currently at an ordinal.

The authority CLI's `--list-authority` mode is metadata-only: ordinal, actor,
revision, prompt, offer, command ID, and cursor, with no projections or offer
labels. The HTTP surface has no unrestricted complete-index endpoint. In the
current local human-vs-bot product it applies a fixed server-side viewer-0
projection policy and exposes only that projection and row resolver; the caller
cannot supply `viewer` or `reveal_hidden`. Trusted player-1 inspection uses an
authority-local API until a real authentication model exists.

Old traces without complete decision records remain loadable in the legacy
replay viewer, but canonical index construction returns
`canonical_replay_unavailable`. It never reconstructs frames or offers from
raw trace positions.

### 6. Preserve one deterministic pinned match and Study handoff

Generate the first authority fixture from a complete deterministic UR Lessons
versus GW Allies match with fixed seed, fixed human policy, fixed opponent
policy, injected match ID/clock, and injected command IDs. Generate it twice
and require byte-identical private authority bytes in the Python authority
test. Do not check the mixed-private authority artifact into a frontend fixture
directory.

Check in separate player-0 and player-1 projection fixtures produced by the
same authority artifact. Each must contain only its declared viewer's rows,
frames, offers, commands, and presentation track. Cross-language Rust/Python/
TypeScript tests validate both projections and prove their global ordinals
interleave into the complete authority metadata timeline without either
projection containing the other's private frame.

Keep Study artifact v1's closed `decision_id: string`, but require an `erd1`
serialization. Study's curated artifact consumes a player-0 address and the
player-0 safe replay projection digest. Rust, Python, and TypeScript Study
validators parse the address and compare replay, match, viewer, frame revision,
prompt, offer, command, and cursor identities. An integration test resolves it
through Game with `authorized_viewer=0` and requires exact equality with the
landmark copies; resolution as player 1 fails.

Study does not see the complete mixed-view authority index, select or rank
landmarks, run policy/search analysis, infer legality, or own replay
restoration. It remains a filtered historical-viewer consumer of Game truth.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|-----------------|
| Can current trace offsets serve as decision addresses? | No. `TraceEvent` stores a legacy pre-action observation, all raw actions, and a positional action index; it stores no authoritative frame, prompt, offer, command, viewer, revision, or decision cursor. The frontend builds replay frames from adjacent observations. | Capture exact protocol objects before mutation. Legacy offsets are never promoted to canonical addresses. |
| Can current protocol revisions identify villain decisions? | No. `hero_command()` and the compatibility paths call `_advance()` through villain and auto-pass actions, then increment revision once. Intermediate policy choices share no authoritative frame revision. | Advance revision and drain semantic facts per engine action while retaining batched client delivery. |
| Can the policy path already produce a command? | No. `_auto_play_villain()` calls the policy for a raw action index and passes that directly to `_step_and_record()`. `_publish_current_prompt()` hard-codes hero actor 0. | Build an authority-local villain decision context, map the selected raw index back to exactly one offer, construct an authority policy command, and apply it through the shared binding/lowering path without publishing it to the hero. |
| Are repeated frame reads byte-stable today? | No. `_experience_frame()` drains pending villain log lines and auto-pass counts, and optional compatibility fields are outside the frame hash. | Make decision frames pure/cacheable and move transient wrapper data outside canonical frames. |
| Are semantic events authoritative and durable enough to reuse? | Yes. `_commit_presentation()` assigns monotonic sequences, returns the same dictionaries live, and persists them in the trace. Its reconnect ledger is bounded, but trace batches remain durable. | Drain/persist per step into viewer tracks; never infer events from snapshots or use the bounded reconnect ledger as replay truth. |
| How are automatic actions distinguishable? | Priority stops/F6 already set `TraceEvent.auto=True`; opponent activity is labelled separately. | Human and opponent-policy selections are deliberate indexed decisions. Auto-pass/F6 steps advance revision and events but get no ordinal. |
| May one client artifact contain every row? | No. A frame can be safe for its acting viewer while revealing facts private to the other viewer. Combining both safe projections creates an omniscient artifact. | Keep the complete index authority-private; expose closed single-viewer projections and authorize every restore. TypeScript models only the safe projection. |
| Can a whole-index digest appear in an address? | Not safely. A digest over both private projections would be a client-visible comparison oracle over hidden material. | Bind addresses to immutable replay ID and a hash of the authorized row payload only. Keep whole-artifact integrity private. |
| Can the existing trace API safely expose raw records? | No. `prepare_trace_payload()` redacts observations, but raw action lists and descriptions are separate and can encode acting-player choices. | Projection endpoints emit only captured viewer-safe protocol objects and semantic events, never raw trace rows or a hidden-info toggle. |
| Does Study bind a landmark to replay truth? | No. `decision_id` is a free string. Validators bind embedded objects to each other but do not resolve the ID against Game. | Give `decision_id` the closed `erd1` grammar and test authorized resolution against the player-0 projection. |
| Can `bolt-target.json` become the replay fixture? | No. It is recovery-plus-next-command conformance, not a chronological match. Its tail spans revision 1 to 2 while the command expects revision 2. | Generate a separate complete deterministic match and safe per-viewer projection fixtures. |
| Will address counters round-trip through TypeScript? | Protocol `u64`s are TypeScript `number`s and the docs flag the `Number.MAX_SAFE_INTEGER` limit. | Serialize address counters as canonical decimal strings and reject unsafe runtime values; never round. |
| Are the current cross-language foundations healthy? | `uv run --extra dev pytest -q tests/gui/test_experience_protocol.py tests/gui/test_study_protocol.py` passes 18 focused tests. | Extend the existing Rust-schema/Python/TypeScript conformance pattern rather than adding a Python-only contract. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Index only the human viewer | Simplifies privacy and reuses the published hero frame. | Omits genuine opponent-policy decisions and is not the complete Game-owned chronology required by the directive. |
| Use trace-event or frontend frame indices | Smallest change and naturally chronological. | Counts automatic steps as choices, reconstructs state from legacy observations, and has no frame/offer/command binding. |
| Address only by `frame_hash` or frame-plus-command hash | Compact and content-oriented. | Does not enumerate chronology, repeated equivalent frames are possible, and no replay/presentation identity is recoverable without scanning. |
| Build the index in Svelte | Avoids backend/schema work. | Gives the client replay authority, repeats privacy and legality logic, and cannot certify policy commands the authority accepted. |
| Replay raw engine actions from the seed on restore | Stores less frame data. | Engine/content changes can alter history; exact viewer equality becomes an execution claim rather than immutable evidence. |
| Publish one artifact containing both acting-viewer frames | Makes the complete index portable to every consumer. | Creates an omniscient client artifact. The complete index belongs inside Game authority; clients receive authorized projections only. |

## Key decisions

1. **One global chronology covers both deliberate players.** Global ordinals
   are contiguous across human and policy choices; automation consumes neither
   rows nor ordinals.
2. **Canonical means captured, not reconstructed.** Every row stores the exact
   actor-safe frame and selected offer before its command mutates authority.
3. **Policies cross the same command boundary.** Raw policy indices are an
   internal adapter input lowered through an authority-local prompt/offer map,
   not replay truth and not a human-visible prompt.
4. **Complete truth stays inside Game.** Client and Study artifacts contain one
   authorized viewer's rows and presentation track only.
5. **Addresses are redundant and viewer-bound.** Replay/row identities plus a
   viewer-safe row digest prevent stale ordinal fallback without exposing a
   hidden whole-index hash.
6. **Revisions advance per authority action.** Intermediate opponent decisions
   and unindexed automatic transitions receive real state identities while the
   live client retains batched updates.
7. **Semantic ranges are per viewer.** A viewer's next indexed row closes the
   prior continuation, preserving opponent/rules events without showing the
   opponent's private choice surface.
8. **Old traces fail closed for canonical restore.** Legacy viewing does not
   justify inventing missing frames, offers, or commands.
9. **Study stores Game's address string.** It consumes a filtered projection
   and never owns the complete index, ranking, legality, search, or replay.

Wild success is that Game can answer "what was decision 47?" for the complete
match, then safely answer "what did player 0 see?" or "what did player 1 see?"
without ever producing an omniscient client payload. Study can deep-link to an
exact human decision while trusted diagnostics can prove every policy decision
is present in the same chronology.

Wild failure would be two separate per-player timelines that cannot agree on
order, a villain history reconstructed from raw action positions, or one
portable artifact that quietly combines both private hands. Global ordinals,
authority-local policy lowering, per-viewer tracks, and mandatory projection
authorization prevent those outcomes.

## Scope

- In scope: a complete Game-private chronological index across both deliberate
  players; Rust/Python authority types and closed viewer projection schema;
  authority-local policy frame/offer/command lowering; per-action revisions;
  exact viewer-safe capture; per-viewer semantic tracks and cursor intervals;
  stable address serialization; strict authorization/restoration; one complete
  deterministic curated match with separate safe viewer fixtures; Study's
  filtered `decision_id` handoff; duplicate, missing, privacy, and replay-
  equivalence tests.
- Out of scope: landmark ranking or selection; policy/value/search execution;
  alternate-line simulation; private/hindsight display; client-side legality;
  Study UI; replay-page redesign; share/bookmark routing; legacy-trace
  reconstruction; generic arbitrary-deck fixtures; a new player-1 HTTP auth
  product.

## Done when

- The deterministic pinned UR Lessons versus GW Allies generator produces the
  same authority artifact twice, and its global ordinals are exactly `0..N-1`
  over every non-auto human command and every opponent-policy choice.
- Every deliberate trace transition has exactly one index row with the acting
  viewer, exact pre-command `ExperienceFrame`, selected `InteractionOffer`,
  bound `Command`, unique revision/prompt/command identities, and acting-viewer
  presentation cursor. Auto-pass/F6/rules-only transitions have zero rows.
- A villain-policy unit/integration test proves its raw action index maps to
  exactly one internal offer, produces a validated authority command and row,
  and never changes the human `published_prompt`, hero recovery offers, or
  WebSocket payload.
- Restoring every row as its authorized viewer reproduces byte-identical frame,
  offer, command, cursor, and same-viewer semantic continuation. Restoring the
  same address as the other viewer is rejected.
- Player-0 and player-1 projection fixtures contain disjoint decision frames,
  only their declared viewer's presentation track, and global ordinals whose
  ordered union equals the complete authority metadata timeline. Neither
  fixture contains the other player's hand identities, offers, or commands.
- Concatenating each viewer's continuation intervals preserves that viewer's
  complete ordered `PresentationEvent` track through its head, including
  semantic consequences of opponent decisions and automatic rules activity;
  no semantic event becomes a decision row.
- Rust/Python authority validation rejects duplicate or gapped global
  ordinals. Rust, Python, and TypeScript projection validation allows gaps left
  by the other viewer but rejects duplicate or non-increasing ordinals,
  duplicate command or prompt identities, missing addresses, row hash drift,
  identity mismatch, cursor/event gaps, mixed-view projections, unsafe numeric
  encodings, and cross-viewer restore attempts.
- The Study curated landmark's `decision_id` parses as `erd1`, resolves through
  Game's player-0 projection, and equals its embedded frame, offer, and command;
  Study never loads the complete authority artifact.
- Verification is green in the same debug mode CI uses:

  ```bash
  cargo test --locked --manifest-path managym/Cargo.toml
  uv run --extra dev pytest -q tests/gui
  npm --prefix frontend test -- --run
  npm --prefix frontend run check
  npm --prefix frontend run build
  ```

## Measure

- **Complete decision coverage:** `(indexed human commands + indexed policy
  choices) / all recorded non-auto deliberate transitions = 100%` for the
  pinned match, with one global contiguous ordinal sequence.
- **Restore equivalence:** `authorized byte-identical frame+offer+command
  restores / indexed decisions = 100%` across authority tests and each safe
  cross-language projection fixture.
- **Semantic coverage:** every event in each viewer presentation track is
  reachable exactly in order from that viewer's decision continuations; zero
  presentation events become decision rows.
- **Privacy:** zero mixed-view decision rows or presentation tracks in client
  and Study artifacts; cross-viewer resolution rejection is 100%.
