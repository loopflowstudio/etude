# Rewrite Etude around AI-assisted testing-house play

## Problem

Etude Fantasia's architecture and first belief-to-strategy prototype now say
more than its product front door does. `README.md` still opens with “a way to
study Magic,” leads with package boundaries, and illustrates the product with a
generic board state. A new reader cannot tell that Etude is meant to feel like
an AI-assisted Pro Tour testing house: pilot a game, watch another player or
manabot, state a read, compare how strategy changes, try a line, and continue
into Study on one authoritative table.

The wave roadmaps contain the required foundations, but they do not yet state
one shared product law. Facts, player-authored beliefs, model-inferred beliefs,
and advice must remain distinct. The same decision, belief, advisor, and compute
identity must produce the same advice whether reached live or in Study. Only
the pilot can commit a live `Command`; viewer exploration cannot pause or mutate
the match. Discord remains the communication layer. Those are product
properties, not implementation trivia, and each owning wave needs to say its
part without creating a second surface or authority.

This work is documentation and roadmap alignment. It must also stay honest
about what is runnable on main. The merged `DecisionAdvice` prototype uses a
checked fixture generated from real flat-MC evidence and renders the same
component in the play and replay hosts, but its pinned decision is not yet bound
to the surrounding live match or selected replay. Its two belief scenarios are
pre-authored rather than free-form player input. Retry/return integration,
runtime advice, pilot/watcher roles, sharing, Avatar Cube Team Sealed, the
sealed-pool builder, and the world-pinned Elo arena remain roadmap work.

## The demo

Open `README.md`. Before installation details, it describes the complete
testing-house loop and shows a legible, versioned crop of the real merged advice
component with **Facts**, two **Beliefs**, complete per-action **Advice**,
explicit **Deltas**, hidden opponent information, and pinned advisor/compute
identity. Its adjacent caption says exactly what is fixture-backed today and
what is still being connected.

Run:

```bash
cd frontend && npx playwright test --config playwright.readme.config.ts
```

The command recreates and byte-compares
`docs/assets/ai-assisted-testing-house-v1.png` from the checked advice fixture
under a fixed browser profile. It does not require or modify application code.

## Approach

### 1. Make the README a product front door

Rewrite the entire root README in this order:

1. **One-sentence identity.** “Etude Fantasia is an AI-assisted Pro Tour
   testing house for Magic: The Gathering.” Follow with the player loop in
   ordinary Magic language: play, watch, state a read, compare strategy, try a
   line, and carry the match into Study.
2. **The first AI interaction.** Explain that one decision surface keeps facts,
   a viewer-safe belief, advisor output, and the strategy delta visibly
   separate. Use “state a read” in player prose; reserve `WorldQuery`,
   `BeliefState`, and `ConditionalStrategyResult` for technical links.
3. **An honest product image.** Replace the generic board image with the
   versioned component crop. The caption must say “fixture-backed prototype,”
   that the evidence came from real pinned flat-MC search, and that live
   decision binding, player-authored belief entry, Retry/return, and shared
   watcher roles are still being connected. The component's own footer keeps
   “advisory only — submit through the ActionPanel” visible.
4. **What works now.** Preserve the runnable one-versus-one curated game,
   human-versus-search/checkpoint play, semantic presentation, recovery,
   finished-game replay, canonical viewer-safe decision/evidence contracts,
   and the fixture-backed comparison prototype. Do not call the fixture the
   current live position or imply that the selected replay owns it.
5. **The destination.** State Avatar Cube Team Sealed as a versioned north
   star: a 540-card cube, two 135-card team pools, three 40-card-minimum decks
   per team, unlimited basics, deck-specific sideboards, the three-by-three
   matchup matrix, and first to five wins. Humans choose pilot seats; manabots
   fill the remaining teammate and opponent seats. Name Game's construction
   UX, Intelligence's later sealed-pool builder, and the versioned world-pinned
   Elo arena.
6. **Product laws.** Keep a compact, player-readable list: facts are not
   beliefs; authored beliefs are not inferred beliefs; neither is advice;
   identity-pinned advice is reproducible live and in Study; only the pilot
   commits; exploration is isolated; Discord carries conversation.
