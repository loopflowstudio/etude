# Deterministic Study Decision Index and Landmark Ranker

## Problem

Study needs two deliberately separate views of a completed match:

1. a lossless navigation index in which every decision recorded by Game is
   chronologically addressable and exactly restorable; and
2. a short recommendation list that tells a player where to begin reviewing.

The current repository has neither combination. The legacy replay path rebuilds
browser frames from `TraceEvent.observation` snapshots and action descriptions,
while `StudyArtifact` v1 represents only already-enriched landmarks with model
and search evidence. Promoting a few legacy replay rows would lose decisions,
duplicate Game's replay truth, and make the first Study experience depend on
uncalibrated value estimates.

STU-1 will land an independently usable Study-owned pure consumer over a closed
canonical recorded-decision input made only from exact `ExperienceFrame`,
`InteractionOffer`, played `Command`, `PresentationEvent`, cursor, and recording
metadata. A checked canonical fixture exercises that boundary without parsing a
legacy trace or asserting a second replay authority. The consumer preserves
every input address byte-for-byte, then derives 3–7 review recommendations from
typed, viewer-safe semantic signals.

W2-275 is a production-integration opportunity, not a prerequisite for STU-1.
If its Game-owned source lands while this Task is active, STU-1 adds a thin
one-way adapter into the already-tested consumer. Otherwise the index/ranker,
contract, fixture, and evidence land on their own. The ranker remains triage,
not an evaluation engine: it will not call a policy, search a position, label a
move good or bad, inspect hidden state, or depend on frontend layout.

This advances the Study objective that a completed creator-selected matchup
opens into a small number of meaningful landmarks while every historical
decision remains reproducible under the deciding player's knowledge boundary.

## User-visible outcome

A Study consumer can load one completed-match decision record and receive two
stable views at once:

- a chronological timeline containing every recorded decision with its exact
  viewer-safe frame, selected offer, played command, and event cursor; and
- a separate ranked list of 3–7 supported landmarks that points into, but never
  replaces or filters, that timeline.

For this Task the observable surface is the `etude.study_index` CLI and portable
JSON contract, not a new table UI. A developer or downstream Study flow sees
the complete count, exact restoration result, deterministic digest, and typed
landmark reasons. Automatic, forced, and unsupported decisions remain directly
addressable even when absent from the landmark recommendations. The landed
artifact gives STU-2 stable decision IDs and landmark evidence to consume; this
Task does not start STU-2.

## Source of truth and derived views

The existing Rust experience-protocol types in `managym/src/experience.rs` are
authoritative for `ExperienceFrame`, `InteractionOffer`, `Command`, and
`PresentationEvent`. STU-1 does not redefine their fields or semantics.

`RecordedDecisionInput` in `managym/src/study.rs` is the closed Study consumer
envelope that orders exact instances of those protocol objects with canonical
decision ordinal, event cursor, and automatic-recording metadata. The checked
`protocol/fixtures/recorded-match-decisions-curated.json` is the authoritative
persisted input for STU-1's independently landable proof. It is a conformance
fixture, not a production replay database.

Everything else is derived:

- `StudyDecisionIndex` preserves the exact input objects and adds stable Study
  decision IDs, typed decision classification, and landmark references;
- the output fixture is the canonical serialization of that derived index;
- the W2-220 semantic receipt is a deterministic summary of input/output
  invariants; and
- observational latency metadata describes one run but has no semantic
  authority.

When W2-275 exists, Game's canonical recorded-decision API becomes the
production source and its adapter must map one-to-one into the same closed input
without changing the pure consumer. Until then, the fixture proves the consumer
contract without claiming production replay ownership.

## End-to-end proof

The checked fixture contains exactly eight ordered decisions at distinct event
cursors:

1. a deliberate non-pass priority commitment;
2. a priority response choice with a public stack object;
3. a multiple-option target selection;
4. the first attacker micro-decision;
5. a second attacker micro-decision in the same combat episode;
6. a blocker declaration in a separate combat episode;
7. an automatic priority pass; and
8. a forced single-option target decision.

