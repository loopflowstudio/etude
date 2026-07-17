# Confirmed interpretations and assumptions

## Canonical replay and Study branching

- The canonical Game index is one complete, globally chronological index over
  every deliberate player decision in the pinned match. Human commands and
  opponent-policy choices are indexed. Configured auto-passes, F6 expansion,
  and rules resolution are not decisions and remain semantic continuation.
- Every decision row is scoped to its acting viewer and stores only the exact
  `ExperienceFrame`, `InteractionOffer`, and `Command` safe for that viewer.
  The complete mixed-view index is authority-private. Any client or Study
  artifact is an authorized single-viewer projection and can never combine
  both players' private frames or presentation tracks.
- A stable address is stable within an immutable replay ID and exact decision
  payload. Editing the stored replay or row invalidates its integrity binding
  instead of silently resolving the address against changed history.
- Legacy traces that did not persist authoritative frames, offers, commands,
  viewer identities, revisions, and presentation cursors are not
  canonicalizable. They remain usable by the legacy replay viewer, but the
  canonical resolver fails closed instead of inferring missing history.
- The first fork-and-return slice retains exact engine roots only for the live
  completed `GameSession`. Durable root persistence or reconstruction is a
  later Game storage decision; this provider never rebuilds authority from
  legacy trace actions.

## INT-4 visit teacher iteration

- Directive v1 is treated as authorization to run the complete visit-based
  teacher/student iteration even though the earlier
  `w2-234-teacher1-pilot-v1` contract made teacher quality an admission gate.
  The new wave operating principle (“lead with building”) and INT-4's explicit
  student/arena deliverable make integrity the only pre-student hard stop.
- The terminal 100-game Teacher-0 pilot checkpoints are the incumbent controls.
  Their registered hashes remain in checked-in evidence, but no `.pt` bytes are
  present in this worktree; the production profile therefore cannot execute
  those control cells until the exact bytes are supplied. The engineering
  smoke omits them explicitly and makes no admission claim.
  The stronger 512-game snapshot checkpoint bytes named in checked-in evidence
  are absent, and the 3,000-game recovery manifest remains `running`; neither
  will be reconstructed or represented as terminal evidence.
- The Rules branching decision is consumed as final for this iteration:
  production compact full clone remains selected. If live PUCT economics fail
  with quality intact, the next build moves the search loop into Rust behind
  `FullCloneDriver`; it does not retest clone+undo or page COW.
- “One historical Study position” is interpreted as one position from the
  immutable, exactly replayed INT-4 self-play history. The existing
  `bolt-target` Study fixture has presentation history but no reconstructable
  authoritative engine state, so replacing its illustrative numbers with
  running search output would be dishonest.
