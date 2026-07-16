# W2-205 — Prove one-command clean-machine launch and offline reload

## Directive and scope

Build the bounded clean-machine follow-on to merged W2-185 in this Task's one
worktree and one serial PR. Preserve the W2-185 manifest, pack treatments,
rights notice, deterministic fallback, deck orientation, and existing
network-denied full-game/replay proof. This Task owns installation and launch
readiness around that pack; it does not redesign the pack or the authority
protocol.

The canonical supported command will be:

```bash
./scripts/play
```

It is a small executable wrapper around the uv-managed Python launcher. Any
Python it invokes, including native build and verification helpers, runs
through `uv`. The existing `uv run scripts/play.py` entry remains compatible
for already-prepared development environments, but only `./scripts/play` is
the clean-machine-certified path because the wrapper can diagnose a missing
`uv` before Python exists.

For this Task, “clean machine” is a provisioned reference host plus a fresh
checkout, not a bare operating-system installation. Host provisioning and the
clone are outside the clock. All project installation is inside it; the Task
must not obtain an easier result by pre-installing `.venv`, the native module,
or `frontend/node_modules`.

## User-visible outcome

On the documented reference profile, a developer starts from a fresh checkout
and runs `./scripts/play`. In less than 60 seconds the command installs the
locked minimal play-runtime dependencies, builds the CPython 3.12 `managym` extension,
installs the locked frontend dependencies, starts both local processes, and
reports one ready URL. Opening that URL and selecting **New Game** reaches the
default **UR Lessons versus GW Allies, Search 64** match with an enabled legal
action and only pack-backed visible card treatments.

The launcher reports a stable, machine-readable ready record containing the
URL, elapsed time, Python/Node/native ABI facts, and the installed pack's
`{id, version, manifest_sha256}`. A failed prerequisite, install, pack
preflight, port bind, or child startup produces one stable diagnostic code,
one actionable message, and a nonzero exit instead of a child traceback or a
launcher that waits forever.

After that first installation, public browser networking can be denied and a
reload resumes the same live session, board, prompt, and pinned matchup. The
reloaded page still exposes a legal action, every visible curated identity uses
the installed treatment, and a post-reload action advances authority. Local
HTTP and WebSocket traffic remain allowed; “offline” does not mean killing the
local authority process.

## Clean-machine profile and measurement boundary

The authoritative profile document added by pursue will name one reproducible
Linux profile: a fresh `ubuntu-24.04` x86_64 GitHub-hosted runner image, with
its observed CPU/memory and exact tool versions captured in the proof receipt.
The provisioned host surface is:

- POSIX shell and standard checkout tools;
- `uv` with CPython 3.12 available;
- a Node release accepted by the locked Vite dependency (pin Node 22 in the
  proof job) and its matching npm;
- the pinned/stated stable Rust toolchain plus Cargo and native build tools;
- the Chromium revision used by the locked Playwright version.

The proof begins only if the checkout has none of `.venv`,
`frontend/node_modules`, `managym/_managym*`, `managym/target`, GUI traces, or a
prior proof browser profile. The CI job must not restore uv, npm, Cargo, target,
or project-install caches. Stock files already present in the hosted runner
image are part of the named host profile and are recorded rather than silently
described as absent.

The 60-second clock starts immediately before spawning `./scripts/play` and
ends only when a fresh browser context has all of the following at once:

1. the WebSocket badge is connected;
2. the default opponent is Search 64;
3. the rendered matchup is `UR Lessons vs GW Allies`;
4. the board has at least one visible pack-backed treatment and no fallback;
5. at least one enabled authoritative legal action is visible.

This clock includes uv's locked play-runtime sync, the release native build,
`npm ci`, backend/frontend startup, browser navigation, and default match
creation. It excludes runner provisioning, repository checkout, and browser
binary provisioning. Record wall-clock milliseconds from outside the launch
process so uv startup and dependency work cannot disappear from the number.
The maximum, not a rounded or percentile value, must be below 60,000 ms on the
reference proof run.

