# Put a Pilot and Watcher on One Testing-House Table

## Problem

Etude Fantasia has one authoritative human-versus-manabot match, stable
viewer-safe replay decisions, exact isolated Study branches, and the GAM-6
fixture-backed belief-to-strategy surface. It does not yet have a safe way for
two people to inhabit that surface together. A live `SessionRecord` owns one
resume token and one WebSocket; attaching a second connection closes the
first. The server also treats every attached client as the hero, so handing the
current credential to a watcher would grant command, stop, rematch, and Study
authority rather than read access.

This slice gives one acting pilot and one explicitly invited watcher a shared
testing-house table. Both are authorized for the same player-0 rules
projection, follow the same live frames and canonical decision addresses, and
continue on that table into Study. Their person identities and capabilities
remain separate from the immutable match frame. The watcher may state a
personal read using one of GAM-6's existing pinned scenarios, share it with an
explicit table audience, compare the existing fixture-backed advice, and
explore an exact isolated historical line. None of those actions can pause or
mutate live authority. Only the current pilot may submit an offered live
`Command`.

The design advances the Game measure: “A pilot and permitted watcher can
inhabit the same canonical decision surface and compare viewer-safe belief and
strategy artifacts without pausing or mutating the match. Only the pilot can
submit its offered live `Command`; an isolated line returns to the identical
recorded decision.” It also advances the release/recovery and accessibility
measures without introducing chat, a generic room system, a live advisor
provider, or a second rules surface.

## The demo

Run `./scripts/play`, start the selected match, and copy the one-use watcher
link into a second browser context. The watcher immediately sees the pilot's
exact board and decision, privately states “Opponent holding interaction,”
shares that read to the table, opens a prior recorded decision in an isolated
line while the pilot keeps playing, and returns to the identical address. A
pilot handoff disables the old pilot's Actions panel and enables the new one;
both browsers remain on the same table when the game becomes Study.

## Approach

### Put a versioned table-control envelope around match truth

Add a Game-owned `testing-house-v1` control contract in Python and TypeScript.
It does not change Rust/managym's `ExperienceFrame`, `InteractionOffer`,
`Command`, canonical replay, Study, or GAM-6 advice contracts.

- `ViewerIdentity` is an opaque participant id bound server-side to one
  `table_id` and `rules_viewer: 0`. The client never chooses the rules viewer.
- `ViewerAccess` carries `role: pilot | watcher`, a canonical capability list,
  and a monotonically increasing `grant_revision`.
- `TableSnapshot` carries table mode (`live | study`), participant presence,
  the current viewer's visible belief scenarios, compact summaries of the
  committed canonical DecisionAddresses, and the unchanged match
  recovery/update payload. Exact replay rows resolve on demand rather than
  duplicating every historical frame into every live broadcast.
- Participant-specific control data stays outside `ExperienceFrame`. Pilot and
  watcher therefore receive byte-identical frames, frame hashes, offers, and
  `erd1` addresses; only their access and visible personal artifacts differ.

Capabilities are derived from role rather than accepted from the wire:

| Capability | Pilot | Watcher |
| --- | --- | --- |
| `view_table` | yes | yes |
| `author_belief` | yes | yes |
| `share_belief` | yes | yes, own beliefs only |
| `compare_advice` | yes | yes |
| `explore_study` | yes | yes |
| `submit_live_command` | yes | no |
| `configure_match` | yes | no |
| `transfer_pilot` | yes | no |

There is no admin, moderator, spectator, team, tournament, or configurable
room-default role. `transfer_pilot` atomically swaps the two roles, increments
both grant revisions, and broadcasts the new access snapshots. A client
command message carries the current `grant_revision` beside the unchanged core
`Command`. The table checks duplicates by command id and submitting participant
first, then checks the current grant/capability, then calls `GameSession`.
Thus a lost accepted response remains idempotently recoverable after a handoff,
while a delayed unaccepted command from an old grant fails as `stale_grant`
without reaching match authority.

The table has one closed server-side action dispatcher. The WebSocket parser
accepts only the tagged `testing-house-v1` message union; it never forwards an
unknown `type`, arbitrary payload, or client-supplied capability to
`GameSession`. Under the table lock the dispatcher resolves the participant
lease, rechecks `grant_revision` and the server-derived capability, validates
the operation's table/match/owner binding, and only then calls the named table
or game method:

| Wire operation | Required authority | Authorized destination |
| --- | --- | --- |
| `join_table`, `resume` | valid invite or participant lease | participant attachment only; never `GameSession` |
| `command`, compatibility `action`, `pass_turn` | current pilot + `submit_live_command` | `GameSession.hero_command`, compatibility action adapter, or `GameSession.pass_turn` |
| `set_stops` | current pilot + `configure_match` | `GameSession.set_stops` |
| `new_game`, `rematch` | current pilot + `configure_match` | replace/reset the table's `GameSession` |
| `transfer_pilot` | current pilot + `transfer_pilot` | atomic table-role swap; never match authority |
| `author_belief` | participant + `author_belief` | that participant's personal belief store only |
| `share_belief` | participant + `share_belief` + belief ownership | immutable personal-to-table audience transition |
| `restore_decision`, `retry_decision`, `branch_command`, `return_from_branch`, `return_to_live` | participant + `explore_study` + attempt ownership where applicable | participant-local replay/Study controller and exact fork provider |

`command` is the product path; `action` remains an explicitly authorized
compatibility adapter and is not an unguarded fallback. Participant-authorized
HTTP Study endpoints use the same dispatcher and capability policy instead of
calling `GameSession` or the Study provider directly. Schema-invalid and
unknown/unsupported message types return typed `invalid_message` or
`unsupported_message` errors before resolving a game method. A known operation
without current authority returns `forbidden` or `stale_grant` before any game,
table, belief, or attempt mutation. There is no default dispatch arm.

The denial tests fingerprint the authoritative match revision/frame hash,
trace-event and canonical-decision counts, stop configuration, table/grant
revisions, belief audiences, and branch receipts before and after every watcher
denial and unknown message. Equality is required, and method spies prove that
no `GameSession` mutation method was entered. Authorization is therefore a
mutation-free boundary, not a UI convention.

### Check one Python/TypeScript control contract

Check in `protocol/testing-house-v1.schema.json` and
`protocol/fixtures/testing-house-control-v1.json`. The schema is generated from
the closed Pydantic control models and describes a
`TestingHouseV1ConformanceBundle`; the fixture contains representative pilot
and watcher access snapshots plus one example of every request, control event,
and typed rejection variant. It contains opaque example identities only—no
invite/reconnect secret and no strategy payload.

`tests/etude/test_testing_house_protocol.py` validates and round-trips the
fixture through Pydantic, compares every tagged variant, enum, field, and
required key to the checked schema, and asserts extra fields fail closed.
`frontend/src/lib/testing-house-protocol.test.ts` validates the same fixture
with AJV and compares the TypeScript tagged union and enums to that schema,
following the existing experience-protocol conformance pattern. Both tests
also compare their supported inbound operation set to the closed dispatch
registry, so adding a wire mutation without authorization and conformance
coverage fails CI. These new control artifacts do not embed, regenerate, or
alter `protocol/fixtures/advice-curated-decision.json`; they certify table
control shape, not strategy evidence.

### Replace one session credential with two scoped participant leases

Refactor the in-memory registry record into one table containing a
`GameSession`, an `asyncio.Lock`, at most two participant records, one consumed
watcher invite, shared beliefs, and a table revision. Each participant gets a
distinct high-entropy reconnect token and at most one active WebSocket.
Reconnecting replaces only that participant's prior socket; it never evicts
the other role.

The pilot receives a one-use watcher link after table creation. Put the invite
token in the page fragment, remove it from browser history after the app reads
it, and send it in the first `join_table` WebSocket message. Do not put bearer
tokens in HTTP or WebSocket query strings: RFC 6750 calls out their high
likelihood of being logged, and the browser `WebSocket` constructor exposes
only URL and subprotocol arguments. The server stores only a digest of the
one-use invite and exchanges it for the watcher's participant reconnect token.
This is deliberately an opaque local bearer identity, not an account or
authentication-system claim.