All eight appear, in order, in `StudyDecisionIndex`. Decisions 4 and 5 remain
separate timeline rows but yield one attack landmark represented by decision 4.
Decisions 7 and 8 remain addressable but are not landmarks. The expected output
therefore has exactly five ranked landmarks: two priority, one targeting, one
attack episode, and one block episode.

Run:

```bash
uv run python -m etude.study_index \
  protocol/fixtures/recorded-match-decisions-curated.json \
  --identity protocol/fixtures/study-index-identity-curated.json \
  --verify --repeats 1000 \
  --semantic-receipt experiments/data/w2-220-study-decision-index-v1.json \
  --observations scratch/study-index-observations.json
```

The command prints `decisions=8`, `landmarks=5`, `completeness=100%`, and
`restoration=100%`; byte-compares the generated index and deterministic semantic
receipt with the checked files; and lists five decision IDs with typed reasons.
It also reports p50/p95 runtime and an observation timestamp in the scratch
metadata file, whose shape and sanity are checked but whose values are not
byte-compared. Focused Rust, Python, and TypeScript tests deserialize the same
input/output fixtures and prove the cross-language contract. No W2-275 or
production trace is required for this end-to-end proof.

## Approach

### 1. Land a pure canonical consumer, then adapt Game opportunistically

Define a closed `RecordedDecisionInput` fixture/consumer boundary. Its enclosing
recorded match supplies:

- a viewer-safe replay ID and declared canonical decision count;
- a contiguous decision ordinal and ordered replay/event cursor;
- whether the recorded action was automatic;
- the exact historical `ExperienceFrame`;
- the exact selected `InteractionOffer` from that frame;
- the exact played `Command`; and
- the exact canonical `PresentationEvent` span committed by that command, when
  one was recorded.

The viewer, prompt, decision family, legal breadth, and command bindings are
validated or classified from those canonical protocol objects. The input is a
consumer DTO and executable fixture, not a replay store: it has no loader for
`TraceEvent`, no action-index translation, no observation normalization, and no
ability to reconstruct missing frames or events. Study does not filter,
deduplicate, or collapse any valid input address in the full index.

STU-1 computes its viewer-safe source digest from the validated canonical input,
then cross-checks `StudyIdentity.source_replay_sha256`. It never accepts a raw
authority-trace digest as equivalent provenance. This proves deterministic
identity at Study's viewer-safe boundary now. It does not claim that two raw
authority traces differing only in hidden cards or RNG project identically;
that upstream twin assertion belongs to the W2-275 adapter integration.

If W2-275 lands before STU-1 is ready to settle, add a thin adapter that maps
each Game-owned canonical decision address one-to-one into
`RecordedDecisionInput` and prove equality with the fixture consumer. If W2-275
does not land, leave that adapter absent and complete STU-1. No fallback legacy
parser is permitted in either case.

### 2. Add a sibling index contract, not evidence-shaped fake landmarks

Keep the existing enriched `StudyArtifact` v1 and `DecisionEvidence` contract
intact. It correctly requires analysis provenance and complete metric coverage,
but those requirements make it the wrong representation for a pre-analysis
navigation index.

Add closed `RecordedDecisionInput` and sibling `StudyDecisionIndex` root
contracts to `managym/src/study.rs`, with generated
`protocol/study-recorded-decisions-v1.schema.json` and
`protocol/study-index-v1.schema.json`, mirrored Pydantic models in
`etude/study_protocol.py`, and structural TypeScript types in
`frontend/src/lib/study-protocol.ts`. The input schema packages exact existing
protocol objects plus cursor/recording metadata; it does not redefine those
objects or replay behavior. The TypeScript representation is contract
portability only; no ranking code or layout behavior lands in the frontend.

The root shape is:

```text
StudyDecisionIndex
  version: 1
  identity: StudyIdentity
  decisions: StudyDecision[]
  landmarks: RankedStudyLandmark[]
```

`StudyDecision` contains:

