# Training persistence

This reference is for maintainers changing training persistence; authors start with [the guide](../training.md).

## What is on disk

This layout is a contract. Everything in it reads with `json` and your own
framework, without Etude installed.

```text
EXPERIMENT/JOB@SEED/         a plain train(config) file's run is EXPERIMENT/
  run.json                 job, seed, config, clocks, parent run and inputs
  outcome.json             how the latest attempt ended
  metrics/  artifacts/     rows and tables (Tracking)
  checkpoints/
    CLOCK-VALUE.json       each checkpoint's record, pointing to its files
  out.json                 what of out is stored: each key, SHA256 and byte length; written by delivery to storage
  out/                     run.out: follows the run to every machine
    state/
      current.json         {"save": "save-00000007"}: the save to load, written last
      save-00000007/       one save, complete in itself; never changes once written
        state.json         {"at": {"updates": 400}, "wall_ms": 1794600000000, "seed": 0, "commit": "8773d51",
                            "extra": {"games": 12800}, "files": {"model": "model.pt", "optimizer": "optimizer.pt"}}
        model.pt           torch.save(model.state_dict())
        optimizer.pt
    checkpoints/
      updates-00000100/    never changes; ROLE.updates-00000100/ for a role given by name
        checkpoint.json    {"at": {"updates": 100}, "files": {"policy": "policy.pt"}}, written last
        policy.pt
```

`run.json` stores its `id` as the address, such as `sweep/wide@1`, or `train`
for a plain `train(config)` file.
The same spelling is its folder below the runs directory and its bucket prefix.

<!-- implementation-reference -->
```python
import json
from pathlib import Path

import torch

folder = Path("~/.etude/track/train/out/checkpoints/updates-00000100").expanduser()
files = json.loads((folder / "checkpoint.json").read_text())["files"]
weights = torch.load(folder / files["policy"])
```

The saved state is read the same way, through its pointer:

<!-- implementation-reference -->
```python
state = Path("~/.etude/track/train/out/state").expanduser()
save = state / json.loads((state / "current.json").read_text())["save"]
files = json.loads((save / "state.json").read_text())["files"]
weights = torch.load(save / files["model"])
```

Always go through `current.json`. A save folder it does not name is one
being written or one waiting to be removed, and may be incomplete. Each entry
of `files` names a file in its own save's folder; no save reads another's.
Registered `**objects` map `<name>` to `<name>.pt`; files written by an
`export(folder)` callback are inventoried under their portable relative POSIX
paths (`{"weights.bin": "weights.bin"}`). `State.write` refuses symlinks,
`state.json`, dot-prefixed path components, `.open`/`.partial` suffixes,
collisions with registered `**objects` and writing when `restore=` is
registered without an `export=` callback. Existing saves written with
`files={}` remain readable and still pass their committed folder to
`restore(path)`. Named state is in `out/state/NAME/`, with its own
`current.json`.

On this computer runs are under `~/.etude/track` (moved by `ETUDE_HOME`);
under a launch the same folder is in your bucket. Using Etude to write a loop
does not tie its results to Etude.

## The pieces underneath

`run.steps` and `run.save` are made of four smaller pieces. A loop that does
not count on one clock uses them directly:

<!-- implementation-reference -->
```python
from etude.stopping import Stopped
from etude.train.records import Time
from etude.train.tracking import track


def train(config: SearchConfig) -> None:
    with track(clocks=[Time("training_seconds", "active seconds")], config=config) as run:
        saved = run.out / "frontier.json"
        frontier = load(saved) if saved.exists() else new()
        budget = run.until("training_seconds", default=config.seconds)
        while frontier.seconds < budget and not run.should_stop:
            frontier = expand(frontier)
            run.log({"open": len(frontier)}, training_seconds=frontier.seconds)
            save(frontier, saved)  # write beside it, then rename over it
        if frontier.seconds < budget:
            raise Stopped
```

| Piece | Is |
| --- | --- |
| `run.out` | a directory that follows the run: the same on every attempt, carried to storage as it is written and restored before each attempt on another machine |
| `run.until(clock, default=)` | the absolute target: the launch's for that clock, else the default |
| `run.should_stop` | true once a stop was asked for, or once the deadline is nearer than the slowest pass so far; ask once per pass |
| `Stopped` | raised after saving, short of the target: the attempt is recorded `stopped`; the launcher retries inside the run's limits, while `etude train` exits 75 and needs `--resume` |

Returning normally short of the target would read as finished. Files you
write in `run.out` yourself are carried one at a time, so write each
atomically and keep what one resume needs in one file: a machine lost between
two of them leaves one new and one old. Registering `export=` and `restore=`
on `run.resume(...)` (or updating `export=` via `run.save(export=fn)`) is the
way to keep several files together in one committed save. On a planned stop,
`run.steps` skips saving namespaces whose declared clocks have not moved from
`last_at` (or `0.0` on a fresh run), avoiding empty step-0 snapshots. A name
ending `.partial` is never sent, and anything under a folder whose name starts
with `.` stays on the machine.
