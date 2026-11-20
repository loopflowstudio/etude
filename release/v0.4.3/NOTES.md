# v0.4.3

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.4.3 lets runs sharing a rented machine fund it together. When several seeds fit on one machine, each can budget for its share of the rental. `etude launch --quote` shows what each run would pay and which runs would wait for a place. ([#131](https://github.com/loopflowstudio/etude/pull/131))

## Fit more seeds into a rental budget

Launch and quote divide the cost of occupied time among the runs holding places on the machine, including peers already on the rental. Etude accounts for setup time and the provider’s billing increments when deciding whether the budgets cover a placement. ([#131](https://github.com/loopflowstudio/etude/pull/131))

- In the shared-rental example, three one-hour seeds with $0.23 each can run together on a $0.69/hour machine for their full hour. A seed launching alone at that budget is refused. With four such seeds, three launch and the fourth queues. ([#131](https://github.com/loopflowstudio/etude/pull/131))
- A run that finishes early, is cancelled, or fails keeps its place through its attempt limit while peers are active, preserving their shares. All places end when the machine empties. ([#131](https://github.com/loopflowstudio/etude/pull/131))
- Quotes identify training runs and tournaments that would queue. The quote total includes any rental extension funded by waiting runs. ([#131](https://github.com/loopflowstudio/etude/pull/131))

## Operational notes

- When `etude tick` picks up a queued attempt, its time limit still uses the full hourly rate. The attempt can end as `over_budget`. ([#131](https://github.com/loopflowstudio/etude/pull/131))
- An automatic retry on a second machine can take a run past `max_usd` by up to the remaining share of its first place. Admission assumes the quoted provider rate and setup within the configured allowance. ([#131](https://github.com/loopflowstudio/etude/pull/131))