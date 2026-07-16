# W2-204 verification boundary

The supplemental development-stack Playwright suite currently fails its two
replay-loading cases after `/api/traces` returns HTTP 200. The existing replay
route enters Svelte's `effect_update_depth_exceeded` loop in
`frontend/src/lib/presentation.svelte.ts`, leaving the trace list at
"Loading traces...". W2-204 does not change that presentation/recovery path,
and directive v4 explicitly excludes the W2-195 recovery and replay-equivalence
semantics that own it.

The W2-204 complete scoped gate remains the GUI Python suite, frontend check,
unit tests, production build, and the release-only terminal prompt-family
matrix. All are green. Do not broaden this Task to repair the pre-existing
development replay failure.