All table mutations and broadcasts serialize through the table lock. The
server computes a game response once, then sends deep-copied table snapshots
to both connections so transient log/presentation metadata cannot be drained
differently per viewer. Dead sockets are detached without dropping the table.
This remains an in-memory, single-process design: FastAPI's own multi-client
example supports broadcast from an in-memory connection list and explicitly
notes that it does not span processes. Redis/database fanout and durable room
leases are release-scale follow-ons, not prerequisites for this bounded local
slice.

### Make personal belief provenance and sharing explicit

Do not modify or regenerate `protocol/fixtures/advice-curated-decision.json`,
`etude/advice.py`, its `StudyArtifact`, or its evidence. GAM-6's two scenario
summaries are immutable condition templates for one pinned completed-match
decision. The new product action is a declaration that references one exact
existing scenario:

```text
BeliefScenario {
  id,
  author_viewer_id,
  source: {
    decision_address,
    gam6_scenario_id,
    advice_identity
  },
  audience: personal | table(table_id),
  provenance: { kind: player_authored, created_at_table_revision,
                shared_at_table_revision? }
}
```

Creation always yields `audience: personal`; the server resolves and pins the
address, scenario id, and advisor/compute identity from the existing GAM-6
metadata rather than trusting client copies. Personal creation changes only
the author's participant state and emits nothing to the other connection.
`share_belief` is an explicit, one-way transition by the author to exactly the
current table audience. It advances table revision and broadcasts author and
provenance. No free-form text is added: that would create an unbound belief or
accidentally become chat. The UI labels the declaration “Your read” or
“Watcher's shared read” and labels the evidence “GAM-6 pinned fixture ·
advisory only.” Selecting a personal or shared read calls the unchanged GAM-6
advice endpoint; no result is represented as strategy for the current live
match.

### Expose live canonical history without inventing a pre-command address

An `erd1` address binds the command that was actually played, so the current
uncommitted prompt cannot honestly have one. Both roles follow that prompt
through the same `ExperienceFrame`. As soon as the pilot's command commits,
the existing `ReplayDecision` and retained `_study_roots` entry make the
decision address canonical and stable.

Extract `GameSession.canonical_replay()` from final trace creation so it can
project the decisions and presentation tracks captured so far. Add
participant-authorized table endpoints that list, restore, retry, and return
through this live projection. They derive `authorized_viewer` from
`ViewerIdentity`, never from request input. The same `replay.{match_id}` and
row digest flow into the final trace, so live addresses do not change when the
game ends.

Study attempts become participant-owned and limited to one active attempt per
participant rather than one global attempt per match. Each attempt forks the
retained root for its address, while live `GameSession.env` continues normally.
Every attempt endpoint requires the participant credential and verifies owner,
table, match, address, and rules viewer. Starting or advancing a branch cannot
change live revision, frame hash, canonical decisions, trace events, or a
sibling participant's branch. Return consumes the branch and normalizes the
private `StudyReturnReceipt` back to the exact public
`RestoredReplayDecision`. The current-main normalization must exclude both
private receipt fields (`source_digest` and `execution`); the prepared baseline
found that excluding only `source_digest` causes three existing
`test_study_runtime.py` return tests to fail under the locked environment.

A branch projection, board, offer, command receipt, and presentation stream are
returned only to the participant who owns that attempt and remain in that
participant's local Study controller. They never enter the authoritative table
snapshot, recovery/update broadcast, or the sibling participant's store, and
they never advance table revision. Every authoritative broadcast is built only
from the live `GameSession`; a participant's branch board/state is never
broadcast or labelled as authoritative table truth.

### Keep one table through terminal and Study

Add a shared table store/controller rather than navigating connected
participants to `/replay`. During live play it renders the existing board,
semantic presentation, ActionPanel, pinned Advice Lab, participant strip, and
the growing list of already-committed canonical decisions. Watchers see the
same offers, but ActionPanel, stops, New Game, and Pass Turn are disabled and
labelled “Pilot is deciding.”

At terminal, the table changes mode to `study`, keeps the same table and
participant identities, and freezes the final canonical projection under the
same addresses. Reuse `StudyStore`, `StudyPanel`, `GameBoard`, and branch
controls in the live route; `/replay` remains a standalone historical browser
but is not the continuation path for this shared match. A participant may
enter a branch while the match is live only from a previously committed
decision. Returning restores that address and the recorded board; a “Live
table” action returns to the current authoritative frame without affecting the
other participant.

