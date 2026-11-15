# Your first experiment

This guide takes a prototype author from installation to a local training run, checkpoint evaluation and report, then shows the same experiment on rented compute.

## Run locally

From this repository, use Python 3.12 and uv:

```sh
uv sync
uv run etude train examples/prototype.py
uv run etude eval examples/prototype.py
uv run etude report examples/prototype.py --out reports
```

Open `reports/prototype.html`. No launcher or provider account is needed.
The example fits a scalar estimate in four updates, evaluates all four saved
checkpoints and charts their scores. It is deliberately tiny; it checks the
workflow rather than teaching a learning algorithm.

[`examples/prototype.py`](../examples/prototype.py) is the complete experiment
file. Copy it into your project as `prototype.py`, then install
`uv add 'etude-sdk[eval]'` and use `uv run etude train prototype.py`,
`uv run etude eval prototype.py`, and `uv run etude report prototype.py`.
The file supplies these functions:

| Function | Your responsibility |
| --- | --- |
| `train(config)` | Update the estimate; save resumable state and immutable checkpoints |
| `training_jobs()` | Name the training job, config and limits |
| `load(checkpoint)` | Return a deterministic batched forward |
| `play(match, agents)` | Use the seat forwards and return scored tables |
| `eval_jobs(state)` and `report()` | Choose checkpoints to evaluate and scores to draw |

Replace its scalar update with [your training loop](training.md), then replace
its score calculation with [your game and players](evaluation.md). Model-library
imports belong in a separate loading module once you add a real network.

## Carry on and inspect

```sh
uv run etude train examples/prototype.py --resume --until updates=6
uv run etude eval examples/prototype.py
uv run etude report examples/prototype.py --open
```

The resumed run extends the target to six updates; evaluation adds only the two
new checkpoints. Rerunning evaluation again plays no delivered matches. A completed
run refuses `--resume` unless you extend its target. Change the experiment name with `--experiment NAME`
to start a separate comparison. Config and seed cannot change inside a resumed run.
See [reports and storage](tracking.md) to compare experiments or read a bucket.

## Run on one rented machine at a time

First provide your RunPod and AWS credentials in the command environment, and
set `ETUDE_BUCKET` to an existing S3 bucket you control. Credentials and bucket
permissions are prerequisites; these commands do not create an account or bucket.
See [machines and budgets](launcher.md) for other providers and owned hosts.

```sh
uv run etude database upgrade
uv run etude storage add prototype --bucket "$ETUDE_BUCKET" --prefix etude/prototype --provider s3 --region us-west-2
uv run etude provider set runpod --kind runpod --storage prototype
uv run etude ondemand add prototype-gpu --provider runpod --gpu "NVIDIA L4" --vcpus 4 --memory-gb 16 --disk-gb 40 --max 1 --max-usd-per-hour 0.80
uv run etude launch examples/prototype.py --rent prototype-gpu --quote
uv run etude launch examples/prototype.py --rent prototype-gpu
uv run etude wait
uv run etude launch examples/prototype.py --rent prototype-gpu
uv run etude wait
uv run etude report examples/prototype.py --source "s3://$ETUDE_BUCKET/etude/prototype" --open
```

Install `uv add 'etude-sdk[eval,s3]'` in an independent project for bucket access.
The first launch asks for training; the second gathers the checkpoints now in
storage and asks for evals. The declaration allows at most $0.25 and 0.1 hours
per run; the on-demand allows one machine at a time. These may be separate
rentals. A GPU is unnecessary for this scalar example; it demonstrates the
same rented path used for larger models. No stock means queued work waits.
Keep the launcher's scheduler running as described in [the launcher guide](launcher.md).
Rental estimates exclude bucket charges and are not a provider billing guarantee.
