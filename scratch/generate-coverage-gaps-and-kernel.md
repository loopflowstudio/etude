# W2-201 — Generated coverage gaps and kernel-change ratio gate

## User-visible outcome

A rules/kernel reviewer can run one local command and inspect one checked-in,
canonical JSON artifact to answer all of the following for the exact
UR Lessons versus GW Allies product boundary:

- which admitted card definitions and referenced tokens are in scope;
- which typed semantic atoms and rule families each definition uses;
- whether each card/family is supported, explicitly sanctioned, or a gap;
- which exact executable tests support each card and rule-family claim; and
- among the latest 20 real curated-card admissions, how many were content-only
  versus kernel-changing, the explicit denominator, whether the redesign
  threshold is crossed, and the current reviewed response.

CI rejects stale or incomplete evidence. When the rolling window breaches the
threshold, CI also rejects a missing or stale response; an acknowledged breach
remains visibly flagged in the artifact rather than being reported as healthy.

This Task is one serial PR. It does not need a second worktree or a second Task.

## Source of truth

The implementation has two reviewed inputs and one derived output:

1. `content/semantic/v1/two_deck.source.json` remains authoritative for the
   admitted decks, closure definitions, registry bindings, characteristics,
   programs, typed opcodes, triggers, targets, predicates, and costs. The
   generator must consume this source or the compiler's in-memory validated
   result; it must not rediscover card meaning from Oracle text or Rust card
   names.
2. Add `content/semantic/v1/coverage.evidence.json` for evidence that is not
   derivable from the semantic source:
   - a closed rule-family catalog and the typed semantic atoms belonging to
     each family;
   - exact test references;
   - per-card support state and sanctioned-deviation references;
   - the append-only card-admission ledger; and
   - the rolling-window threshold and response acknowledgment.
3. Generate and check in
   `content/semantic/v1/generated/coverage-gaps.json`. It is a view, never an
   authoring surface.

The existing `docs/rules_coverage.yaml` and
`experiments/card-conformance-audit.md` are useful migration inputs and links,
but neither is authoritative for this gate. W2-201 must not silently rewrite
them into competing sources of truth.

## Evidence schema and derived artifact

Use schema version 1 and reject unknown fields. Prefer plain JSON and the
existing `pretty_json`/canonical-hash conventions over adding a YAML or schema
dependency.

### Rule-family catalog

Each family has a stable key, human label, optional CR references, one or more
typed semantic atoms, and exact tests. Semantic atoms are drawn from the
validated source, including:

- program kinds;
- opcodes;
- trigger event and subject kinds;
- cost features such as kicker, waterbend, affinity, and sacrifice;
- selector, predicate, and condition kinds; and
- characteristics used as semantics, especially keywords and token status.

The generator derives a card's families by matching its semantic atoms to the
catalog. Every semantic atom used by the admitted closure must map to at least
one family. A family may cover several atoms and an atom may intentionally
support several families, but duplicate catalog keys and unknown atoms fail.

### Test references

Each test reference records a runner, repository-relative source path, and
exact test identifier. Initial evidence should use exact Rust test identifiers
from the already-running `managym` integration suites, for example
`rules::stage3_cards::earthbend_animates_land_with_counter`; Python node IDs
may be supported if a boundary claim genuinely needs them.

The generator validates that the file exists and the named test is declared in
that file. CI's existing full Rust job remains the execution authority, while
the new coverage job depends on it and checks the references and artifact. A
path-only reference, module-wide glob, prose claim, ignored test, or missing
identifier is not executable evidence.

Every admitted definition, including the two referenced tokens and the four
basic lands, must have at least one exact test reference. Every supported rule
family must have at least one exact test reference. Tests may be shared.

### Support and gap states

Each admitted definition is annotated as one of:

- `supported` — the declared curated behavior is implemented and tested;
- `sanctioned_deviation` — the selected product behavior is implemented and
  tested, with a nonempty decision/document reference; or
- `unsupported` — the behavior is a coverage gap with a nonempty reason.

