# Tracking and reports

This guide is for authors recording metrics, reading results, watching live runs
and choosing a report; [training](training.md) owns learning state and
checkpoints.

```python
from etude.train.tracking import track

with track(config=config) as run:
    run.log({"loss": 0.42, "entropy": 1.3}, updates=1, learner_transitions=128)
    run.log_table("episodes", [{"reward": 4.0}, {"reward": 7.0}])
```

Call track inside `train(config)`. Etude supplies experiment, job, seed and
output paths. A process without an Etude assignment is refused; standalone
tracking is not a supported entry point.

## Rows and clocks

Metrics go in the mapping; clock values are keyword arguments. `run.steps`
declares and fills its clock. New log keywords declare other count clocks.
Use `Time` from `etude.train.records` only for a clock measured in seconds.
Keep optimizer updates, learner transitions and training steps distinct.

Every row gives every used clock. Metrics can be sparse: absence is missing,
not zero or the previous value. `strict=True` turns validation warnings into
errors. Logging queues rows without network I/O; a slow writer can lose rows
when its bounded queue fills, reported by the run's incomplete counter.

`log_table` attaches a JSON row table to the current training point. It is for
collections such as episode outcomes; eval results belong to the separate
match table path in [evaluation](evaluation.md).

## Storage

Local results live below `~/.etude/track`; `ETUDE_HOME` relocates Etude's home.
Add destinations with
`storages=[storage_from_uri("s3://bucket/runs")]` (from `etude.storage`).
Install `etude-sdk[s3]` for S3 or R2. S3 uses normal AWS configuration; R2 uses
`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`. Inject secrets
from your secret store; never put them in a declaration or checkpoint.

Storage writes files, not an Etude-hosted database. A launched run's courier
carries those files to its bucket. Readers see only delivered results. Retain
local files when delivery fails, then retry:

```sh
uv run etude sync --directory /data/retained-runs --to s3://bucket/runs
uv run etude checkpoints --source s3://bucket/runs
```

