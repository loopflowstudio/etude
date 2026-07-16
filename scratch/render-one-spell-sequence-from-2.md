# W2-184 sequence 2: authority-authored presentation events

## Outcome

Ship one follow-up PR to merged PR #69 that makes the existing presentation
runtime consume an authority-authored Lightning Bolt sequence in both live play
and replay. The accepted target command must produce one ordered `cast`,
`targeted`, `resolved`, `damage`, and `died` batch from committed engine facts,
and the exact same batch must be persisted in the trace. No presentation fact
may be inferred from an observation diff.

The candidate implementation is already committed on
`jack-heart/render-one-spell-sequence-from-2` at `3e02ece`; compute from and
preserve that focused implementation. PR #69 is merged, and no GitHub PR
currently exists for sequence 2.

The user-visible finish is one narrated Bolt transition on the existing table:
live play and replay show the same five ordered beats, the decision-inspector
seam exposes those same facts, skip/fast-forward/reduced-motion controls remain
operable by pointer and keyboard, and all unrelated table behavior is
unchanged.

## Source of truth

- The Rust engine's committed `recent_events` and zone-bearing `EventData` are
  authoritative for spell resolution, damage, and battlefield-to-graveyard
  movement.
- Server-authored action offers and the accepted command are authoritative for
  the selected Lightning Bolt and target identities and for `caused_by`.
- The Python `PresentationProjector` derives one protocol-v1
  `PresentationEvent` batch only from those two authority inputs.
- `FrameUpdate.presentation` and the final transition trace step carry the
  exact same batch. Live play, replay, the shared presentation player, and the
  decision-inspector projection are consumers; none may infer semantic events
  from snapshots or labels.

## Contract

- Stage the Lightning Bolt card and target identities selected through the
  server-authored action offers. Staging alone emits nothing.
- Confirm meaning only from the engine's committed `recent_events`: spell cast,
  spell resolved, damage dealt, and battlefield-to-graveyard card movement.
- Bind the resulting presentation batch to the authoritative base/resulting
  revisions and accepted command id. Sequence numbers remain match-local,
  monotonic, and ordered.
- Emit `died` only when a committed battlefield-to-graveyard move and the
  resulting viewer-safe battlefield agree that the targeted permanent left.
  Lethal damage must not be mislabeled as the distinct rules action
  `destroyed`.
- Return the batch in `FrameUpdate.presentation` and attach that same value to
  the transition's final trace step. Older traces without the optional field
  replay as an empty event sequence.
- Merge pre- and post-transition viewer-safe labels in live play and replay so
  a departed target remains nameable. Labels provide display context only and
  never establish that an event occurred.

## Implementation plan

1. Extend `EventData` and its Python binding with committed card-move
   `from_zone`/`to_zone` metadata, using `-1` for event kinds without zones.
   Keep the learning event tensor unchanged and prove the new metadata with a
   focused Rust scenario test.
2. Add a match-local Python `PresentationProjector`. Record selected Bolt and
   target identities before stepping, consume committed domain events after
   the step, validate output through the protocol-v1 `PresentationEvent`
   model, and drain facts only when the server commits a revision transition.
3. Integrate the projector into every `GameSession` transition. Reset it for a
   new game, discard setup-only facts, pass through `caused_by`, and persist a
   non-empty batch on the transition's final trace event.
4. Make live and replay presentation label resolution retain adjacent-frame
   labels while continuing to use the already-merged shared player and
   inspector seam.
5. Add end-to-end proof that the recorded Lightning Bolt scenario produces the
   five canonical beats, that live and persisted replay arrays are identical,
   and that direct snapshot/scenario changes emit nothing. Update the runtime
   architecture note to state the resulting boundary.
6. Before publication, remove branch-only Loopflow scratch-stash artifacts and
   let the normal Loopflow submit/land path clear `scratch/`; neither belongs in
   the product diff.

## Affected surfaces and compatibility

- **Rust engine/Python binding:** add card-move zone metadata without changing
  the learning event tensor or existing event meanings.
