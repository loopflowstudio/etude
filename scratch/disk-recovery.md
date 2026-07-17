# Bound per-worktree build storage

Etude worktrees currently duplicate roughly 700 MiB Python environments, but
those disappear with the worktree and uv already shares package downloads. The
unbounded part is Rust debug output: every managym checkout can retain a full
incremental compiler graph even though agent worktrees usually build once.

Use Cargo's line-table debug format and disable incremental state for dev and
test profiles. Preserve debug assertions because CI and managym's invariants
depend on them.
