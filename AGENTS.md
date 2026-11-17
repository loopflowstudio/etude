# Etude

Conventions for people and coding agents working on this repository.

## Units

Use steps for scientific cadence and preserve absolute continuation coordinates.
Use hours and dollars for machine rental windows, credentials, watchdogs and budgets.
Distinguish training steps, learner transitions and optimizer updates/exposures.

## Design

Design around the domain's data structures and public APIs, with one code
concept per real concept. Keep core types and entry points easy to find. Start
with the smallest types and APIs that are right; every line must earn its place.

Treat the codebase as yours. Existing code is a draft, not a precedent:
when it is wrong for the change, change it instead of working around it.

Keep one way to do each thing. Before adding a mechanism, helper, record or
option, find where the codebase already does that job and extend or
replace it; never build a second one beside it. When a change reveals two
pieces doing one job, unify them in that change and delete the one that
loses. A new general utility starts with that search, and its commit
message names what was found and why it did not fit.

A redesign deletes what it replaces, in the same change. Keep one
implementation and one owner for each fact: no `_old`, `_new`, `v2_` or backup
variants, and no compatibility shims unless a released database or a published
API with outside users requires one. Git holds old versions.

When code fails because it assumed a fixed context, prefer adapting to the
context over adding a guard, allowlist or precondition that refuses.

Name things for what they are (`Checkpoint`, not `CheckpointHelper`). Action
functions start with a verb. Private helpers start with an underscore.

## Python

Use Python 3.12 and uv for every Python command (`uv run pytest`, never bare
`python` or `pip`), in docs and scripts as well.

Write fully typed Python: annotate parameters and returns on every function
and method, including private helpers and tests, with `-> None` where it
applies. Use 3.12 forms (`list[T]`, `T | None`). Represent structured domain
data with named types; do not pass untyped dictionaries or positional tuples
across application boundaries. Prefer precise unions and literals to `Any`. At
untyped library or JSON boundaries, validate once and expose a typed
interface; keep a necessary cast or suppression local, with its reason. Do not
weaken a type to silence the checker.

A nullable annotation means `None` is a real value; a default decides whether
an argument may be omitted. Return `None` for expected absence only where the
API defines it. Raise for violated invariants and impossible states, and never
disguise a failure as an empty result.

Put imports at the top and import explicitly from the defining module. There
are two exceptions, each with a comment saying why. An optional dependency is
imported where it is used. A command line or other top-level entry point
imports what one command needs inside that command, so starting the program
loads only what that command runs; keep these entry points as fast to start
as possible, and keep the rule everywhere below them. Declare dependencies in
`pyproject.toml`.

## Comments and docs

Give a substantial module a short opening note: its purpose, principal types
and entry points, and any non-obvious ownership or data flow. Explain intent,
invariants, units and algorithmic choices beside the code that depends on
them. Keep docstrings short; include non-obvious contracts, side effects and
failure behaviour. No `Args:`/`Returns:` sections that repeat a signature, no
comments that restate code, and no comments that narrate a change.

Wrong documentation is worse than none: update it with the behaviour or delete
it. In user-facing docs, lead with an example and keep prose terse around it;
tables are for lookup, not teaching.

## Tests

**NEVER intentionally induce load for testing**, including debugging,
benchmarks or demos, on local, CI or remote hosts. No CPU burners (`yes`),
busy loops, stress tools or artificial resource pressure. Use controlled
fixtures and injected measurements; see [TESTING.md](TESTING.md).

Test behaviour a user would care about, and assert on results. Mock side
effects (network, subprocesses, paid services) but never assert on how a mock
was called. Do not reshape production code for a test: no interface, factory
or extra parameter that exists only for test doubles. Delete a flaky test
instead of adding retries. Experiment fixtures whose functions are imported by
module name use distinct module names for distinct implementations: Python's
import cache outlives a test's temporary files.

## Development

