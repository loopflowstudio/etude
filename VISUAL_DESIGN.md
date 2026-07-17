# Etude Fantasia Visual Design System — Sepia Etude

The design contract for Etude Fantasia's player-facing surfaces (Play,
Replay, and everything that grows from them). It inherits its token
architecture from the Loopflow contract; its visual identity is Etude's own.

---

## Brand Foundation

### Concept

**An annotated score in an old library.** The interface is sepia ink on aged
paper — quiet, studied, monochrome — except the active decision, which burns
Mountain red. The palette is drawn from Magic itself: card-frame parchment,
and the five mana colors reduced to library volume.

| Visual Choice | Brand Signal |
|---------------|--------------|
| Parchment grounds | A card's text box; the game as an open book |
| Sepia-bronze display text | Margin annotations, studied notes |
| One Mountain-red accent | The live decision is the only urgent thing |
| WUBRG color pie | The five mana colors syntax-highlight the interface |
| Fixed dark card art | The cards are the illustrations on the page |

### The two metals

- **Bronze** (`--accent-text`) is for reading: headings, the brand mark, the
  decision-prompt annotation.
- **Mountain red** (`--accent`) is for acting: buttons, focus rings, links,
  selected and clickable states.

Errors share the Mountain-red family — urgency has one color here — while
decision prompts speak Island blue, so an error surface is never mistaken
for a prompt.

---

## Colors

### Brand constants

| Token | Hex | Usage |
|-------|-----|-------|
| `mountain-red` | `#973427` | The accent; see adaptive values below |
| `sepia-bronze` | `#6D5A35` | Display/annotation tone; see below |
| `ivory` | `#F8F1E0` | Text on accent fills; constant across modes |

### Grounds: page, panel, field

Every mode has three ground levels so bordered things separate by value, not
just by outline: the **page** (`bg`), **panels** on it (`surface`), and the
**field** fill inside bordered controls — selects, inputs, action options,
life chips (`field`). Zone internals additionally take their register's wash
(see the color pie below).

### Light mode (Parchment)

| Token | Hex | Usage |
|-------|-----|-------|
| `bg` | `#EBDFC6` | Main page background |
| `surface` | `#F7F0E0` | Elevated cards, panels |
| `muted` | `#DED0AF` | Secondary surfaces, chips |
| `field` | `#FCF9EE` | Paper-white fill inside bordered controls |
| `border` | `#C9B892` | Borders, dividers |
| `border-strong` | `#A5926A` | Emphasized borders |
| `text` | `#3A3122` | Primary text |
| `text-secondary` | `#5F553D` | Secondary text, captions |
| `accent` | `#973427` | Buttons, focus, links |
| `accent-hover` | `#AB4634` | Hover states (lighter) |
| `accent-text` | `#6D5A35` | Headings, annotations |
| `accent-soft` | `#EAD6C6` | Accent-tinted washes |

### Dark mode (The library after dark)

| Token | Hex | Usage |
|-------|-----|-------|
| `bg` | `#191510` | Main page background |
| `surface` | `#221C14` | Elevated cards, panels |
| `muted` | `#2B241A` | Secondary surfaces, chips |
| `field` | `#2F2717` | Lifted fill inside bordered controls |
| `border` | `#423A2B` | Borders, dividers |
| `border-strong` | `#625741` | Emphasized borders |
| `text` | `#ECE4D0` | Primary text |
| `text-secondary` | `#B3A88E` | Secondary text, captions |
| `accent` | `#B24D38` | Buttons, focus, links |
| `accent-hover` | `#A34531` | Hover states (**darker** — see note) |
| `accent-text` | `#C3A568` | Headings, annotations (lamplight bronze) |
| `accent-soft` | `#3F2B20` | Accent-tinted washes |

> **Dark hover deepens.** A red lighter than `#B24D38` cannot hold ivory
> button text at WCAG AA, so the dark-mode hover moves down, not up.

### The color pie — WUBRG as syntax highlighting

The five mana colors sit on the sepia ground the way a syntax theme sits on
an editor: each family owns a semantic register, and each carries roughly a
fifth of the interface's color. Base values are shared across modes; each
family ships a contrast-safe text companion (`*-text`) that adapts per mode.
Washes come free from alpha modifiers on the base (`/10`–`/30`).

