# W2-215: Viewer-safe semantic program projection

## Directive and finish line

Directive v1 is acknowledged. This Task owns only the learning projection of
the checked-in two-deck semantic IR. It does not change rules execution, the
legacy fixed observation/action ABI, or train a semantic encoder or structured
decoder.

The Task holds when a learning consumer can bind the exact immutable
`ContentPack` used by an environment to the reviewed semantic IR once, obtain a
deterministic typed token graph for every visible card and its programs, batch
visible runtime bindings with stable masks, select an identity-bearing or
identity-ablated view, and reject incompatible artifacts before model use.

## User-visible outcome

The immediate user is an experiment author preparing W2-214/W2-213. Given the
UR Lessons versus GW Allies environment, they can:

1. load one versioned semantic catalog for the environment's exact
   `ContentPack`;
2. project raw player-perspective observations into typed program inputs with
   no fixed token or visible-object cap;
3. batch those inputs deterministically, with explicit object/token/reference
   masks;
4. switch to `semantic_only` mode, in which no card name or opaque
   `CardDefId` is a model feature; and
5. persist and validate the projection schema, semantic IR, and content-pack
   hashes in a semantic dataset or checkpoint header.

Existing gameplay, rules traces, Python observation dictionaries, model
checkpoints, and presentation JSON remain compatible when the new projection
is not requested.

## Source of truth and derived views

There are three authoritative inputs, with no new mutable rules state:

- `content/semantic/v1/two_deck.source.json` remains the reviewed semantic
  authoring source. `content/semantic/v1/generated/two_deck.ir.json` is its
  deterministic, checked-in typed IR and carries `schema_version` and
  `ir_hash`.
- The exact environment's `Arc<ContentPack>` remains authoritative for
  admitted immutable definitions, `CardDefId` allocation, content schema, and
  `content_digest`. A small read-only manifest API exposes those facts from the
  environment; the projector must not assume that the process-global default
  pack is the environment's pack.
- `managym::agent::observation::Observation` remains the viewer boundary for
  dynamic state. The projector accepts this already-filtered view, never
  `Game`, `MatchState`, zones, suspended private decisions, or a search fork.

The learning schema, bound catalog, padded batches, dataset headers, checkpoint
headers, and benchmark report are derived views. Rules continue to execute the
current `CardDefinition`/`Effect` path; the semantic IR is not interpreted by
the reducer in this Task.

## Versioned schema

Add a reviewed `content/semantic/v1/learning_schema.json`. Canonical JSON
(UTF-8, sorted keys, compact separators, excluding its own hash field) is
SHA-256 hashed as `learning_schema_hash`. The file pins:

- schema version and supported semantic-IR schema version;
- every token kind and categorical vocabulary value;
- the supported numeric opcode table, which must agree with
  `manabot.semantic.compiler.Opcode`;
- the meaning and dtype of all catalog, edge, object, padding, and mask arrays;
  and
- the two identity modes, `identity` and `semantic_only`.

### Token graph

The core representation is a shared ragged catalog, not a copy of each
program on every runtime object:

```text
SemanticCatalogV1
  token_kind:                    uint16[T]
  token_value:                   int32[T]
  definition_offsets:            int32[D + 1]
  program_offsets:               int32[P + 1]
  definition_program_offsets:    int32[D + 1]
  definition_program_rows:       int32[P]
  definition_ref_source_tokens:  int32[R]
  definition_ref_target_rows:    int32[R]
```

`token_value` is interpreted only through `token_kind`; it is never a raw
string hash. V1 includes closed tokens for:

- definition, characteristic, program, cost, target, trigger, instruction,
  selector, predicate, condition, list, `then`, `otherwise`, and `body`
  begin/end boundaries;
- program kind and numeric opcode;
- signed integer, boolean, mana/color component, card type, supertype,
  subtype, keyword, duration, zone, controller, destination, and ordering
  values;
- selector, predicate, condition, trigger event, and trigger subject kinds;
- target declarations (`min`, `max`, selector) and local target/choice roles;
  and
