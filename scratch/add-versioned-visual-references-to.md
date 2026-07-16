# W2-212: versioned release visual references

## Directive and scope

Produce one focused PR on the current Task branch. Extend W2-209's two fixed
release-preview trajectories with committed Playwright references; do not add a
second scenario runner or a second source of gameplay truth. The PR ends when
the ordinary CI gate compares current output to reviewed references and the two
scenarios still reach their pinned terminal results.

## User-visible outcome

A reviewer changing the selected-matchup play UI gets an objective release
failure when the rendered prompt, representative board, reconnect status, or
terminal result no longer matches the committed appearance. A player sees the
same UR Lessons versus GW Allies product visuals and authority behavior as
before this Task; the only product-runtime adjustment is to serve the already
declared Inter typography as a bundled local font so the intended face is
stable and does not fall through to a host-dependent system font.

The reference set covers:

- the Actions panel for each of the nine reachable prompt families;
- the board at the opening `PRIORITY`, first `DECLARE_BLOCKER` combat state,
  and first `WATERBEND` developed state;
- the existing `disconnected`, `reconnecting`, and recovered `connected`
  statuses during the bounded first-scenario socket close; and
- both terminal boards/results, one for each fixed scenario.

These are visual regression references, not a visual redesign or a claim that
screenshots prove semantic correctness or real assistive-technology behavior.

## Source of truth

`frontend/e2e/release-prompt-matrix.json` remains the machine-readable selected
matchup authority for scenario order, seeds, policies, reachable families,
terminal expectations, and curated-pack identity. Extend it with one
`visual_references` record containing:

- an integer reference-set version and the committed reference directory;
- the fixed render profile;
- exactly one scenario and occurrence for every reachable prompt family;
- the three semantic board triggers;
- the reconnect trigger and required status list; and
- both scenario IDs as terminal references.

Also pin each scenario's full pre-terminal prompt-family sequence. The browser
must compare the family at each command to that expected sequence before
choosing an action, so a reordered or substituted intermediate decision fails
at the first drift rather than being hidden by equal aggregate counts.
`tests/gui/test_release_prompt_matrix.py` validates that the visual inventory is
complete and non-duplicated, references only reachable scenario/family
occurrences, that each sequence length and histogram matches the existing
command count and prompt counts, and that both terminals are inventoried.

The authoritative game state still comes only from uvicorn's protocol frames.
The test may select and photograph states but must not fabricate observations,
offers, commands, recovery envelopes, or terminal results. The selected pack
manifest remains the authority for card/token treatments. Committed PNGs under
the versioned reference directory are the expected render derived from those
records; they are never generated during an ordinary gate.

## Deterministic render profile

Use a single named reference profile in `playwright.release.config.ts` and the
matrix metadata:

- GitHub-hosted `ubuntu-24.04`, x86-64;
- Node 22 plus the exact Playwright and Chromium revisions in
  `frontend/package-lock.json`;
- Chromium only, headless, one worker, and the existing real Vite preview plus
  uvicorn release stack;
- viewport 1600 x 1200 CSS pixels, device scale factor 1, dark color scheme,
  `en-US`, UTC, and reduced motion;
- Chromium font-render hinting disabled so host FreeType hinting does not alter
  the pinned local glyph rasterization;
- bundled local Inter faces for weights used by the application, with
  `document.fonts.ready`, a successful local font response, and the active font
  asserted before the first reference; and
- locator screenshots with animations disabled, caret hidden, CSS-pixel scale,
  and zero pixels beyond Playwright's 0.2 perceptual color threshold on the
  reference profile.

Prompt references photograph `data-testid="action-panel"`; board and terminal
references photograph `data-testid="game-board"`; reconnect references
photograph the existing game header containing the connection badge. Wait for
the authoritative update, the expected semantic trigger, font readiness, and
the reduced-motion presentation stage to settle before each comparison. Do not
use arbitrary time sleeps, masks, or dynamic style injection that would make
the reference differ from the shipped preview.

Make the transient reconnect reference deterministic with Playwright's
WebSocket routing: pass the initial socket through, close it at the existing
command-zero proof point, pause only the replacement handshake while the real
client reports `reconnecting`, then connect that route to the real loopback
server and wait for the genuine recovery frame. The test does not synthesize a
server message or alter reconnect timing in product code. Compare
`disconnected` before advancing the reconnect timer, `reconnecting` while the
replacement handshake is held, and `connected` only after resume restores the
pre-close offer and advances the authoritative update sequence.

The local font is a pinned npm dependency imported into the production CSS so
Vite emits it into the release bundle. No Google Fonts, CDN, Scryfall, or other
public runtime request is permitted.

## End-to-end proof

One ordinary release run builds the Svelte application, starts Vite preview and
uvicorn, and drives both matrix scenarios through the same protocol and policy
as W2-209. In the first scenario it captures the opening board, holds the
existing bounded reconnect long enough to compare each status, resumes the
same authoritative offer, compares each designated prompt/board state, and
finishes at opponent win on turn 14 after 24 commands. The reverse-seat
scenario adds the `SCRY`, `WATERBEND`, and developed-board references and
finishes at hero win on turn 46 after 148 commands. The run finally proves that
all nine prompt references, all three board references, all reconnect statuses,
and both terminal references were consumed exactly once.

