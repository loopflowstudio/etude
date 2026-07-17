# RUL-3: checked authority continuation for the authored match

Status: PR #130 is merged enablement. It does not complete RUL-3 and does not establish any Playable Curated World KR. This design records only checked evidence and scopes the continuation to one deterministic, terminal, release-stack authority receipt for UR Lessons versus GW Allies.

## Evidence standard

The Task requires one deterministic terminal authored match in which the compiled semantic runtime is the exercised production authority: every encountered behavior comes from admitted typed programs, every player decision is represented by a structured `InteractionOffer` and applied as a structured `Command`, and ordered canonical events and state witnesses agree across the required surfaces. Evidence must also make fallback absence observable; manifest selection or generic substrate tests cannot substitute for an exercised match trace.

## What merged PR #130 added

PR #130 (`25900f8db4848517aba51d78053967e96068eb08`, merge commit `731ade20f1fabde4b6cc721dabe61cbbae440136`) added one enabling vertical slice:

- Lowered the reviewed two-deck typed IR into the live Rust `ContentPack` used by the engine.
- Selected that compiled pack only when both submitted decklists exactly equal the authored UR Lessons and GW Allies lists. Other games, including frozen conformance receipts, continue to use the general/default pack.
- Added `Game::new_with_content` and made authored-match construction expose compiled manifest provenance: pack key, IR hash, and source hash.
- Added a definition-level differential test that compares every compiled semantic definition with the reviewed reference behavior after normalizing legacy-derived target and ability ordering.
- Added a deterministic seed-0 Rust test that uses `Game::random_playout` to reach a winner and checks that the run emitted at least one spell resolution, triggered ability, and damage event.
- Kept the clean-machine Play smoke path working after the upstream rebase by making the Vite warmup deterministic.

The terminal test still chooses positional action indices through `Game::step`; it does not use `structured_offers` or structured command application. Its event assertions are aggregate presence checks, not an ordered semantic trace.

## Checked evidence ledger

| Check or artifact | Checked result | What it proves | What it does not prove |
| --- | --- | --- | --- |
| `cargo test --test authored_match_tests exact_authored_match_runs_to_a_winner_on_compiled_semantics -- --exact --nocapture` | Passed in debug. | Seed 0 with the exact authored decklists selects the compiled pack, reaches a winner within 100,000 positional actions, and encounters spell-resolution, trigger, and damage event categories. | Structured decisions, invoked-program provenance, event order, zero fallbacks, surface parity, or a reusable terminal witness. |
| Full `cargo test` | Passed in debug after the final upstream integration. | The merged Rust suite, including debug-only invariants, remains green. | Match-authority KRs beyond the assertions in those tests. |
| `managym/tests/authored_match_tests.rs` | Reviewed as the authored-match evidence test. | Exact pack selection, manifest provenance, terminal positional playout, event-category presence, and compiled/reference definition equivalence are encoded as regression checks. | A structured command tape or canonical receipt. |
| `content/semantic/v1/generated/two_deck.ir.json` and `two_deck.coverage.json` | Checked-in generated artifacts consumed by the compiled pack. | The reviewed two-deck definitions have typed IR and static coverage artifacts. | Which programs were encountered or invoked during the terminal run. |
| `./scripts/verify-clean-machine` and final GitHub run `29599450555` (`Clean-machine Play`) | Clean-machine smoke passed; every final PR #130 check passed. | The release-stack Play server can install, start, accept the smoke interaction, reload, and shut down with the merged slice. | A terminal live match, structured authority throughout it, or equivalence with headless and replay surfaces. |
| `uv run --extra dev pytest -q tests/etude tests/semantic` | 185 tests passed. | The affected Etude and semantic Python surfaces remain compatible. | A compiled terminal authority receipt. |
| Existing semantic-kernel conformance and canonical replay fixtures | Passed in final CI, but intentionally use the general/default pack or pre-existing fixture paths. | Existing conformance and replay contracts did not regress. | Evidence that the authored compiled terminal match produced or reproduced those receipts. |
| Deterministic diagnostic: exact decks, environment seed 0, `random.Random(0)` positional policy; probe `structured_offers()` before each `env.step(index)` | Reached terminal winner 0 after 154 positional decisions. Encountered `Priority` 85, `DeclareAttacker` 20, `ChooseTarget` 9, `DeclareBlocker` 6, `DiscardThenDraw` 4, `LookAndSelect` 9, `PayOrNot` 1, `Scry` 4, and `Waterbend` 16. Structured offers rejected 49 decisions across seven families; the first rejection was `ChooseTarget` at decision 13 with `current decision has no structured offer support`. | A separate deterministic diagnostic exposes gaps in the engine-level experimental `structured_offers()` helper. | This is not the Rust terminal test's RNG/policy or the selected release-stack server-offer path, and every action was applied positionally. It cannot preselect an extension for the next PR; it is failure localization on a different surface, not authority evidence. |

