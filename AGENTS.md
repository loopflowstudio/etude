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

Test behaviour a user would care about, and assert on results. Mock side
effects (network, subprocesses, paid services) but never assert on how a mock
was called. Do not reshape production code for a test: no interface, factory
or extra parameter that exists only for test doubles. Delete a flaky test
instead of adding retries.

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

Commit messages say what changed and why in a sentence to a paragraph, not a
list of edits. Never add Co-Authored-By or other AI attribution footers.

Keep secrets out of code, output and artifacts. Prefer running a command with
the secret injected to reading the value. A raw value must never reach a
terminal, log or chat: consume it inline or redirect it. If one leaks, say so
and flag it for rotation without quoting it again.

Launcher records use frozen Pydantic models with named constructor fields and
model-owned JSON serialization. Keep `providers/runpod/http.py`,
`providers/runpod/pod.py`, `providers/package.py` and `environment.py`
standard-library-only: the RunPod guardian must start before
client dependency installation. The client and each job use separate environments.
`logging.py` is also in the standard-library bootstrap subset. Operational errors
use its sanitizer before retention; subprocess diagnostics omit argv and traceback
locals. Journal log reads return `LogStream`, preserving missing versus silent
output, and share the caller's monotonic deadline through `deadline.py`.
Captured console bytes pass through `Console` across reads before retention.
Only the owning producer closes a stream, after its captured children exit and
final console collection succeeds. Courier shutdown and final delivery share one
monotonic allowance capped by the provider deadline; uncertain exit or collection
leaves the stream incomplete while parent cleanup still runs.
An explicit empty worker cleanup command leaves deletion to its parent; omitted
cleanup selects the provider's command.

Check before pushing: `uv run pytest`, `uv run mypy`, `uv run ruff check .` and
`uv run ruff format --check .`. CI runs the tests with the outside network
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

**Layout.** Add a folder only when it has a rule a test can enforce, such as
"nothing in here imports the scheduler". A theme is not a reason for a folder.

**Records.** Anything stored or sent between machines is a frozen Pydantic
model. Do not keep a second kind of record beside them.

**Command line.** Use Typer. Every input is a `--flag`; do not add positional
conventions a person has to remember, such as a bare `--` or `name=value`.
Keep output plain, because agents and logs read it.

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
queries, and modules imported at start-up. Pin those numbers in tests. A change
to a path that reaches another machine comes with a counting test.

Launcher vocabulary: Machine owns acquisition and provider facts; Rental owns
only a paid window, keyed by its machine; RunAttempt is the allocation on any
machine. Session is internal: one start of Etude's process on a machine, and its
journal folder. The process itself is the worker (`etude worker`). Use rent and
terminate for making and deleting paid machines. A record refers to another by
its name or id as a plain `str`. While the launcher is a prototype its database
has no migrations: change the schema version and refuse older files.
