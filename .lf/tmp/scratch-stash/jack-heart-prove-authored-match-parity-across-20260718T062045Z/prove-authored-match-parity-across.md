# Prove Authored Match Parity Across Live, Headless, and Replay

## Problem

PR #136 proves that one seed-0 UR Lessons versus GW Allies match reaches a
winner through the compiled release authority. It does not yet prove that the
same authored match means the same thing when consumed through Etude's live
boundary, a direct headless `managym.Env`, and persisted canonical replay.
Playable Curated World KR2 needs that stronger claim at every revision, not a
terminal-only comparison.

The input is immutable:
`conformance/authored-match-v1/release-stack-ur-vs-gw-seed-0.json` and its
exact 132 recorded `Command` values. The parity proof must load that tape; it
must never call the receipt generator or choose from a newly generated policy
trace. The result is for players, replay consumers, and future Study/search
consumers that need one shared rules history rather than surface-specific
interpretations.

This work advances the Rules measures that live play and deterministic replay
"reproduce the same semantic consequences at shared identities" and that
"viewer-private information [and] stale object incarnations ... remain exact."
It does not claim the still-separate Intelligence search or Study workload
integrations.

## The demo

From any checkout of the revision, run:

```bash
./scripts/verify-authored-match-parity
```

The command prepares the pinned CPython 3.12 native extension when necessary,
runs the focused Rust proof in debug, recomputes the checked receipt, and
prints one line like:

```text
RUL5_PARITY_OK commands=132 checkpoints=133 viewers=266 divergence=none object_ref=102@2 object_ref_exact=true stale_offer=35@37 rejection=stale_or_illegal
```

Any mismatch exits nonzero and names the first surface, revision, field,
frozen-input digest, and relevant-source digest.

## Approach

### Freeze the authority tape

Add a parity verifier that reads the PR #136 JSON bytes and validates its fixed
identity before starting any environment:

- version 1, matchup `ur-lessons-vs-gw-allies`, seed 0;
- exactly 132 deliberate commands and terminal revision 132;
- contiguous ordinals and revision bindings;
- both actors present;
- the recorded terminal witness and zero fallback counters unchanged.

Record both the whole authority-receipt SHA-256 and an order-sensitive digest
of the 132 canonical `Command` JSON objects. The verifier must not import or
call `generate_authored_match_receipt`; `--write` may write the new parity
receipt, but it still consumes the old receipt as data.

### Produce one comparable transition record

Normalize every surface into the same transition shape:

```text
revision / actor / command digest
state witness before and after
ordered semantic-event payload and cumulative semantic cursor
viewer-0 and viewer-1 projection digests
terminal flag
```

Revision 0 is an explicit initial checkpoint. Each of the 132 accepted
commands adds the next checkpoint, giving 133 witness comparisons. Event
comparison uses the complete normalized payload already established by the
authority receipt, including source/target entity and incarnation, zones,
controller, amounts, and related definition IDs. Hashes in the compact
receipt are order-sensitive; verification compares the full in-memory lists
before hashing them.

The first mismatch stops comparison immediately and reports:

```text
surface=<live|headless|replay> from_revision=N field=<...>
expected=<...> actual=<...>
authority_receipt_sha256=<...> relevant_source_sha256=<...>
```

The checked successful receipt records `first_divergence: null` plus the
per-revision witness and consequence digests for all three surfaces.

### Execute the three real surfaces

1. **Release-stack live.** Drive `/ws/play` with FastAPI's in-process WebSocket
   client. Inject only deterministic receipt fixtures—fixed IDs, time, trace
   directory, and a villain tape cursor—into the existing `GameSession`; do not
   bypass the WebSocket command handler. Send every hero `Command` byte-for-
   semantic-byte from the frozen tape. The existing policy callback selects
   each villain offer from the tape, and the captured authority transition
   must contain the exact recorded villain `Command`, including its generated
   command ID, revision, prompt, offer, and empty answers.
2. **Direct headless.** Reset a fresh `managym.Env` with seed 0 and the two
   curated deck manifests. At each revision validate actor, action count, and
   the recorded offer's action type/focus before lowering that same command's
   `offer_id` to the direct engine action. This was experimentally confirmed
   for all 132 revisions; every before/after `state_digest` and full ordered
   `recent_events` list already matches the PR #136 authority receipt.
3. **Canonical replay.** Finalize the live trace, load its persisted
   `canonical_replay` through the canonical validator, and assert that its 132
   commands exactly equal the frozen tape. Feed those persisted commands into
   a second fresh `managym.Env` through the same headless lowering function.
   This is a replay driver over the one rules engine, not a replay rules
   implementation. Compare its witnesses and ordered semantic consequences
   against both the frozen authority and the other two surfaces at every
   revision.

