# v0.2.2

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.2 has two changes:
- A resumed run can say where it stands when that is not its latest checkpoint.
- A running attempt's log is sent to storage once, as appended pieces, instead of being re-uploaded whole every 15 seconds.

Nothing breaks on upgrade from 0.2.1.

## A resumed run can say where it stands

Since 0.2.1, a resumed attempt's rows replace earlier attempts' rows from the run's latest checkpoint on. A process that restored its state from somewhere else had no way to say so, and the rows between the checkpoint and its real position were dropped or doubled. Examples are a snapshot of its own taken after that checkpoint, or a later stage that continues the same run. Such a process now passes its position:

```python
run = track(
    experiment="e",
    run="r",
    clocks=[Step("step", "steps")],
    resumed_at={"step": 13},
)
```

Keys must be declared clocks; anything else raises `resumed_at_unknown_clock`. Without `resumed_at`, the latest checkpoint is used as before. ([#40](https://github.com/loopflowstudio/etude/pull/40))

## Attempt logs are sent once

The courier used to re-upload an attempt's whole growing log to `attempts/ATTEMPT.log` every 15 seconds, so a 12-hour run uploaded its log hundreds of times.

- New output is sealed into immutable pieces at `attempts/ATTEMPT.log/OFFSET`, named by their first byte offset.
- `AttemptRecord.log_ends` lists where each piece ends, and a reader fetches only the pieces the record names.
- The first output goes out at once. After that, a piece waits about a twentieth of the attempt's age, between 15 seconds and 5 minutes.
- A 12-hour run now makes about 200 log uploads instead of about 2,880.

Readers see the same log text as before. ([#38](https://github.com/loopflowstudio/etude/pull/38))

## Known limits

- The session's `out/worker.log`, bounded at 1 MiB, is still re-uploaded whole every 15 seconds.