- a `definition_ref` marker whose destination is carried by the graph edge
  arrays rather than exposed as an opaque integer token.

Local target names are lowered to declaration-order role ordinals. Reserved
roles such as source, current target, and triggering spell have fixed schema
values. Card names, registry names, `semantic_key`, `program_index`,
`semantic_index`, `kind_name`, and `op_name` are validation/debug metadata and
are never tokenized. Numeric `opcode` is accepted only when it matches the
schema's named opcode during load.

Definition characteristics are included so a referenced token definition is
meaningful without identity: types, supertypes, subtypes, printed keywords,
mana/color components, power/toughness, and token status. A definition with no
programs still emits explicit definition/program-list boundaries.

### ContentPack binding

Expose a read-only environment content manifest through Rust and PyO3:

```text
ContentPackManifestV1
  schema_version
  content_digest
  definitions: [(CardDefId, registry_name)]
```

The manifest is obtained from the environment's retained `Arc<ContentPack>`.
At projector construction, each IR `content_pack_binding` name must resolve
exactly once, all resulting `CardDefId`s must be unique, and the existing
characteristic-conformance tests must continue to pass. Names are discarded
from the bound model view after this one-time resolution.

The bound catalog records an internal `CardDefId -> definition row` lookup.
The definition row is a gather address into semantic tokens, not an embedding
feature. This distinction is what permits a real identity ablation while
still selecting the correct program.

Hash provenance is explicit:

```text
learning_schema_hash  = SHA-256(canonical learning schema)
semantic_ir_hash      = checked and recomputed IR hash
content_pack_hash     = ContentPack.content_digest()
binding_hash          = SHA-256(canonical CardDefId -> definition-row table)
semantic_pack_hash    = SHA-256(schema hash + IR hash + content schema/hash)
```

`binding_hash` is required when opaque identity features are enabled. The
representation-neutral `semantic_pack_hash` does not depend on numeric
`CardDefId` allocation.

### Viewer projection and batching

For each raw `Observation`, enumerate only data already present in that view,
in this order:

1. `agent_cards` in observation order;
2. `opponent_cards` in observation order; and
3. public non-spell `stack_objects` in stack observation order, bound through
   `source_card_registry_key` and tagged as a visible ability source.

The card lists already contain the agent's hand and privately revealed
top-library cards, all public graveyard/exile/battlefield cards, and public
stack spells. They do not contain the opponent's hand or either unrevealed
library. Battlefield cards supply the immutable semantics for the parallel
mutable `PermanentData`; mutable counters, damage, control, and other match
facts stay in the existing observation.

One or more projected observations form a ragged `SemanticObjectBatchV1`:

```text
sample_offsets:           int32[B + 1]
object_definition_rows:   int32[O]
object_roles:             uint8[O]
object_slots:             int32[O]
opaque_identity_ids:      int32[O]
opaque_identity_valid:    bool[O]
```

The pure padding adapter returns deterministic `[B, Omax]` object tensors and
`object_mask`, plus `[D, Tmax]` catalog token tensors and `token_mask`; reference
edges receive an equivalent mask. Padding uses `-1` for indices, zero for token
values, and false masks. Empty observations produce offsets `[0]` and a valid
zero-width object batch. No program, token, or visible-object truncation is
allowed; `Omax`/`Tmax` are derived from the batch/catalog being padded.

In `identity` mode, `opaque_identity_ids` contains the bound `CardDefId` and its
mask is true. In `semantic_only` mode, the same stable arrays contain only `-1`
and a false mask. Names are absent in both modes. A test with permuted numeric
`CardDefId` allocation must produce byte-identical semantic tokens and ablated
model tensors while the identity view and `binding_hash` change.

## Dataset and checkpoint boundary

Provide a `SemanticArtifactHeaderV1` serializer/validator with:

- learning schema version/hash;
- semantic IR schema/hash;
- content-pack schema/hash;
- semantic pack hash;
- identity mode, and `binding_hash` only for identity mode; and
- projection array dtypes/layout version.

