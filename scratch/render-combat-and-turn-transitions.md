# W2-203 — Render combat and turn transitions from semantic events

## Directive and boundary

Directive v3 is incorporated. W2-184 merged as `43023ac`; this preserved
worktree was reconciled onto that authority before combat work resumed. Keep
the extension in this Task's one serial PR. It consumes W2-184's existing
protocol-v1 `PresentationEvent` path and does not duplicate the spell-event
seam, start another Task, create another worktree, or redesign the table.

The smallest correct authority change is additive, viewer-safe domain facts
published at existing Rust rules sites into the committed observation event
window. W2-184's Python projector consumes them from
`Observation.recent_events` and adds protocol revision, command, sequence,
grouping, pacing, and sound metadata. Presentation-only facts do not enter the
trigger/rules ledger, and the fixed Rust/Python learning encoders explicitly
filter their event types, so combat theater does not change policy inputs. The
projector may not derive combat meaning from before/after observations.

## User-visible outcome

During one recorded **UR Lessons versus GW Allies** sequence, a player sees
ordered semantic beats for an attacker group, an actual block when the
defender has and chooses one, each combat-damage assignment in engine order,
the resulting creature death, and the next turn starting. The authoritative
board commits immediately; the beats are disposable theater over it.

The same sequence and wording appear when the saved match is replayed and when
projected through the decision-inspector seam. In both live play and replay,
the player can skip one beat, finish the sequence, enable 4x fast-forward, or
use the operating-system reduced-motion preference without changing, dropping,
or reordering any canonical fact.

The touched `DECLARE_ATTACKER` and `DECLARE_BLOCKER` prompts have distinct,
Magic-readable legal choices. Their actionable cards and action buttons work
by pointer and keyboard, expose visible focus plus non-empty accessible names,
and submit only an offer currently published by the authority. In particular,
"attack" and "do not attack" must not share the current ambiguous label.

## Source of truth

Rules meaning remains native:

- the existing attacker declaration, blocker completion, combat damage,
  state-based death, and untap-step sites publish `CombatAttackersDeclared`,
  `BlockersDeclared`, `CombatDamageDealt`, `PermanentsDied`, and `TurnStarted`;
- those committed facts capture `ObjectEventRef` identities while the relevant
  permanents still exist, using retained LKI for departing permanents, and are
  drained through the established `observation_events` / `recent_events` path;
- viewer-only facts are excluded from trigger matching and rule-event counts;
  Rust and Python encoders filter event types above `AbilityTriggered`, keeping
  policy tensors byte-for-byte compatible;
- PyO3 exposes the additive event fields through the existing observation
  bridge. No parallel native presentation sidecar exists; the protocol's
  bounded presentation ledger remains the recovery envelope's consumer.

`managym/src/experience.rs` remains the protocol type authority. Replace the
opaque `serde_json::Value` kind with the already shipped cast/target/resolve/
damage/death variants plus typed `attack_group`, `blocked`, and `turn_started`
variants. Regenerate `protocol/experience-v1.schema.json`; TypeScript mirrors
that discriminated union and rejects unknown or malformed kinds.

`GameSession` is the envelope authority. For every accepted command it drains
the ordered native facts produced by the command and bounded auto-advance,
assigns strictly monotonic `seq` values, binds every event to that update's
`base_revision`, resulting revision, and command ID, and returns those exact
event dictionaries in `FrameUpdate.presentation`. The same dictionaries are
attached to the corresponding `TraceEvent.presentation`; trace persistence,
not a frontend fixture or snapshot comparison, is the recorded authority for
replay. Old trace events without `presentation` remain an empty sequence.
Its bounded 256-event recovery ledger stores those same dictionaries. A
cursor names the first returned event; a cursor outside the retained window
restarts disposable theater at the oldest retained event while the complete
frame converges game truth.

Display labels are context, not rules facts. Live presentation builds a label
registry from both the pre-commit and committed viewer-safe frames so a dead
permanent keeps its name. Replay derives the same registry from its adjacent
stored frames. Missing labels render the stable object fallback already used
by W2-184; they never cause a semantic event to be invented or discarded.