Before calling work done, review it as a sceptical maintainer would: can it be
explained in one screen, does the API map to the real thing or to an accident
of implementation, what breaks at 2 a.m. and will the logs say why, is each
dependency and knob earning its keep, and would deleting code make it more
true. Fix what that finds or record it.

Make small coherent pull requests with useful descriptions and focused checks.
Linux account/permission tests use a disposable container’s native filesystem,
not macOS bind mounts (which can present different permission behavior). Keep
fixture inputs below its private root home, then copy journals out after execution.
Worker deadline tests resolve runner dependencies from the fixture image's local
wheelhouse with networking disabled; downloading packages must not consume the
short execution window used to test partial-output retention.
Server fixtures call the CLI entry point in process and pass its explicit
startup allowance: the installer must own its deadline and child cleanup, rather
than an outer test timeout killing the client while installation continues.
Scopes that change `TMPDIR` must restore `tempfile.tempdir` with the environment:
Python caches its first temporary directory, which may be deleted on scope exit.
Eval scratch and thread environments are configured only in dedicated child
processes; pass their paths explicitly, since concurrent evals share the parent
environment and tempfile cache.

Commit messages say what changed and why in a sentence to a paragraph, not a
list of edits. Never add Co-Authored-By or other AI attribution footers.

Keep secrets out of code, output and artifacts. Prefer running a command with
the secret injected to reading the value. A raw value must never reach a
terminal, log or chat: consume it inline or redirect it. If one leaks, say so
and flag it for rotation without quoting it again.

Launcher records use frozen Pydantic models with named constructor fields and
model-owned JSON serialization. For `Json[Model]` fields, pass serialized text
through `model_validate`: the typed constructor describes the decoded model,
while runtime validation expects JSON text. Keep `provisioner/providers/runpod/http.py`,
`provisioner/providers/runpod/pod.py`, `provisioner/providers/vast/http.py`,
`provisioner/providers/vast/instance.py`,
`provisioner/providers/watchdog.py`,
`provisioner/providers/http.py`, `provisioner/providers/bootstrap.py`,
`provisioner/providers/package.py` and `environment.py`
standard-library-only: each rental guardian must start before
client dependency installation. The client and each job use separate environments.
`logging.py` and top-level `storage_timeout.py` are also in the standard-library
bootstrap subset. New bootstrap imports must be present in both the guardian
archive (`provisioner/providers/package.py`) and SSH's pre-install loader (`ssh.py`).
Lambda Stack mounts `/run` with `noexec`. Invoke the host's termination script
there with `/bin/bash`, including from the systemd timer. Keep the timer
independent of the job environment and container.

Operational errors
use the shared sanitizer in `etude.errors` before retention; subprocess diagnostics omit argv and traceback
locals. Journal log reads return `LogStream`, preserving missing versus silent
output, and share the caller's monotonic deadline through `deadline.py`.
HTTP body reads may run in a deadline-bounded thread; emit transfer observations
on the invoking thread so database retention keeps its connection ownership.
Captured console bytes pass through `Console` across reads before retention.
Only the owning producer closes a stream, after its captured children exit and
final console collection succeeds. Courier shutdown and final delivery share one
monotonic allowance capped by the provider deadline; uncertain exit or collection
leaves the stream incomplete while parent cleanup still runs.
An explicit empty worker cleanup command leaves deletion to its parent; omitted
cleanup selects the provider's command.
Out membership (`out.json`) carries each key, SHA256 and byte length, published
after its files using the delivery fingerprints. Continuation reads membership
without out bytes; sources without membership are hashed. Recovery selects
members without imposing their fingerprints on newer mutable saves. Retained
checkpoint objects are not evidence that they still belong in `out`. Complete
host relays include `checkpoints/.stored`; single-file inputs do not. Only a
complete restoration reconciles stale out files.