| Token | Mana | Base | Light text | Dark text | Register |
|-------|------|------|------------|-----------|----------|
| `warning` | Plains | `#9F7A1C` | `#6F5712` | `#D9BC66` | Order & structure: turn/phase line, villain log, reconnecting, targeting |
| `info` | Island | `#2E6DA8` | `#31599A` | `#8AB8E8` | Knowledge: decision prompts, the stack, hand headings, auto-passing |
| `neutral` | Swamp | `#6A5F86` | `#554B6E` | `#B4A9D1` | The hidden & the spent: graveyard, exile, hidden cards, game over |
| `error` | Mountain | `#973427` | `#7E2A20` | `#DE9181` | Action & urgency: shares the accent — buttons, focus, failures, damage |
| `success` | Forest | `#4C8040` | `#3A6531` | `#9ECB8B` | Presence: battlefield heading, connected, your move, hero log |

Balance rule: no family should visibly dominate the chrome. Red gets the
interactive surfaces, so it needs no additional decoration; when adding a
new colored element, give it to the register it belongs to, not the one
that looks best.

**Ink discipline.** Text is two inks — `text` and `text-secondary` — plus
the bronze display tier. The pie speaks through container washes and
borders, never through body text: the turn line is ink on a sun wash, log
entries are ink on actor-tinted grounds. The `*-text` companions exist for
the rare moments a family must speak in text (error banners); treat each
new use as an exception to justify.

**Washes are tokens, not arithmetic** *(adopted; implementation pending)*.
Each family ships a `--{family}-wash` override per mode — roughly 10% of
the base over the light ground and 22–28% over the dark, lifted until the
wash reads at 3:1 against `bg` as a non-text boundary. Until those tokens
land, alpha washes in components must be verified in dark mode by eye —
`/10` over the umber grounds all but disappears.

**Hidden information is counted, not drawn.** A hidden zone shows its
count (the heading already carries it) and renders hidden cards as
material — the Swamp-violet frame, unlabeled; a caption saying "HIDDEN"
restates what the frame already says. Unknown information never occupies
more space on the page than known information.

**Empty states are silent.** An empty zone is one short phrase in
`text-secondary` on the zone's own ground — no border, no chip, no dashed
frame. Placeholders never restate what a heading already says.

### Token source of truth

Token values are stated once, in the mode tables above.
`frontend/src/app.css` is the single source of truth for the
implementation; this document never restates hex values outside the
tables. If a table and `app.css` disagree, `app.css` wins and the table is
the bug. Semantic tokens are defined on `:root` and overridden wholesale
in the `prefers-color-scheme: dark` block; the Tailwind `@theme` aliases
(`slate-*`, `blue-*`, `emerald-*`, …) resolve through them, so components
never name raw colors.

### Card art is not themed

Curated-pack art palettes are fixed, dark-toned colors. Overlays on art (name
plates, power/toughness badges) use literal dark scrims with literal light
text and must never route through adaptive tokens — in light mode an adaptive
scrim turns parchment and erases the text. `visual-system.spec.ts` pins this.

Badges state facts or do not exist: power/toughness renders only on
creatures — a `0/0` on a land is ornament. Name plates stay horizontal at
every size; a name the player cannot read is a name removed.

---

## Typography

Unchanged from the prior contract: three pillars, bundled locally in
`frontend/static/fonts/`.

| Role | Font | Variable | Usage |
|------|------|----------|-------|
| Serif | **Cormorant Garamond** | `--font-serif` | Headlines, brand, zone headings |
| Sans | **Lato** | `--font-sans` | Body text, buttons, UI |
| Mono | **JetBrains Mono** | `--font-mono` | Turn line, action types, technical |

Headings render in `--accent-text` (bronze), not the accent red — display
text reads, it does not shout.

**Notation, not enumeration.** Mono is a register for game notation, never
a pipe from the engine. Player-facing surfaces write human notation
("Turn 5 · Precombat Main"); raw engine identifiers
(`PRECOMBAT_MAIN_STEP`, `PRIORITY_PLAY_LAND`) belong to Replay/Study
surfaces or a developer toggle, where studying the machine is the point.
Identical legal actions coalesce with a count ("Play Island ×3").

### Font weights

```
Cormorant Garamond: 400, 500, 600 (600–900 alias to SemiBold)
Lato: 400, 700 (700–900 alias to Bold)
JetBrains Mono: 400 (400–700 alias to Regular)
```

---

## Spacing