- `id`: a stable `StudyDecisionId`;
- `ordinal`: the canonical zero-based decision ordinal;
- `viewer`: the actor whose historical projection is embedded;
- `event_cursor`: the exact Game-owned replay/event address;
- `automatic`: copied from Game, never inferred;
- `kind`: Study's closed classification derived only from canonical prompt,
  action-space, offer-verb, and presentation variants;
- `frame`, `offer`, and `played`: exact canonical protocol objects.

`RankedStudyLandmark` is intentionally lightweight:

- `decision_id`: reference into `decisions`;
- `rank`: contiguous one-based recommendation rank;
- `reasons`: a non-empty, enum-ordered set of `LandmarkReason` values.

The closed `LandmarkReason` enum is:

- `priority_commitment` — the recorded command took a non-pass priority action;
- `priority_response` — a non-forced priority choice occurred with a public
  stack object or response option;
- `target_selection` — the canonical target prompt exposed more than one legal
  choice;
- `attack_declaration` — the representative decision starts a supported attack
  declaration episode;
- `block_declaration` — the representative decision starts a supported block
  declaration episode;
- `branching_choice` — the canonical offers/choice grammar exposed multiple
  legal continuations; and
- `public_semantic_impact` — the exact command span contains an emphasized or
  critical canonical `PresentationEvent`.

These are evidence-backed descriptions of why a position is worth reviewing,
not claims about move quality. Each reason must be derivable from typed Game,
Rules, or experience-protocol fields; labels, help text, DOM structure, and
render order are forbidden inputs.

`StudyIdentity` remains the provenance anchor defined by W2-222. The stable
decision ID uses only its recorded-match fields, not model or analysis-budget
fields, and the ranker function accepts only canonical decisions. Mutating
model identity, analysis budget, or evidence must therefore leave decision IDs
and landmark ordering unchanged.

### 3. Make identity and restoration executable invariants

`StudyDecisionId` is lowercase SHA-256 over canonical JSON prefixed with the
domain separator `etude.study-decision.v1\0`. The hashed object contains:

- viewer-safe source replay digest;
- match ID;
- viewer;
- event cursor;
- frame hash and revision;
- prompt ID;
- offer ID; and
- played command ID.

Canonical JSON recursively sorts object keys, preserves array order, encodes as
UTF-8 with `,`/`:` separators and no insignificant whitespace, and rejects
non-finite numbers. The viewer-safe source digest applies the same encoding to
the validated `RecordedDecisionInput` root. Checked human-readable JSON files
use sorted keys, two-space indentation, and one trailing newline; semantic
digests always use the compact canonical encoding.

It excludes ordinal, labels, presentation timing, model, budget, generated
timestamps, and frontend state. The source replay is immutable, so including
its viewer-safe digest prevents accidental identity reuse after canonical
history changes.

The builder rejects rather than repairs:

- a declared count that differs from the input length;
- missing, repeated, or out-of-order ordinals;
- non-increasing or duplicate event cursors;
- duplicate decision IDs;
- a frame whose match/content identity differs from `StudyIdentity`;
- a prompt whose actor differs from `viewer`;
- an offer absent from, or unequal to, the selected frame offer;
- a command with mismatched match, revision, prompt, or offer;
- any non-empty opponent-private hand in the embedded frame; and
- a landmark reference outside the complete decision list.

It preserves input order and exact protocol objects. It never silently sorts a
malformed source, because doing so could make an incomplete replay look valid.

### 4. Rank structural salience with deterministic diversity

Indexing runs first and is unconditional. Ranking operates over the validated
index and may recommend only decisions whose canonical trace supports a typed
priority, target, attack, or block reason.

Eligibility rules are exact:

- automatic decisions remain indexed but are not landmarks;
- forced decisions remain indexed but are not landmarks;
- unsupported decision kinds remain indexed but are not landmarks;
- target/choice reasons require at least two canonical legal continuations;
- a pure pass-only priority window is not eligible; and
- missing typed semantics make a decision ineligible rather than triggering a
  label/action-string heuristic.