7. **Keep the useful developer front door.** Retain and tighten Play, training,
   research-ledger, development, repository-map, and naming sections. Preserve
   every command, package boundary, link, clean-machine claim, and naming rule
   unless it is demonstrably stale. Remove the duplicated “development of
   intelligent” sentence and stop leading with subsystem internals.

Use tense as an evidence boundary:

| Class | README wording | Included examples |
|---|---|---|
| Runnable now | “Etude can,” “the current game,” “the prototype shows” | curated play, semantic table, recovery, replay, fixture-backed advice comparison |
| Contract or active integration | “the contract preserves,” “is being connected” | canonical decision identity, Retry/return UI, live decision binding, runtime advisor |
| Destination | “Etude is building toward,” “will” | watcher roles/sharing, Avatar Team Sealed, builder, Elo arena |

### 2. Give each roadmap one non-overlapping part of the story

Edit the four active/folded north-star files without deleting their existing
measures, sequencing, ownership, or Avatar promises.

| Document | Product statement to add | Ownership boundary to keep explicit |
|---|---|---|
| `wave/game/GOAL.md` | Game is the shared testing-house surface for play, watching, explicit reads, strategy comparison, Retry, isolated lines, replay, and Study. | Game owns player-authored belief UX, roles/capabilities, presentation, construction UX, and the sole live Command path; it does not infer hidden truth or generate strategy evidence. |
| `wave/intelligence/GOAL.md` | A manabot is opponent, teammate, sparring partner, and advisor. Given one viewer-safe belief at a pinned decision, it returns the complete aligned strategy distribution, value, robustness, and uncertainty with reproducible identity. | Intelligence owns priors, inferred beliefs, advisor/search identity, compute identity, evidence, the later sealed-pool builder, and the Elo arena; it does not own facts, legal Commands, or the player surface. |
| `wave/rules/GOAL.md` | Rules provides the viewer-relative possible-world space and typed query grammar that make “state a read” safe and exact. | managym owns facts, query meaning, the reference compatible-deal measure, materialization, authority, and isolated forks; query validation never reveals actual hidden truth. |
| `wave/study/GOAL.md` | Folded Study is the same testing-house decision surface under historical/counterfactual time controls, not a post-game analysis product with different semantics. | Keep the no-new-Study-Projects rule; Game owns the mode, Rules the exact position/fork/return, Intelligence the attributable evidence, and Discord the human conversation. |

The revised Game measures must continue to advance the existing goals that
“the same authoritative frames, offers, commands, semantic events, and replay
identities” survive direct play and Study, and that every historical decision
is restorable. Add the first AI UX as a computable extension of those measures:
two explicit viewer-safe belief scenarios at the same canonical decision, one
pinned advisor/compute identity, complete aligned action distributions and
deltas, no fact/belief/advice conflation, no hidden-truth leak, and no match
mutation. Do not mark that product measure complete merely because the fixture
prototype exists.

### 3. Replace the generic image with a reproducible capture

Add a capture-only Playwright spec and config; do not change Svelte, server,
protocol, or application behavior.

- `frontend/e2e/readme-capture.spec.ts` opens the current play host, starts the
  deterministic curated game, expands `DecisionAdvice`, selects **Opponent
  holding interaction**, waits for the second scenario evidence and deltas,
  and snapshots only `[data-testid="decision-advice"]`.
- `frontend/playwright.readme.config.ts` fixes Chromium, 1600×1200 viewport,
  device scale 1, dark color scheme, reduced motion, `en-US`, UTC, local
  backend/frontend ports, and the snapshot path under `docs/assets/`.
- The spec asserts the crop contains hidden opponent information, both complete
  legal alternatives (“Play Mountain” and “Pass priority”), non-empty deltas,
  `flat-mc-search-v1`, the compute identity, and the advisory-only footer before
  taking the snapshot. Browser/page errors fail the run.
- The fixture owns the data and already pins replay, match, model, compute,
  seeds, and evidence hashes. The screenshot filename adds an independent
  presentation version: `ai-assisted-testing-house-v1.png`.