State delivery freezes `out/state/**/current.json` before enumerating its
selected save. Recovery follows that pointer even when `out.json` is older.
A launched training process must not prune saves: its courier owns storage
acknowledgement and local retention; the scheduler removes remote saves
where upload-only grants cannot.

A declared run's address is always `EXPERIMENT/JOB@SEED`; a plain
`train(config)` file trained with `etude train` is `EXPERIMENT`, and stored
compact spellings stay readable, matched by their recorded job name and seed. Job code sees the local `ETUDE_RUN` name;
`ETUDE_RUN_ADDRESS` preserves its full storage address, including when a job
is named for its experiment. Discovery permits sibling runs below a compact
run address while excluding its `out`, `artifacts` and metric data. `Input.run` names the producer for relay; its unsigned `uri` locates the
actual bytes and is excluded from scientific job identity. `Storage.uri` uses
literal keys in `s3://` and `r2://` locators, including `%`, `?` and `#`;
file URIs use normal URL escaping. Fixtures that call `track` as a worker must
supply its assigned `ETUDE_RUN`, `ETUDE_ATTEMPT` and `ETUDE_OUT` environment.

Etude has one cache of storage reads, `etude/cache.py`, keyed by content
SHA256 and length with locators for where bytes came from. Tracking, reports and
the launcher all read an `Artifact` named by the object's unsigned URL (never a
signed link) through transports that perform I/O and keep nothing of their own; it and `deadline.py` sit at the top level because neither `etude.run`
nor `etude.launch` may import the other.

Check before pushing: `uv run pytest`, `uv run mypy`, `uv run ruff check .` and
`uv run ruff format --check .`. The formatter also checks Python code blocks in
Markdown, including release notes. CI runs the tests with the outside network
denied (`scripts/test_network.py`).

## Code style

Readability matters more now that code is cheap to write: optimise for the
person reading it.

**Names.** Prefer clarity over cleverness. Use one word per concept and use it
everywhere. Every word in a path or name should be different from its
neighbours and tell the reader something: `etude.launch.scheduler`, not
`etude.launch.launcher`. Avoid words that say nothing, such as engine, service,
contract, api, util or manager. Before choosing a name, an interface or a
layout, look at what established tools call the same thing and say what you
found.

**Imports.** Write every import as a full path from the module that defines the
name. Relative imports are a lint error. Put every import at the top of its
module: an import inside a function is a lint error except for the documented CLI and
optional-dependency exceptions above. If that makes two
modules import each other, move the shared part or pass it in; do not hide the
cycle inside a function. Keep every `__init__.py` empty, so importing one
module never loads the rest.

**Layout.** Lay the package out by the parts of the system: one folder per part the docs name, such as the controller, scheduler, provisioner and worker, called what the docs call it. Each part's import rule is enforced by a test. A grouping that is not a part of the system is not a folder.

**Records.** Anything stored or sent between machines is a frozen Pydantic
model. Do not keep a second kind of record beside them.

**Command line.** Use Typer. A command's one required input may be positional
(`etude storage add NAME`); the rest are `--flag`s. Do not add conventions a
person has to remember, such as a bare `--` or `name=value`. Keep output plain,
because agents and logs read it.

**Python is the interface.** Work is described in Python, not in a config
format. A command-line command is a thin way to call the same functions.

**Timeouts.** Customers own operation allowances. Default to `None` unless a
documented finite default is justified; every finite default must be overridable.
Children and retries share remaining time. No hidden per-call caps. Caller wait
expiry does not prove remote cancellation. Rental deadlines remain binding.

**Dependencies.** Manage them with uv. The launcher and tracking use one set of
versions for every dependency, from one lock file, whether they ship as one
package or several. Each must work without the other being used. Code Etude runs on someone else's
machine gets its own environment, separate from the job's. The one exception
is anything that must start before an install can finish, such as the process
that deletes a rented machine at its deadline: that stays standard library
only.

**Consequential actions are explicit.** Spending money, deleting something or
publishing something is never a side effect of a command that reads as doing
something else.