- **FastAPI authority:** project the accepted Bolt transition into
  `FrameUpdate.presentation`; all other actions continue to emit an empty
  presentation list unless authoritative facts support this exact slice.
- **Trace/replay:** persist the same transition batch. Older traces with no
  optional presentation field remain valid and replay with no semantic beats.
- **Frontend live/replay:** retain adjacent viewer-safe labels for display and
  feed both paths through the existing shared presentation player.
- **Decision inspector:** continue consuming the shared canonical event
  projection; no separate rules or inspector-only event model is added.
- **Interaction/accessibility:** retain native pointer/keyboard buttons,
  skip, fast-forward, and reduced-motion semantics from PR #69.
- **Unchanged consumers:** current snapshot board rendering, action legality,
  hidden-information redaction, protocol command validation, and non-Bolt
  table flows remain compatible.

## Absent and error states

- Missing or unrelated committed events produce an empty presentation batch;
  snapshots, changed life totals, or missing labels never synthesize facts.
- Staged cast/target identities without matching committed resolution facts
  emit nothing and are discarded at the transition boundary.
- Lethal damage without a committed battlefield-to-graveyard move emits damage
  but not death. A move that does not match the staged target emits no target
  death.
- Invalid projected output fails protocol-v1 model validation in focused tests
  rather than being sent as a best-effort event.
- Old traces without `presentation` replay normally with an empty list; unknown
  object labels fall back to existing viewer-safe naming without changing
  event meaning.
- Failed native-extension rebuild, Rust/Python/frontend verification, PR
  publication, CI, or final rebase blocks release and must not be treated as a
  completed Task.

## Operational boundary

- One match-local projector consumes only the bounded `recent_events` batch for
  each accepted transition; it performs no network I/O, subprocess work,
  history scan, snapshot diff, or client-side rules evaluation.
- Presentation sequence numbers are monotonic within a match and the five Bolt
  beats are delivered as one ordered transition batch. Skip and reduced-motion
  may collapse display timing but must not reorder, duplicate, or discard the
  canonical event facts exposed to replay or the inspector.
- Release remains one serial PR rooted on `main`. After focused checks and CI
  are green, rebase immediately before the single headless `lf pr land -c`.

## Non-goals

- General presentation projection for every spell or game event.
- Client-side rules, snapshot-diff narration, or a visual-system redesign.
- Recovery-tail reconstruction or retrofitting old traces.
- Exact object incarnations or a separate stack render id; this vertical slice
  retains the documented incarnation-zero/visible-card-id adapter boundary.
- W2-203, W2-195, or unrelated table and decision-inspector work.

## Acceptance proof

- A recorded seed-7 Lightning Bolt into Gray Ogre produces exactly `cast`,
  `targeted`, `resolved`, `damage`, and `died`, with one revision range and the
  accepted target command as `caused_by`.
- The live `FrameUpdate.presentation` array is byte-for-byte equal to the
  flattened persisted replay array.
- Scenario-only zone changes produce no presentation events.
- A committed `CardMoved` observation retains battlefield/graveyard zone
  metadata through Rust and Python.
- Presentation controls remain native buttons, reduced-motion/skip behavior
  remains intact, and a dead target retains its viewer-safe name.

Verification:

```bash
cargo test --manifest-path managym/Cargo.toml
(cd managym && uv run maturin build --release -i ../.venv/bin/python)
```

Place the wheel's cp312 extension at
`managym/_managym.cpython-312-darwin.so`, then run:

```bash
uv run pytest tests/gui/test_presentation.py tests/gui/test_server.py tests/gui/test_trace_api.py
cd frontend
npm test -- --run src/lib/presentation.test.ts src/lib/components/PresentationStage.svelte.test.ts src/lib/socket.svelte.test.ts
npm run check
```

The end-to-end observable condition is satisfied only when the seed-7 Bolt
scenario crosses Rust committed events, the Python authority projector, the
live `FrameUpdate`, persisted trace/replay, shared presentation player, and
decision-inspector projection with the same ordered facts, while the focused
commands above pass and the existing table remains operable.
