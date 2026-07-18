# Review decisions and assumptions

## Confirmed by human review

- Proceed with the complete canonical timeline and Retry/return substrate now;
  do not block that work on the final Intelligence provider.
- Keep the evidence boundary typed and fail closed. Normal runtime serves no
  fixture or relabelled evidence; a direct fixture provider is allowed only as
  constructor-injected test infrastructure.
- Reveal requires one accepted retry command. The player may Return or leave
  Study without revealing, but there is no reveal escape hatch that bypasses
  the prediction.
- Every Policy/Search alternative preview starts from a fresh exact fork,
  executes exactly one ordinary semantic command, projects only that committed
  transition, returns, and discards the fork. It never mutates replay or reuses
  the player's Retry branch.

## External evidence seam

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

Decision: this is not a blocker for the reviewed substrate. Implement a typed
`HistoricalStudyEvidenceProvider` request carrying the Game projection, its
Game-computed canonical digest, address, and restored decision. The production
default returns typed `study_evidence_unavailable`; Game still validates every
identity on a successful response. Do not recreate search, policy, model,
budget, or provenance authority in Game.

## Genuine remaining blockers

- None for implementing and landing the Retry/return substrate as a serial PR.
- Honest player-facing Policy/Search comparison in normal runtime still cannot
  complete until Intelligence supplies exact evidence for the selected Game
  address. If that provider is still absent after substrate verification, keep
  GAM-4 open for the next serial PR rather than completing the Task or claiming
  the comparison KR.

## Non-blocking first-slice assumptions

- Retry is available only for the just-completed match while its in-memory
  Rules roots remain retained. Expired recordings remain fully replayable and
  show an honest Retry-unavailable state.
- The first evidence-backed landmark must have at least one authored semantic
  event for its played or compared offer so the bounded continuation is
  observable without expanding presentation semantics in this Task.
- The historical played command already exists in canonical replay but remains
  visually hidden until reveal.