**What a command may wait on.** Every command is one of three kinds and says
which. A read answers from the database, at once, and says how fresh its answer
is. An ask records what is wanted and returns with an id. Only a wait may
depend on another machine, and accepts an explicit optional time limit. A command needs only what
its kind implies: a read must work with no provider key and no network.

**Measure by counting.** Machine load makes timings unreliable, so tests count
what matters to a user: calls to a provider, a bucket, SSH and git, database
queries, and modules imported at start-up. Pin those numbers in tests.
`tests.run.storage.MeasuredStorage` counts real storage reads, bytes and uploads
in bounded directory or mocked-bucket fixtures. A change to a path that reaches
another machine comes with a counting test.

Experiment declarations import `TrainingJob`, `EvalJob` and `ExperimentState`
from `etude.jobs`, where they are defined. `etude.experiment` reads state; the
worker startup path must not import either module. Resolve an authored callable's
source through `inspect.unwrap` before locating its file: a decorator is
implemented in its own library, while the wrapped function belongs to the
author's module.

Experiment files import sibling game and loading modules by their top-level
names; the declaration reader adds the file's directory to the import path.
Game functions are importable top-level functions, including fixtures: the game
pool imports their defining module and cannot inherit a parent closure.

Eval vocabulary: `load(checkpoint)` returns a deterministic callable from a list
of inputs to a list of outputs. `Agents` declares it and an optional game-side
`build(player, leaves)`, whose return value belongs to the author and is never
called by Etude. Sampling belongs in game code, seeded from `match.seed`. Player
wrappers are frozen author records; checkpoint leaves share loading and batches.
The shared agent process or each CPU game process holds checkpoint forwards;
game processes build wrappers and play; say neither "server", "worker" nor "policy"
for any of these ("policy" is reserved for policy keys).
Etude sets the model-library thread environment before importing the checkpoint
loading module. CPU game processes load through the same cache/eviction owner
as shared GPU inference and report failures to the parent even when play catches them. Loaders must honor that inference budget when configuring
their library; each game process keeps one thread.
On visible NVIDIA hardware, active games fill the declared batch, bounded by
pending matches and host memory. Probe hardware without importing the loader;
code-only games keep CPU capacity and perform no GPU probe. Hardware capacity
does not select the author's model device or promise GPU memory residency.

Launcher vocabulary: Machine owns acquisition and provider facts; Rental owns
only a paid window, keyed by its machine; RunAttempt is the allocation on any
machine. Session is internal: one start of Etude's process on a machine, and its
journal folder. The process itself is the worker (`etude worker`). Use rent and
terminate for making and deleting paid machines. A record refers to another by
its name or id as a plain `str`.

The launcher database changes only through a new SQL file in
`src/etude/launch/migrations/`, named for what it does and never numbered;
`docs/dev/launcher.md` ("Writing a migration") shows one. It may change until it merges to main; afterwards it never changes
(`scripts/check_migrations.py`). Explicit `database upgrade` advances the one
operational `launcher.sqlite`; ordinary opens never migrate or select copies.
Recovery snapshots are never scheduling inputs.
Unmerged SQL runs only in explicit empty development fixtures. Expand before
contracting; destructive changes export facts before commit and name their
preceding expansion. Release metadata records reader floors, and packaged
merged-frontier evidence authorizes operational upgrades before release.
Inserts name their columns. Old row shapes are rewritten by a migration, never
tolerated in Python, and no second schema definition sits beside the SQL
files. An older etude may keep writing after an additive release, so new
columns must read sensibly when empty; journal records a previous release's
worker writes must still decode, so new fields on them take defaults.

Supported Python modules declare a literal `__all__` tuple of locally defined
names. Keep the independent inventory in `tests/test_public.py` and authored
examples consistent; implementation examples in Markdown carry
`<!-- implementation-reference -->` immediately before their code block.