Network access is allowed while the command installs locked dependencies.
Once installation and local readiness complete, the browser proof denies all
non-loopback `http`, `https`, `ws`, and `wss` requests before its first
navigation and keeps that denial through initial play, reload, and the
post-reload action. Thus the runtime half is stronger than the requested
offline reload without pretending first installation can occur without
package registries.

## Source of truth

- The committed W2-185 manifest at
  `frontend/src/lib/packs/tla-ur-lessons-vs-gw-allies/v1/manifest.json` remains
  authoritative for exact decks, reachable identities, treatments, fallback,
  pack ID/version, and the bytes whose SHA-256 is reported. Its sibling
  `NOTICE.md` remains the installed rights/provenance notice.
- `uv.lock`, `managym/Cargo.lock`, and `frontend/package-lock.json` are the
  installation authorities. The launcher must use their locked modes and must
  not replace `npm ci` with resolving `npm install`.
- `./scripts/play` is the host-prerequisite and canonical-command boundary;
  `scripts/play.py` is the executable install/preflight/process-lifecycle
  authority. Documentation derives commands and diagnostics from these paths.
- `docs/clean-machine-play.md` defines the reference host, clean-state
  predicate, timing boundary, offline boundary, diagnostic table, and manual
  reproduction instructions. It must not claim other platforms or a bare-OS
  install are certified.
- The clean-machine browser proof emits a versioned JSON receipt as a test/CI
  artifact. It records clean-state assertions, exact host/tool/browser facts,
  launch argv, elapsed milliseconds, pack reference, opponent/deck names,
  before/after session and board signatures, public-request ledger, and result.
  It is evidence derived from the sources above, not a second pack or version
  source of truth.

No new hard-coded deck list, manifest hash, or treatment inventory is allowed
in the launcher or proof. They load and assert the installed manifest.

## Smallest build

Keep the implementation in one focused PR, ordered as follows.

1. Add the `./scripts/play` wrapper. Before invoking Python it checks for `uv`,
   Node/npm, Rust/Cargo, the three lockfiles, and the pack directory, emitting
   the stable diagnostic envelope on failure. It then runs the launcher through
   uv with explicit Python 3.12 and locked dependency resolution. Add a
   repository Python pin so an unqualified uv operation cannot silently choose
   CPython 3.14, as the current fresh-worktree probe did.
2. Make `scripts/play.py` an idempotent clean launcher. Strictly validate the
   manifest and notice before starting children; run the pinned maturin build
   when the CPython-3.12 native module is absent or cannot import; use `npm ci`
   when frontend dependencies are absent or inconsistent with the lock; then
   start uvicorn and Vite. Native build and frontend install may run in
   parallel after the Python environment exists. Do not download card assets.
3. Replace the fixed sleep with bounded child-liveness and loopback-readiness
   probes. Do not print ready until both services answer. Preserve coordinated
   Ctrl-C/SIGTERM shutdown, propagate install/child failures, detect occupied
   ports before waiting, and support E2E-selected backend/frontend ports.
4. Emit one parseable `MANABOT_PLAY_READY` JSON line on success and one
   `MANABOT_PLAY_ERROR` JSON line on controlled failure. Human-readable context
   may follow, but tests key on schema version and diagnostic code. Keep normal
   child logs available for diagnosis without making their wording the API.
5. Add focused Python contract tests for preflight, version checks, missing and
   malformed pack/notice, failed native import/build, stale/missing frontend
   install, occupied ports, readiness timeout, child exit, ready-record shape,
   and cleanup. Test failure seams through temporary paths and injected command
   runners; never damage the real installed pack or toolchain.
6. Add a short clean-machine Playwright scenario, complementary to W2-185's
   full-game/replay spec. Configure it to reuse the single canonical launcher,
   use a fresh browser context, and inspect the actual WebSocket pack reference
   rather than duplicating the hash in TypeScript.
7. Add the clean-checkout verification wrapper/job and documentation. The job
   provisions only the named host tools/browser, asserts the clean predicate,
   invokes `./scripts/play` once, measures it externally, runs the offline
   browser flow against those same processes, uploads the JSON receipt, and
   always tears the process group down. Add this as a required CI signal rather
   than testing `scripts/play.py` through separate Playwright web servers.