V1 classifies `PRIORITY`, `CHOOSE_TARGET`, `DECLARE_ATTACKER`, and
`DECLARE_BLOCKER` action spaces into the four supported decision kinds and maps
everything else to `other`. It uses the exact count of `frame.offers` as legal
breadth; it does not enumerate combinations inside a structured choice grammar.
A one-offer structured decision is still indexed but receives no
`branching_choice` or multiple-target reason unless a later canonical protocol
field states that breadth explicitly. A priority response requires either a
public stack object in the frame or a stack-referencing event in the exact
presentation span.

Consecutive attack or block micro-decisions with the same viewer, turn, step,
and decision kind form one combat episode. The episode's first decision is its
landmark representative so Retry begins before the whole declaration; all
member decisions remain separately addressable in `decisions`. Semantic impact
and legal breadth are aggregated across the episode only for ranking.

Every eligible candidate gets an integer-only salience tuple, in this order:

1. sum of canonical presentation importance for the exact command/episode span
   (`critical=8`, `emphasized=4`, `normal=1`, `ambient=0`), capped at 32;
2. deliberate non-pass priority action (`1` or `0`);
3. legal continuation count, capped at 16;
4. combat episode size, capped at 16; and
5. earlier event cursor, then lexicographic decision ID, as deterministic
   tie-breaks.

No float, clock, random source, insertion-ordered map, hash iteration order,
policy probability, search value, visit count, robustness statistic, or
uncertainty field participates.

Selection uses at most seven slots:

1. take the highest-salience candidate from each supported family present
   (`priority`, `targeting`, `combat`) so one noisy family cannot erase the
   others;
2. fill remaining slots from the global salience order, with a maximum of three
   landmarks from one family;
3. if at least three candidates exist but the diversity pass selected fewer,
   relax only the family cap until three are selected;
4. stop at seven or when candidates are exhausted; and
5. order selected rows by descending salience and assign contiguous ranks.

If the trace supports at least three eligible candidates, the selector returns
between three and seven; family caps may intentionally leave later slots unused.
If it supports fewer than three, Study returns the honest smaller set and the
measurement records `insufficient_supported_landmarks` rather than inventing
filler highlights.

### 5. Ship fixtures and a W2-220 measurement receipt

STU-1 owns an executable canonical consumer fixture assembled solely from exact
experience-protocol objects. It is not exported from legacy `TraceEvent` rows
and does not claim to be Game's production replay representation. STU-1 adds:

- `protocol/fixtures/recorded-match-decisions-curated.json`, the canonical
  recorded-decision input spanning supported priority, targeting, and combat;
- `protocol/fixtures/study-index-identity-curated.json`;
- `protocol/fixtures/study-decision-index-curated.json`, the exact expected
  index and ranked output; and
- `experiments/data/w2-220-study-decision-index-v1.json`, a deterministic
  semantic receipt.

The checked semantic receipt records only reproducible meaning: source/output
digests, total decisions, counts by viewer, automatic status and typed kind,
eligible episodes, selected landmarks and reason distribution,
completeness/restoration ratios, boundary-privacy checks, and repeat digest
count. It names the exact fixture, content pack, engine identity, and command.
It contains no timestamp, host identity, or latency statistic, so verification
can regenerate and byte-compare it.

The same command measures 1,000 in-process builds and reports p50/p95 latency,
an absolute observation timestamp, and optional environment details to
`scratch/study-index-observations.json` and stdout. Verification checks that
this observational metadata has the documented schema and sane values, but
never byte-compares it and never includes it in the artifact or semantic-receipt
digest. Counts are evidence for the fixture only; existing decision-profile
work already shows that matchup and representation can change the mix sharply.