Every dataset or checkpoint that contains semantic arrays must carry this
header. Loading semantic inputs is fail-closed: absent metadata, an unknown
version, or any hash/mode/layout mismatch is an error before arrays reach a
model. Legacy datasets/checkpoints without semantic inputs remain loadable by
their existing code paths; they are not silently upgraded or claimed as
semantic artifacts.

This Task supplies round-trip helpers and focused `.npz`/checkpoint-header
tests. It does not modify `Agent`, train a model, or make existing legacy
training automatically consume the new arrays.

## Affected surfaces and consumers

- `managym/src/cardsets/alpha.rs`: immutable manifest construction only.
- `managym/src/agent/env.rs` and PyO3 bindings/stubs: retrieve the exact
  environment manifest without adding it to every observation or public JSON
  frame.
- `manabot/semantic/`: schema loader, strict IR validator, binder, tokenizer,
  viewer projector, batch/padding adapter, and artifact-header helpers.
- `content/semantic/v1/`: checked learning schema beside the existing source
  and generated IR.
- semantic dataset/checkpoint writers and readers: opt-in header round trip and
  compatibility validation.
- `scripts/benchmark_semantic_projection.py` and checked experiment receipt:
  reproducible selected-matchup measurements.
- focused Rust/Python tests and semantic-content documentation.

The fixed Rust observation encoder, `ObservationSpace.shapes`, `VectorEnv`
caller-owned buffers, legacy action adapter, structured offers, GUI protocol,
and replay/presentation JSON remain unchanged. A fused vector-environment
semantic hot path can be considered by W2-214 after this data contract is
proven; it is not required to define or train the substrate here.

## Absent and error states

- A visible `CardDefId` not admitted by the bound semantic IR is
  `UnadmittedDefinition`; the whole projection fails rather than emitting an
  `UNK` program. The selected two-deck matchup, including referenced tokens,
  must have zero such failures.
- A missing/duplicate content binding, mismatched characteristic, invalid IR
  hash, unknown IR/learning schema, unknown opcode, opcode/name disagreement,
  unknown categorical value, malformed role reference, or out-of-range
  definition reference is a typed load error.
- Catalogs or observation batches from different semantic pack hashes cannot
  be combined.
- A card with no programs is valid and produces explicit empty program-list
  structure. An observation with no visible semantic objects is valid.
- Hidden cards and private choices have no placeholder identity, definition
  row, token span, or reference edge. Aggregate public zone counts remain in
  the existing observation and are outside this projection.
- A semantic dataset/checkpoint missing its header, or with a mismatched
  schema/pack/mode/layout, is rejected by semantic loaders. Legacy loaders
  remain unchanged.

## End-to-end proof

A focused integration test uses the actual UR Lessons versus GW Allies match:

1. reset an `Env`, obtain its exact `ContentPackManifestV1`, bind the checked-in
   IR, and assert all selected-matchup definitions resolve;
2. make `Invasion Reinforcements` visible to the acting player through the
   scenario harness and refresh the viewer observation;
3. project and batch it in both identity modes;
4. assert its definition contains program-kind, target/choice, opcode,
   structural-boundary, and `create_token` definition-reference edge tokens,
   with the reference leading to the Soldier Ally token definition;
5. determinize a cloned environment from the same viewer perspective so the
   opponent hand and libraries differ, refresh, and assert the viewer semantic
   projection is byte-identical;
6. serialize a semantic dataset header and checkpoint header, reload them, and
   reject a mutated schema hash, pack hash, and opcode; and
7. confirm existing deterministic rules traces and fixed observation shapes
   are unchanged.

Primary proof commands:

```bash
uv run scripts/compile_semantic_content.py --check
(cd managym && cargo fmt --check)
(cd managym && cargo clippy --all-targets --all-features -- -D warnings)
(cd managym && cargo test)
(cd managym && uv run maturin build --release -i ../.venv/bin/python)
# Place the cp312 extension from the built wheel at
# managym/_managym.cpython-312-darwin.so before Python verification.
uv run pytest tests/semantic tests/env/test_observation.py tests/agent/test_managym.py
uv run scripts/benchmark_semantic_projection.py --seed 215 --states 4096 \
  --batch-sizes 1,32,256 \
  --out experiments/data/w2-215-semantic-projection.json
```

