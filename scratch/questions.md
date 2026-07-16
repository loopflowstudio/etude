# W2-205 assumptions

- “Clean machine” means a documented, newly provisioned reference host with the
  required OS-level tools and browser already present, but a fresh repository
  checkout with no `.venv`, `frontend/node_modules`, native extension,
  `managym/target`, traces, browser profile, or project package caches. The
  under-60-second clock starts when the one supported repository command is
  invoked and includes locked Python dependency installation, the native build,
  locked frontend installation, both local servers, browser navigation, and the
  first legal action in the default Search-64 matchup. It excludes cloning the
  repository and installing the host OS, `uv`, Node/npm, Rust/Cargo, and the
  browser. This is the stricter computable reading of the Task; the pursue phase
  must not move dependency installation outside the clock if the first
  measurement misses the budget.

- The inherited W2-185 `offline-pack.spec.ts` full-game/replay gate currently
  fails on this Task's untouched base after the trace is saved and `/api/traces`
  returns 200. The current replay route enters Svelte's
  `effect_update_depth_exceeded` loop in `PresentationPlayer.recover`, leaving
  its already-completed trace fetch rendered as “Loading traces…”. W2-205 does
  not modify that presentation/replay seam under directive v3; its bounded
  offline proof covers the required live-session reload and post-reload action.