The artifact derives per-card entries, per-family entries, and sorted gap
lists for unsupported, untested, unmapped, or sanctioned behavior. Missing
annotations are gaps, not implicit support. The current two-deck admission
closure must generate with no unsupported, untested, or unmapped entries;
existing sanctioned behavior remains conspicuous rather than being counted as
an unsupported card.

The artifact includes source and evidence hashes, the pack key, deck and
closure counts, and stable sorted records so two runs over identical inputs
are byte-for-byte equal.

## Rolling card-change classifier

The unit is one distinct real curated card admitted to a product deck. Basic
lands, referenced token/helper definitions, count changes to an already
admitted card, test-only changes, and generated-file churn do not enter the
denominator.

`coverage.evidence.json` contains one monotonically sequenced row per admitted
card with the card's semantic key, admission/change identifier, date,
classification, and evidence. The same semantic key cannot appear twice.

- `content_only`: the card could be admitted by adding/changing declarative
  content, bindings, fixtures, tests, docs, and generated artifacts while using
  the pre-change semantic vocabulary and executor behavior.
- `kernel_changing`: admitting the card required a new or changed IR opcode,
  program/selector/condition/cost vocabulary, rules transition behavior,
  event/identity/state semantics, legal-choice ABI, observation/binding ABI, or
  another runtime semantic path. Refactoring or fixing the kernel in the same
  change is not enough by itself; the evidence must state why the card needed
  that change.

Every row has a nonempty evidence reference. Kernel-changing rows additionally
name the IR/kernel surface that changed. The generator validates the closed
classification vocabulary and required evidence fields; classification is a
reviewed historical assertion, not a heuristic over current filenames.

Seed the ledger with the 25 nonland, non-token definitions in the Milestone 1
two-deck slice. They are `kernel_changing`: the milestone's capability stages
admitted those cards by expanding triggers, decisions/costs, earthbend,
statics, dynamic values, and related executor behavior. Preserve an explicit,
stable sequence in source order and cite
`experiments/milestone-1-two-deck-slice.md` plus the relevant family evidence.
This makes the initial gate report historical pressure rather than pretending
the now-representable cards arrived content-only.

## Threshold and response policy

The checked policy is:

- rolling window: the 20 highest admission-ledger sequence numbers;
- minimum denominator: 10 distinct eligible cards;
- explicit denominator: eligible ledger rows selected into that window;
- reported counts: `content_only`, `kernel_changing`, and `denominator`;
- reported ratios: `content_only / denominator`,
  `kernel_changing / denominator`, and the display pair
  `content_only:kernel_changing`;
- breach: `kernel_changing / denominator > 0.20` (strictly more than four of a
  full 20-card window).

Twenty cards smooths one family-enabling outlier while keeping the signal near
the current curated work. A sustained need for kernel work on more than one in
five cards is incompatible with the research target that an ordinary supported
card requires zero kernel changes.

On a breach, the evidence input must contain a response with:

- one of `ir_redesign`, `kernel_redesign`, or `stop_expansion`;
- a repository-relative decision document that exists;
- a short rationale; and
- `acknowledged_through_sequence` equal to the newest ledger sequence.

No response, a missing document, or an acknowledgment behind the newest row
makes `--check` exit nonzero. A valid response produces
`gate_status: breached_acknowledged`, includes the response in the artifact,
and still lists the breach prominently. This sequence binding prevents one old
design note from becoming a permanent waiver.

The seeded 25-card history therefore has an initial window of 20
kernel-changing cards: `content_only = 0`, `kernel_changing = 20`,
`denominator = 20`, content-only share `0/20`, and kernel-changing share
`20/20`. Seed the response as `ir_redesign`, acknowledged through sequence 25,
pointing to `docs/research/semantic-kernel.md`; that document is the existing
typed-program redesign response to the one-off expansion pattern. The artifact
must report the breach as acknowledged, not pass it as below threshold.

An empty ledger, duplicate/nonmonotonic sequence, missing eligible baseline,
or denominator below 10 is `insufficient_history` and fails CI rather than
silently declaring the ratio healthy.

## Affected surfaces and consumers

- Add a focused generator/validator module under `manabot/semantic/` and a
  thin CLI, `scripts/generate_coverage_gaps.py`.
