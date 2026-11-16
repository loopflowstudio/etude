# Your first experiment

This guide takes an author from a local training/eval prototype to the same
experiment on one rented machine.

From this repository checkout, run:

```sh
uv sync
uv run etude train examples/prototype.py
uv run etude eval examples/prototype.py
uv run etude report examples/prototype.py --open
```

[`examples/prototype.py`](../examples/prototype.py) is the complete experiment.
It learns a coin's bias, publishes a JSON checkpoint, compares its predictions
with a fair-coin baseline, and reports the success rate. It needs no model
library. For a separate project, copy it as `experiment.py`, run
`uv init` if needed, then `uv add 'etude-sdk[eval]'`.

The file defines `training_jobs()` (training config and independent run seeds),
`eval_jobs(state)` (checkpoint selection and match generator), and `report()`
(the displayed score). The actual work lives in `train(config)`,
`play(match, agents)`, `load(checkpoint)` and `build(player, leaves)`.

`etude train` calls training; `etude eval` reads its saved checkpoints and
calls play. Run eval again to skip delivered matches. Increase the seating's
`games` to add seed indices without replaying its prefix.

`track(config=config)` gets the run's identity and seed from Etude.
`run.resume(extra=Counts())` restores counters; `run.save()` keeps resumable
state; `run.checkpoint(path)` publishes the model for other runs. A save and a
checkpoint serve different readers. See [training](training.md).

Each eval match is one config, resolved players and one index. Its seed is
independent of the players, so opponents face the same draws. Your game splits
that seed into config, engine and player streams. The prototype's `load`
returns a deterministic list-in/list-out function; `build` supplies the
baseline. See [evaluation](evaluation.md) for wrappers and model libraries.

## Put the same file on one rental

First configure your own bucket and inject provider/bucket credentials through
your secret store. Substitute your bucket name below; the prototype already
caps each job at $0.10 and 0.01 hours. These commands can spend money.

```sh
uv run etude database upgrade
uv run etude storage add demo --bucket YOUR_BUCKET --prefix etude/demo --provider s3 --region us-west-2
uv run etude provider set runpod --kind runpod --storage demo
uv run etude ondemand add demo-cpu --provider runpod --vcpus 2 --memory-gb 4 \
    --max 1 --max-usd-per-hour 0.40
uv run etude launch examples/prototype.py --rent demo-cpu --max-usd 0.20 --quote
uv run etude launch examples/prototype.py --rent demo-cpu --max-usd 0.20
uv run etude wait
```

Launch reconciles the declarations with retained results: it starts training
when no checkpoint exists, then a later pass discovers the eval matches.
Keep using the same experiment file and storage:

```sh
uv run etude launch examples/prototype.py --rent demo-cpu --max-usd 0.20
uv run etude wait
uv run etude report examples/prototype.py --source s3://YOUR_BUCKET/etude/demo --open
```

A quote rents nothing. Dollar and time caps bound admission, not the provider's
final bill. [The launcher guide](launcher.md) explains storage, credentials,
owned machines and cancellation; [limits](limits.md) lists the sharp edges.

For a bounded wait from Python, call `etude.launch.client.wait(timeout=120)`;
the CLI `etude wait` waits until its selected runs finish.