This checked index, landmark set, and semantic receipt are the evidence STU-2
may build on. STU-2 does not wait for the W2-275 production adapter once STU-1
lands.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|------------------|
| Can STU-1 land before W2-275? | Yes. The ranker is a pure consumer of already-canonical protocol objects, and a closed canonical recorded-decision fixture can prove its complete behavior. Current `TraceEvent` lacks the required frame/offer/command address, but STU-1 does not need to parse it. | Make W2-275 a production-adapter gate only. Land the contract, pure consumer, fixture, ranker, and evidence independently; wire Game one-to-one only if its seam lands during this Task. |
| Can the existing `StudyArtifact` represent the complete pre-analysis timeline? | No. V1 requires at least one enriched landmark, alternatives, and full policy/search/visits/robustness/uncertainty coverage. | Add a sibling lightweight decision-index contract and reference it later by decision ID; do not fabricate empty evidence or weaken W2-222 validation. |
| Will naive top-N ranking produce useful variety? | No. Existing measurements found attack/block micro-decisions were roughly 58% of surfaced choices in one baseline, while the interactive deck shifted toward roughly 71% priority share. | Group contiguous combat episodes, guarantee family coverage, and cap one family's selected rows while preserving all raw decisions in the index. |
| Can action descriptions or frontend replay frames classify decisions safely? | No. They are presentation strings/derived browser objects, and current replay builds frames from snapshot adjacency. | Classify only from the exact prompt/action-space, offer verb/choices, command, and presentation variants in the canonical input. Treat absent typed support as ineligible. |
| What privacy claim can STU-1 prove without Game's adapter? | It can prove that its closed viewer-safe boundary rejects populated opponent-private hands and unknown private/RNG sidecars, hashes only validated canonical input, and produces stable output for the same permitted input. It cannot prove that two raw authority traces project identically. | Make rejection and deterministic boundary stability unconditional STU-1 gates. Add the hidden-card/RNG authority-twin equivalence only as a W2-275 integration assertion when that adapter exists. |
| Can source file SHA-256 be used directly for stable IDs? | Not safely if it covers authority-private cards or RNG state. | Compute the digest over validated canonical recorded-decision input and cross-check the supplied `StudyIdentity`; never accept a raw trace digest at the Study boundary. |
| Does `ExperienceFrame` alone prove privacy? | No. The transitional type can carry an opponent hand; current Study validators explicitly reject a populated opponent hand. | Validate privacy before identity hashing and ranking, retain closed schemas, and reject unknown authority-private sidecars. |
| Can event impact be inferred from before/after state? | No, and the Study ownership boundary forbids it. | Use only the canonical `PresentationEvent` span attached to the recorded command; missing events reduce available salience instead of triggering snapshot diffing. |
| Can the semantic receipt stay checked while latency and time vary? | Yes, if semantic evidence and observations are separate outputs. Digests, counts, reasons, and invariant results are deterministic; p50/p95, timestamps, and host details are observations. | Byte-compare the artifact and checked semantic receipt. Schema-check but never byte-compare the scratch observational metadata. |
| Do current protocol implementations agree enough to extend safely? | Yes. Baseline debug Rust tests passed 9/9, Python Study protocol tests passed 4/4, and focused TypeScript protocol/replay tests passed 6/6. The broader trace API test requires the local PyO3 extension, which is absent in this fresh venv. | Extend the established Rust-schema/Pydantic/TypeScript conformance pattern. The PyO3/trace API path is required only if the W2-275 production adapter joins this Task. |
| What must W2-220 be able to consume? | The local controller has no W2-220 Task Session, but the directive explicitly requests fixtures and measurements. Existing measurement docs require matchup-specific, reproducible counts rather than generalized claims. | Land a checked JSON receipt and reproducible `uv run` command alongside protocol fixtures; do not depend on PM-session availability. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Turn the legacy frontend replay frame list into highlights. | Fastest path to visible rows. | It drops protocol identities, reconstructs history from adjacent snapshots, couples ranking to layout, and duplicates Game's replay authority. |
| Expand `StudyArtifact.landmarks` until it also contains every decision. | Reuses one existing root type. | It conflates navigation with analyzed evidence, forces fake model/search fields for ordinary decisions, and makes highlights navigation gates. |
| Wait for calibrated policy/value/search deltas and rank mistakes. | Could eventually produce stronger chess-like review labels. | It blocks the first Study slice, risks false precision, and violates the directive to keep this ranker independent of model/search scoring. Those signals can rerank or annotate the same stable decision IDs later. |
| Select the seven highest raw structural scores. | Simple and deterministic. | Measured combat/priority mix is highly skewed; one repetitive family would dominate. Family coverage and combat episode grouping produce a better guided-review starting set. |
| Collapse repetitive decisions before indexing. | Smaller artifacts and easier UI. | It destroys chronological completeness and exact addresses. Collapsing is acceptable only in the separate recommendation list. |

