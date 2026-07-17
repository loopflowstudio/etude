# Etude Fantasia Visual Design System — Sepia Etude

The design contract for Etude Fantasia's player-facing surfaces. It
describes one visual language in three artifacts: **the Sheet** (Play),
**the Plate** (the card), and **the Score** (Replay, and Study as it
grows). `frontend/src/app.css` is the single source of truth for every
token value; this document explains what the values mean and the rules
that keep them coherent. If the two disagree, `app.css` wins and this
document is the bug.

---

## Brand Foundation

**An annotated score in an old library.** The interface is sepia ink on
aged paper — quiet, studied — except the active decision, which burns
Mountain red. The palette is drawn from Magic itself: card-frame
parchment, the five mana colors as registers, and the cards as printed
plates tipped into the book.

| Visual choice | Brand signal |
|---------------|--------------|
| Parchment grounds, ruled regions | The game as an open book |
| Bronze display serif | Margin annotations, studied notes |
| One Mountain-red accent | The live decision is the only urgent thing |
| WUBRG registers | The five mana colors as the semantic spine |
| Printed plates with real art | The cards are the illustrations |

### The two metals

- **Bronze** (`display`) is for reading: headings, rubrics, marginalia.
- **Mountain red** (`action`) is for acting: buttons, focus, links, the
  prompt's rule, the stack, targeting marks, errors.

Urgency has one color. A prompt is a red-ruled italic annotation; an error
is a red-washed banner; they share the family but never the shape.

---

## The Language

### Grounds: page, panel, field

Three ground levels per mode, so surfaces separate by value, not just by
outline: the **page** (`ground`), **panels** on it (`panel`, with `panel-muted`
wells), and the **field** fill inside bordered controls.

Light — Parchment:

| Token | Hex | | Token | Hex |
|-------|-----|-|-------|-----|
| `bg` | `#EBDFC6` | | `text` | `#3A3122` |
| `bg-surface` | `#F7F0E0` | | `text-secondary` | `#5F553D` |
| `bg-muted` | `#DED0AF` | | `accent` | `#973427` |
| `bg-field` | `#FCF9EE` | | `accent-hover` | `#AB4634` (lighter) |
| `border` | `#C9B892` | | `accent-text` | `#6D5A35` |
| `border-strong` | `#A5926A` | | `accent-soft` | `#EAD6C6` |

Dark — The library after dark:

| Token | Hex | | Token | Hex |
|-------|-----|-|-------|-----|
| `bg` | `#191510` | | `text` | `#ECE4D0` |
| `bg-surface` | `#221C14` | | `text-secondary` | `#B3A88E` |
| `bg-muted` | `#2B241A` | | `accent` | `#B24D38` |
| `bg-field` | `#2F2717` | | `accent-hover` | `#A34531` (**darker**) |
| `border` | `#423A2B` | | `accent-text` | `#C3A568` |
| `border-strong` | `#625741` | | `accent-soft` | `#3F2B20` |

> **Dark hover deepens.** A red lighter than `#B24D38` cannot hold ivory
> button text at WCAG AA, so the dark-mode hover moves down, not up.

`ivory` (`#F8F1E0`) is constant across modes: it is the text on accent
fills and nothing else.

### Two inks

Text is `ink` and `ink-2`, plus the bronze `display` tier for headings and
rubrics. The pie speaks through container washes, rules, and marks — never
through body text. The `-ink` register companions exist for the rare
moments a register must speak in text (error banners, a hot rubric); treat
each new use as an exception to justify.

### The color pie — five registers

Each mana color owns a semantic register. Assign new colored elements to
the register they *belong* to, never the one that looks best. Play
whispers the pie (deck rules, the red stack, status chips); the Score
sings it (every line carries its register). There is no quota — there is
fidelity.

| Register | Mana | Base | Carries |
|----------|------|------|---------|
| `mountain` | Mountain | `#973427` | Action and urgency: the accent itself — prompts, the stack, targeting, damage tallies, errors |
| `plains` | Plains | `#9F7A1C` | Order and structure: lands and turn-structure lines in the score, reconnecting, targeting highlights |
| `island` | Island | `#2E6DA8` | The engine thinking: spells in the score, auto-passing, and the study apparatus to come |
| `swamp` | Swamp | `#6A5F86` | The hidden and the spent: card backs, game over, deaths in the score |
| `forest` | Forest | `#4C8040` | Presence and health: connected, hero's lines in the score |

