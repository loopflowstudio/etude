# Developer references

These references help maintainers trace Etude's implementation, persistence and operational behavior while preserving the complete API details needed for debugging.

- [Training](training.md): continuation, state ownership, checkpoints and layouts.
- [Tracking](tracking.md): clocks, metric storage, reports and retention.
- [Evaluation](evaluation.md): identities, scheduling, transport and durable results.
- [Launcher](launcher.md): providers, guardians, database migrations and measurements.

Authors building a prototype should start with [getting started](../getting-started.md).

## Checks

Read [TESTING.md](../../TESTING.md) before running checks. Never induce resource
pressure for a test or benchmark. Use bounded fixtures and count behavior.

```sh
uv sync
uv run pytest
uv run mypy
uv run ruff check .
uv run ruff format --check .
```