## Key decisions

- Every canonical input decision is indexed exactly once. Highlight status
  never controls navigation.
- STU-1 lands the pure consumer and canonical recorded-decision fixture without
  waiting for W2-275. W2-275 gates only a production Game adapter.
- The fixture is an executable consumer contract, not replay authority. Legacy
  trace parsing and compatibility belong to Game and never enter Study.
- Indexing and ranking are separate pure stages. A ranking failure cannot erase
  a valid timeline.
- Stable decision identity is viewer-safe and excludes model, budget, time, and
  UI fields.
- Automatic and forced actions remain historical addresses but are not credited
  as deliberate landmarks.
- Combat micro-decisions are grouped only for recommendation; Retry points to
  the first exact decision in the episode.
- Presentation importance is a bounded public semantic signal, not an
  evaluation. No landmark is called best, mistake, winning, or confident.
- Missing semantics produce fewer honest landmarks. The ranker never guesses
  from prose.
- The artifact and checked semantic receipt are deterministic JSON. Latencies,
  timestamps, and host details are un-compared observational metadata.
- STU-1's unconditional privacy proof ends at rejection and stability of its
  viewer-safe boundary. Raw hidden-card/RNG twin equivalence is conditional on
  the W2-275 adapter.
- The landed index, landmarks, and semantic receipt unblock STU-2 even when the
  production adapter has not landed.

Wild success looks like a 100-decision match opening on five or seven genuinely
different review moments while the player can still jump to decision 37, retry
the entire first attack declaration, and return to the exact event cursor. The
same IDs later accept policy/search evidence without remapping annotations or
links.

Wild failure would be a supposedly pure index that quietly parses a legacy
trace, accepts private sidecars, or emits seven adjacent creature declarations.
Closed boundary rejection, stable canonical-input digests, episode grouping,
and family caps are immediate release gates. When a Game adapter exists, a
separate upstream twin test guards against hidden-card/RNG projection drift.

## Affected surfaces and consumers

| Surface | Required change or compatibility condition |
|---------|--------------------------------------------|
| `managym/src/study.rs` | Add closed input/index/landmark authority types and cross-field validators. Existing `StudyArtifact` and `DecisionEvidence` v1 types and fixture remain valid and unchanged. |
| Rust schema exporters and tests | Generate `study-recorded-decisions-v1.schema.json` and `study-index-v1.schema.json`; round-trip the shared eight-decision input and five-landmark output; retain debug test coverage. |
| `etude/study_protocol.py` | Mirror the two new closed roots and cross-field validation without weakening existing Study evidence validation. |
| `etude/study_index.py` | Add the pure builder, deterministic ranker, canonical serializer/digester, receipt builder, observation measurement, and CLI. It imports protocol models only and has no server, trace, network, or engine dependency. |
| Python tests | Prove completeness, restoration, deterministic bytes, classification, combat grouping, privacy rejection, empty/insufficient cases, receipt separation, and CLI output. |
| `frontend/src/lib/study-protocol.ts` | Add structural input/index/landmark types plus boundary assertions needed for portable consumption; add no UI, store, ranking, or legality behavior. |
| Protocol fixtures | Check in the exact eight-decision input, its matching identity, the five-landmark index, and both generated schemas. Existing `bolt-target` and `study-curated-decision` fixtures remain compatible. |
| W2-220 evidence | Consume the checked deterministic semantic receipt and separately reported observational metadata. No PM session is required to read the file. |
| STU-2 | May consume stable decision IDs, exact restored decisions, typed landmarks, and the semantic receipt only after this PR lands. This run does not start or modify STU-2. |
| W2-275, if available | Add one thin production adapter and authority privacy-twin integration test. Otherwise make no Game or legacy replay changes. |

