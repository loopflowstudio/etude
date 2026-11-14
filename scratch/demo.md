# Hanabi handoff

Use [docs/hanabi.md](../docs/hanabi.md) from the repository root. The example is
one file: a tiny shared PyTorch policy, complete episode saves and a pure
benchmark whose playing function receives `Match[Deal]`. Begin a fresh run
with a new `--name`, interrupt once, then resume without repeating config.

The hands-on demo did not happen: this run was headless, with nobody present.
No reactions or approval are inferred. PR #103 remains draft; full acceptance
belongs to gate.

Headless receipt, 2026-11-13: OpenSpiel 2.0.2 installed/imported on macOS arm64
and in a Linux amd64 Python 3.12 container. The actual example used two
players, one color, three ranks, two-card hands and three lives. A signal sent
to the CLI after episode 2 was observed after episode 3; it saved and exited
75. Resume used its stored eight-episode config and finished episodes 4–8.
The reader returned 8 rows, 2 writers and absolute updates=8. Evaluation ran
8 matches and stored 16 team rows, scores 0–2, all in group `all`. A second
pass reported `Hanabi team: 0 new`. The report rendered successfully.

Evidence remains at
`/Users/jack/.local/share/etude-demos/exp-56-hanabi-small/track/reports/hanabi.html`.
Set ETUDE_HOME to that path's ancestor `exp-56-hanabi-small` to read the receipt;
its `team-seed-0` run is finished. Use another name for a fresh interactive run.
No demo process, server or watcher remains. The earlier wrapper-targeted signal
probe finished before interruption and is separate in `exp-56-hanabi`; the
successful receipt targets the actual CLI process.