The focused proof commands are:

```bash
uv run pytest tests/gui/test_release_prompt_matrix.py -q
npm --prefix frontend run check
npm --prefix frontend test
npm --prefix frontend run test:e2e:release
```

The last command is the observable finish line: on the named profile it reaches
both terminal receipts with no missing or changed reference, no scenario drift,
and no runtime/asset failure. The CI job repeats the Python contract and release
browser gate on every pull request and main push, and the aggregate `result`
job depends on it.

## Affected surfaces and consumers

- `frontend/e2e/release-prompt-matrix.json`: adds the visual inventory and full
  expected prompt sequences without changing seeds, policies, decks, command
  counts, winners, or turns.
- `tests/gui/test_release_prompt_matrix.py`: certifies the new matrix contract;
  it remains a metadata/engine-enum check and does not render.
- `frontend/e2e/release-prompt-matrix.spec.ts`: reuses its single pair of real
  terminal playthroughs to compare references and fail at intermediate
  sequence drift.
- `frontend/playwright.release.config.ts`: names and fixes the only reference
  browser profile, screenshot path, and comparison options while retaining the
  existing release preview servers.
- `frontend/src/app.css`, `frontend/package.json`, and the lockfile: bundle the
  declared font locally. No component layout, copy, color, or authority logic
  changes.
- `frontend/e2e/visual-references/v1/` (or the version selected in the matrix):
  stores reviewed PNGs named by category/state, never opaque test counters.
- `docs/experience-proof.md`: names the profile, inventory, ordinary gate, and
  intentional update procedure and keeps the existing accessibility and
  recovery disclaimers.
- `.github/workflows/ci.yml`: adds a visual-reference job and includes it in
  the aggregate result. Every Python entry point added by this Task is invoked
  through `uv`.

Existing CLI and wire DTOs, server routes, protocol schemas, replay consumers,
deck selection, and presentation authority remain compatible and unchanged.

## Asset, network, and runtime failure semantics

Install HTTP and WebSocket guards before navigation. Loopback release-stack
traffic is allowed; any attempted public `http`, `https`, `ws`, or `wss`
request is recorded, aborted, and fails the scenario. For every reference,
require at least one curated treatment, require all treatments to identify the
matrix pack rather than `fallback`, require every rendered image (if any) to be
complete with nonzero natural dimensions, require the bundled font request to
succeed locally, and retain W2-209's failed-request and local HTTP status
checks. Any console error or page error fails.

Normal comparison mode treats each of these as an error:

- a missing matrix visual record, prompt family, semantic trigger, or terminal;
- a missing committed PNG (never auto-created);
- any unexpected pixel difference on the named reference profile;
- a prompt-family sequence mismatch before terminal, an early terminal, a
  command overrun, changed counts/winner/turn, or missing terminal trace;
- a pack fallback, broken local asset/font, remote/public request, failed
  request, local response at status 400 or higher, console error, or page
  error; or
- a reconnect status that is not observed, or a recovered frame whose legal
  offer differs from the pre-close offer.

An empty or newly versioned reference directory therefore fails the ordinary
gate. Snapshot creation is possible only through the documented explicit
update mode.

## Intentional baseline updates

Add an opt-in `workflow_dispatch` boolean to the visual CI job. On the selected
branch, update mode runs the same named Linux profile with Playwright's explicit
snapshot-update flag, immediately reruns normal comparison, and uploads only
the versioned reference directory as an artifact. It does not commit, alter the
matrix trajectory, or weaken failures. The maintainer downloads the artifact,
reviews every changed image and the product diff, commits the accepted PNGs,
then requires a normal pull-request run to pass.

For an intentional new appearance, increment the matrix reference-set version
and target directory before dispatching; git history retains the prior set.
For a recapture caused only by a pinned runner/browser update, keep the visual
version only when appearance is demonstrably unchanged and document the
reference-profile change in the same review. Never accept a screenshot update
to mask prompt-sequence, terminal, authority, asset, network, console, or page
errors; update mode continues to enforce all non-pixel assertions.

## Operational boundary

The browser suite stays serialized at one worker, runs exactly the existing two
fixed scenarios with no seed search/retry, and retains the 600-second test and
bounded per-command/reconnect timeouts. Screenshot capture adds no production
network dependency and no new gameplay round trip. This Task sets no latency,
frame-pacing, or memory budget and must not modify W2-186's performance
baseline. CI may retain failure artifacts, but successful ordinary runs need
not publish screenshots already committed in the repository.

## Exclusions

- No visual redesign, new layout, prompt copy change, deck, card treatment, or
  general content platform.
- No command, recovery, checkpoint, replay, or presentation-event semantics;
  this Task only photographs the existing bounded reconnect status path.
- No mobile, touch, zoom, multiple-browser, or multiple-OS visual matrix.
- No performance-budget change or claim of cold-start, memory, or frame-pacing
  proof.
- No claim of real screen-reader or other assistive-technology automation;
  W2-209's browser accessibility-tree checks remain described at their actual
  scope.
- No broad offline/replay screenshot coverage beyond the directed live release
  matrix.
