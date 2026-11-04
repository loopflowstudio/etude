# v0.2.14

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.14 adds Vast.ai GPU rentals alongside RunPod, using the same launch, queue, wait and fetch workflow. You can choose another provider without rewriting jobs. The release also makes the limits of rental estimates explicit: quotes describe planned costs, while acquisition recovery and deadline cleanup account for uncertain provider responses.

## Choose Vast.ai for GPU jobs

Vast.ai is available through the CLI and Python API. It shares package preparation and bootstrap behavior with RunPod, so provider choice fits into the existing launcher workflow. ([#79](https://github.com/loopflowstudio/etude/pull/79))

- **Quotes include disk costs.** Eligible offers are verified, on-demand, single-GPU amd64 machines with free upload and download.
- **Inspect capacity before renting.** With storage configured and `VAST_API_KEY` injected, register the provider with `etude provider set vast --kind vast --storage west`, then add a template following `docs/launcher.md`. Use `etude availability rtx4090 --timeout 30` and `etude launch job.py --rent rtx4090 --quote` to inspect compatible capacity and planned cost without renting.
- **Provider choice is explicit.** There is no cross-provider fallback. Excluding offers with metered transfer can leave work waiting.

## Recover uncertain rentals without losing ownership

A lost create reply must not erase the launcher's record of a possible rental. Vast acquisition records intent before sending one create request, then uses paginated inventory to recover a lost reply. ([#79](https://github.com/loopflowstudio/etude/pull/79))

- **Uncertainty retains reservations.** Capacity stays reserved while acquisition is unresolved; permanent uncertainty can hold capacity indefinitely.
- **Deletion requires ownership evidence.** Recovery does not authorize deleting an instance without evidence that it belongs to the acquisition.
- **Deadline protection starts before installation.** The standard-library guardian starts before client dependencies install and uses instance-scoped credentials. The account API key stays on the controller.

## Operational notes

- **Vast requires a public image and an account SSH key.** See `docs/launcher.md` for setup.
- **Planned costs are not guaranteed bill caps.** Charges are reconciled only through the provider's reported billing interval. Provider outages or machines that never boot can delay deletion and exceed rental budgets. External storage charges are excluded.
- **Paid operation remains unverified live.** Read-only validation parsed a compatible offer and an empty inventory search. Paid creation, boot, output delivery, cleanup and nonempty billing still require a separately authorized paid pilot, with retained output, confirmed deletion and provider-reported charges as evidence.
- **Recorded checks passed.** Network-isolated tests reported 589 passed, 6 skipped and 3 deselected; mypy, Ruff lint and formatting, and prepared-image CI passed. Controlled fixtures cover execution, retained outputs, cancellation, acquisition recovery, ownership checks and provider call counts. These are the PR's recorded results, not checks rerun for this release note.
- The release lists no compatibility break.

## Small changes

- Quote output and launcher documentation now describe planned rental costs rather than guaranteed budget caps.