## Focused proof matrix

- deterministic encoding: repeated loads, mapping key-order changes, repeated
  projections, batch permutations restored to original order, and artifact
  round trips are byte-identical;
- ContentPack binding: exact environment digest/schema, unique complete
  selected-matchup bindings, and rejection of altered/missing definitions;
- hidden-information safety: hidden determinization and opponent private-choice
  changes do not change the projection, while revealing a card does;
- ablation: no names in either model view and no valid opaque identity value in
  `semantic_only`, including under permuted `CardDefId`s;
- structural recombination: the same instruction/selector/value subtree in two
  different definitions has identical ablated token spans, while reordered
  branch/body boundaries remain distinguishable;
- reference integrity: `create_token` edges reach typed referenced definitions
  without placing definition IDs in token values;
- stable batching/masking: empty, singleton, heterogeneous-length, and repeated
  batches have exact offsets, padding, masks, dtypes, and no cap/truncation;
- rejection: unknown learning/IR schemas, unknown or mismatched opcodes,
  unknown categorical values, corrupt hashes, cross-pack batches, and
  incompatible semantic artifacts all fail closed; and
- compatibility: old observation shapes/keys and legacy artifact loaders remain
  unchanged when semantic projection is absent.

## Operational boundary and measurement

Binding, schema validation, tokenization, and catalog padding occur once per
`semantic_pack_hash` and are cached. Hot observation projection is linear in
the number of visible card/ability-source bindings; it must not parse JSON,
walk `MatchState`, duplicate the static catalog per object, or call the rules
engine. Batching is linear in objects plus explicit reference edges and has no
network or subprocess dependency.

The benchmark uses the release CPython 3.12 extension, fixed seed 215, 4,096
decision observations collected by random legal play from the selected UR/GW
matchup, and batch sizes 1/32/256. It records:

- definition/program token counts and per-visible-object p50/p95/max tokens;
- cold bind time separately from hot per-observation p50/p95 encode latency;
- observations/second and semantic tokens/second for each batch size;
- static catalog bytes, padded batch `nbytes`, Python traced peak allocation,
  and process RSS baseline/peak/delta;
- empty/unadmitted counts, seeds, deck hashes, schema/IR/content/semantic-pack
  hashes, identity mode, command, revision, OS, CPU, and Python/Rust build
  metadata.

There is no invented pass/fail speed threshold before a baseline exists. The
checked JSON receipt and a short `experiments/w2-215-semantic-projection.md`
must report all requested measurements and zero correctness failures; future
work can set a regression budget from this baseline.

## Implementation sequence (one serial PR)

1. Land the versioned learning schema and strict loader/token grammar.
2. Expose the exact immutable environment `ContentPack` manifest and bind it
   once to the semantic IR.
3. Implement the viewer-only object projection, identity ablation, ragged
   catalog, reference edges, and deterministic padding/masks.
4. Add semantic artifact headers and fail-closed compatibility helpers without
   changing legacy loaders by default.
5. Add the proof matrix and end-to-end selected-matchup test.
6. Rebuild/place the CPython 3.12 extension, run gates, execute the benchmark,
   and check in the measurement receipt.

## Exclusions

- No semantic-program interpretation in rules execution and no migration from
  the current `Effect` path.
- No learned semantic encoder, structured decoder, training run, four-arm
  comparison, held-out transfer claim, or work on W2-214/W2-213.
- No expansion beyond the already admitted two-deck semantic IR and referenced
  token closure.
- No UI/presentation/replay protocol change and no exposure of opponent hand,
  unrevealed libraries, or private choices.
- No fixed-action, structured-offer, or current model architecture change.
- No fused `VectorEnv` semantic buffer path until a downstream training Task
  demonstrates that the Python projection contract needs it.