- Capture the component only. A full-page play or replay screenshot would put
  the fixture's decision beside an unrelated surrounding board and would be a
  false integration claim.
- Add concise alt text that describes the visible comparison, not marketing
  intent. Keep the prototype/live boundary in adjacent prose so it survives
  image-loading and screen-reader paths.

The capture harness is the only non-Markdown exception to the docs-only diff.
It is acceptance evidence for the README asset, not UI implementation.

## De-risking

| Question | Finding | Impact on design |
|---|---|---|
| Is the branch on the correct GAM-5 base? | Yes. `lf rebase` moved the branch from `f41180f` to `origin/main` at `8ca40e2` on 2026-07-17. Main includes the advice prototype, the unified semantic decision contract, and conditional shard/belief ablation work. | Design and prose target the actual merged state; no speculative transplant from the draft worktree. |
| Does a belief-to-strategy surface actually exist? | Yes. `DecisionAdvice.svelte` renders Facts, Beliefs, Advice, Deltas, and advisor/compute identity in both play and replay hosts through one `POST /api/advice` seam. Pointer, keyboard, narrow-viewport, reduced-motion, identity-mismatch, and viewer-safety e2e coverage landed in `3b58863`. | The README may show and describe a prototype, not merely a mockup. |
| Is the prototype live-bound advice? | No. Both hosts fetch one completed-match `erd1` fixture. The current live frame has no finalized replay address, and the selected replay need not be the fixture's source replay. | Never say the advice describes the current live board or selected replay. Caption the image as fixture-backed; describe live binding as active integration. |
| Are the displayed beliefs player-authored or model-inferred? | The two scenarios are checked presentation metadata selected by the player; the component also displays an “Inferred range” string. There is not yet a durable personal authored-belief object or a separately identified model-inferred `BeliefState` in the player surface. | State the four-way distinction as a product law and future contract. Describe today's control as choosing between two pinned scenarios, not authoring arbitrary beliefs. |
| Is the advice fabricated? | No. `scripts/generate_advice_fixture.py` forks a retained decision from UR Lessons vs GW Allies, runs real flat-MC search over two disjoint 16-seed families, and records policy mass, value, robustness, uncertainty, budget, and evidence hashes in a validated `StudyArtifact`. | The image may call the evidence real and pinned. It must not imply production advisor strength or runtime latency. |
| Does changing a scenario mutate play? | No. The prototype adapter is a read-only fixture consumer, and the live `ActionPanel` remains the only Command path. Exact exploratory fork/return exists below the UI, but end-user Retry and watcher isolation remain incomplete. | Keep “advisory only” visible. State isolated viewer exploration as a law/destination, not a fully shipped collaboration claim. |
| Can a full-page screenshot be honest? | Not yet. The surrounding live/replay board is not identity-bound to the fixture whose Facts are shown inside `DecisionAdvice`. | Snapshot the component itself. Do not crop a mismatched board and side panel into one apparent decision. |
| Is the old visual reference reproducible? | It is versioned under the release prompt-matrix references, but it shows only a generic developed board and does not manifest the new product story. The advice e2e currently asserts behavior but takes no screenshot. | Add one dedicated deterministic docs-asset snapshot instead of reusing or manually editing a board PNG. |
| Should the draft be landed mechanically? | No. The draft is an architecture-heavy exploration with pseudotypes and a roadmap that predates the merged prototype. It is useful for product laws and role boundaries, but too abstract for the README. | Translate its intent into player language, retain types only in wave/architecture links, and use main as truth for current claims. |
| Does folded Study need an edit? | Yes. The updated brief explicitly names folded Study, and its current objective says only “post-game and counterfactual mode.” It does not mention live/watch parity, beliefs, advice identity, or Discord. | Edit `wave/study/GOAL.md` while preserving its folded status and prohibition on new Study Projects/Tasks. |
| Will the rewrite erase Avatar, builder, seating, Elo, or authoritative replay/Retry promises? | All are already distributed across the three wave goals, but not all are visible at the front door. The user explicitly requires preservation. | Use a coverage matrix during review; keep every numeric Avatar parameter and existing authority/Retry measure. |
| Is an external facts search needed? | No external factual claim is introduced. The requested emotional model and laws come from the task; current capability claims are verified against repository code, fixtures, tests, architecture, and wave goals. | Avoid citation theater and product-comparison claims. Link to primary in-repo contracts. |
| Is there a configured Markdown link checker? | No. CI runs Rust, Python, frontend, integration, release, and visual checks, but no Markdown link validator. | Run a focused `uv run` relative-link resolver over every edited Markdown file and `git diff --check`. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|---|---|---|
| Add “AI-assisted testing house” to the current opening and leave the rest | Smallest diff, but package-first structure, generic screenshot, separate Play/Study story, and stale capability boundaries remain. | Directive v2 explicitly requires a whole-README editorial pass and a product-front-door proof, not vocabulary insertion. |
| Copy `scratch/ai-assisted-play-north-star.md` from the draft worktree | Preserves the original thinking and pseudotypes. | It predates merged work, reads like an internal architecture proposal, overweights future collaboration, and is not concise player-facing copy. |
| Describe the intended surface entirely in future tense | Safest against overclaiming, but hides that a real evidence-backed prototype now exists. | Show what landed and label its exact limits; honest specificity is more compelling than generic aspiration. |
| Present the prototype as complete live/Study advice | Strongest marketing story. | False: current hosts share a component and fixture seam, not a live-bound decision/advisor or selected-replay binding. |
| Keep the developed-board screenshot and explain AI in prose | No capture work. | The first visual still teaches “generic Magic client,” contradicting the requested front-door repositioning. |
| Capture the whole live table with advice expanded | More visually dramatic and better conveys one surface. | The table and advice fixture can represent different decisions. The composite would manufacture integration the code does not have. |
| Manually capture and check in a PNG | Minimal files. | It cannot be reproduced or reviewed for semantic drift. A deterministic locator snapshot is small and self-verifying. |
| Fold all laws into `docs/ARCHITECTURE.md` only | Central technical authority. | The task is product positioning; readers and wave owners need the laws at the front door and in their owning roadmaps. The architecture already defines the substrate and stays unchanged. |

