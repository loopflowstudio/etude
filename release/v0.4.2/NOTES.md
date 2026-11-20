# v0.4.2

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.4.2 adds AWS EC2 and Lambda Cloud GPU rentals and lets you use spot capacity on EC2, Vast and Lium. Losing a spot machine after work starts preserves the run's retry budget, and Etude can clean up rentals even when provider details are unreadable. Evaluation reports can now put curves with different scoring rules in the same section. ([#124](https://github.com/loopflowstudio/etude/pull/124), [#125](https://github.com/loopflowstudio/etude/pull/125), [#128](https://github.com/loopflowstudio/etude/pull/128))

## Rent from more providers and recover from spot interruptions

Choose a GPU and resource requirements for EC2, or use Lambda Cloud with separate launch and termination credentials. Rental deadlines still govern cleanup, including when a machine never finishes starting. ([#125](https://github.com/loopflowstudio/etude/pull/125))

- Register EC2 with `etude provider set aws --kind ec2` using the AWS credentials S3 storage already reads. Specify a GPU such as `--gpu L4` and the GPU count, vCPUs and memory you need. Etude selects the cheapest matching x86_64 instance type, shows it in `availability` and includes disk and public address charges in the AWS quote. ([#125](https://github.com/loopflowstudio/etude/pull/125))
- Register Lambda Cloud with `--kind lambda` and separate launch and termination keys. EC2 and Lambda arm host timers before downloading the worker. On EC2, Etude confirms instance termination and session volume removal before releasing the rental. ([#125](https://github.com/loopflowstudio/etude/pull/125))
- Set `--spot` on an on-demand template for EC2, Vast or Lium. If the machine disappears after the attempt starts, Etude records `lost (spot_machine_lost)` and retries on a fresh rental. The lost attempt still spends hours and dollars but uses none of `max_attempts`. A loss before the attempt starts still counts. `etude status` shows how many attempts the run has used. ([#125](https://github.com/loopflowstudio/etude/pull/125))

## Terminate rentals that cannot finish starting

A malformed Lium pod description could leave a paid rental running until the provider removed it. Etude now retains the pod ID independently of the address, hardware and price, so termination can proceed even when those details are unreadable. RunPod and Vast use the same separation between identity and allocation details. ([#124](https://github.com/loopflowstudio/etude/pull/124))

- A machine stays `starting` until its worker reports startup. The scheduler records a `machine readiness` diagnostic when it cannot read allocation details, and `etude terminate` can delete the machine using its recorded ID. ([#124](https://github.com/loopflowstudio/etude/pull/124))
- Startup cleanup now defaults to the rental deadline. Set `--startup-seconds` for an earlier cutoff. When a Lium create reply is lost and Etude cannot find the pod, it keeps reconciling until one hour after the rental deadline before settling the request as nothing rented. ([#124](https://github.com/loopflowstudio/etude/pull/124), [#125](https://github.com/loopflowstudio/etude/pull/125))

## Compare different scores in one report

A report can show a pooled rate and a mean side by side while retaining the section's default score for other panels. ([#128](https://github.com/loopflowstudio/etude/pull/128))

- Give a curve its own `Score` to select its table, numerator and denominator. The report index displays the first curve that uses the section score, or the first curve if every curve specifies its own score. ([#128](https://github.com/loopflowstudio/etude/pull/128))

## Operational notes

- Run `etude database upgrade` where the launcher database lives. The additive `spot_rentals.sql` migration records spot rentals and reads existing rentals as standard-price rentals. ([v0.4.1 release notes](https://github.com/loopflowstudio/etude/releases/tag/v0.4.1), [#125](https://github.com/loopflowstudio/etude/pull/125))
- EC2 costs in `etude machines` are marked with `~` because AWS bills arrive about a day later. If Etude detects a spot loss late, the estimate uses the quoted rate through the time the loss was detected. ([#125](https://github.com/loopflowstudio/etude/pull/125))

## Small changes

- Lium timestamps without a timezone are read as UTC, and Etude recognizes Lium's published terminal pod states. ([#124](https://github.com/loopflowstudio/etude/pull/124))
- Vast compresses its bootstrap environment so signed URLs issued with session credentials fit within the provider's environment size limit. ([#125](https://github.com/loopflowstudio/etude/pull/125))