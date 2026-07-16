# Port the Loopflow visual system into Etude Fantasia

> "just literally copy it and get it live in this branch"

> "if loopflow and kata are slightly off invest in trying to make them all align somehow. if theyre totally off just default to loopflow"

## What to build

Make Etude Fantasia a full member of the Loopflow/kata visual family by adopting the shared visual-design contract, fonts, adaptive cream/slate palette, burgundy accent, semantic status colors, spacing, radii, focus states, and reduced-motion behavior across Play and Replay.

## The demo

Run `./scripts/play`, open Play and Replay, and see the existing game surfaces rendered in Cormorant Garamond, Lato, and JetBrains Mono over the shared cream/slate palette. Start a game and verify that prompts, actions, board focus, status, and terminal overlays remain legible and distinct.

## Source of truth

- Loopflow `VISUAL_DESIGN.md` at commit `1e68ba107` is copied into this repository as `VISUAL_DESIGN.md`.
- Loopflow's current `DesignSystem.swift` and `BrandColors.swift` supply the canonical fonts and tokens.
- Kata confirms the same palette and spacing system; its lifted dark-mode burgundy (`#9B4A54` / `#B25A64`) resolves Loopflow's dark contrast gap and is the only deliberate reconciliation.

## Constraints

- Preserve Etude's board geometry, card treatments, protocol semantics, and accessibility labels.
- Styling must remain local and offline-capable; bundle the same font files Loopflow ships.
- Use semantic CSS variables and Tailwind theme aliases so Play, Replay, and shared components cannot drift.
- Existing screenshot baselines must be regenerated deliberately after browser verification.

## Done when

- `npm --prefix frontend run check`
- `npm --prefix frontend test -- --run`
- `npm --prefix frontend run build`
- `uv run --extra dev pytest -q tests/etude`
- Playwright's play, replay, accessibility, offline-pack, and release visual checks pass.
- The rendered experience visibly matches the shared design contract in both light and dark system appearances.
