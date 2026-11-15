# Training

This guide is for authors adding their own learning loop, resumable state and checkpoints to an Etude experiment.

Start from the [runnable prototype](getting-started.md) or the
[PyTorch coin example](../examples/coin.py).

```python
def train(config: Config) -> None:
    with track(config=config) as run:
        model, optimizer = build(config, seed=run.seed)  # your code
        run.resume(model=model, optimizer=optimizer)
        for update in run.steps("updates", to=config.updates):
            run.log(one_update(model, optimizer))  # your code
            run.save()
            if update % 100 == 0:
                run.checkpoint(policy=model)
        run.checkpoint(policy=model)
```

Import `track` from `etude.train.tracking`. Your top-level `train` takes one
JSON-serializable Pydantic config. The command supplies identity and seed:

```sh
uv run etude train train.py
uv run etude train train.py --resume
uv run etude train train.py --experiment another-idea
```

## Saving and continuing

`resume` registers objects with `state_dict()` and `load_state_dict()` and
restores the last save. Call `save` inside each update; it writes when due
(default five minutes), at completion and before a planned stop.
`run.steps("updates", to=n, save_every=25)` chooses step cadence.
The run keeps absolute update coordinates after a restart.

Small extra state may be a mutable Pydantic model:
`counts = run.resume(model=model, extra=Counts())`; mutate `counts`, then save.
Register random streams, replay buffers and games in flight if resuming them
matters. Etude restores only what you register. Your code must remain able to
read its earlier saves; Etude cannot validate model compatibility.

A checkpoint is a published evaluation/input snapshot; a save is continuation
state. `run.checkpoint(policy=model)` writes a folder with `checkpoint.json`.
For your own format, write a unique path under `run.out`, then call
`run.checkpoint(path)`. Do not change a published checkpoint's bytes.

## Seeds, stages and inputs

Return `TrainingJob(name="base", train=train, config=Config(), seeds=(0, 1, 2),
max_hours=2)` from `training_jobs`. A run is one seed; `run.seed` is yours to
apply to the model and game libraries. [Launch](launcher.md) controls machines.

A later stage can take a checkpoint with `TrainingJob(..., start=checkpoint)`.
Read it from `run.input("start")`, initialize your model, then call
`run.resume(...)` so an existing continuation wins over initialization.
For dependencies, let `training_jobs(state: ExperimentState)` choose a stored
`run.final` from `state.runs`; keep returning the earlier jobs too.
To use another experiment, obtain its checkpoints through
`etude.experiment.observe(source, experiment)`.

Multiple policies in one checkpoint use `run.checkpoint(actor=actor, critic=critic)`.
Separate clocks distinguish learner transitions, optimizer updates and model
exposures; use `run.log(metrics, learner_transitions=n)` beside the update clock.
Do not label all of them steps.

[Tracking](tracking.md) owns metrics and reports; [evaluation](evaluation.md)
owns players. Maintainers can consult the [state and training reference](dev/training.md)
for serialization, advanced continuation and file layouts.