Sync is repeatable and delivers retained files; it cannot recover a lost disk.
`run.finish(timeout=10)` allows final delivery ten seconds; an active network
request can outlive that wait. See [limits](limits.md) and the developer
[storage reference](dev/tracking.md#storage-outages-and-recovery).

## Reading

```python
from etude.storage import default_root
from etude.train.read import read_all

runs = read_all(default_root(), "prototype")
for run in runs:
    print(run.name, run.reached, len(run.checkpoints))
```

`read` and `read_all` read directories or buckets directly;
there is no launcher in this path. A later attempt replaces earlier metrics
from its first resumed coordinate, while old files remain inspectable.
`etude.experiment.observe(storage, experiment)` supplies the `ExperimentState`
used in declarations. [Evaluation](evaluation.md#querying) owns match SQL.

## Reports

```python
from etude.report import Curve, Report, Score, Series


def report() -> Report:
    return Report(
        score=Score(table="scores", value="won", reference=0.5),
        evals=(
            Curve(title="Win rate"),
            Curve(
                title="Actions taken", score=Score(table="actions", value="taken", per="offered")
            ),
        ),
        ml=(Series(title="Loss", keys=("loss",)),),
    )
```

```sh
uv run etude report examples/prototype.py --open
uv run etude report examples/prototype.py --source s3://bucket/runs
uv run etude report examples --open
```

`etude report` writes a self-contained, printable HTML page you can archive or
commit to git. For a folder, it writes one page per experiment file and an
`index.html` with their scores. `--watch SECONDS` rewrites on change.

Each build lists the source once. Training, tournaments and the evaluation database
share that listing across the reports in the build. For a large S3 or R2 source, Etude
remembers key boundaries in its local cache to divide later listings into
parallel requests across reports, live view refreshes and `statsdb` reads.
Added, replaced and deleted records show up on the next build.

The report joins training progress with eval results. `Score` names the table
and value; add `per="opportunities"` for a sum(value)/sum(per) rate.
Each curve can pin its own score. Curves without one use the report score.
Curves draw training progress; `Matrix(rows=..., columns=...)` and `Ranking(by=...)`
use your table columns. `Matches(tournament=..., seating=...)` selects a panel's
matches; `tag=` selects tagged seatings. Omit seating to show scores for any
player chosen in the report. A composed player remains a whole player, even if
its leaves share models with another composition.

Use `perf` and `ml` to choose metric panels for your own learning system.
`Report()` defaults assume RL metric names; absent data leaves gaps, not invented
measurements. A checkpoint with no training record can still be an anchor.
See the [report declarations](../src/etude/report.py) for the supported panel
fields and [record semantics](dev/tracking.md) when developing Etude itself.

## Live view

Leave the view running in one terminal.

```sh
uv run etude view examples/prototype.py
```

In a second terminal, run `uv run etude train examples/prototype.py`. Open the
link it prints, `http://127.0.0.1:8765/runs/prototype/coin@0`, to see the
coin's training curve, config and checkpoint.

`etude view [PATH] [--source SOURCE] [--host HOST] [--port PORT]` serves a
read-only view. `PATH` is a Python file or folder containing the experiment's
`report()` declaration. Omit it to use the default report. `--source` chooses
where to read run files from. It defaults to this computer's runs directory
and accepts a directory (as a path or the `file://` address Etude prints),
`s3://bucket/prefix` or `r2://bucket/prefix`.
Bucket sources need read credentials. Viewing requires no machine-provider key.
The view listens on `127.0.0.1:8765` by default and prints its address.

To browse a bucket’s history, open the experiment list. The view lists the
source and reads run declarations, outcomes and any writer records it needs
to identify a resume. Experiments and run tables appear before charts load.
Open runs chart in the background. A directory also charts its completed
runs, one experiment at a time.

In S3 or R2, opening a historical run or selecting its **Curves** or
**Checkpoints** tab loads that run’s metrics and checkpoint records. You can
read its state, config and file names while it loads. The view remembers
which runs you opened across restarts. Opening an experiment’s **Report**
reads the data for that report separately. Passing an experiment file
selects its report code and still discovers runs across the source.
The [hosted view](#hosted-view) receives updates directly from launched workers.

A local view advertises its address in `ETUDE_HOME/view`. While it runs,
`train`, `launch`, `queue`, `continue` and `status RUN` print a `view:` link.
Launchers configured for a hosted view print its address. A declared run uses
`EXPERIMENT/JOB@SEED`, such as `prototype/coin@0`. For a file defining only
`train(config)`, the address is the experiment name. `train --json` prints the
link on stderr. Starting a second local view redirects links to it. When it
closes, a surviving view advertises again at its next index pass. A link
opened before the run appears names the source the view is watching.

| Path | What you see |
| --- | --- |
| `/` | Experiment cards, Open before Completed, with live curves, run states, spend and latest file write |
| `/status` | The experiment list used to refresh `/` |
| `/experiments/NAME` | Run table and selected run detail on Status, with a separate Report tab |
| `/experiments/NAME/status` | The table and URL-selected detail used to refresh Status |
| `/experiments/NAME/report` | The same report pane, opened on its own |
| `/experiments/NAME/report?observe=1` | Whether the waiting report can load |
| `/runs/ADDRESS` | Training Overview, or an evaluation’s status and link to its report |
| `/runs/ADDRESS?tab=charts` | All training charts. The URL also selects Console, Config, Checkpoints or Files. |
| `/runs/ADDRESS?observe=1` | Current source ages and the version of the run shown in the tab |
| `/machines` | Held machines with their pool or on-demand, readiness, window and cost. Each occupied slot links to its run and shows its state and clock. |
| `/machines/status` | The inventory used to refresh the Machines page |
| `/reports` | Experiment states and the reports built in this view |
| `/reports/status` | The rows used to refresh the reports index |

Open **Reports** and select an experiment to see its report. An experiment
with runs is **Open** while any run is active, then **Completed**. A
declaration with no runs reads **Waiting** until the first successful file
listing, then **Not started**. **Never** means this view has yet to build it.
After a successful build, the row shows its UTC build time, the declaration
file’s Git revision (**Working tree** for a local version, **Default** for
the built-in report), and **Runs drawn** (a training run count or **Eval only**).
**Headline** shows the primary evaluation curve’s score at each job’s or
external line’s latest evaluated checkpoint. Before there is a score it uses
the first drawable ML series, including ungrouped metrics. Different latest
values appear as a low–high range. These build details stay with the
served report until a successful refresh. If a run file listing or launcher
read fails, Reports shows the error above the last known rows. In local mode,
restarting the view clears the build details.

The breadcrumb above an experiment, run or report links back to Experiments.
Use the side rail to switch pages, open an active experiment or jump to a
held machine’s row. Idle machines are marked. The hourly total counts each
held rental once, including idle rentals. The loop age dates the last
scheduler heartbeat. A local database without a heartbeat shows **No server
loop**, as when using `etude wait`. A file-only view shows **No launcher**.
With a selected server, **On user@host** links to Machines. The rail shows
**Awaiting launcher** or **Awaiting server** before an observation. A failed
read shows **Launcher unavailable** or **Server unavailable** until the next
successful read. Previously observed held machines and the last heartbeat
age stay visible during the outage.
The rail updates with the visible page, including
beside a report you have already built. Below 700 px it becomes a scrollable
top row. Printing omits it.

**Experiments.** Each card at `/` shows the first drawable ML series in report order.
An authored series keeps its title and metric keys. When no authored series
has a drawable value, each run contributes its first drawable ungrouped metric
in logging order. Its key titles the curve and labels the value in the run
table’s **Latest metric** column. The curve draws bucket means. **Latest
metric** shows the last finite logged endpoint on the report clock. If a
reduced bucket ends in a nonfinite value, the display uses its finite mean.
A log panel uses positive values. Counters and diagnostics can lead when there
is no drawable series. Metric curves use the report clock. When a bucket run
finishes with new files before anyone has viewed it, opening the run loads
its final curve. Hover over a curve to read its value.
A panel without a curve leaves the run summary on its own. Each dot links to
a run and names its state. Dollars sum the launcher’s recorded run costs,
with `~` when any share is estimated.

Open `/experiments/prototype?run=coin@0&tab=curves` to see the coin’s curves
beside the run table. Each row shows the run and job, state or queue reason,
clock, last file write, **Latest metric** from training, machine and dollars. A dash
means the index has no value for that cell. Select a run to inspect its
**Console**, **Curves**, **Config** or **Checkpoints**. Picking another run
keeps the same tab. Copy the URL to share that selection, or open the run’s
own page from the detail heading.

Click a command to copy it for your terminal. The view supplies launch, eval
and report commands when it knows the experiment file. The cancel command
names the unfinished runs on the selected launcher.

Open **Report** to load the experiment report in a separate frame.
Before the first run files arrive, the pane shows **Waiting for run files**. Once the
view indexes its first training or evaluation run, the visible tab builds the
report. If the build fails, the pane shows the reason and offers **Retry report**.
After a successful build, use **Refresh report** to see newer data. Status
refreshes keep your place in the report. An experiment stays open
while it has unfinished launcher work or an active file-only run. A file-only
run reads **no end recorded** and stops keeping its experiment open when the
view confirms 10 minutes without a write and finds no outcome. It measures
that interval from the last write to the start of the listing that supplied
the run’s file facts. During a storage outage, the run keeps its last
observed file state.

**Runs.** Open `/runs/prototype/coin@0` for **Overview**. It shows the
clock, **Latest metric**, mean pace, dollars spent, occupied machine’s
rental rate and last file write. The first two report panels with data sit
beside the console tail and saved checkpoints. A dashed line marks the
latest checkpoint when the curve’s clock places it. The default group of
ungrouped metrics previews its first drawable metric here. Charts shows the whole group.
Opening Overview keeps the run URL clean. Changing a clock or smoothing adds
just that choice to the link and preserves it across refreshes.

Mean pace divides the change in the displayed clock between the first and
latest sealed training rows by their elapsed seconds. Both rows must give
that clock. For a running job with a target and positive pace, **Ends about**
adds the remaining clock distance divided by that pace to the latest row’s
time. The estimate is shown in UTC.

Open **Files** for the indexed storage keys belonging to the run, including
open metric blocks. Copy a tab’s URL to share that view. **Charts** opens with the recorded series and counters from `report()`,
in `ml` order, then `perf` order. Remaining metrics follow in first-logged
order. The report supplies the default clock, units and scales. Performance
appears when a diagnostic has data, such as a timer breakdown or a measured
writer span. Checkpoint pace starts with the first measured interval on a
step clock. A writer restart leaves a gap. Checkpoints show their saved
clock positions. For a launched run, the page also shows placement,
hardware, cost, attempts and source revision when available. All four pages
show the launcher’s clock until run files appear, then the sealed file position.
A replacement allocation shows **setting up** beside the previous curve and
checkpoints. Its writer can confirm **running** before the next launcher
poll. An explicit resume shows **queued** as soon as the launcher reopens
the run. A recorded result for the current allocation can appear while its
machine slot is still occupied.

```sh
uv run etude continue --run paced/talk@0 --until updates=200
```

When the view runs on this computer, a local continuation prints the child's
live link as soon as it queues. With a selected launcher, run
`etude status CHILD` on this computer for the link. The child page and its experiment row
show **Continued from paced/talk@0 at updates 150** when 150 was the inherited
position. Follow the parent link to see its curve and checkpoints. The child
shows its own target of 200 and adds its own sealed points as they arrive.
Both runs belong to one learner line from submission. A copy containing only
the child still shows its recorded parent and starting position.

`last file write` dates the run files. On a run page, `files observed` dates
the listing that supplied that run’s file facts. On the overview, experiment
and machine pages, `Run files listed` dates the most recent successful listing.
If a writer replaces a listed metric block before the view reads it, that
run keeps its previous chart, file state and observation date while other runs
advance. Its experiment row shows the older observation beside the last write.
A recorded scheduler heartbeat appears as `last loop`. When the file and
launcher observations differ by more than 5 seconds, `launcher observed` dates the launcher facts.

Open the evaluation link printed by the launcher to see its match count and
retained console. A launch that plays several tournaments occupies one row
on the experiment page. Its last file write includes all its tournaments.
The experiment report shows their scores separately. After copying a new
launch with `etude sync`, the same run path shows its recorded outcome from
the copied files alone.

Opening Overview or Console for a launched training run requests the retained
console available through
`etude logs --run RUN`. The first visit can show **Waiting for retained console**
until the next launcher poll. It opens at the end of the last 500 lines of
retained output, dates the latest collection and marks gaps. Plain
`etude train` writes its console to the terminal.

**Refresh.** Every 5 seconds, a visible tab checks the view’s index. A finished
run can still receive its final metrics, and an idle overview can show a newly
queued job. When only dates change, the run page updates them in place. When
run details or chart inputs change, the page reloads with its chart choices
in the URL. The experiment report stays mounted while status updates. A failed
check keeps the current page visible and retries on the next check.
The view indexes run files in the background under `ETUDE_HOME/views`. Reading
a run graph uses the last published index, including during a bucket scan.
While storage is slow or unavailable, new queue entries, placement, retained console
and local machine inventory still appear. Charts keep their last indexed position,
including after a view restart. Before the first successful file pass, the view shows
known launcher runs with **File observation unavailable**. It shows any config
the launcher supplied. Missing config, curves and checkpoints wait for the
files. A declared experiment with no known runs says **Waiting for the first
file observation**. A failed **Refresh report** leaves the last report visible.
When storage recovers, the clock and curves advance together. The files in
your directory or bucket are the portable record.

You can edit `report()` while runs are writing. Their clocks and curves keep
advancing while the view reads the edit. If it fails, the affected experiment
and run pages show the error. Run charts keep the last working panels,
including after a restart, and use default panels until the first successful
declaration. A later index pass publishes the repaired panels.

Without a launcher database the view reads runs from files, and Machines says
“No launcher is used here.” With `etude server set`, the view polls the selected
server for run status and requested consoles at most once every 30 seconds.
While a reply is pending or a read fails, the view joins fresh run files to
the last successful observation from that launcher, including after a restart.
A failed read shows its reason. Selecting another launcher leaves your indexed
runs and charts visible. The view clears the previous launcher’s placement,
queued jobs and console. It keeps showing your files while it waits for the
selected launcher.
Use `--source` to select the matching run files. The Machines page names the
selected server and directs you to `etude machines`.

### Hosted view

```text
# On the view host (install etude-sdk[hosted], plus eval for evaluation results)
$ export ETUDE_VIEW_DATABASE_URL=postgresql://user@host/etude_view
$ export ETUDE_VIEW_SECRET=...
$ uv run --extra hosted etude view-upgrade
$ uv run --extra hosted etude view examples/prototype.py --push --host 127.0.0.1 --port 8765
http://127.0.0.1:8765/

# In the submitting terminal or SSH `etude server` environment
$ export ETUDE_VIEW_URL=http://127.0.0.1:8765
$ export ETUDE_VIEW_SECRET=...
$ uv run etude launch examples/prototype.py --pool lab --job coin --seed 0
...
view: http://127.0.0.1:8765/runs/prototype/coin@0
```

This example uses a worker on the view host. For remote workers, set
`ETUDE_VIEW_URL` to the reachable HTTPS origin. Supply the same
`ETUDE_VIEW_SECRET` to the launcher and the view from your secret store.
The launcher requires the URL and secret together. It gives each worker a
private token scoped to its run and attempt. Workers refuse HTTP redirects.

Run `etude view-upgrade` before starting a release. The server checks the
schema revision at startup and serves health checks at `/healthz`. Both
commands accept `--database-url` in place of `ETUDE_VIEW_DATABASE_URL`.
Only the view host needs the hosted extra. Install the eval extra there
to receive Parquet tables and build evaluation reports.

Plain `etude view [PATH] [--source SOURCE]` reads its local index even with a
database URL in the environment. Use `--push` to receive observations.
The two modes have separate data sources. `--push` with `--source` and
`--database-url` without `--push` are errors. A push view leaves the local
address in `ETUDE_HOME/view` available.

Open the link printed by `launch`, `queue`, `continue` or `status RUN`.
The selected launcher supplies the hosted origin, including over SSH.
Without a configured origin, the command uses the invoking computer’s local
view. `quote` prints no link. Plain `etude train` publishes only to its
configured tracking destinations.

| Surface | Evidence at the shared URL |
| --- | --- |
| Run | Every tab shows the latest attempt’s state, machine and reported position. Training Overview shows sealed curves, config, checkpoints and the last 500 console lines. Evaluation runs link to their experiment report and show Console and Files. |
| Experiments | Each card shows its first drawable metric panel. The table shows the latest values and the selected run’s details. Visible pages refresh every 5 seconds. |
| Report | The first visit builds from Postgres. **Refresh report** picks up new results and edits to the declaration on the view host. The last successful build survives declaration errors and web restarts. |
| Files | Run and outcome records, metric blocks, checkpoint records and JSON row tables. Eval groups include tournament and environment records. A sealed eval block appears once its index and every referenced Parquet table have arrived. |
| Machines and Dollars | The launcher holds inventory and spending. The hosted view shows the assigned machine and reported hardware for each attempt. |

The run page waits for the first push. The active attempt’s stored outcome
can close the run if its final status update is lost. A replacement attempt
reopens it. After 10 minutes of silence an unfinished attempt shows **no end recorded**.

Each pushed file can be up to 32 MiB. Weights, job outputs and packed match
blobs stay in canonical storage. During a view outage, training and bucket
delivery continue. A live or resumed worker catches up from its local files,
including compacted blocks. If the machine is lost, a gap can persist.
Projection errors leave the last complete chart visible with the error.

The view reads all run data from Postgres. Ingest requires an attempt token.
The UI has no login. Use authentication or a private network when exposing it
beyond loopback.