Live authoritative presentation events must also equal the PR #136 event
groups at each revision. The reloaded canonical presentation tracks must equal
the live groups after applying the existing viewer rule: the acting viewer may
retain `caused_by`, while the other viewer must see it redacted. Headless rules
play is not required to manufacture Game-owned presentation events.

### Prove viewer privacy at every state

At revisions 0 through 132, ask the engine for fixed projections for viewers 0
and 1, for 266 checks total. On every live, headless, and replay checkpoint:

- `agent.player_index` is the authorized viewer and `opponent` is the other
  player;
- the opponent hand contains no card identities and its hidden count equals
  the hand zone count;
- neither library is serialized as card identities, only counts;
- offers, answers, and command IDs belonging to the other player are absent
  from that viewer's replay projection;
- non-acting presentation entries do not expose the other player's
  `caused_by` command identity.

Project the persisted canonical replay separately for players 0 and 1 and
prove each projection contains only that player's decision rows. No spectator
track is admitted by protocol v1, so viewer 2 must fail closed with
`DecisionNotFoundError`; the receipt records `spectator_admitted: false`
instead of inventing a spectator policy.

### Prove exact object binding and stale-offer rejection on the real trace

Use the existing trace rather than a synthetic card scenario. At revision 35,
the structured attacker offer contains entity 102; the transition from
revision 36 to checkpoint 37 shows that exact incarnation, 102@2, dying.

The trace exposed one reusable projection bug: `managym` structured offers
currently hardcode `ObjectRenderId.incarnation` to zero. Replace that helper
with projection from the current internal `ObjectRef` while preserving the
stable viewer-safe render entity. Apply it to spell sources, target candidates,
and attacker candidates. Bind object candidates privately to that captured
exact `ObjectRef`, not only a `PermanentId`, so a candidate can never rebind to
a later incarnation. Update only the focused fixtures and tests that assert
these identities.

At revision 35, clone the parity environment, retain the published
`StructuredOfferSet`, and select the candidate whose public value is 102@2.

Make two independent assertions rather than treating one rejection as proof of
both properties:

1. **Exact `ObjectRef` binding while current.** Before advancing the clone, use
   a focused Rust assertion at revision 35 to resolve the selected candidate ID
   through its still-current private offer binding and prove that it names the
   exact internal `ObjectRef` for 102@2, not a bare `PermanentId`, storage slot,
   or incarnation-zero reconstruction. This is an identity/projection proof;
   it does not submit stale input or claim a rejection.
2. **Atomic stale revision/offer rejection.** Retain that revision-35 offer,
   advance the clone through frozen commands 35 and 36 so 102@2 dies and the
   environment reaches checkpoint 37, then submit the retained candidate ID
   through the normal public structured command boundary. Assert the typed Rust
   result `StructuredOfferError::StaleOrIllegal` and the public Python
   `managym.AgentError` with the stable stale/illegal message. Because the
   retained prompt, offer, and revision binding are stale, the public path may
   reject before object lookup. This proves that stale public input is rejected
   atomically; it does not claim that exact-object lookup was the rejection
   stage or cause.

Candidate IDs remain the submitted wire value in both assertions. Clients
never submit or fabricate raw engine storage IDs. For the rejection assertion,
compare `state_digest` and the committed semantic event cursor before and after
submission. If the cursor is not currently exposed to Python, add a narrow
read-only `semantic_event_cursor()` accessor to `Env`/PyO3; do not add a new
mutation or replay API. The checked receipt stores the exact-object validation
separately from the stale submission: captured render ref, resolved exact
`ObjectRef`, capture and submission revisions, death transition, rejection
type and observed rejection stage, and identical before/after witness and
cursor.

### Check in a reproducible receipt

Write
`conformance/authored-match-parity-v1/release-live-headless-replay-seed-0.json`
and a short README. The receipt contains no absolute paths, worktree names,
mtimes, or incidental environment data.

Bind it with `relative-path-and-file-sha256-v1`: enumerate a declared relevant
closure, hash each file's bytes, sort by repository-relative path, and hash the
canonical manifest. The closure includes:

- the frozen authority receipt and checked semantic IR;
- the curated pack manifest;
- the parity verifier, launcher, receipt tests, and focused stale-reference
  regression;
- the Etude live/replay/projection modules actually imported by the verifier;
- `managym/Cargo.toml`, `managym/Cargo.lock`, and all Rust runtime files under
  `managym/src`.

