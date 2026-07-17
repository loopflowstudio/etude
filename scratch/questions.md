# Assumptions and review disposition

- Directive v2 was the greeting “hello.” It is treated as non-substantive, so
  INT-10 retains its existing architecture-review scope.
- This is a headless kickoff even though the original task was framed as an
  interactive review. No architectural changes are implemented. The scratch
  design is the human review surface.
- The recommended durable location is
  `docs/architecture/search-learning.md`, linked from `manabot/README.md` and
  the docs index after human acceptance. The map is intentionally not promoted
  there during kickoff.
- INT-6 arena work and INT-9 exact-range work were inspected in their active
  sibling Task worktrees to distinguish in-flight designs from current APIs.
  Their contracts are not described as landed on this branch.
- Missing exact frozen checkpoint bytes remain a hard blocker for production
  gameplay/evidence claims. This document describes the fail-closed boundary
  and does not substitute fixtures or another replay.
- The frozen INT-4 smoke contract also does not match the current engine source
  or rebuilt extension digest. That is a successful fail-closed check, not a
  reason to edit historical inputs. The next proposed end-to-end build begins
  with a newly registered current-runtime smoke contract.
- The next build uses a self-starting uniform-prior/random-leaf PUCT teacher and
  trains every checkpoint it consumes inside the run. Its current-harness games
  are explicitly non-admission evidence, so it neither depends on nor
  duplicates the in-flight INT-6 arena.
