# Unreleased

## Rename job-file declarations

Rename `def runs(...)` to `def training_runs(...)` in job and experiment files,
and update Python calls such as `capacity.training_runs(hidden=512)`. Keep the
same parameters, annotations and return values. For example:

```python
def training_runs(hidden: int = 256) -> Job:
    return training(train, config=TrainConfig(hidden=hidden), max_hours=2)
```

`etude launch`, `etude queue`, `etude train`, `etude smoke` and job-file help
use `training_runs`. An old-only declaration is refused with an error naming
the replacement; there is no warning alias. If both declarations exist, only
`training_runs` is used. A file defining only `train(config)` remains usable
for local training; a file also defining old `runs` must rename that declaration.

`Job.runs(...)`, `ExperimentState.runs` and the `etude runs` command keep their
names. Stored runs and their addresses need no conversion.
