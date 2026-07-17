# Questions and assumptions

## Blocking provider seam

- No Intelligence symbol or stored lookup on this Task base accepts a
  Game-issued `CanonicalReplayV1`, trace ID, `ReplayDecisionAddress`, restored
  decision, or retained Study root and returns a `StudyArtifact` bound to that
  exact replay.
- The present `manabot.sim.study_evidence.build_study_artifact` consumes a
  separate Teacher-1 audit, hard-codes `int-4-trajectory-audit-v1`, and accepts
  policy mass from its caller rather than running a historical model inference;
  it cannot be relabelled as evidence for `replay.<match_id>`.
- The matching checked-in pinned artifact is fixture-only evidence and is not
  acceptable as the player-facing policy/search provider.
- `source_replay_sha256` has no shared runtime digest helper: the pinned fixture
  hashes pretty-printed projection bytes, the Intelligence builder trusts a
  caller string, and validators do not join that digest back to a loaded replay.

Decision: stop before runtime implementation, as directive v1 requires, and
resume only when Intelligence supplies the exact historical evidence seam or a
validated artifact for the selected Game replay. Do not recreate search,
policy, model, budget, or provenance authority in Game.

## Non-blocking first-slice assumptions

- Retry is available only for the just-completed match while its in-memory
  Rules roots remain retained. Expired recordings remain fully replayable and
  show an honest Retry-unavailable state.
- The first evidence-backed landmark must have at least one authored semantic
  event for its played or compared offer so the bounded continuation is
  observable without expanding presentation semantics in this Task.
- “Retry before reveal” means one accepted ordinary command is required before
  the server releases policy or search evidence. The historical played command
  already exists in canonical replay but remains visually hidden until reveal.