Each register ships a contrast-safe `-ink` text companion per mode and a
`-wash` container tint.

**Washes are tokens, not arithmetic.** `--{register}-wash` is pre-mixed
with the panel ground per mode — 10% on parchment, 26% by lamplight — so a
wash can never silently vanish when the ground changes beneath it. Free
alpha modifiers on register bases are for borders and marks, not grounds.

### The vocabulary

Components speak semantic utilities and no others — there is no stock-name
shim, so a stale Tailwind color class fails to resolve instead of silently
meaning something:

```
ink / ink-2            reading text
ground / panel / panel-muted / field   the ground levels
line / line-strong     hairline and emphasized borders
action / action-hover / action-soft    the interactive accent
display                bronze display text
ivory                  text on accent fills
forest / plains / island / swamp / mountain   register bases
{register}-ink         contrast-safe register text
{register}-wash        pre-mixed register ground
```

The migration recipe and full mapping live in
`frontend/docs/semantic-utilities.md`.

---

## The Shell

The site around the book. The **banner** is the brand's one moment of full
color: the W→U→R→B→G Lotus Cobra weave, a fixed rich world in both modes,
carrying the ivory brand and the nav — the active page sits in a filled
ivory chip (`aria-current="page"`). The **colophon** closes every page the
way a book closes with its imprint: identity line, table of contents, and
the Fan Content attribution the WotC policy asks us to display. Everything
between them belongs to the sheet.

## The Sheet — Play

One continuous leaf. Regions are ruled, never boxed.

- The game header is the sheet's **masthead**, in conscious levels: the
  matchup names the players — "You (UR Lessons) vs Search 64 (GW Allies)",
  serif with mana pips; configuration fields appear only while they matter
  (before a game, and again at game over) with small-caps labels set above
  them; New Game is the one red action; and the connection is a register
  dot and a whisper of mono beside it. Nothing on the masthead competes
  with the sheet below.
- The turn is a **tempo marking**: small caps between flanking rules, in
  human notation ("Turn 6 · Combat · Declare Blockers"). Raw engine
  identifiers live in data attributes for Study, never on the player's
  table. Identical legal actions coalesce with a count.
- Each player is a **region**: bronze serif heading with the deck name in
  italics, the deck's color identity as a thin **pie rule** beneath it,
  life as bare numerals. Zones are **staves** — hand, battlefield,
  graveyard — named by small-caps **rubrics** in a margin column, divided
  by hairlines. The opponent reads hand-first; the hero battlefield-first.
- **The stack is the one thing on the sheet allowed to burn red**: a hot
  rubric and red-framed spell lines between the two regions.
- The **margin column** holds the apparatus behind a single vertical rule:
  the prompt as a red-ruled italic annotation, action options as field
  buttons, "Your move" in red small caps, stops, and the log as ruled
  lines with actor rubrics.
- The log records **game vocabulary only** — actor and action; key hints
  and control labels never enter the record.
- **Hidden information is counted, not drawn**: hidden cards are the
  violet-framed back with its blind lozenge, unlabeled. **Empty states are
  silent**: one italic serif aside in `ink-2` with a staff's worth of
  breathing room — no borders, no chips.
- Modals sit on the **scrim** (`color-mix(in srgb, var(--text) 45%,
  transparent)`, z 190); page content is never dimmed by its own opacity.

## The Plate — the card

The card is a printed plate in the book, and the one object on the table
that keeps its own light in both modes: art palettes and everything
painted on them are **literal, never adaptive** — an adaptive scrim turns
parchment in light mode and erases the name (`visual-system.spec.ts` pins
this).

**Anatomy** — print grain over the art, varnish light above and a vignette
below, an engraved rule fading across the name plate, the name re-set (not
scaled) per size and clamped to two lines, power/toughness in a serif oval
— an object on the card, not a UI chip. Small cards earn only a name;
larger sizes add the rule and the motif line.

**States are things that happen to printed matter:**

| State | Treatment |
|-------|-----------|
| Tapped | *Turned and re-set*: landscape footprint, plate re-typeset on the long edge — boards never read sideways; art one step dimmer |
| Summoning sick | *The ink hasn't dried*: engraver's diagonal hatching |
| Damaged | *Tallied*: red strokes above the oval, one per point (capped at five; the accessible name carries the number) |
| Targeted / focused | *The printer's marks*: four red registration brackets, breathing gently, still under reduced motion |
| Graveyard / exile | *An etching of itself*: monochrome, unvarnished, a fine hatch letting the paper through |
| +1/+1 counters | *Counted in brass*: a brass bead, and a brass hairline on the oval; remove the counter and the brass goes with it |