## One-PR build target

1. Add typed native viewer facts to the existing observation event window.
   Populate only attacker groups, completed blocker assignments, ordered
   combat damage, battlefield deaths, and turn starts. Unit-test the exact
   committed order through one real combat-to-turn tape.
2. Expose those facts through PyO3 while filtering them from both fixed
   learning encoders, rebuild the CPython 3.12 extension, and extend W2-184's
   `PresentationProjector`. Empty facts produce `presentation: []`; they never
   create generic "state changed" messages.
3. Persist the same event dictionaries sent live through W2-184's existing
   `TraceEvent.presentation` field, preserving hero normalization and hidden-
   hand redaction, and keep `buildReplayFrames` consuming them unchanged.
4. Type the Rust schema and frontend union for `attack_group`, `blocked`, and
   `turn_started`; extend W2-184's validator, beat copy, ARIA copy, label
   registry, and `presentationInspectorRows`. Table beats and inspector rows
   must be projections of the same event array.
5. Keep W2-184's one `PresentationPlayer` implementation for live and replay.
   Extend focused tests so skip-current, finish, 4x timing, and reduced-motion
   timing preserve the full combat sequence and replay does not advance its
   authoritative timeline until the current event array is exhausted or
   finished.
6. Make attacker choices distinct (`Attack with X` / `Do not attack with X`)
   from native action metadata, retain blocker labels (`Block A with B` /
   `B: do not block`), and add a bounded browser authority for interaction
   only. Assert prompt kind, focus ring, accessible name, exact current offer
   submission, and authority revision advance for both prompt families by
   pointer and Tab/Enter.
7. Record the deterministic curated tape as test data produced by the real
   named decks and engine. Pin the seed, passive/deterministic policy choices,
   action descriptions, deck pack reference, and expected ordered event kinds;
   do not hand-author a second set of semantic facts in TypeScript.

## Affected surfaces and compatibility

- **Native rules/events:** additive blocker-completion fact and presentation
  projection at existing attacker, damage, zone-move, and turn sites. Card
  legality, damage order, SBAs, triggers, search, and policy encoding remain
  unchanged.
- **PyO3/observation bridge:** additive viewer presentation data; rebuild the
  release extension as required by `AGENTS.md`. Existing observation fields
  and NumPy shapes remain byte-for-byte compatible.
- **Protocol artifacts:** typed `PresentationKind`, regenerated JSON Schema,
  and a non-empty combat conformance fixture. Existing Bolt fixture and
  protocol-v1 recovery/command fields remain valid.
- **FastAPI session:** monotonic presentation sequence state, per-command batch
  construction, and the current main recovery cursor/256-event ledger.
  Revision, idempotency, stops, and hero-view behavior stay authoritative.
- **Trace API:** optional/defaulted `TraceEvent.presentation`; normalization
  and redaction must retain viewer-safe events without exposing hidden cards.
- **Frontend:** existing socket commit gate, presentation player/stage, live
  board, replay timeline, and inspector projection. No second player or combat
  rules interpreter is added.
- **Interaction:** action metadata/labels for `DECLARE_ATTACKER` and
  `DECLARE_BLOCKER`, board target mapping, ActionPanel buttons, focus styling,
  and Playwright coverage. Other prompt families are unchanged.
- **Automation/docs:** Rust protocol/rules tests, Python GUI/trace tests,
  frontend unit/component tests, one focused browser spec, and an update to
  `docs/architecture/presentation-runtime.md` describing the now-live producer.

## Absent and error states

- A legal transition with no relevant native fact yields no beat. Empty
  attacker groups and combat with no damage/death are valid, not errors.
- If no legal blocker exists, no `blocked` beat is emitted and the curated
  proof records that absence. The pinned proof tape must choose a reachable
  block; if deck or engine drift makes it unreachable, the fixture test fails
  rather than fabricating one.
- Multiple blockers, first/double-strike passes, trample, and simultaneous
  deaths retain native ordering. The client does not recompute assignment,
  lethality, or simultaneity.
- A malformed, unordered, stale-revision, or unknown-kind presentation batch
  is rejected by the existing optional-theater boundary. The already committed
  frame remains canonical and usable.