The receipt stores every relative path and individual digest as well as the
closure digest. Directory discovery is asserted so a newly added runtime Rust
file cannot be omitted silently. Tests and receipt output are not permitted to
derive identity from `HEAD`, an absolute worktree path, or an untracked binary.
The same source bytes therefore verify from another checkout.

The single launcher uses locked uv dependencies, builds the CPython 3.12
extension only when it is absent or not importable, runs the focused Rust test
with debug assertions enabled, then runs the Python receipt verifier in check
mode. Every Python invocation remains under `uv run`.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|------------------|
| Can the PR #136 command tape be consumed without rerunning its random policy? | Yes. A fixed-tape `GameSession` accepted all 132 recorded commands and reproduced the terminal witness `e48de247...2de7`. | Load commands from the checked receipt; never invoke its generator or policy chooser. |
| Does direct headless play already match the authority at each revision? | Yes. A fresh `managym.Env` matched all 132 before/after digests and every complete ordered semantic-event list. | Headless parity is a receipt/checking implementation, not a rules rewrite. |
| Is canonical replay executable today? | Canonical replay validates, projects, and restores decisions but has no execution loop. Reloading its persisted commands into a fresh `managym.Env` reproduced all 132 revisions and terminal state. | Add a thin shared tape runner over `managym.Env`; do not create a second replay engine. |
| Can the release-stack path preserve exact villain commands? | Yes. The deterministic villain callback returns the recorded offer ID, and the existing authority command sequence recreates the recorded policy command IDs exactly; captured transitions can compare the complete commands. | Exercise the WebSocket handler and verify captured commands, rather than adding an all-actors public mutation API. |
| Are both player projections private across the whole trace? | Yes in the experiment: 266 projections over 133 states hid opponent hand identities and both libraries. Canonical projections for viewers 0 and 1 contain only their rows; viewer 2 fails closed. | Make these exhaustive checks part of the receipt, with no new spectator mode. |
| Does the real trace contain a stale-incarnation case? | Yes. Entity 102 is offered at revision 35 as incarnation 2 and dies on the transition from revision 36 to checkpoint 37. | Reuse that exact state and avoid a synthetic conformance scenario. |
| What is missing for the stale proof? | Structured offer render IDs hardcode incarnation 0 and private object candidates retain storage identities even though events carry the true epoch. Existing command application already snapshots and returns `StructuredOfferError::StaleOrIllegal` without mutation. The retained revision-35 prompt/offer is itself stale at checkpoint 37, so normal validation may reject before looking up the object. | Prove exact candidate-to-`ObjectRef` binding while the offer is current, then separately prove atomic stale revision/offer rejection and an unchanged event cursor. Do not infer the rejection stage from the exact-binding assertion. |
| Will the verifier run from a fresh checkout? | Not without preparing the native extension; this worktree initially failed with `ModuleNotFoundError: managym._managym`. The repository already has a locked `maturin develop` pattern in `scripts/play.py`. | The single launcher must prepare/import the pinned CPython 3.12 extension before importing parity code. |
| Can source identity avoid incidental worktree state? | Yes. A canonical manifest of explicit repository-relative file digests is independent of checkout location and does not have the self-reference problem of embedding the final commit hash. | Store the manifest and closure digest; exclude binaries, mtimes, absolute paths, and unrelated files. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Rerun the seed-0 offer policy on all three surfaces | Simple and currently deterministic | It could choose a new trace after offer-order drift and would violate the directive that PR #136's 132 commands are immutable input. |
| Compare only terminal winner/state | Small receipt | It cannot locate a transient divergence that later reconverges and does not prove ordered consequences or privacy at public revisions. |
| Treat canonical replay validation as replay parity | Reuses existing schema checks | It proves that recorded rows are internally well formed, not that persisted commands reproduce rules state and events. |
| Build a replay-specific state reducer | Could consume replay JSON without the engine | It creates a parallel rules authority and is explicitly out of scope. |
| Expand Etude protocol offers for every prompt family before parity | Would make all commands natively structured at once | It reopens the already accepted authority/prompt work and obscures the narrow KR2 proof. The stale case can use the existing candidate-bound structured command seam. |
| Use a synthetic blink/death fixture for stale references | Easier to arrange | The authored trace already exposes a precise death, and the task requires focused regressions tied to real play. |

## Wild success

The receipt becomes the small, trusted seam test for any future replay, Study,
or server refactor. A developer changes rules, event projection, or replay
loading and gets the exact first bad revision with source identity in one
command. The useful surprise is that the same artifact also catches privacy
regressions and public incarnation drift without growing into a general Magic
conformance suite.

## Wild failure and guardrails