- Add the reviewed `coverage.evidence.json` and generated
  `coverage-gaps.json` described above.
- Add focused Python tests under `tests/semantic/` for deterministic output,
  full closure/family/test mapping, stale output, classifier arithmetic,
  boundary exclusions, threshold edges (4/20 allowed, 5/20 breached), and
  missing/stale response failures.
- Update `content/semantic/README.md` with authoring, refresh, and check
  commands.
- Add a `Semantic Coverage Gate` CI job that depends on the Rust tests, sets up
  Python 3.12 with `uv`, runs the focused tests, and runs
  `uv run scripts/generate_coverage_gaps.py --check`. Add it to the aggregate
  `Tests Result` dependencies.
- Existing Rust `ContentPack`, match state, rules execution, PyO3 bindings,
  browser, policy ABI, decks, and checked semantic IR remain compatible and
  behaviorally unchanged.

All Python invocations in code, documentation, tests, and CI use `uv run` as
required by `AGENTS.md`.

## End-to-end proof

Use Badgermole Cub as the concrete trace through every boundary:

1. `two_deck.source.json` admits `tla.badgermole_cub` and its typed earthbend,
   counter, mana-trigger, and linked zone-return semantics.
2. `coverage.evidence.json` maps those atoms to the relevant rule families,
   names the exact existing positive/interaction/return tests, marks the card
   supported, and records its Milestone 1 admission as kernel-changing.
3. The generator resolves the semantic definition, verifies its test IDs,
   derives the card and family entries, includes the ledger row in the rolling
   calculation when selected, and writes canonical `coverage-gaps.json`.
4. The Rust job executes the referenced rules suite; the coverage job then
   proves the checked artifact is current and the breach has a current redesign
   response.

Implementation verification target:

```bash
uv run pytest tests/semantic/test_coverage_gate.py -q
uv run scripts/generate_coverage_gaps.py --check
cargo test --manifest-path managym/Cargo.toml --test rules_tests
```

The observable finish line is a byte-stable artifact in which Badgermole Cub
and every other admitted definition have supported semantic/family/test
evidence, the gap lists have the expected contents, and the rolling result is
the explicit acknowledged `0 content-only / 20 total` historical breach.

## Absent and error states

- Missing/invalid semantic source or evidence JSON: fail with a path and field
  context; do not emit a partial artifact.
- Source definition absent from evidence, evidence card absent from the source,
  unknown semantic atom/family, or unclassified used atom: fail.
- Missing source file, missing exact test declaration, duplicate test ID, or a
  card/family without tests: fail.
- `sanctioned_deviation` without a decision reference or `unsupported` without
  a reason: fail.
- Stale/missing generated artifact under `--check`: fail with the exact
  `uv run scripts/generate_coverage_gaps.py` refresh command.
- Malformed, duplicate, or undersized admission history: fail as
  `insufficient_history`.
- Breached threshold without a response current through the newest sequence:
  fail and say that IR/kernel redesign or stopped expansion must be documented.
- Existing unrelated registered cards outside the two-deck semantic admission
  closure are out of scope, not silently reported as supported or as gaps.

## Operational boundary

Generation and checking are deterministic, offline, read-only except when the
refresh command writes the single generated artifact, and linear in the small
semantic source/evidence/test inventory. They must not invoke the network,
Scryfall, Git/GitHub, Loopflow, a Rust build, or the running app. The focused
check should complete in a few seconds; execution evidence remains with the
normal Rust CI job.

## Exclusions

- No new cards, card registrations, semantic opcodes, rule families, or broader
  TLA/cube coverage.
- No changes to rules behavior, deviations, event semantics, identity, legal
  choices, observations, or the semantic IR/runtime adapter.
- No branching representation or benchmark work.
- No reference reducer, optimized-executor differential work, property/fuzz
  harness, persisted seeds, or Phase overlap matrix (W2-197 through W2-200).
- No runtime Oracle parsing, Scryfall refresh, general format legality, or claim
  of comprehensive Magic support.
- No automated inference of historical intent from commit paths; the admission
  ledger is explicit, reviewed evidence.