- A missing object label produces a deterministic object-ID label. Missing
  presentation on an old trace produces no theater; replay remains usable.
- Skip, finish, fast-forward, reduced motion, unmount, and recovery may cancel
  timers or theater only. They never mutate a frame, trace event, offer, or
  command receipt.
- Trace redaction must reject any presentation payload that names or follows a
  hidden-zone object not visible to the hero.
- Duplicate/stale command and snapshot-plus-event recovery remain owned by the
  Experience Contract. This PR preserves their interfaces and proves the new
  combat dictionaries survive the current cursor-addressed retained tail.

## End-to-end proof

The semantic proof is a real engine-backed Python scenario using the installed
UR Lessons versus GW Allies pack and a minimal injected reachable board. It
executes the authoritative blocker choice through combat damage, both deaths,
cleanup, and the next turn, then asserts that the exact live dictionaries equal
the persisted trace dictionaries and the checked-in frontend fixture:

```text
attack_group -> blocked -> damage [-> damage ...] -> died -> turn_started
```

Frontend unit coverage loads those exact dictionaries into both live and
replay players, maps them through `presentationInspectorRows`, and proves skip,
4x fast-forward, and reduced motion change only duration/cursor.

The bounded browser proof uses a routed authority only for the UI interaction
contract. It repeats the two touched prompt actions with input modalities
swapped (attacker by keyboard, blocker by pointer), asserts distinct accessible
names and visible keyboard focus, and verifies that each submitted command is
bound to the currently published match revision, prompt, and offer. The real
engine scenario—not the browser route—owns combat semantics.

## Operational boundary

- One command may batch all native facts produced before the next surfaced
  hero prompt, but presentation order must match native emission order exactly.
- Maintain one monotonic event counter per match. Traces retain the complete
  committed sequence; recovery retains the latest 256 viewer-safe events and
  restarts theater from its oldest event when a requested cursor is outside
  that window.
- The authoritative frame is applied before the first beat. Presentation must
  never hold the rules loop, WebSocket response, replay frame data, or legal
  prompt hostage to its suggested duration.
- Normal timing remains authority-suggested with W2-184's 80 ms floor;
  fast-forward remains 4x; reduced motion remains a fixed 100 ms beat with no
  transition class. These are behavior contracts, not animation-quality or
  frame-rate claims.
- The focused browser scenario uses the existing 30-second per-authority-step
  bound and runs once, deterministically, with no random action selection or
  public network dependency.

## Verification target

After Rust changes, rebuild exactly as the repository requires, then run:

```bash
cd managym && uv run maturin build --release -i ../.venv/bin/python
uv run pytest tests/gui/test_server.py tests/gui/test_trace_api.py tests/gui/test_play_modes.py
cd managym && cargo test --test experience_protocol_tests
cd managym && cargo test
cd frontend && npm test
cd frontend && npm run check
cd frontend && npm run build
cd frontend && npm run test:e2e -- e2e/combat-presentation.spec.ts
```

The pursue finish line is observable: one real curated live match, its saved
replay, and the inspector seam all expose the same ordered native combat-to-turn
facts; both combat prompts work by pointer and keyboard; theater controls change
only pacing/cursor; and all focused plus regression gates pass. Rebase with
`lf rebase` immediately before landing if main moved, then use
`lf pr land -c` after verification. Do not use `lf pr submit`.

## Exclusions

- No generic animation engine, board redesign, new card art/audio production,
  combat preview/assignment UI, drag-and-drop, touch certification, or visual
  regression suite.
- No client-side combat, damage, blocker, death, or turn inference; no
  snapshot-diff narration.
- No every-card or every-prompt proof. This PR covers only
  `DECLARE_ATTACKER`, `DECLARE_BLOCKER`, and the filed combat-to-turn sequence.
- No replay re-simulation, checkpoint architecture, stale/duplicate-command
  redesign, durable match lease, WASM/worker adapter, or event-storm benchmark.
- No change to Magic rules, deck contents, bot policy, search strength,
  priority stops, hidden-information policy, or curated asset licensing.
