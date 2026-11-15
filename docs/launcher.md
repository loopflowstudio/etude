# Machines and budgets

This guide is for people running an existing experiment on local, owned or rented machines and controlling its time and spending limits.

Write the experiment using [training](training.md) and [evaluation](evaluation.md),
or use the [first experiment](getting-started.md). This page controls where it runs.

## Local and owned machines

```sh
uv run etude database upgrade
uv run etude launch experiment.py
uv run etude status
uv run etude wait
uv run etude logs --run experiment/base@0
```

The explicit upgrade initializes or advances the launcher database. Ordinary
reads never migrate it. With no pool/rental selection, launch uses the configured
default, initially `local`. `launch` makes one pass over the declaration;
run it again as checkpoints arrive. It does not continuously reevaluate the file.

```sh
uv run etude pool add lab
uv run etude machine add bench me@bench.local --pool lab
uv run etude launch experiment.py --pool lab
```

Owned hosts need working key-based SSH, Python 3.12 and uv. Keep the scheduler
running: use `uv run etude server run` in a persistent terminal for local operation;
[server setup](dev/launcher.md#the-server) covers an always-on host and clients.
Use `uv run etude smoke experiment.py` before a costly launch; it runs the
job's declared small config locally, without renting.

## Rent with explicit limits

```sh
uv run etude storage add west --bucket "$ETUDE_BUCKET" --prefix etude/launch --provider s3 --region us-west-2
uv run etude provider set runpod --kind runpod --storage west
uv run etude ondemand add l4 --provider runpod --gpu "NVIDIA L4" --vcpus 6 --memory-gb 48 --disk-gb 40 --max 1 --max-usd-per-hour 0.80
uv run etude launch experiment.py --rent l4 --quote
uv run etude launch experiment.py --rent l4
```

Provide `RUNPOD_API_KEY` and storage credentials in the launcher's environment.
The bucket must already exist. Each declaration needs `max_hours` and, when
renting, `max_usd`. The on-demand also caps hourly price and simultaneous machines.
A quote reads availability; launching explicitly permits spending.
[Limits](limits.md) explains cost estimates and cleanup failure boundaries.

Vast uses `--kind vast`, `VAST_API_KEY` and a registered SSH public key.
Lium uses `--kind lium` and `LIUM_API_KEY`; provider-specific prerequisites,
images and billing differences are in the [provider reference](dev/launcher.md#rented-machines).
There is no automatic cross-provider fallback. No suitable stock leaves work queued.

## Inspect and stop

```sh
uv run etude runs
uv run etude status
uv run etude cancel experiment/base@0
uv run etude logs --run experiment/base@0
```

Read commands use retained launcher facts; their timestamps tell you freshness.
`wait` explicitly waits for work elsewhere.
A client timeout does not prove a remote process stopped; inspect retained state.
A failed run needs an explicit retry; completed declarations are left alone.
A retry resumes saved training state and delivered evaluation outcomes.

Compatible eval jobs share an allocation. Rental admission waits for estimated
work of `--min-eval-hours` (default 2), or for training to finish; unknown game
durations allow a first pass. [Evaluation](evaluation.md) owns compatibility
and inference tuning. Budgets are hours and dollars, never training steps.

For source capture, inputs, framework images, startup allowances, API commands,
and upgrade compatibility use the [launcher reference](dev/launcher.md).
Maintainers writing migrations start [here](dev/launcher.md#writing-a-migration).
