# W2-204 — Prove the terminal release-stack prompt-family matrix

## Directive and decision

Directive v4 keeps this as one bounded serial PR on merged W2-186. Its
incorporation receipt was persisted before implementation resumed. The PR adds
the deterministic release-build terminal matrix only. It does not reopen the
W2-186 warm development-stack baseline, duplicate W2-195 command/recovery
semantics, redesign the table, or add cards or decks.

The selected product orientation remains UR Lessons as hero versus GW Allies
as villain. A reverse-seat proof scenario is necessary because the current
server resolves villain choices internally: `SCRY` and `WATERBEND` are reachable
only from GW cards and therefore cannot reach the human DOM in the default
orientation. The reverse scenario uses the same two named decks and the same
public `new_game` contract; it is a proof fixture, not a new product default or
content pack.

## User-visible outcome

A developer or release reviewer can run one command against the built Svelte
application, uvicorn, and the release-built CPython 3.12 engine extension. Two
fixed-seed games render every prompt family reachable in the curated matchup,
accept deterministic commands through the shipped table, and reach the Game
Over overlay. The gate fails rather than retrying another seed when a game is
non-terminal, a family is missing, an unexpected family appears, or the page
emits a console/page error.

No visible table behavior changes. Additive DOM data attributes expose the
already-authoritative action-space kind and action type so the proof does not
infer prompt semantics from English labels.

## Source of truth

- `managym/src/agent/action.rs::ActionSpaceKind` remains the prompt vocabulary.
- The v1 curated-pack manifest remains the exact deck/content authority:
  `tla-ur-lessons-vs-gw-allies@1.0.0`, manifest SHA-256
  `f5816a2792a67a79dc7e1f02e1d71c6296e56537cacd25f24328c7d6104ee787`.
- A checked-in machine-readable matrix under `frontend/e2e/` classifies every
  current action-space enum as reachable, terminal (`GAME_OVER`), or excluded
  (`MODAL`, whose proof card is absent from both decks). It owns the exact
  scenarios, policies, seeds, expected prompt counts, and terminal receipts.
- Runtime `ExperienceFrame.action_space` is the authority for the current
  browser prompt. `GameStore` and `ActionPanel` remain derived consumers.
- The W2-186 experience baseline imports the reachable-family inventory from
  the matrix so there is one checked-in list rather than two drifting lists.

The two deterministic rows, established against merged W2-186, are:

| Scenario | Orientation | Seed | Expected terminal | Exact surfaced prompt counts |
| --- | --- | ---: | --- | --- |
| `ur-lessons-seed-51` | UR hero vs GW random villain | 51 | villain, turn 14, 24 hero commands | `PRIORITY` 16; `DECLARE_ATTACKER` 1; `DECLARE_BLOCKER` 2; `CHOOSE_TARGET` 2; `LOOK_AND_SELECT` 1; `PAY_OR_NOT` 1; `DISCARD_THEN_DRAW` 1 |
| `gw-prompts-seed-62` | GW hero vs UR random villain | 62 | hero, turn 46, 148 hero commands | `PRIORITY` 49; `DECLARE_ATTACKER` 8; `DECLARE_BLOCKER` 7; `CHOOSE_TARGET` 9; `SCRY` 19; `LOOK_AND_SELECT` 4; `PAY_OR_NOT` 1; `WATERBEND` 51 |

Both use one versioned deterministic policy: play the first offered land;
cast spells in a deck-specific checked-in priority order; activate an offered
ability; otherwise pass priority. Attack and block when offered, prefer the
villain then hero for targets, keep scry cards, select the first eligible card,
pay offered costs, and tap before paying the remainder for waterbend. The
policy never uses `Math.random`, runtime seed search, retries, or hidden deck
mutation.

## Smallest build

1. Add the machine-readable matrix and a focused Python contract test. The
   contract checks the enum classification, curated pack ID/version/hash,
   exact named decks, family source cards, scenario coverage, and expected
   count/terminal schema.
2. Make the W2-186 reachable inventory derive from the matrix. Preserve every
   existing baseline number and exclusion.
3. Add non-visual `data-action-space-kind` and `data-action-type` hooks to the
   existing action panel/buttons.
4. Add a separate Playwright release configuration. It must build once, serve
   with Vite preview, proxy `/api` and `/ws` to a backend launched with
   `uv run uvicorn`, isolate traces under ignored test output, use one worker,
   and refuse to reuse an already-running dev server.