## Unmet directive and KR evidence

### Absence of evidence

- No checked terminal trace identifies each encountered semantic program and binds it to an admitted typed-IR definition. Pack hashes prove selection, not invocation provenance.
- No terminal decision tape records a structured offer and structured command for every decision. `random_playout` applies positional action indices.
- No checked receipt records per-command pre/post state witnesses and the ordered canonical semantic events caused by each command. The merged test only counts three event categories after the match.
- No match-scoped counters demonstrate zero legacy fixed-action fallback, zero card-name dispatch, zero client-side legality authority, and zero candidate-cap fallback.
- No fixed terminal seed has been exercised through live release-stack Play, headless engine play, and canonical replay with matching witnesses and ordered consequences. Existing smoke, privacy, stale-reference, and replay tests are generic or fixture-based rather than witnesses from this compiled match.
- No checked terminal witness artifact is produced for reviewer inspection or deterministic regeneration.
- No match-scoped command p50/p95, step throughput, complete-game throughput, peak RSS, semantic-program token counts, or fallback totals have been recorded. Performance evidence should wait until the structured terminal path exists; fallback totals are required earlier because they are authority assertions.
- The selected match has not yet demonstrated every reachable prompt family through structured offers, including targeting and combinatorial combat.
- KR 5's subsequent creator-selected card/mechanic increment has not begun and is outside RUL-3's immediate authored-match proof.

### Observed failure

- The deterministic positional diagnostic reached a real `ChooseTarget` decision at ordinal 13, where the engine-level experimental `structured_offers()` helper returned `current decision has no structured offer support`.
- The same diagnostic observed 49 helper rejections in seven families: `ChooseTarget`, `DeclareBlocker`, `DiscardThenDraw`, `LookAndSelect`, `PayOrNot`, `Scry`, and `Waterbend`. This directly fails that helper for the diagnostic policy, but it does not prove that the release-stack server-offer trace has the same first gap.
- There is no observed card-name, client-legality, or candidate-cap fallback in the diagnostic because those paths are not counted. Their status remains absence of evidence, not an observed zero or observed failure.

Accordingly, Playable Curated World KRs 1-4 remain unproved. The selected release-stack trace must discover its own unsupported prompt fail-closed rather than inheriting a conclusion from the positional diagnostic. KR 5 is untouched.

## Approved next PR

### Scope: one terminal release-stack authority receipt

Produce one checked receipt at `conformance/authored-match-v1/release-stack-ur-vs-gw-seed-0.json` from a fixed seed-0 UR Lessons versus GW Allies match driven through the Etude Play server authority boundary. Both deliberate actors select only from server-built `InteractionOffer` values and submit revision-bound `Command` values. The runner must never call the compatibility `action`/raw-index endpoint, `hero_action`, or `env.step(index)` directly.

The receipt records:

- exact seed, deck identities and deck hashes, asset/content identities, and the compiled semantic pack key, IR hash, and source hash;
- one ledger row for every deliberate hero and villain decision, with contiguous ordinal, actor, authority revision, prompt and offer family, exact selected offer, exact bound command, and resulting command receipt;
- the ordered semantic and presentation event slice caused by each deliberate command, including an explicit empty slice where appropriate;
- before/after deterministic state witnesses per deliberate command and one terminal witness containing winner, terminal revision/frame hash, turn, life totals, and stable zone/object summaries;
- every typed semantic program encountered by the match, with an identifier that resolves to the admitted program in `two_deck.ir.json`;
- counts by prompt/offer family and explicit counters for deliberate structured commands, automatic rules passes, legacy fixed-action fallback, card-name dispatch, candidate-cap fallback, and client-legality fallback.