## Wild success and failure tests

| Outcome | What happened | Design response |
|---|---|---|
| Wild success | A Magic player understands the product in under a minute, recognizes the testing-house rhythm, sees one read change the whole strategy distribution, runs the current game, and knows which parts are prototype versus destination. Teams use the same four evidence nouns in Game, Rules, and Intelligence discussions. | Lead with player activity, make the comparison visible, state the laws once in plain language, and repeat only ownership-specific consequences in each goal. |
| Wild failure | “AI-assisted” becomes chatbot-flavored marketing; the screenshot implies a live advisor on the displayed board; Study re-emerges as a separate analysis app; inferred ranges are presented as facts; Avatar, construction, or Elo quietly disappear; or the README becomes an internal architecture dump. | Ban hidden-truth and causal wording, crop only the identity-pinned component, caption limitations, retain a north-star coverage matrix, keep types below the player story, and preserve folded Study and Discord boundaries. |

## Key decisions

- **Etude is the testing house, not an AI feature attached to a game.** The
  opening describes a continuous social/learning activity; subsystems follow
  later.
- **“State a read” is the player phrase.** `WorldQuery`, `BeliefState`, and
  `ConditionalStrategyResult` remain the precise internal contracts. “Typed
  belief” never means an unconstrained text prompt.
- **Four evidence kinds stay separate.** Facts come from viewer-safe managym
  authority; player-authored beliefs come from Game; inferred beliefs and
  advice come from identified manabot producers. Advice never upgrades a
  belief into fact.
- **Advice reproducibility is identity equality, not visual similarity.** The
  same decision, viewer-safe belief, advisor, compute class, and required
  provenance must yield the same advice live or in Study; mismatches fail
  closed.
- **Participation does not grant authority.** Pilot, watcher, and Study roles
  may share the table, but only the acting pilot submits the live offered
  `Command`. Comparing advice or exploring an isolated fork never mutates or
  pauses the authoritative match.