5. Add one release-only Playwright spec. Before page startup, wrap WebSocket
   `send` only to inject the matrix seed into the existing `new_game.config`;
   do not change the application protocol. Select the named decks and random
   opponent through the real UI, assert each frame's DOM family and enabled
   named actions, choose through the data-driven policy, serialize on
   `data-update-seq`, and continue to terminal. Attach a JSON receipt and
   assert the persisted trace has the expected seed/decks/end reason without
   claiming replay equivalence.
6. Compare observed per-scenario counts to the exact row and the global union
   to the reachable inventory. `GAME_OVER` is proven by the overlay and
   terminal trace, not counted as an input prompt.

## End-to-end proof

From a fresh browser context, `ur-lessons-seed-51` selects UR Lessons, GW
Allies, and Random in the built table. The test injects seed 51 into the same
outgoing `new_game` message, then drives stable action types through protocol-v1
commands until the villain wins on turn 14. A second fresh context reverses the
same named decks, injects seed 62, and drives through GW's healer and waterbend
decisions until the hero wins on turn 46. Every authoritative family is read
from the rendered action panel before the associated command; every action is
enabled and named; both games finish; and the exact union is the nine-family
inventory.

Focused proof:

```bash
uv run pytest tests/gui/test_release_prompt_matrix.py
npm --prefix frontend run test:e2e:release -- release-prompt-matrix.spec.ts
```

Complete PR gate:

```bash
uv run pytest tests/gui
npm --prefix frontend run check
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend run test:e2e:release -- release-prompt-matrix.spec.ts
```

## Affected surfaces and compatibility

- **Engine/rules/decks:** read and classified; unchanged.
- **Curated pack:** hash and named definitions are verified; unchanged.
- **Protocol-v1 and wire DTOs:** existing `new_game`, frame, offer, and command
  shapes are exercised; unchanged. Seed injection is test instrumentation.
- **Svelte table:** only additive DOM observability attributes; presentation,
  interaction, default matchup, and selector behavior remain unchanged.
- **Vite/Playwright:** preview gains the same loopback proxy as dev; a new
  release config and package command do not change the current dev suite.
- **Traces/API:** isolated terminal traces are checked for attribution and
  completion; replay rendering/equivalence is not asserted.
- **W2-186 baseline:** numeric budgets and development-stack proof are
  unchanged; its prompt inventory derives from the new matrix.

## Absent and error states

- Missing/unreadable matrix, unknown enum family, stale pack hash, invalid
  source card, duplicate classification, or a scenario union different from
  the reachable list fails the contract test.
- A frame with no family, no legal enabled action, an empty accessible action
  name, an unrecognized policy choice, a sequence that does not advance, or a
  count different from the checked-in row fails immediately.
- Reaching the command cap without the Game Over overlay is a non-terminal
  failure. The gate does not start another seed or accept partial coverage.
- Any console error or `pageerror` fails. Unlike dev E2E, the release gate has
  no favicon exception.
- Missing CPython 3.12 extension, failed frontend build, unavailable preview or
  backend, failed proxy, missing trace, or non-`game_over` trace end reason is
  a hard failure rather than a skip.
- `MODAL` surfacing is unexpected and fails until the inventory and scenarios
  are deliberately revised. `GAME_OVER` appearing as an input family fails.

## Operational boundary

- Build once, start one uvicorn and one Vite preview process, use one
  Playwright worker, and run the two contexts serially.
- Fixed ports remain environment-overridable; existing processes are never
  reused, preventing a dev server from satisfying the release gate.
- Each scenario is capped above its pinned command count but below 300 hero
  commands; each authority update keeps the existing 30-second bound and the
  whole focused gate keeps a 10-minute bound.
- All traffic is loopback. This Task does not duplicate W2-185's fresh-cache
  public-network denial or establish a new performance budget.
- No runtime scenario search, test-only backend endpoint, direct engine state
  mutation, second worktree, or parallel PR is introduced.

## Exclusions

- Stale-command, duplicate-command, reconnect/resume, checkpoint recovery, and
  replay equivalence owned by W2-195.
- W2-186 metric recalibration, cold-start measurement, memory/process budgets,
  or Phase-parity claims.
- Screenshot references, contrast/axe, keyboard-complete, reduced-motion,
  screen-reader, responsive, or broad visual work.
- Curated-pack changes, public-network asset proof, new cards/decks, arbitrary
  custom deck reachability, or modal-card inclusion.
- Rules, opponent policy, protocol, recovery, trace-schema, or product-default
  changes.

The pursue finish line is the exact two-row matrix passing from the release
build with terminal receipts, exact nine-family coverage, no unexpected
browser errors, and all complete PR gates green.