The deliberate-decision ledger, accepted command receipts, and caused-by command event groups must be one-to-one. Automatic passes and rules resolution may advance revisions and emit events, but they are marked separately and never masquerade as deliberate decisions.

### Fail-closed implementation order

1. Add the receipt runner and assertions against the existing release-stack offer/command path before adding any new prompt or semantic support.
2. Drive both actors with a deterministic policy that consumes only server offers and structured choice values. It must not inspect card names or choose a raw engine action index.
3. If the fixed trace cannot express or apply a current prompt as a server-built offer plus bound command, stop immediately and report the prompt family, revision, and receipt prefix.
4. Only for that observed fixed-trace gap, add the smallest reusable offer or semantic extension required to express it. Do not preemptively implement `ChooseTarget` or any family named by the separate engine-helper diagnostic.
5. Replay seed 0 from reset after each such fix. The checked artifact and acceptance test are valid only when the unchanged seed reaches terminal with all four authority-fallback counters at zero.

This PR closes the specific gap left by PR #130: there is currently no checked terminal proof that the compiled authored match is exercised through the production Play authority boundary. It may contain a trace-driven offer/semantic fix only if the fail-closed run proves that fix necessary.

### Non-goals

- No additional cards or decklists, broad semantic conformance, or generic rules architecture.
- No UI or client implementation, and no client-side legality model.
- No live/headless/canonical-replay cross-surface parity, viewer-privacy expansion, or stale-reference expansion beyond the command binding already exercised by the receipt.
- No full workload budget: command p50/p95, multi-game throughput, peak RSS budgeting, and performance tuning remain later evidence. This receipt records only counts needed to prove its authority path and terminal completeness.
- No prompt-family implementation based only on the earlier positional diagnostic; production changes must be forced by the fixed release-stack trace.
- No change to the general/default content path or frozen conformance receipts.
- No claim that RUL-3 or a Project KR is complete; cross-surface and workload evidence remains outstanding after this PR.

### Existing support to preserve, not new authority evidence

These checks protect substrate that already exists; passing them alone does not accept the new PR:

```bash
cargo test --manifest-path managym/Cargo.toml --test authored_match_tests -- --nocapture
uv run --extra dev pytest -q tests/etude/test_server.py -k 'protocol_v1_bolt_and_pass_offers_round_trip or protocol_v1_rejects_stale_and_dedupes_retry'
./scripts/verify-clean-machine
```

They respectively preserve compiled exact-deck selection and positional terminal admission, revision-bound server offer/command behavior on the narrow Bolt fixture, and clean-machine Play startup. None is the terminal authority receipt.

### Newly checked acceptance evidence

The implementation is accepted only when all of the following are checked:

- Seed 0 reaches terminal through the Play server command endpoint with both deliberate actors represented by server-built offers and revision-bound commands. The compatibility raw-index endpoint is instrumented and has count zero.
- The checked receipt binds the exact compiled semantic manifest and validates every encountered typed-program identifier against `content/semantic/v1/generated/two_deck.ir.json`.
- Deliberate decision rows, accepted command receipts, and caused-by ordered event groups are one-to-one; prompt/offer family counts reconcile with the ledger.
- The terminal witness and every before/after state witness regenerate identically.
- Legacy fixed-action, card-name dispatch, candidate-cap, and client-legality fallback counters are present and exactly zero rather than inferred from silence.
- The test fails at the first unsupported prompt and may pass only after the unchanged seed replays to terminal. Any production extension in the diff names the exact prompt/revision that forced it.
- The full Rust suite passes in debug, as required by `AGENTS.md`.

Evidence-backed commands for the new acceptance surface:

```bash
uv run --extra dev python scripts/generate_authored_match_receipt.py --check
uv run --extra dev pytest -q tests/etude/test_authored_match_authority.py::test_release_stack_authored_match_receipt_is_terminal_and_authoritative
cargo test --manifest-path managym/Cargo.toml
uv run --extra dev pytest -q tests/etude tests/semantic
```

The generator check proves the checked receipt is canonical and current; the focused Etude test proves terminal release-stack authority and all receipt invariants; the full debug Rust command checks engine invariants; and the scoped Python suite checks the affected server and semantic integration. Frontend checks are unnecessary unless implementation changes frontend code or shared generated protocol artifacts.