- **The prototype is evidence, not the promise fulfilled.** Main proves the
  component, fixture, viewer safety, and identity-failure path. It does not
  complete the Project KRs for exact live/Study binding, personal belief
  authorship, runtime advising, Retry integration, or watcher collaboration.
- **The hero image is a tested artifact.** A component-only locator snapshot
  makes the current proof legible and prevents the surrounding board from
  laundering a fixture into a live claim.
- **Avatar Cube Team Sealed remains the destination.** Exact format parameters,
  human/manabot seating, Game construction UX, Intelligence builder, and Elo
  arena stay explicit. The near-term one-match loop still governs sequencing.
- **Discord remains the room.** Etude owns canonical decisions, belief and
  strategy artifacts, and shared actions—not chat.

## Scope

- In scope: whole-root-README editorial rewrite; honest current/prototype/
  destination boundaries; one versioned deterministic advice-component image
  and its capture-only Playwright harness; updates to Game, Intelligence,
  Rules, and folded Study objectives/measures/bounds; preservation of Avatar
  Cube Team Sealed, human/manabot seating, builder, Elo, authoritative play,
  replay, Retry, Study, and Discord promises; relative-link and prose audits.
- Out of scope: any Svelte component, route, server, protocol, engine, search,
  fixture-data, or schema change; live decision binding; runtime advice;
  free-form or natural-language belief entry; GAM-4 Retry/return UI; branch
  exploration UI; pilot/watcher sessions or sharing; chat; new Study Projects;
  Avatar content implementation; builder implementation; arena/Elo
  implementation; changes to `docs/ARCHITECTURE.md` or server-owned
  `wave/*/MEMORY.md`.

## Done when

- `README.md` leads with the AI-assisted Pro Tour testing-house loop, contains
  the product laws and claim-boundary section, links the versioned prototype
  capture, preserves runnable commands and technical navigation, and names all
  four long-horizon elements: Avatar Cube Team Sealed, human/manabot seating,
  builder, and Elo.
- The five north-star files tell one compatible story without moving
  ownership: `README.md`, `wave/game/GOAL.md`,
  `wave/intelligence/GOAL.md`, `wave/rules/GOAL.md`, and
  `wave/study/GOAL.md`.
- The Game roadmap still contains the existing authoritative play/Study and
  exact historical-decision measures and adds a non-completed, computable
  belief-to-strategy measure matching the Linear Project KR.
- The component capture is legible, includes the second scenario's complete
  advice and deltas plus pinned identity/footer, excludes the unrelated board,
  and is reproducible with:

  ```bash
  cd frontend && npx playwright test --config playwright.readme.config.ts
  ```

- Frontend type/config checks pass after the capture harness addition:

  ```bash
  cd frontend && npm run check
  ```

- All relative Markdown links in the five edited product files resolve. The
  checker runs through `uv run` per repository policy, and `git diff --check`
  is clean.
- A final claim audit finds none of these unqualified present-tense claims:
  runtime/live-position advice, player-authored free-form belief input,
  Retry/return UI, watcher sharing, implemented Avatar Team Sealed, implemented
  builder, or a shipped Elo arena.
- `git diff --stat` contains Markdown, one PNG, and the capture-only Playwright
  spec/config; no application, protocol, engine, search, fixture-data, schema,
  package-lock, or memory file changes.

## Measure

This docs task does not complete a gameplay KR and should not claim a product
metric movement. Its acceptance is binary and auditable:

- one front-door loop: play → watch → state a read → compare strategy → try a
  line → Study;
- four evidence kinds kept distinct in every owning roadmap;
- one identity law shared by live and Study;
- one live Command authority and zero viewer mutation paths;
- all seven durable north-star constraints preserved: Avatar format, seating,
  builder, Elo, authoritative play/replay/Retry/Study, exact viewer safety, and
  Discord;
- one deterministic image diff proving the README depicts the merged
  fixture-backed surface rather than a mockup.

The work makes two existing Game measures easier to evaluate—authority parity
across direct play and Study, and exact restoration of every historical
decision—and gives the Project's belief-to-strategy KR a stable player-facing
definition. Implementation evidence, not this prose, will decide when those
measures are complete.
