# Training API rebuild (EXP-56)

## Outcome and authority

Build `design-train-api.md` and the edited user docs, preserving existing
output carriage, planned-stop retries, row replacement and continuation.
`docs/training.md` is the intended experience, not a description of current code.
PR #103 stays draft. Jack Heart's recorded steer: "#103 was converted to
draft; keep it draft, do not mark it ready."

The complete earlier design, decisions, probes and QA history are in git:
`1666be1:scratch/exp-56.md` and its siblings `positioning.md` (every dated
remark), `ontology.md` (the naming exploration) and `etude-readme-draft.md`
(league-led README; its local_launch/until recipe surface is superseded). The
earlier 793-pass gate covers `105abdd`, not the unbuilt API.

Later work: EXP-57 live view; EXP-58 machines/metrics; EXP-59 broader JSON
commands; EXP-60 evaluation expansion; EXP-61 joinable shared budgets.

## State publication

Blocked on persistence review; no replacement API code has been started. The
new API promises one atomic save across several model/optimizer files, and
the existing carrier updates each mutable out object separately, so an
interrupted delivery leaves a mixed save. Direction, 2026-11-13: "snapshot
directory + pointer published last, recovery follows the pointer"; Jack Heart
on cost: "local storage probably fine, but ideally avoid remote extra
storage". `questions.md` (State publication) holds both probes' results, the
layout the docs show and the four open points; file sharing between saves
and cleanup under upload-only grants block the build.

## Docs for the build

The intended experience, to build against and keep true:

- `docs/training.md`: the whole train API; Carrying on and What is on disk
  carry the save layout.
- `docs/launcher.md`: restoration, delivery order of a save and its pointer,
  the storage layout, upload-only grants.
- `docs/tracking.md`: Etude's clocks across a resume, the run folder.
- `docs/evaluation.md`: players, folder checkpoints, on main's seatings and
  relative seats.

The branch holds main at `66ffb28` (merge `1fae5d9`); `questions.md` records
how the two sides were reconciled.

## Delete — do not maintain

Replace these end to end:

- `etude.run` internals package -> `etude.tracking`, updating all real consumers.
- Launch `Run` -> `RunRequest`; authors return Jobs with launch seeds instead.
- Separate tracking `RunDeclaration`/`RunFile` and observed `TrainingRun` ->
  the single stored `RunRecord`, shared with the launcher.
- Tracking `Attempt` -> the single `RunAttempt`, shared with the launcher.
- Experiment `Checkpoint`, eval `CheckpointKey`/`CheckpointRef` and tracking
  `CheckpointRecord` -> one record and one reference; preserve identity,
  lineage, ordinals, tags, clocks, digests and relative-seat resolution.
- Eval model noun -> player; report line noun -> job.
- Root `state.json` writer -> `outcome.json`; retain old outcome readers.
- Implicit resume on taken names -> create-only identity and explicit resume.
- `local_launch`, `track(seed=)` and `latest_checkpoint()` leave the taught
  API; the design requires one-release warnings for published spellings.

Preserve stored runs/evaluations, old file/field readers, progress, stop exit
75, cancellation, cross-machine restoration and hours/dollars/attempt limits.
Do not repair exclusive tests of deleted APIs; move behavior coverage to the
surviving API. Manabot cutover is MTG-329; do not edit its checkout here.

## Remaining implementation

1. Build state publication as the docs describe it, with interruption and
   recovery checks, once the persistence review settles `questions.md`.
2. Unify/rename records and package; wire historical readers and outcome files.
3. Build state registration, steps, clocks, save cadence, seed and input APIs.
4. Carry folder checkpoints through tracking, launch inputs and evaluation;
   add start= and shared checkpoint enumeration.
5. Add local `etude run`, explicit local/launched --resume, create-only names,
   config/seed refusals and per-attempt source recording; keep finished runs safe.
   `etude launch` and `etude run` accept the experiment name; the file's stem
   is only the default (design, Set up for Etude Cloud; `positioning.md`).
6. Move job seeds and examples; focused acceptance tests, then the caller's
   gate/demo steps. No publication, landing or Graph changes in implement.

## Verification

After merging main: mypy and ruff clean, 812 tests passed (`questions.md`).
The two state probes (`scratch/state-publication.py`,
`scratch/state-retention.py`) check the retained transport, not the new API.

Compress, 2026-11-13: no code cut; `git status --short src tests docs` empty.
The branch's source is the retained carriage (out membership, planned stops,
continuation), whose save path the persistence review will reshape, plus
tracking code this plan replaces.
