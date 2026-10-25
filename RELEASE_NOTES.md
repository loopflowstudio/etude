# v0.2.3

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.3 lets an evaluation follow a training run as it goes, without you keeping track of what has already been played. An experiment file's `eval(state)` picks checkpoints and matchups from what the training runs have stored. Each `etude eval` run plays, or launches, only the matches no eval run has delivered yet. Agents and checkpoints are now identified by key rather than by a digest of their bytes and code, so results still line up after an inference-code edit. Nothing breaks on upgrade from 0.2.2, and etude-sdk 0.2 eval results stay readable.

## Evaluate from an experiment file

Before this release, evaluating a run while it trained meant writing out each `hold` or `evaluation` call by hand and keeping track yourself of which checkpoints and pairs had been played. Now the file states the design and `etude eval` works out what is missing:

```
etude eval ppo.py                  # play what is missing, here
etude eval ppo.py --pool …         # or launch it on a pool
etude eval ppo.py --rent …         # or on a rented machine
etude eval ppo.py --watch 600      # repeat every 600 seconds
etude eval ppo.py --retry-failed   # play failed matches again
```

- The experiment is named by the file's stem (`ppo.py` → `ppo`). Etude reads that experiment's training runs with their checkpoints, ordinals, clocks, tags and status, then calls `eval(state)` for the evaluation jobs.
- Choose checkpoints with `every(n)`, `at(clock, …)`, `tagged(tag)` and `final`.
- A `Matchup` with no model seat is played once, not once per checkpoint. Seat a chosen checkpoint with `Model(...).agent(ref)`. This lets "each checkpoint against the one before it" or "final against each earlier one" grow as new checkpoints arrive.
- Matchups can carry `tags` (for example `consecutive` or `endpoint`), and `schedule` exposes them to readers.
- If `eval` stops returning a matchup, it is no longer played. Its results are kept.
- Each run reports its counts, for example `ladder: 0 new, 1 failed`. Failed matches are not replayed quietly; replay them with `--retry-failed`.

See the new **Experiments** section in `docs/evaluation.md`. ([#39](https://github.com/loopflowstudio/etude/pull/39))

## Launches are not repeated

Running the file again is safe while training is still going, after it ends, and after you add a matchup. Only new matches cost time or money.

- A launched batch is named after its matches and recorded in `ETUDE_HOME/experiments/launched.json`. A later run won't launch that batch again, and won't play its matches here while it is still running.
- If a launched run failed, the next `etude eval` stops, so nothing is spent again while nobody is watching.

## Results survive code changes

- A `CheckpointKey` is a training run plus a file name, or an outside address. An `AgentKey` is that checkpoint, or a code agent's name, plus its config.
- The code's commit and source digest are a `Version`. It is recorded with each match as `seats.commit` and `seats.source` but is not part of the key. If you change inference code, matches already played still count.
- Two runs warm-started from the same file no longer look like the same agent.
- `CheckpointRef` pairs a key with the artifact file to read. A checkpoint `Input` names its `run=`, so the machine knows the checkpoint's key.
- `evaluation()` leaves `experiment=` to `etude eval` for jobs an experiment returns. A job you launch directly must name it.

## Operational notes

- **Upgrading from etude-sdk 0.2 eval runs.** Agents in these runs load with their old digest as `AgentKey.recorded`. That value never matches a new key, so the old results stay readable but never count as played, and `etude eval` plays those matches again under the new keys.
- **Training code must track under the file's stem.** `etude launch` does not yet pass the file's name to its training runs. `ppo.py`'s training code must call `track(experiment="ppo")`, or `etude eval ppo.py` finds no runs.
- **`launched.json` is local.** It is what stops repeat launches. Run `etude eval` for an experiment from the same `ETUDE_HOME` each time.

## Known limits

- Replaying at a chosen commit is not built. Reads default to each match's latest record; `seats.commit` selects a version.
- Outside checkpoints can be played here but cannot be launched.