The stable diagnostic codes are:

| Code | Meaning |
| --- | --- |
| `prerequisite.uv` | `uv` is unavailable, so no Python command can run |
| `prerequisite.node` / `prerequisite.npm` | frontend host runtime is missing or unsupported |
| `prerequisite.rust` / `prerequisite.cargo` | a clean native build cannot run |
| `lock.python` / `lock.cargo` / `lock.frontend` | required lockfile is missing or rejected |
| `python.version` | the launcher is not running CPython 3.12 |
| `pack.missing` / `pack.invalid` / `pack.notice` | the installed v1 pack cannot satisfy its strict contract |
| `native.build` / `native.import` | the release extension could not be built or imported for CPython 3.12 |
| `frontend.install` | locked frontend installation failed |
| `port.in_use` | requested backend or frontend port is unavailable |
| `backend.start` / `frontend.start` | a child exited before readiness |
| `ready.timeout` | the bounded readiness window expired |

Errors may contain a nested command exit status and concise detail, but must
not contain credentials or claim a missing curated identity can be repaired by
fetching it at runtime.

## End-to-end proof

The clean proof crosses every relevant boundary in one scenario:

1. On the provisioned reference runner, assert the fresh-checkout predicate and
   start the wall clock.
2. Invoke only `./scripts/play`. Wait for its parseable ready record while also
   failing immediately if the process exits.
3. In a fresh Chromium context, deny every public request, navigate to the
   ready URL, leave the default deck and opponent selections untouched, and
   select **New Game**.
4. Before 60,000 ms total, require connected WebSocket state, Search 64, exact
   `UR Lessons vs GW Allies` names, an enabled legal action, an observation
   whose `asset_pack` equals the manifest-derived ID/version/hash, at least one
   pack treatment, and zero fallback treatments.
5. Take one deterministic legal action (play a land first, otherwise the first
   legal pass), wait for authority sequence advancement, and capture the
   session credentials plus a canonical visible board/prompt/action signature.
6. Reload while public networking remains denied. Require the same credentials,
   session, pack reference, deck names, board/prompt/action signature, and
   pack-only treatments. Take another legal action and require a further
   authority sequence advancement.
7. Assert the public-request ledger, page-error ledger, and unexpected console
   error ledger are empty; write the complete JSON receipt; terminate the one
   launcher and prove both children exit.

The clean-profile proof command will be:

```bash
./scripts/verify-clean-machine
```

It is an orchestration receipt for CI and reviewers; the user-facing launch
command exercised inside it remains exactly `./scripts/play`.

Focused verification for the PR is:

```bash
uv run --python 3.12 --extra dev pytest tests/gui/test_play_launcher.py tests/gui/test_curated_pack.py
npm --prefix frontend test
npm --prefix frontend run check
npm --prefix frontend run build
./scripts/verify-clean-machine
```

The clean proof must run from its fresh checkout job. A developer worktree that
already contains install artifacts is useful for focused tests but cannot
produce the clean-machine receipt.

## Affected surfaces and consumers

- **Operator CLI:** `./scripts/play` becomes the clean-machine-certified entry;
  `scripts/play.py` retains current ports and `--no-frontend` compatibility and
  gains deterministic preflight/readiness behavior.
- **Python/runtime installation:** explicit CPython 3.12 selection, uv locked
  dependencies, and the local PyO3 extension. Training, simulation, and
  checkpoint commands remain unchanged.
- **Frontend installation/server:** `frontend/package-lock.json` through
  `npm ci`, Vite local serving, and selectable E2E ports. No production host or
  service worker is introduced.
- **Installed pack:** existing strict Python and TypeScript loaders, manifest,
  notice, and exact hash. Pack contents remain unchanged unless a real W2-185
  defect is discovered; any such scope change must be called out rather than
  hidden in launcher work.
- **Wire/client:** existing `new_game`, observation, resume, and `asset_pack`
  fields are consumed unchanged. No new gameplay DTO or client-side rules are
  needed.
- **Browser recovery:** sessionStorage resume credentials, WebSocket reconnect,
  game store, board/action DOM, and shared treatment resolver are exercised but
  retain their current semantics.
