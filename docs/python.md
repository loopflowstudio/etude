# Python imports

```python
from etude.experiment import observe
from etude.storage import default_root

state = observe(default_root(), "ppo")
for run in state.runs:
    print(run.name, len(run.checkpoints))
```

This reads the experiment's recorded state and prints each training run's name
and number of stored checkpoints. It does not launch work. Use the same storage
source your runs write to; `default_root()` selects the configured tracking
root. See [reading runs](tracking.md) and
[experiment declarations](launcher.md#one-pass-over-the-file) for using that
state to choose new work.

Etude requires Python 3.12. Install the library with `uv add etude-sdk`; add
`etude-sdk[eval]` for evaluation tables or `etude-sdk[s3]` for S3 and R2 reads.
Your training and model-loading libraries belong to your project's environment.

## Supported names

Import a name from the module that defines it. Each supported module declares
its supported names in a literal `__all__` tuple. A name absent from that tuple
is an implementation detail, even if an explicit Python import succeeds.
`__all__` identifies support; it does not create a facade or change when imports
run. Every package's `__init__.py` stays empty.

The tables below are a map of common imports, not a list of every supported
name. The module's `__all__` is the complete list; `tests/test_public.py` pins
those lists independently. Importing an implementation detail carries no
compatibility promise. Existing deprecated spellings retain their documented
removal dates; use the defining modules below for new code.

## Declare experiments and train

| Task | Defining module and names | Guide |
| --- | --- | --- |
| Declare training and evaluation jobs | `etude.jobs`: `TrainingJob`, `EvalJob`, `Agents`, `ExperimentState` | [Typed jobs](launcher.md#typed-jobs) |
| Observe an experiment | `etude.experiment`: `observe` | [Evaluation](evaluation.md) |
| Continue a launched run | `etude.jobs`: `continue_from` | [Launching from Python](launcher.md#launching-from-python) |
| Track training, saves and checkpoints | `etude.train.tracking`: `track`, `TrainingRun` | [Training](training.md) |
| Declare a clock measured in seconds | `etude.train.records`: `Time` | [Training](training.md) |
| Read training data | `etude.train.read`: `read`, `read_all`, `read_batches`, `RunData` | [Tracking](tracking.md) |
| Name checkpoints and policies | `etude.checkpoints`: `CheckpointRef`, `CheckpointRecord`, `PolicyKey` | [Evaluation](evaluation.md) |
| Describe shared stored values | `etude.records`: `Record`, `ArtifactRef`, `Clock`, `SourceKey` | [Tracking](tracking.md) |
| Select a tracking source | `etude.storage`: `default_root`, `storage_from_uri`, `Storage`, `DirectoryStorage`, `S3Storage` | [Tracking](tracking.md) |
| Handle a planned stop or an operational error | `etude.stopping`: `Stopped`; `etude.errors`: `EtudeError`, `LauncherError` | [Training](training.md) |

Count clocks are declared by use in `run.steps` or `run.log`. Keep scientific
coordinates in steps, learner transitions or optimizer updates, as appropriate
to the clock; continuation preserves absolute coordinates. Rental limits use
hours and dollars.

## Play games and report results

| Task | Defining module and names | Guide |
| --- | --- | --- |
| Type a deterministic batched checkpoint forward | `etude.eval.agents`: `Forward` | [Agents](evaluation.md#agents) |
| Describe players and composition | `etude.eval.records`: `CheckpointPlayer`, `Code`, `Held`, `compose` | [Evaluation](evaluation.md) |
| Declare tournaments and seatings | `etude.eval.records`: `Benchmark`, `RoundRobin`, `Seating`, `Seat`, `Earlier`, `Fraction`, `At`, `Final`, `Aligned` | [Evaluation](evaluation.md) |
| Type the game input | `etude.eval.records`: `Match` | [Evaluation](evaluation.md) |
| Describe a game's environment | `etude.eval.match`: `environment` | [Evaluation](evaluation.md) |
| Type the tables a game returns | `etude.eval.result`: `Tables`, `Row`, `Value` | [Evaluation](evaluation.md) |
| Play and summarize tournaments | `etude.eval.tournament`: `evaluating`, `scores`, `standings` | [Evaluation](evaluation.md) |
| Query stored tables | `etude.eval.query`: `statsdb`, `read_blobs`, `estimate` | [Evaluation queries](evaluation.md#querying) |
| Declare a report | `etude.report`: `Report`, `Score`, `Matches`, `Curve`, `Matrix`, `Ranking`, `Anchors`, `Planned`, `PlannedPoint`, `ML` | [Reports](tracking.md#reports) |

`Tables` belongs to `etude.eval.result`, including when annotating `play` in a
standalone game module. `Agents` belongs to `etude.jobs`; `Forward` belongs to
`etude.eval.agents`. Game code owns sampling and uses `match.seed` for randomness;
`load(checkpoint)` returns a deterministic list-in/list-out callable.

## Drive the launcher from a script

| Task | Defining module and names | Guide |
| --- | --- | --- |
| Submit, inspect and wait for runs | `etude.launch.client`: `queue`, `launch`, `quote`, `status`, `wait`, `logs`, `fetch`, `default` | [Launching from Python](launcher.md#launching-from-python) |
| Configure machines and storage | `etude.launch.records`: `MachineTemplate`, `OnDemand`, `Provider`, `Pool`, `Storage` | [The launcher](launcher.md) |
| Describe command jobs, limits and inputs | `etude.spec`: `Job`, `RunRequest`, `Until`, `Input`, `Variable`, `Runtime`, `Preparation`, `SmokeForm` | [Typed jobs](launcher.md#typed-jobs) |
| Inspect or explicitly upgrade a database | `etude.launch.database`: `Database`, `inspect`, `upgrade` | [Upgrading Etude](launcher.md#upgrading-etude) |
| Resolve and prepare a machine image | `etude.launch.provisioner.images`: `resolve_image`, `prepare_pytorch` | [Prepared images](launcher.md#prepare-a-framework-image-before-renting) |
| Read and price retained transfer evidence | `etude.launch.client`: `usage`; `etude.launch.usage`: `Usage`, `R2`, `S3_US_WEST_2`, `Access`, `reconcile` | [Transfer usage](launcher.md#transfer-usage-and-verified-retrieval) |

`etude.storage.Storage` is the tracking transport interface;
`etude.launch.records.Storage` is the launcher's named bucket configuration.
They are different concepts, so import the one your call expects.

Use the client for a script's launcher operations; it honors the selected server.
An explicit `Scheduler` owns a particular local database and is an implementation
entry point. See the launcher's [implementation reference](launcher.md#implementation-reference)
and [ownership rules](launcher.md#ownership-and-failure-behavior) when working on
Etude itself.
