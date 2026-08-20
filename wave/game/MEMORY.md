# Game memory

## North star

- Etude Fantasia grows toward Avatar Cube Team Sealed: two teams build three
  decks from shared pools, play the full three-by-three deck matchup matrix to
  five wins, and can study every recorded game afterward.
- Study is a named Game mode, not an independent product wave. Construction,
  play, replay, Retry, and comparison are one player loop.
- The Avatar starting values—540 cube cards, 135 cards per team, three
  40-card-minimum decks, unlimited basics, deck-specific sideboards, and five
  wins—are versioned format parameters rather than engine constants.
- The first robot team may use fixed authored decks. Manabots initially pilot
  without sideboarding; sealed-pool deck construction is an important later
  Intelligence capability, while drafting is separate.
- Discord is the assumed human communication layer. Do not build chat.
- This destination guides interfaces and sequencing but does not justify a
  speculative Team Sealed backlog before one polished play-to-Study loop works.

## Decisions

- Renamed from `gui` to `game` on 2026-07-15 because the wave owns the full
  playing experience, not a rendering technology.
- The experience target is Phase-level or better smoothness, performance,
  polish, reliability, and portability for creator-selected decks.
- Preserve Etude Fantasia's differentiators: visible AI identity, decision inspection,
  research-grade traces, replay, and a deliberately tiny curated product.
- The authority seam is `ExperienceFrame`, `InteractionOffer`/`Command`,
  `PresentationEvent`, and `RecoveryEnvelope`.
- Commands bind to revision + prompt + offer and carry a stable command ID.
- Presentation consumes semantic events; it does not infer meaning by diffing
  arbitrary snapshots.
- Canonical replay exposes a stable address for every historical player
  decision. Study may rank highlights, but it does not define or reconstruct
  the replay timeline.
- GAM-7 / PR #177 shipped the first live belief-conditioned advice seam on
  2026-07-19. A surfaced prompt receives its canonical `ed2` identity before
  the pilot acts, and that exact identity promotes into replay/Study; committed
  summaries resolve directly from decision rows rather than rebuilding replay
  history or downgrading to frozen `erd1` identities.
- As of 2026-08-20 the repository also contains the exact Study interaction
  that the earlier memory still described as next: retained-root fork, Retry,
  sealed reveal, bounded preview, exact return, and participant-local branches
  that do not pause the live match. The Testing House surface includes pilot
  and watcher roles, private and shared reads, role transfer, and terminal
  Study. Do not file another generic fork/Retry/return task without a narrower
  observed gap.
- The admitted live-advice slice remains deliberately narrow: Interactive
  mirror, player 0, and the server-authored Has/Lacks Counterspell conditions
  over one pinned tracked posterior. Advice is participant-authenticated,
  bounded off the match lock, and observational; ActionPanel remains the only
  live `Command` path.
- The next source-verified player-facing gap is mana display. The Rust
  observation currently exposes only aggregate `combat_mana`; Etude and the
  shared frontend `PlayerState` expose no mana-pool field. Keep this as a thin
  live/replay/Study presentation slice rather than inferring mana in the
  browser.
- Offline command queues must not replay gameplay decisions into a newer state.
- Curated assets are versioned content, not opportunistic runtime fetches.
- WASM is deferred until adapter benchmarks show a product benefit.

## Evidence

- `docs/research/phase-experience.md`
- `docs/architecture/experience-protocol-v1.md`
- GAM-7 / PR #177, merge `e61931e75024e61e90d1d83337e80a9bfcf8e422`:
  parent-reviewed live/Study identity and non-mutation proof; 17 focused Python
  tests, full debug `cargo test`, and 10/10 exact-head CI.
- `etude/study_branch.py`, the Study attempt routes in `etude/server.py`,
  `tests/etude/test_study_runtime.py`, `tests/etude/test_testing_house.py`, and
  `frontend/e2e/testing-house.spec.ts` are the 2026-08-20 source evidence for
  Retry/return and shared-table behavior.
- `cargo test --manifest-path managym/Cargo.toml --test authored_match_tests`
  passed all six debug tests on 2026-08-20. The ignored local PyO3 module can
  lag Rust source; if Python tests report `compiled_semantics` as absent while
  these Rust tests pass, rebuild the native module using the repository-root
  AGENTS.md procedure before treating the result as a Game regression.
- Legacy implementation notes in `01-play-interface.md` and `05-polish.md`
- Previous charter in `legacy-gui-charter.md`

## Open tensions

- Keep protocol design ambitious without blocking a thin vertical slice.
- Preserve the useful existing Svelte/FastAPI table while replacing its
  snapshot/action seam incrementally.
- Once valid Project/Task state is available, prefer the observed mana-display
  gap or a higher-priority filed KR over duplicating the existing
  fork/Retry/return surface or broadening advice to arbitrary worlds.
- Treat visual authorship as a product requirement without creating a generic
  content platform.