Badges state facts or do not exist: an effective 0/0 permanent (a land)
shows no oval. Every visual state is also stated in the card's accessible
name.

**Card art is versioned content, not runtime fetches.**
`frontend/scripts/fetch-card-art.mjs` pulls each curated-pack identity's
art crop from Scryfall exactly once per machine (identified User-Agent,
150ms pacing, cache-first, never at runtime) into `static/card-art/`;
CardImage layers it over the procedural treatment, which remains the
automatic fallback for anything missing. Images stay untracked; rights sit
in `static/card-art/NOTICE.md` under the WotC Fan Content Policy, as do
the mana symbols in `static/mana/`.

## The Score — Replay and Study

The score is the spine; the board is the current page of the book.

- Every decision is a **line**: actor rubric, game text, a register rule
  and wash on its left edge, grouped under serif **turn rubrics**.
- Clicking a line turns the board to that moment; the scrub rail (red
  thumb) and frame steps walk it. The active line is ringed in red.
- The **margin takes marginalia**: bronze italic notes marked with a
  fleuron, pinned per trace, edited inline. The margin is also where the
  study apparatus will live (see Reserved).
- Registers in the score are currently inferred from game vocabulary — a
  presentation-level heuristic until the engine annotates its log.

---

## Reserved

Named in the contract so they land where they belong, not yet shipped:

- **The bot's mind in the margin** — manabot's weighed alternatives as
  quiet tabular numerals and hairline meters beside the score's lines,
  with one italic gloss under its preferred line; Island register. Waits
  on policy data reaching the frontend.
- **Identity pips** — a small mana-colored glass point on the plate's
  motif line. Waits on color identity in the pack manifest.
- **Held cards, tipped in** — parchment photo corners mounting the hero's
  hand to the page.

---

## Fundamentals

**Typography** — three pillars, bundled locally: Cormorant Garamond
(display, rubrics, marginalia, plate names at readable sizes), Lato (body,
controls, small plate names), JetBrains Mono (notation: tempo markings,
rubrics, actor labels). Headings read in bronze; they do not shout.

**Spacing** — 4pt grid (`xxs` 2 … `xxxl` 32). **Radii** — `sm` 4 / `md` 8 /
`lg` 12 / `xl` 16 / `full`. **Hit targets** — 32px desktop, 44px touch
(≤640px). **Z-layers** — dropdown 100, scrim 190, modal 200, toast 300,
tooltip 400.

**Interaction states** — every interactive element defines rest, hover
(`action-hover`, or a border turning `action`), focus (the global
Mountain-red ring), and disabled (reduced ink with borders retained, never
whole-panel opacity). Transitions run 100ms ease-out; all motion collapses
under `prefers-reduced-motion`.

**Accessibility** — contrast is enforced by tooling, not by a table in
this file: `npm run validate:contrast` checks every documented pair (37 at
last count) straight from `app.css` and fails under AA. Status is never
color-only; state-bearing objects carry their state in the accessible
name; scrollable regions are keyboard-reachable; the scrubber is labeled.

---

## Verification

Before merging UI changes:

- [ ] Components speak only the semantic vocabulary
- [ ] `npm run validate:contrast` passes
- [ ] `npm run check` and `npm test` pass; the e2e suites pass
- [ ] Overlays on card art use literal colors, not adaptive tokens
- [ ] New colored elements are assigned by register, not by taste
- [ ] Dense boards hold: `DENSE_BOARD=1 npx playwright test e2e/dense-board.spec.ts` and read the captures
- [ ] Both modes checked by eye — parchment and lamplight

---

## Lineage

Sepia Etude replaced the shared Loopflow/kata cream-and-burgundy palette
on 2026-07-16, chosen from a ten-direction exploration seeded by WUBRG and
four reference cards (Lotus Cobra ZNR showcase, Growth Spiral, Deathsprout,
Time Warp Mystical Archive). The Sheet, the Plate, and the Score landed
the same day from a prototype round reviewed by three simulated design
panels; the founding thesis — the active decision burns Mountain red — was
diluted once (prompts briefly spoke Island) and restored by the Sheet. The
token architecture and typography descend from the Loopflow contract.