- **Automation:** launcher unit tests, the focused clean-machine E2E spec,
  Playwright reuse-existing-server configuration, and one clean-checkout CI
  job/receipt.
- **Documentation:** `docs/clean-machine-play.md`, the root play/install path,
  and repository operator instructions. Every Python command shown or added
  uses `uv`.

Existing named/custom deck inputs, legacy traces without `asset_pack`, replay,
training, and protocol-v1 consumers remain compatible.

## Absent and error states

- Missing host tools fail before project installation with the exact
  prerequisite code and a documented host-provisioning remedy. The launcher
  does not install OS packages or curl remote installers.
- Missing project dependencies on the first online run are installed only from
  lock-governed commands. Resolver/build failure is fatal and attributed to
  its boundary; the launcher never starts a partially ready table.
- A missing/unreadable manifest, malformed JSON, incomplete inventory or
  treatment, remote treatment URL, unknown fallback, missing rights data, or
  missing notice fails pack preflight before either server starts. The normal
  W2-185 fallback remains valid for an out-of-pack card in a legacy/custom game;
  it is not permission to accept a damaged curated pack.
- A native extension built for the wrong Python ABI is treated as absent,
  rebuilt for 3.12, and import-checked. If it still cannot import, fail with
  `native.import`; do not fall back to another Python.
- Missing/stale frontend dependencies use `npm ci` while installation networking
  is available. After installation, an offline run with damaged dependencies
  fails deterministically instead of attempting a public fetch or serving an
  incomplete UI.
- If either configured port is occupied, name the port and exit before spawning
  the other child. If either child dies or readiness times out, terminate the
  complete process group and return nonzero.
- No legal action, wrong default opponent/decks, fallback treatment for a
  curated visible identity, pack-reference mismatch, changed session or board
  after reload, post-reload action failure, or any public request invalidates
  the end-to-end proof.
- Empty localStorage is the required initial state. Unavailable storage retains
  the existing deterministic defaults but cannot satisfy the same-session
  reload proof and therefore fails this scenario rather than being reported as
  success.

## Operational boundary

- Reference proof: one clean checkout, one launcher, one backend, one Vite
  process, one fresh Chromium context, one live game, and the exact oriented v1
  matchup against Search 64.
- First command to playable: strictly less than 60,000 ms at the specified
  external boundary. Each child readiness probe is bounded and child death
  short-circuits the wait.
- After installation: zero non-loopback browser requests through initial load,
  first action, reload/resume, and post-reload action. Loopback HTTP/WebSocket
  is required and allowed.
- Installation may contact only the Python, npm, and Cargo sources implied by
  the locked toolchains/dependencies. Runtime never downloads card data,
  treatments, fonts, or other pack assets.
- Pack lookup stays in-memory and exact-name O(1), as established by W2-185.
  No cache/database/background worker is added.
- The launcher owns its child process group and leaves no backend/frontend
  process behind after signal, failure, or proof completion.

## Exclusions

- No deck builder, collection UI, arbitrary-card fetcher, Scryfall fallback,
  pack updater, or generic content platform.
- No service worker, PWA/offline application shell, IndexedDB asset cache, or
  promise that the local authority is unnecessary during offline reload.
- No process/native-worker protocol adapter, protocol-v1 redesign, recovery
  envelope work, worker authority, browser authority, or WASM.
- No production deployment, installer package, Docker image, signed binary,
  Windows certification, broad Linux distribution matrix, or claim that a bare
  OS reaches play in one command.
- No change to card rules, default Search-64 behavior, deck contents, trace
  semantics, replay behavior, or W2-185 rights claims.
- No claim that dependency installation succeeds offline. Offline proof begins
  only after the first successful locked installation and local readiness.

## Pursue finish line

Pursue is complete when the canonical command and diagnostics are implemented;
the documented clean profile starts from the asserted empty project state; the
external receipt proves the exact default Search-64 matchup playable in under
60 seconds including project installation; the public-network-denied reload
resumes the same session and remains actionable with the exact installed pack;
all focused and existing W2-185 checks are green; and the one serial PR is
rebased immediately before `lf pr land -c` if main moved.
