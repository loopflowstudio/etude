# W2-209: release-matrix accessibility proof

## User-visible outcome

A player can complete every prompt family reachable in the curated UR Lessons
versus GW Allies matchup using only the keyboard with reduced motion enabled.
Each new decision moves focus to a legal choice; the prompt instruction, exact
legal choices, connection/game status, existing reconnect status, and terminal
result are exposed through stable screen-reader semantics. Non-legal board
objects do not enter the keyboard action path, and reduced motion removes
perceptible animation without removing semantic presentation.

The visual hierarchy and game rules remain unchanged. This Task adds the
smallest semantic, focus, and motion-preference fixes needed to make the
existing table operable and provable.

## Source of truth

- `ExperienceFrame.prompt`, `ExperienceFrame.status`, and its
  `InteractionOffer`s remain authoritative for the current decision and legal
  commands. The client must expose those offers; it must not invent legality.
- `frontend/e2e/release-prompt-matrix.json` remains the single deterministic
  inventory of reachable/excluded prompt families, fixed-seed scenarios,
  action-selection policy, exact prompt counts, and terminal outcomes.
- The client-owned `DECISION_PROMPTS` map supplies human instructions for each
  reachable family. The release proof must fail if any matrix family lacks an
  instruction; no duplicate accessibility inventory is added to the JSON.
- The curated pack manifest remains authoritative for selected-matchup assets.
  Accessibility checks only verify that rendered identities resolve through
  that installed pack rather than a missing/fallback asset path.

## End-to-end proof

One release-gate execution builds the Svelte app, serves it through Vite
preview with the real uv-managed uvicorn authority, and runs the two existing
matrix scenarios in headless Chromium with `prefers-reduced-motion: reduce`:

1. `ur-lessons-seed-51` covers the seven UR-side families and ends with the
   pinned opponent win on turn 14.
2. `gw-prompts-seed-62` is the existing proof-only reverse seat that adds
   `SCRY` and `WATERBEND` and ends with the pinned hero win on turn 46.

The harness configures the scenario, starts play, and activates every selected
offer with Enter or Space. At every prompt occurrence it asserts a named and
described Actions region, a complete instruction, exact accessible names for
all currently legal offer buttons, one keyboard command for a currently legal
offer, and focus on the first legal choice after the next authority update. On
the first occurrence of each of the nine families it also walks focus through
and out of the choice list to prove there is no trap, runs an automated WCAG
A/AA audit including color contrast, and checks reduced-motion computed styles.

The first scenario includes one bounded close/reconnect of the existing socket
to prove the already-rendered connection/recovery status is announced and
legal-choice focus is restored. This is presentation evidence only: it neither
changes nor broadens recovery protocol semantics. Terminal state must announce
the pinned result and move focus to the existing Play Again action. Every run
also fails on browser/page errors, failed local responses, public runtime asset
requests, or selected-matchup treatments that resolve outside the curated
pack. The original prompt counts, trace, winner, and turn assertions remain in
force, proving the accessibility path used the same authority and deterministic
game trajectory.

Focused verification:

```bash
uv run pytest tests/gui/test_release_prompt_matrix.py
npm --prefix frontend run check
npm --prefix frontend test
npm --prefix frontend run test:e2e:release -- release-prompt-matrix.spec.ts
```

Full relevant verification:

```bash
uv run pytest tests/gui
npm --prefix frontend run check
npm --prefix frontend test
npm --prefix frontend run test:e2e:release
```

The observable finish condition is a terminal accessibility receipt covering
all nine reachable families, both pinned game results, the named status/focus
checks, zero accessibility/contrast violations, zero motion violations, zero
runtime errors, and zero broken or external play assets.

## Affected surfaces and consumers

- `frontend/src/lib/game.svelte.ts`: complete prompt instructions for all nine
  matrix families; no authority or wire behavior changes.
- `ActionPanel.svelte`: accessible region/description/status relationships,
  exact offer names, and deterministic focus handoff for each new prompt.
- The play route and `GameBoard.svelte`: live connection/error/result semantics
  and terminal focus. The visible labels and layout stay intact.
- `Card.svelte`, stack/card wrappers, and `StopsPanel.svelte`: remove inert
  controls from the tab path and supply missing control names without changing
  pointer behavior or visual treatments.
- `PresentationStage.svelte` and `app.css`: preserve semantic beats while
  suppressing perceptible transition/animation under reduced motion.
- `release-prompt-matrix.spec.ts`: reuse the exact W2-204 scenarios and
  receipts while replacing gameplay clicks with keyboard activation and adding
  screen-reader, focus, motion, contrast, status, error, and asset assertions.
- `playwright.release.config.ts`, `package.json`, and the lockfile: include the
  automated accessibility audit dependency/configuration in the existing
  release gate.
- `tests/gui/test_release_prompt_matrix.py` and frontend unit tests: keep matrix
  classification exhaustive and prove every reachable family has an
  instruction.
- `docs/experience-proof.md`: document the strengthened claim and exact gates.
- Existing backend DTOs, command binding, traces, replay consumers, visual
  design, and non-release Playwright suites remain compatible.

## Absent and error states

- A reachable matrix family with no instruction, no rendered offer, an empty
  accessible name, a disabled legal offer, or an unexpected action family is
  a hard failure.
- Focus on `body`, a removed element, an inert card, or inside the Actions
  region after tabbing past its final choice is focus loss/trapping and fails.
- Enter/Space that sends no command, sends more than one command, or names an
  offer outside the current authoritative offer set fails before trajectory
  assertions.
- Connection changes and existing reconnect/resume messages are polite live
  status; errors and terminal results are assertive. A missing announcement or
  failure to restore a legal choice after the bounded reconnect fails. No new
  recovery reason or server state is introduced.
- Missing terminal state/result/focus, a changed winner/turn/prompt count, or a
  missing terminal trace fails.
- Any automated accessibility or contrast violation, perceptible animation or
  transition while reduced motion is active, console/page error, failed local
  response, public play-asset request, or curated identity falling back fails.
- `MODAL` remains explicitly excluded by the matrix because its proof card is
  absent from both selected decks; it is not treated as missing evidence.

## Operational boundary

- Reuse the existing two fixed seeds, policies, command ceilings (60 and 250),
  600-second release-test timeout, single worker, no retries, real release
  build/preview, and uv-managed authority process.
- Run full-page accessibility/contrast analysis once per distinct prompt
  family plus status/terminal boundaries; keep the cheaper semantic, focus,
  legal-command, asset, and error assertions on every prompt occurrence.
- Reduced-motion play must preserve the exact deterministic command counts and
  terminal outcomes. Presentation may collapse to a short semantic beat, but
  CSS animation/transition must not remain perceptible.
- Do not publish from this Task while W2-194/PR #75 is open. After focused and
  full relevant validation, stop and report `ready-to-publish`; do not open,
  submit, or land this PR under directive v2.

## Exclusions

- No new recovery protocol semantics, stale/duplicate/checkpoint proof, or
  replay-equivalence claim.
- No screenshot or visual-baseline files and no broad visual redesign.
- No new decks, cards, prompt families, or runtime seed search.
- No touch/mobile/zoom certification or real assistive-technology automation;
  this Task proves the browser accessibility tree and keyboard path.
- No engine, Rust, Python binding, bot policy, or command-authority changes.

## Pursue target

Implement the semantic/focus/reduced-motion fixes, strengthen the existing
release matrix in place, run the focused and full relevant commands above, and
leave the branch stopped with a complete validation receipt and no PR while
the directive-v2 publication hold remains active.