This is one serial PR in the existing STU-1 worktree. It does not open another
worktree or rotate to a second PR unless the Task gate explicitly returns
follow-up work after landing.

## Absent and error states

- A valid input with `decision_count=0` produces an empty decision index and an
  empty landmark list. It is complete, not an error; the semantic receipt marks
  `no_recorded_decisions`.
- A valid input with decisions but fewer than three supported eligible
  candidates preserves every decision and returns 0–2 landmarks. The receipt
  marks `insufficient_supported_landmarks`; no filler is invented.
- An unsupported action space maps to `other`, remains addressable, and is not a
  landmark. A missing presentation span is valid and simply contributes no
  public-semantic-impact weight.
- A structured one-offer decision whose internal breadth is not explicitly
  available remains addressable but is not treated as a branching choice.
- Count mismatch, missing/repeated ordinal, non-increasing cursor, duplicate
  decision ID, frame/identity drift, prompt/offer/command mismatch, non-finite
  number, populated opponent hand, unknown field, hidden/RNG sidecar, or raw
  authority digest rejects the entire build before any partial artifact or
  receipt is emitted.
- A checked artifact or semantic-receipt byte mismatch makes `--verify` fail
  nonzero and reports which deterministic output drifted.
- When `--observations` is supplied, failure to write or schema-validate that
  requested metadata also fails the CLI, but observation values never alter a
  semantic digest.
- Missing W2-275 means the production adapter and authority-twin assertion are
  not applicable. It never selects a legacy fallback and never blocks STU-1.

## Operational boundary

- The unconditional path is local and offline: no network request, browser,
  server, subprocess, model load, search call, or engine extension is required.
- Building the index is linear in decision count; selecting landmarks may sort
  at most one candidate per decision and is `O(n log n)`. Memory is `O(n)` for
  the exact preserved objects and derived rows. The ranker never enumerates
  combinatorial choice assignments.
- The fixture proves eight decisions, while unit tests additionally exercise a
  generated 500-decision input so accidental quadratic behavior is visible.
- The measurement performs 1,000 in-process builds after one untimed warm-up.
  It records p50/p95 wall time as observational metadata. This first receipt
  establishes a baseline rather than inventing a latency release threshold.
- Deterministic verification compares compact semantic digests and checked
  pretty JSON bytes; clock, hostname, process ID, and measured latency are
  excluded from both comparisons.
- All Python entry points and checks use `uv run`; Rust validation uses the CI-
  matching debug profile before landing.

## Scope

- In scope unconditionally: define and validate the closed canonical
  recorded-decision input and `StudyDecisionIndex` contracts; implement the pure
  index/ranker; stable viewer-safe decision IDs; complete chronological
  indexing; exact event/frame/offer/command preservation; deterministic typed
  landmark classification/ranking; combat episode grouping; cross-language
  schema conformance; canonical input/output fixtures; viewer-safe boundary
  rejection/stability tests; deterministic W2-220 semantic receipt; and
  non-compared observational p50/p95/timestamp reporting.
- In scope conditionally: if W2-275 lands before STU-1 settles, add a thin
  one-to-one Game source adapter plus raw hidden-card/RNG twin integration
  assertion. Absence of that adapter does not block Task completion or landing.
- Out of scope: waiting for W2-275; parsing legacy `TraceEvent`; generating,
  reconstructing, or migrating replay truth; proving authority-to-viewer privacy
  projection without the Game adapter; legality inference; policy/value/search
  execution; move-quality labels; evidence reveal; Retry execution; branches;
  annotations; sharing; hindsight; frontend landmark UI; layout-based ranking;
  and persistent services.

## Done when

The implementation passes all of these observable gates:

1. The eight-decision canonical fixture produces eight index rows in input
   order; ordinals are contiguous; cursors strictly increase; IDs are unique;
   and every output frame/offer/command is JSON-equal to its input object.
