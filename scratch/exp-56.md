# Training API rebuild (EXP-56)

## Outcome and authority

Build `design-train-api.md` and the edited user docs, preserving existing
output carriage, planned-stop retries, row replacement and continuation.
`docs/training.md` is the intended experience, not a description of current code.
PR #103 stays draft. Jack Heart's recorded steer: "#103 was converted to
draft; keep it draft, do not mark it ready."

The complete earlier design, decisions, probes and QA history are in git:
`1666be1:scratch/exp-56.md` and its sibling scratch files. That checkpoint also
preserves the full positioning quotes and ontology exploration. The earlier
793-pass gate covers `105abdd`, not the unbuilt API.

## Blocked persistence decision

The existing carrier updates each mutable out object separately. The new API
promises one atomic save across several model/optimizer files. Interruption
between uploads leaves a mixed save and destroys some of the previous save.
Membership publication last does not preserve overwritten bytes. See
`questions.md` for the controlled result and candidate correction.

The implement instruction says: "If implementation turns up a counterexample
that breaks the slice, the data model, the deletion path or the overall design,
stop dependent work and revise the design or send it back to review."
This design is sent back to review for its state publication/layout and
retention protocol. No replacement API code has been started. The docs have
been preserved as written; their whole-save promise has not been weakened.

## Delete — do not maintain

When the persistence decision is resolved, replace these end to end:

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

1. Resolve the state publication protocol with interruption and recovery checks.
2. Unify/rename records and package; wire historical readers and outcome files.
3. Build state registration, steps, clocks, save cadence, seed and input APIs.
4. Carry folder checkpoints through tracking, launch inputs and evaluation;
   add start= and shared checkpoint enumeration.
5. Add local `etude run`, explicit local/launched --resume, create-only names,
   config/seed refusals and per-attempt source recording; keep finished runs safe.
6. Move job seeds and examples; focused acceptance tests, then the caller's
   gate/demo steps. No publication, landing or Graph changes in implement.

## Verification

`uv run python scratch/state-publication.py` — controlled interrupted delivery:
model=save-2, optimizer=save-1, state.json=save-1; membership unchanged.
No build or repository tests run: production code is unchanged. This probe
checks the retained transport, not a failing implementation of the new API.
