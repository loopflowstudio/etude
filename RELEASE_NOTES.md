# v0.2.9

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.9 lets a report show checkpoints that were trained outside Etude. Before this release, a report needed a training run, and an external checkpoint could only be drawn as a fixed anchor. Now you can tell Etude where each checkpoint sits in its own training and which learner produced it. `etude report` then draws an evaluation-only experiment as one learning curve per learner. Upgrade if you evaluate checkpoints from another trainer, or if you want to compare a learner against its own earlier checkpoints.

## Evaluation-only experiments draw learning curves

A checkpoint reference can now say where the checkpoint sits and which learner it belongs to. That is enough for a report to place external checkpoints on a curve. Neither field changes the identity of a player or a match, so matches you have already recorded still count. ([#55](https://github.com/loopflowstudio/etude/pull/55))

- `CheckpointRef.at` records the checkpoint's clock positions, for example `at={"updates": ...}`. `CheckpointRef.line` names the learner it came from. Checkpoints tracked by Etude fill in both fields automatically.
- External checkpoints draw one curve per learner line. A report with no training runs is labelled "Evaluation only". A checkpoint with no position is still drawn as an anchor.
- Reports use the most recent seat metadata you supplied and fall back to training clocks. If two declarations for one checkpoint disagree, the report says so.

## Compare a learner with its own earlier checkpoints

Curves and crosstables can now compare checkpoints inside one learner, even across training continuations.

- `Curve(opponent=...)` accepts a player name, or a position on the same learner line given with `Earlier` or `At`. For example, `Curve(opponent=Earlier(clock="updates", by=400))` plays each checkpoint against the one 400 updates earlier.
- `Crosstable()` compares checkpoints within each learner. Pairs that were never played show `·`.
- If an opponent is missing or ambiguous, the curve leaves a gap and explains why.

## Matches in several environments count once each

- A match played outside a tournament counts once per environment, using its latest delivery. Commit views keep that environment scope, and failures are still reported per environment.
- If one comparison was played in several environments, the report shows the environment whose latest match is newest and notes the values it left out.

## Operational notes

- The release lists no compatibility break. Earlier stored eval records still load, and nothing needs to be played again.
- The derived stats database moves to schema 4 and is rebuilt from the stored records.
- Contributors: CI now runs the headless chart tests (`tests/test_charts.cjs`) on Node 22.

## Small changes

- `docs/evaluation.md` and `docs/tracking.md` describe checkpoint positions, learner lines and the new curve opponents.
- An unused type-check suppression that failed CI under a clean `mypy` run has been removed.