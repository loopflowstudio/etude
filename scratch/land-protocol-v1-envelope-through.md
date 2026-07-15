# W2-183 — Protocol-v1 envelope through one Bolt choice

## User-visible outcome

A player uses the existing table exactly as before, but a click no longer sends
a positional engine action that can be replayed against a different decision.
The table submits a client-generated command ID plus the current match,
revision, prompt, and offer IDs. The authority either applies that exact
offered action once or returns a typed rejection/recovery outcome.

The observable vertical slice is:

1. play a Mountain;
2. choose `Cast Lightning Bolt`;
3. choose `Target Villain`;
4. choose `Pass priority`;
5. observe the opponent at 17 life and the same current board/table UI.

Double-click/retry does not apply the action twice. A command selected from an
older frame is rejected and the client converges on the current frame. If the
connection is unavailable, the gameplay command is not queued; reconnect
recovers first and requires a selection from the recovered prompt.

## End-to-end proof

Use one deterministic Bolt/pass exchange with a passive opponent, seed 3,
`Mountain`/`Lightning Bolt` versus `Island`, and auto-pass disabled.

The proof must cross every boundary:

- `managym.Env` publishes legal positional actions;
- `GameSession` freezes them in a server-owned `PublishedPrompt` and emits an
  initial `RecoveryEnvelope` containing one atomic `ExperienceFrame`;
- WebSocket `command` requests lower offer IDs through that frozen map;
- accepted outcomes advance revision and include the next complete frame;
- the TypeScript socket applies the frame once to `GameStore`;
- the existing `ActionOption` table projection renders and submits the next
  offer without reconstructing legality.

Required receipts:

- `uv run python -m pytest tests/gui/test_server.py -q` proves the real
  Mountain -> Bolt -> Villain -> pass exchange, stale rejection, exact
  duplicate idempotency, unchanged trace length after stale/duplicate input,
  and reconnect reuse of match/revision/prompt.
- `npm test -- --run` proves a shared protocol-v1 fixture is accepted by the
  TypeScript declarations/store and that a gameplay selection made while the
  socket is closed is not added to the outbound queue.
- The shared fixture is consumed by both Python and TypeScript contract tests;
  handwritten lookalike payloads on each side do not count as compatibility
  proof.
- `npm run check` and `npm run build` prove the existing table remains a valid
  consumer.

The finish line is the observable exchange and failure behavior above, not the
existence of files, tests, a commit, or an open PR.

## Source of truth

`managym.Env` remains the rules authority. For one surfaced human decision,
`GameSession.published_prompt` is the authoritative adapter record binding:

- wire revision and prompt ID;
- each public offer ID;
- the exact engine action index that offer lowers to.

The client never invents an action index, target, or legality rule. An
`ExperienceFrame` is a viewer-safe projection of the current engine
observation plus the prompt and offers from that same revision. Command
receipts and the bounded in-memory command-ID map are the source for duplicate
outcomes. `RecoveryEnvelope` is derived from the current session authority;
the established trace remains the record of engine steps.

## Affected surfaces and consumers

- `gui/server.py`: prompt publication, frame/recovery construction, command
  validation/lowering, receipts, stale/duplicate outcomes, reconnect reuse.
- WebSocket `/ws/play`: existing new-game/resume/settings compatibility plus
  the protocol `command_outcome` transport.
- `frontend/src/lib/types.ts`: explicit v1 frame, offer, command, receipt,
  presentation-event, update, rejection, and recovery shapes for this slice.
- `frontend/src/lib/socket.svelte.ts`: command creation, one in-flight command,
  sequence/recovery gates, and the non-queue rule for gameplay commands.
- `frontend/src/lib/game.svelte.ts` and the play route: one atomic frame writer
  projected into the existing table/action model.
- GUI traces, replay readers, stop controls, legacy observation fields, hidden
  information redaction, and current table components must remain compatible.

`PresentationEvent` is an explicit typed boundary in v1, but this slice emits
empty ordered `presentation` and `presentation_tail` lists. Semantic event
production and playback belong to W2-184.

## Absent and error states

- No active match, malformed command object, or missing command ID: transport
  error; no engine step.
- Wrong match: `wrong_match` rejection; do not disclose another match through
  recovery.
- No published prompt or terminal game: no actionable offers; reject as
  authority busy or surface the game-over frame.
- Old revision or prompt: typed stale rejection plus complete current recovery.
- Unknown offer: typed rejection plus current recovery; never reinterpret an
  ID against the new prompt.
- Non-empty answers in this empty-answer positional bridge: invalid-selection
  rejection; no engine step.
- Exact command-ID retry: duplicate outcome with the original receipt and a
  current recovery frame, checked before stale validation; no second trace
  event.
- Reconnect: reuse the published prompt for the same decision rather than
  minting new IDs. An older recovery cannot overwrite a newer client frame.
- Closed/reconnecting socket: settings may retain their existing queue
  behavior, but gameplay commands are discarded before enqueue and are never
  replayed after recovery.
- Presentation lists may be empty; authoritative frame application must not
  depend on presentation.

## Operational boundary

- One WebSocket request/response per surfaced command; internal villain and
  auto-pass engine steps may be consolidated into the next frame.
- At most one gameplay command is in flight in the client.
- Retain only the latest 64 accepted command receipts in the process-local
  session.
- Preserve the existing 15-minute process-local session TTL and recovery
  model. Durable checkpoints and authority-restart recovery are not required.
- No added client-side rules computation and no new engine copies.

## Exclusions

- Rust/PyO3 changes or a Rust protocol representation.
- Generated bindings or full Rust/Python/TypeScript schema certification; the
  Project KR remains open after this Python/TypeScript fixture proof.
- Structured multi-step choice grammar or making cast-plus-target one new
  atomic engine command; this slice wraps the engine's existing surfaced steps.
- Semantic presentation events, animation, audio, or table redesign (W2-184).
- Replay-from-checkpoint persistence, authority restart, cross-process command
  dedupe, or durable recovery storage.
- Changes to game rules, card behavior, opponent policy, traces, stop
  semantics, asset packaging, or WASM.

## Pursue target

Keep PR #62 as the only serial PR. Reconcile its existing implementation with
this contract by adding only the missing explicit presentation type/seam,
shared Python/TypeScript fixture, reconnect prompt-stability assertion, and
closed-socket non-queue assertion. Re-run the focused GUI/frontend checks and
update the same PR. Do not broaden the projection, render semantic events, or
touch Rust.