2. Two complete builds and 1,000 measurement repeats produce one identical
   canonical artifact digest and one identical deterministic semantic receipt.
3. The closed input rejects a populated opponent-private hand, unknown hidden
   card/RNG sidecars, raw authority-trace digests, and identity drift before
   hashing or ranking. Revalidating the same permitted viewer-safe input yields
   byte-identical source identity, decision IDs, and landmark ranking.
4. The canonical fixture yields exactly five landmarks with contiguous ranks:
   two priority, one target, one attack-episode representative, and one block
   representative. The automatic pass and forced target are not landmarks.
5. Mutating model identity, analysis budget, policy/search evidence, or
   frontend-only ordering does not alter decision IDs or landmark selection.
   Changing a canonical frame or viewer-safe replay digest correctly creates a
   new historical identity.
6. The checked input fixture, output fixture, both generated schemas, and
   `experiments/data/w2-220-study-decision-index-v1.json` semantic receipt
   reproduce byte-for-byte from the documented command. The same run reports
   p50/p95 and a timestamp as schema-valid scratch observations that verification
   does not byte-compare or include in any semantic digest.
7. If W2-275 has landed, its adapter maps every canonical Game decision
   one-to-one into the same consumer and a raw hidden-card/RNG twin integration
   test proves equal viewer-safe input/output. If it has not landed, this gate is
   recorded as not applicable and STU-1 still completes.
8. Relevant unconditional checks pass, including debug Rust tests as CI runs
   them:

```bash
cargo test --locked --manifest-path managym/Cargo.toml --test study_protocol_tests --test study_index_protocol_tests
uv run pytest -q tests/etude/test_study_protocol.py tests/etude/test_study_index.py
uv run ruff check etude/study_protocol.py etude/study_index.py tests/etude/test_study_index.py
npm --prefix frontend test -- --run src/lib/study-protocol.test.ts
```

9. The verified PR lands with `lf pr land -c`; its checked decision index,
   landmarks, and semantic receipt are sufficient evidence for STU-2 to begin
   without waiting for W2-275.

These gates directly advance the Wave measure that “a completed
creator-selected matchup opens directly into a guided review that identifies a
small number of meaningful decision landmarks rather than replaying raw engine
actions,” while preserving the Process requirement that Study consumes
canonical frames, offers, commands, and presentation events and does not create
a second replay or rules truth.

## Measure

Before implementation, the repository has one single-decision Study fixture,
zero complete Study decision indexes, zero landmark-ranking fixtures, and no
W2-220 receipt. The baseline protocol checks are 9/9 focused Rust tests, 4/4
focused Python tests, and 6/6 focused TypeScript tests passing. The absence of a
local PyO3 extension and production Game adapter does not affect the pure
consumer baseline.

After implementation, reproduce the receipt with:

```bash
uv run python -m etude.study_index \
  protocol/fixtures/recorded-match-decisions-curated.json \
  --identity protocol/fixtures/study-index-identity-curated.json \
  --verify --repeats 1000 \
  --semantic-receipt experiments/data/w2-220-study-decision-index-v1.json \
  --observations scratch/study-index-observations.json
```

Success is `completeness_ratio=1.0`, `exact_restoration_ratio=1.0`,
`duplicate_decision_ids=0`, `repeat_artifact_digests=1`,
`viewer_boundary_stable=true`, `private_input_rejections` covering hand/RNG/raw
digest cases, `decision_count=8`, and `landmark_count=5` for the canonical
fixture. Verification regenerates and byte-compares the Study artifact and
semantic receipt only. It reports p50/p95 and timestamp from the observations
file without comparing their values or setting a speculative release threshold.
W2-220 and STU-2 receive a trustworthy semantic baseline before production
integration or later analysis.

If W2-275 lands during STU-1, a separate integration measurement records
`game_adapter_complete=true` and `authority_privacy_twin_equal=true`. Otherwise
those fields do not appear in the unconditional semantic receipt and their
absence does not prevent landing.