The new invite, role-transfer, state/share, branch, and return controls use
native buttons and fields, Enter/Space activation, deterministic focus, polite
status announcements, and at least 44 CSS-pixel targets. W3C WCAG 2.1.1
requires a keyboard equivalent for pointer functionality, and WCAG 2.5.8's AA
minimum is 24 by 24 CSS pixels. The larger project convention provides a safer
touch target. At 390x844 the role strip and decision controls stack without
horizontal page overflow; branch work cannot cover the live role/connection
status.

## De-risking

| Question | Finding | Impact on design |
| --- | --- | --- |
| Can a watcher reuse the existing session? | No. `SessionRecord` has one token and one WebSocket; `_attach_session_websocket` closes the previous socket. Every attached connection can currently call every mutation. | Introduce per-participant leases and derived capability checks above `GameSession`; reconnect replaces only the same participant. |
| Should person/role identity be added to `ExperienceFrame` or `Command`? | No. Replay addresses digest the exact frame/offer/command, and viewer-equivalent participants must resolve the same address. Role churn is not match truth. | Keep `ViewerAccess` in the table envelope and `grant_revision` beside, not inside, core `Command`. |
| Can both roles safely see player-0 facts? | Yes, if permission explicitly binds both identities to `rules_viewer: 0` and the server derives all live/replay/Study projections from that grant. Existing `viewer_view`, `project_replay`, and `restore_decision` already fail closed across rules viewers. | Support exactly one permitted same-perspective watcher; no client-selectable viewer or opponent-seat spectating. Assert byte-equivalent frames and address sets. |
| Can the watcher branch without waiting for game over? | Yes. Deliberate commands already append a canonical `ReplayDecision` and clone its exact root before engine mutation. A valid partial `CanonicalReplayV1` can be built from the captured rows and current viewer tracks. The unplayed current prompt cannot have an `erd1` address because the address binds the played command. | Publish committed addresses as they appear; fork retained roots through participant-authorized table endpoints. Never synthesize a current-decision address. |
| Does a Study branch pause or mutate live play? | Existing `StudyForkProvider` clones the retained root and returns a source-digest receipt; live commands do not consult `_study_attempts`. The current global active-attempt guard and unauthenticated REST lookup are the actual shared-role blockers. | Make attempts participant-owned, route every operation through the table lock and credential, and assert live frame/trace/root immutability while pilot commands continue. |
| Can an authored belief honestly produce strategy now? | Only by referencing one of GAM-6's two pinned scenario conditions. GAM-6 evidence is static and bound to its own replay, match, address, advisor, and compute identity. | Store a player-authored declaration/provenance around an immutable GAM-6 scenario reference. Label it pinned fixture evidence and never imply live-match applicability. Do not change fixtures or advice generation. |
| How should the invite token travel? | Browser WebSocket construction accepts a URL plus optional subprotocols, not arbitrary authorization headers. RFC 6750 discourages bearer tokens in URI queries because URLs are commonly logged. | Put the invite in the page fragment, scrub it, then exchange it in the first WebSocket message for a scoped reconnect token. Never echo secrets in snapshots, logs, traces, or errors. |
| Does FastAPI support two live connections? | Yes. Its official `ConnectionManager` example holds multiple WebSockets and broadcasts to them, but explicitly says the in-memory pattern works only in one process. | Use an ordered table lock and two connections in the current single-process stack; state multi-worker fanout as out of scope. |
| Are existing recovery/Study paths sound enough to extend? | Focused prepared baselines: 4 recovery/stale/duplicate server tests passed; 8 Study branch/runtime tests passed; 24 socket/Study/advice frontend tests passed. Three Study runtime return tests fail because `execution` is not removed from the private receipt before validating the public return. | Preserve the proven recovery/branch primitives, fix the narrow return normalization in this PR, and add shared-role tests around them. |
| What input evidence is required? | Existing controls are largely native and the project has Playwright plus axe. WCAG requires keyboard-equivalent functionality; 24px is the AA target minimum. | Prove the complete new flow with two contexts, keyboard-only activation, axe, a 390x844 touch viewport, 44px critical controls, focus restoration, and live announcements. |