4pt grid, unchanged.

| Token | Value | Usage |
|-------|-------|-------|
| `xxs` | 2px | Hairline gaps |
| `xs` | 4px | Tight spacing |
| `sm` | 8px | Small gaps |
| `md` | 12px | Default padding |
| `lg` | 16px | Section padding |
| `xl` | 20px | Large gaps |
| `xxl` | 24px | Section margins |
| `xxxl` | 32px | Hero spacing |

## Corner Radius

| Token | Value | Usage |
|-------|-------|-------|
| `sm` | 4px | Inline code, small badges |
| `md` | 8px | Buttons, cards, chips |
| `lg` | 12px | Large cards |
| `xl` | 16px | Modals, beat overlays |
| `full` | 9999px | Pills |

## Hit Targets

| Context | Size |
|---------|------|
| Desktop minimum | 32×32px (enforced on buttons/inputs) |
| Touch/mobile ≤640px | 44×44px |

## Z-Index Layering

| Layer | Value |
|-------|-------|
| Base | 0 |
| Dropdown | 100 |
| Scrim | 190 |
| Modal | 200 |
| Toast | 300 |
| Tooltip | 400 |

The scrim is a full-viewport layer under every modal:
`color-mix(in srgb, var(--text) 45%, transparent)`. Page content is never
dimmed by reducing its own opacity.

## Animation

Transitions run 100ms ease-out on interactive elements. All motion collapses
under `prefers-reduced-motion: reduce`; presentation beats also expose
`data-reduced-motion` for tests.

## Interaction States

Every interactive element defines its states from tokens: rest, hover
(`--accent-hover`), pressed (accent darkened ~8%), focus (the global
Mountain-red ring), and disabled — 40% ink on `field` with borders
retained, never whole-panel opacity. Loading and reconnecting states speak
Plains.

---

## Accessibility

### Focus states

Focus is Mountain red everywhere — the global `:focus-visible` outline and
component-level rings both resolve to `--accent`:

```css
:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
```

### Color contrast

Every pairing below is validated (WCAG AA needs 4.5:1 for text, 3:1 for
non-text). The full matrix lives in the palette validator used during design.

| Combination | Ratio | Grade |
|-------------|-------|-------|
| `#3A3122` on `#EBDFC6` (light text/bg) | 9.7:1 | AAA |
| `#5F553D` on `#DED0AF` (light secondary/muted) | 4.8:1 | AA |
| `#6D5A35` on `#EBDFC6` (bronze display/bg) | 5.0:1 | AA |
| `#F8F1E0` on `#973427` (ivory on accent) | 6.6:1 | AA |
| `#ECE4D0` on `#191510` (dark text/bg) | 14.3:1 | AAA |
| `#F8F1E0` on `#B24D38` (dark ivory on accent) | 4.7:1 | AA |
| `#C3A568` on `#191510` (dark bronze/bg) | 7.7:1 | AAA |

### Semantics

- Icon-only and state-bearing controls carry accessible names; card state
  (tapped, summoning sick, damage) is in the card's `aria-label`.
- Status is never color-only: chips carry text, logs carry actor names.
- The game log records game vocabulary only — actor and action. UI
  affordances (key hints, control labels) never enter the record.
- Scrollable regions are keyboard-reachable; the replay scrubber is labeled.

---

## Verification Checklist

Before merging UI changes:

- [ ] Uses semantic tokens or theme aliases, never raw hex in components
- [ ] Overlays on card art use literal scrim colors, not adaptive tokens
- [ ] Buttons meet hit targets (32px desktop, 44px touch)
- [ ] Focus states visible and Mountain red
- [ ] Respects `prefers-reduced-motion`
- [ ] Color contrast meets WCAG AA in **both** modes
- [ ] Headings bronze (`--accent-text`), actions red (`--accent`)
- [ ] `visual-system.spec.ts` passes (palette, fonts, hit targets, art plates)

---

## Lineage

Sepia Etude replaced the shared Loopflow/kata cream-and-burgundy palette on
2026-07-16, chosen from a ten-direction exploration seeded by WUBRG and four
reference cards (Lotus Cobra ZNR showcase, Growth Spiral, Deathsprout, Time
Warp Mystical Archive). The token architecture, typography, spacing, and
accessibility contract carry over from the Loopflow system unchanged;
`frontend/src/app.css` remains the single source of truth.
