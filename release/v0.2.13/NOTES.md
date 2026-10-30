# v0.2.13

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.13 lets a run move from a RunPod pod to an SSH host that has no storage. Before this release, if a pod left a run's checkpoint in its bucket and you continued or retried that run on an SSH host registered without storage, the host never received the checkpoint. Every attempt ended `setup_failed` until the retries ran out. Now the controller relays the checkpoint to the host from the bucket. An input that can never arrive ends its attempt once, as `input_undelivered`, and the log says why, so retries are no longer spent on it.

## Continue bucket-held runs on any machine

A machine that does not keep records in the storage holding a run's folder now gets the run's file from the controller. This covers an SSH host with no storage and a host with a different storage. A machine that shares the holder's storage still reads the file there directly. ([#77](https://github.com/loopflowstudio/etude/pull/77))

- **The controller stages the file from the bucket.** `BucketJournal.held` makes one listing of the run's folder, reads the declaration and checkpoint records, and reads one checkpoint file through `etude/cache.py`. It checks the file against its record and hands it over the same way a host's disk would.
- **The first relay downloads the checkpoint once.** Later relays of the same checkpoint read the controller's cached copy and download nothing more.
- **SSH reports missing files as absent.** When a file is missing, an SSH host now reports it as absent (`FileNotFoundError`) instead of failing the call. This lets the relay tell a file that is gone from a host that cannot be reached.
- **No extra queries while waiting.** The relay loads a session only when the attempt has a run folder to carry. A wait that relays nothing makes no extra database queries.

## Stop retrying inputs that cannot arrive

`input_undelivered` is a new cause for an attempt's end, and the launcher does not retry it. A retry would get the same missing or wrong bytes, so it would only spend attempts. ([#77](https://github.com/loopflowstudio/etude/pull/77))

- **When an attempt ends `input_undelivered`:** the input's record or file is gone from the holder's storage or disk; the bytes do not match the record; or a worker refuses an input digest. The digest case used to end as `setup_failed`.
- **A stored object whose length differs from its record is a mismatch.** It used to be reported as a bad range. It now ends the attempt as `input_undelivered`, like any other mismatch.
- **A session whose first work fails this way is not claimed.** If every attempt in a session's first packet ends `input_undelivered`, the session is confirmed ended.
- **Temporary failures still wait.** `OSError`, a mismatch, `LauncherError` and `StorageUnavailable` make a relay wait instead of ending. These are now listed once in the scheduler (`_RELAY_WAITS`). Creating a session and handing work to a kept session therefore wait on the same failures. While it waits, the supply's trouble note reads "waiting for the machine or storage holding a run's folder".

## Operational notes

- **Only a run input ends early.** When a retry's full restore set has a missing or mismatched file, the attempt still waits instead of ending. Only an `Input.run` ends the attempt as `input_undelivered`.
- **Bucket relays read through the controller.** The controller downloads each relayed checkpoint once into its cache, so it needs room for those files.
- **Not yet run live.** To confirm it, let a RunPod run leave a checkpoint in its bucket. Then run `etude continue` (or a retry) onto an SSH host registered without storage. The host's attempt log should show `inputs: verified checkpoints/NAME …`, and the job should start from that checkpoint.
- The release lists no compatibility break.

## Small changes

- `docs/launcher.md` describes the new relay sources and the `input_undelivered` outcome.