External references: [FastAPI WebSockets](https://fastapi.tiangolo.com/advanced/websockets/),
[WHATWG WebSockets Standard](https://websockets.spec.whatwg.org/),
[RFC 6750 bearer token usage](https://www.rfc-editor.org/rfc/rfc6750.html),
[WCAG 2.1.1 Keyboard](https://www.w3.org/WAI/WCAG22/Understanding/keyboard),
and [WCAG 2.5.8 Target Size](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum).

## Alternatives considered

| Approach | Tradeoff | Why not |
| --- | --- | --- |
| Give the watcher the pilot resume token and make the UI read-only | Minimal code, but the second connection evicts the first and UI-only disabling leaves every server mutation authorized. | Fails coexistence, authorization, reconnect, and role-change requirements. |
| Put role/viewer fields inside `ExperienceFrame` and participant fields inside `Command` | One apparent protocol object, but role changes alter frame hashes and replay decision digests; equivalent viewers no longer share an address. | Conflates access control with rules truth and destabilizes replay/Study identity. |
| Build a generic persistent room/event service now | Solves multi-process fanout and future audiences, but requires accounts, membership, databases, retention, moderation, and generalized permissions. | Violates the one-watcher slice and creates the social/platform work the wave excludes. |
| Allow arbitrary belief text and send it to a new provider | Richer authorship, but no merged provider can bind arbitrary text to checked viewer-safe strategy for the selected live match. | Would implement GAM-7/INT-9 authority or make unsupported strategy claims. Use explicit GAM-6 scenario declarations only. |
| Defer all branching until terminal replay | Reuses current trace endpoints unchanged. | Misses the testing-house behavior: a watcher must be able to test a recorded line while the pilot keeps playing. Captured live roots already make this safe. |

## Key decisions

1. A `ViewerIdentity` is a person/lease identity; `rules_viewer` is the hidden-information perspective. Both participants are distinct people explicitly bound to player 0.
2. Match truth is role-neutral. `ExperienceFrame`, `Command`, canonical replay, Study contracts, and GAM-6 evidence remain unchanged.
3. Capabilities are closed, server-derived, and rechecked on every mutation. Role transfer is an atomic swap with a grant revision, not a generic permission editor.
4. Accepted-command deduplication precedes current-role rejection only for the same submitting participant. A different participant reusing a command id fails closed.
5. Beliefs are personal until an author performs an explicit one-way share to `table(table_id)`. No room default and no metadata broadcast for personal creation.
6. “Authoring” in this slice means stating one of the existing pinned GAM-6 conditions. Advice remains fixture-backed, separately labelled, and never claimed for the live match.
7. Canonical addresses appear only after a played command commits. The live Study rail grows from captured history; it never invents a pre-command `erd1` address.
8. A participant owns their branch attempt. Branches never lock live play or one another and must return the exact public recorded decision.
9. Branch state is an owner-only Study projection. It is never part of an authoritative table broadcast or visible as the sibling participant's table truth.
10. The closed dispatch registry is the only route to a mutation. Unknown messages and unauthorized known messages fail before `GameSession`, with mutation-free denial as a tested invariant.
11. One checked control schema and fixture keep Python and TypeScript `testing-house-v1` shapes and supported operations aligned without changing GAM-6 evidence.
12. The table stays in memory and single-process, matching the existing release stack. Durable multi-worker rooms are not smuggled into this task.
13. Terminal changes table time control to Study in place. The shared match does not continue by redirecting both people into the standalone replay browser.

Wild success looks like a real testing session: a Discord link opens directly
to the exact table; the watcher understands whose move it is, shares a read
without exposing drafts, tests a line while the pilot plays, and can take the
pilot seat without refresh or ambiguity. The surprising win is that the same
viewer-safe frame and address become both the collaboration primitive and the
strongest leakage regression oracle.

Wild failure would come from treating “shared” as “broadcast everything”:
personal belief metadata leaks, invite tokens enter logs, role transitions
race with old commands, branch endpoints trust caller-supplied viewers, or two
socket handlers drain different game metadata. The closed table envelope,
server-derived viewer, grant revision, per-participant attempts, one-use
fragment invite, and single serialized broadcast path directly prevent those
failure modes.

## Scope

- In scope: one selected local table; one pilot and one same-perspective
  watcher; opaque viewer identities; one-use invite and scoped reconnect;
  closed role capabilities; atomic pilot handoff; ordered two-client live
  updates; personal GAM-6 scenario declarations; explicit table sharing;
  existing fixture advice comparison with provenance; live committed decision
  addresses; participant-owned exact Study branches; in-place terminal Study;
  keyboard, touch/mobile, reconnect, stale grant/command, viewer-equivalence,
  and isolation proof.
- Out of scope: changing or regenerating GAM-6 fixtures/evidence; a live
  advisor or possible-world provider; INT-9/GAM-7 authority; arbitrary belief
  text; accounts or durable identity; opponent-seat, public, or unlimited
  spectators; generic rooms/audiences/default permissions; chat, voice,
  reactions, feeds, presence history, broadcast infrastructure, moderation,
  tournament roles, Redis/database fanout, multi-worker deployment, and
  client-side legality.

## Done when

Backend contract and integration tests prove:

- the checked `testing-house-v1` control fixture round-trips through the closed
  Python and TypeScript contracts; their tagged mutations, enums, fields,
  requiredness, and dispatch operation set cannot drift;
- pilot and watcher join concurrently with distinct secrets and identical
  player-0 frames/addresses; reconnecting either preserves the other;
- every `command`/`action`, `pass_turn`, `set_stops`, `new_game`/rematch, role,
  belief/share, and branch mutation enters through the closed dispatcher;
  unknown/invalid messages and watcher commands/configuration fail before
  `GameSession` with identical pre/post authority fingerprints;
- a pilot handoff swaps capability and focus; old grant and stale revision
  commands fail closed; same-submitter accepted retries remain idempotent;
- personal beliefs are visible only to their author, explicit sharing reveals
  exact audience/author/provenance, and wrong fixture bindings expose no advice;
- a watcher forks a committed live decision while a pilot command advances the
  live match, then returns to the identical address/frame/cursor with zero
  changes to authoritative trace/root evidence; branch board/state reaches
  only its owner and is absent from all authoritative table broadcasts;
- both participants receive terminal mode `study` with the same final
  canonical addresses; cross-viewer/table/participant attempts fail closed;
- the private Study receipt normalizes to the public return under the locked
  Pydantic version.

Frontend unit and two-context Playwright tests prove:

- join, resume, role transfer, personal/share/compare, branch/return, and
  terminal Study all mutate the rendered DOM in both contexts without console
  or page errors;
- the watcher cannot activate live offers with pointer, Enter, Space, or F6;
  the active pilot can, and focus moves with the role;
- a 390x844 watcher viewport has no horizontal page overflow, all new critical
  targets are at least 44px high, the full new flow is keyboard operable, and
  axe reports no A/AA violations;
- role/connection/branch/share changes have named polite announcements and
  reduced motion preserves meaning.

Focused verification:

```bash
uv run --extra dev pytest tests/etude/test_testing_house.py \
  tests/etude/test_testing_house_protocol.py \
  tests/etude/test_server.py tests/etude/test_study_runtime.py \
  tests/etude/test_study_branch.py -q
npm --prefix frontend run check
npm --prefix frontend test -- --run src/lib/testing-house-protocol.test.ts
npm --prefix frontend run test:e2e -- testing-house.spec.ts
```

Before publication, also run `uv run --extra dev pytest tests/etude -q` and
`npm --prefix frontend run build`. No Rust protocol change is planned; if
implementation proves one necessary, run debug `cargo test` per `AGENTS.md`
before publishing.

## Measure

The correctness gate is zero unauthorized mutations, zero viewer-projection or
address drift, zero personal-belief disclosure before share, and exact branch
return. The Playwright proof also records 20 serialized pilot actions from
pilot activation through the watcher's matching `frame.revision`; target local
P95 is at most 250 ms with the passive opponent and no client may remain more
than one table broadcast behind. Record the observed distribution as test
attachment evidence rather than changing the existing release performance
baseline.