Six months later this would be removed if it silently regenerated the policy
tape, compared only hashes produced by one shared buggy normalizer, called an
in-memory object "replay," or accumulated a second rules executor. Guard
against those failures by hashing the immutable input bytes, comparing full
event payloads before receipt compaction, reloading canonical replay from the
saved trace, keeping execution in `managym.Env`, and reporting the first raw
field mismatch before any aggregate summary.

It would also fail if "privacy" meant only an empty hand in one terminal frame.
The exhaustive 133-state, two-viewer checks and fail-closed unadmitted viewer
are therefore part of receipt generation, not separate broad tests.

## Key decisions

- PR #136's receipt is frozen input. The new proof never regenerates it.
- The live surface is the actual `/ws/play` command handler with deterministic
  fixture injection, not a direct call mislabeled as live.
- Headless and replay share one small command-to-engine lowerer and one
  `managym` rules implementation; their independently sourced tapes are still
  compared to the frozen authority at every transition.
- Canonical replay is serialized, saved, loaded, validated, then executed. An
  in-memory copy does not satisfy the replay claim.
- Ordered semantic events are compared in full. The receipt stores compact
  order-sensitive digests only after equality succeeds.
- Public projection uses the stable render entity plus the internal exact
  incarnation. Bare internal storage IDs still never cross the boundary.
- A client submits the candidate ID bound to the published exact-object value;
  it does not echo a trusted raw `ObjectRef`. Exact candidate-to-`ObjectRef`
  validation is proved while the revision-35 offer is current.
- Reusing that candidate at checkpoint 37 separately proves atomic stale
  revision/offer rejection. It does not prove that object lookup was reached or
  caused the rejection.
- There is no spectator surface in protocol v1. Fail closed and record that
  fact rather than designing one in RUL-5.
- Source identity is a content manifest over the relevant closure, not a local
  path or self-referential commit hash.
- No latency, throughput, RSS, token-count, fallback, new-card, or broad
  conformance measurement enters this receipt.

## Scope

- In scope: consume the exact 132 PR #136 commands; WebSocket live execution;
  direct headless execution; persisted canonical replay execution; 133
  revision witnesses; ordered engine events; live/replay presentation tracks;
  both player projections; fail-closed unadmitted viewer; real-trace stale
  object rejection; exact public incarnation projection; focused regression
  tests; reproducible source manifest; one fresh-checkout verifier.
- Out of scope: compiled authority or prompt-family rework; regenerated policy
  traces; workload/latency/RSS/token budgets; Intelligence search; Study fork
  performance; new cards or mechanics; general spectator design; broad CR
  conformance/fuzzing; a replay-specific rules engine; frontend rendering.

## Done when

`./scripts/verify-authored-match-parity` succeeds from a checkout with no
prebuilt native extension and proves all of the following in its checked-in
receipt:

- the frozen input SHA and command-tape SHA name the unchanged PR #136 seed-0
  authority receipt;
- live, headless, and persisted replay each accept the same 132 commands and
  have identical witnesses at all 133 revisions;
- all ordered semantic consequences match at every transition and
  `first_divergence` is null;
- live presentation groups and both canonical viewer tracks match with only
  the specified non-actor command-identity redaction;
- all 266 player projections hide opponent hand, library, and choice facts,
  and viewer 2 fails closed because no spectator is admitted;
- while the revision-35 offer is current, entity 102 is projected as
  incarnation 2 and its candidate ID resolves privately to exact `ObjectRef`
  102@2;
- after 102@2 dies during transition 36→37, the retained revision-35
  prompt/offer is submitted at checkpoint 37 and rejected as
  `StructuredOfferError::StaleOrIllegal`, with the observed validation stage
  recorded and the state witness and committed event cursor unchanged;
- the source manifest contains only repository-relative paths and recomputes
  the checked relevant-source digest from another checkout;
- the focused Rust tests run in debug and the focused Python receipt tests pass.

This closes only Playable Curated World KR2 and the corresponding live/replay
and privacy/incarnation portions of the Rules measures.

## Measure

This is an exactness receipt, not a performance experiment. The acceptance
counts are fixed and binary:

- 132/132 exact commands consumed on each of three surfaces;
- 133/133 state checkpoints equal across all surfaces;
- 132/132 ordered semantic transition groups equal;
- 266/266 viewer projections private;
- 2/2 canonical player projections isolated;
- 0 admitted spectator tracks and one fail-closed viewer-2 check;
- 1/1 real-trace candidate resolves to exact `ObjectRef` 102@2 while current;
- 1/1 retained revision-35 offer is rejected atomically at checkpoint 37;
- 0 first divergences and 0 privacy leaks.

Command latency, throughput, RSS, semantic token counts, and workload budgets
belong to Playable Curated World KR4 and are deliberately not measured here.
