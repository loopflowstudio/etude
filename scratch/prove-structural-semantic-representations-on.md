# Prove Structural Semantic Representations on Diagnostic Katas

## Problem

The landed W2-214 probe established typed-opcode alignment, not program
understanding. Its semantic arm pools token embeddings with masked mean and max,
so it is intentionally invariant to token order. That model can identify an
opcode that is directly present in its input, but it cannot distinguish two
programs with the same token multiset when execution order, tree attachment,
field ownership, or role binding changes the meaning.

W2-214 must cheaply determine whether a small model can represent those static
distinctions before Intelligence pays for dynamic binding, held-out semantic
recombination, or gameplay integration. The beneficiaries are the later search
teacher and semantic-conditioned policy Projects: they need an encoder contract
whose structural capability and inference cost are measured rather than
assumed.

This work advances the Intelligence measure that "tiny semantic representation
katas distinguish equal-token programs" and the Project KR requiring a
matched-capacity, multi-seed comparison with accuracy, calibration, parameter,
latency, throughput, and decision evidence. It makes no gameplay-strength or
semantic-transfer claim.

## The demo

After the pre-registration commit exists, a developer runs:

```bash
PYTHONHASHSEED=0 uv run experiments/runners/run_structural_semantic_katas.py \
  --contract experiments/workloads/structural-semantic-katas-v1.json \
  --suite experiments/katas/structural-semantic-katas-v1.json \
  --out experiments/data/structural-semantic-katas-v1.json \
  --report experiments/w2-214-structural-semantic-katas.md \
  --preregistration-revision <revision printed by lf commit>
```

The command prints one five-row kata table per arm, the exact bag-symmetry
audit, parameter and CPU cost ratios, cross-seed uncertainty, all artifact
hashes, and exactly one terminal decision:
`NOMINATE_FOR_W2_213 relational_semantic_encoder_v1` or
`REDESIGN <failed boundary>`.

## Computable task contract

### User-visible outcome

This Task changes the research developer's offline diagnostic workflow, not a
player's gameplay. From one checked-in command, the developer can observe
whether the invariant bag encoder exhibits its exact expected symmetry and
whether one bounded relational encoder distinguishes all five static relation
families at matched data, capacity, and measured CPU cost. The observable end
state is a complete result table and exactly one declared W2-213 candidate or
redesign decision; a code-only encoder or an incomplete seed run does not make
the Task hold.

### End-to-end proof

The concrete proof starts with one source pair from each kata family and
crosses every experiment boundary:

1. the authoritative semantic compiler lowers each source program under the
   pinned learning schema;
2. an independent hand fixture and compiled-IR query agree on the opposite
   exact labels;
3. the suite compiler proves equal flattened token multisets, unequal ordered
   sequences, pair-safe content-addressed splits, nuisance disjointness, and
   identity exclusion;
4. the existing bag projection and the relational projection feed identical
   family-specific probe heads on identical examples and candidate-order
   permutations for five training seeds;
5. the runner writes the raw JSON receipt, derives the Markdown report, and
   prints the pre-registered terminal branch.

The demo command above is the end-to-end observation. It is valid proof only
when the pinned hashes and all fail-closed suite audits pass, the bag arm is
exactly 50% with no paired-logit divergence, all ten arm/seed runs and every
required metric are present, and the raw receipt and rendered report agree.
The focused `--check`, pytest, and Ruff commands in **Done when** prove the
compiler, projector, oracle, and model boundaries independently.

### Source of truth

| Authority | Owns | Derived consumers |
|---|---|---|
| `structural-semantic-katas-v1.source.json`, the pinned compiler bytes, and the pinned learning-schema bytes | Admitted diagnostic programs and their valid typed IR | Canonical suite records and model projections |
| Hand-written oracle fixtures plus deterministic queries over compiler-lowered IR | Exact binary label semantics, independent of model tokenization | Suite labels, audit expectations, and report rows |
| `structural-semantic-katas-v1.json` | Canonical programs, content hashes, pair membership, split assignment, structural relations, and leakage-audit inputs | Both model arms and suite checks |
| `structural-semantic-katas-v1.json` under `experiments/workloads/` | Predictions, model/optimizer dimensions, seeds, budgets, thresholds, and result branches frozen at the pre-registration revision | Runner validation and terminal decision logic |
| `experiments/data/structural-semantic-katas-v1.json` | Measurements, provenance, audit results, seed-level outcomes, and terminal decision | The human-readable Markdown report and W2-213 candidate intake |

The Markdown report is a derived view and must be reproducible from the raw
receipt. Neither the report nor a checkpoint may override the contract,
canonical suite, oracle, or raw measurements. W2-213 may consume only a
successful candidate nomination and its versioned static encoder contract; it
must not treat W2-214 accuracy as recombination, dynamic-binding, or gameplay
evidence.

### Affected surfaces and consumers

| Surface or consumer | Required change or compatibility boundary |
|---|---|
| Kata source, canonical suite, and workload contract | Add the versioned, deterministic, content-addressed experiment artifacts described above. |
| Suite compiler CLI | Generate and byte-check the canonical suite; abort on authority drift, invalid structure, identity leakage, or audit failure. |
| Semantic model library | Preserve the landed `TransferPolicy` bag path as the negative control; add the relation projector and bounded candidate without changing existing callers. |
| Experiment runner CLI | Validate the pre-registration revision, run both arms and five seeds, measure all declared metrics, enforce caps, and emit one receipt and report. |
| Focused test automation | Cover hand-oracle parity, compiler parity, equal-token pairs, split/leakage audits, bag symmetry, relation projection, fail-closed inputs, and deterministic replay. |
| W2-213 | Read a successful `relational_semantic_encoder_v1` nomination as a candidate for recombination and dynamic-binding probes, or read the exact redesign branch. |
| Existing gameplay, content, and world consumers | Remain byte- and behavior-compatible: no admitted `ContentPack`, observation/action ABI, runtime viewer projection, checkpoint ABI, or `WORLDS.md` change. |

There is no wire DTO, app surface, network service, database migration, or
external API in this Task. The only persisted interface is the versioned local
experiment artifact chain above.

### Absent and error states

- A missing or empty source, suite, contract, oracle family, split, arm, seed,
  metric, provenance field, or terminal decision makes the result incomplete;
  the runner emits no nomination.
- Python other than 3.12, a compiler/schema/contract/suite hash mismatch, an
  uncommitted or post-training pre-registration revision, or an amended
  threshold without a new barrier invalidates the primary run before training.
- A definition reference, opaque identity feature, unknown relation,
  malformed delimiter stack, ambiguous or dangling local role, duplicate
  normalized program, split overlap, nuisance reuse, label-correlated
  generator field, unequal pair multiset, or oracle/compiler disagreement
  fails closed and requires a repaired, newly pre-registered instrument.
- Any partial run, cap overrun, nondeterministic replay, missing latency sample,
  or report/receipt disagreement is non-evidence. Recovery is a complete rerun
  from the unchanged pre-registration, or a new amendment commit if the
  contract must change; partial seeds are never combined across contracts.
- A bag arm above its symmetry ceiling or with divergent paired logits is an
  instrument failure. Structural trainability failure selects the declared
  optimization/capacity branch; a localized semantic miss selects the
  relation redesign branch; aggregate or cost-gate failure selects the
  corresponding encoder or cost redesign. None may be repaired in-run by
  scaling or retuning.

### Operational boundary

The primary experiment is local, offline, CPU-only, and deterministic: CPython
3.12 through `uv`, one Torch thread, five fixed seeds, at most 800 optimizer
steps per arm/seed, 8,000 optimizer steps and 512,000 presented examples in
total, and 30 wall-clock minutes. It performs no network calls or external
side effects. Parameter matching includes the projection and all five heads;
latency and throughput measurements include relation projection, family
routing, and those heads. Selection requires the structural arm to stay within
5% of bag parameters, at most 2.5x bag batch-1 p95, and at least 40% of bag
batch-128 throughput. A cap or dependency failure stops the whole run instead
of degrading the sample count.

### Exclusions

This Task does not alter runtime target identity, legal offers or commands,
dynamic facts, choice handling beyond 32, definition-reference semantics,
held-out compositions or card identities, unseen-opcode semantics, gameplay
policy/search integration, PPO, admitted card coverage, Rules authority, or
Study presentation. A successful result nominates a candidate only for
W2-213. Gameplay integration remains gated on useful evidence from the full
Semantic Representation Katas Project and the independent Search Teacher &
Distillation Project.

## Approach

### 1. Put a hard commit barrier before training

The first implementation commit contains the generator, checked suite,
workload contract, model definitions, runner, tests, and a report whose status
is `PRE-REGISTERED / RESULTS PENDING`. It contains no learned weights or result
rows. Run all non-training checks, then create the barrier with:

```bash
lf commit -m "experiment: preregister structural semantic katas"
```

Capture the revision printed by Loopflow. Only then may the runner train. The
result receipt records that revision, the contract and suite SHA-256 digests,
the measurement-code revision supplied to the runner, and the absolute start
and end timestamps. The contract itself pins the SHA-256 of the exact
`manabot/semantic/compiler.py` bytes
(`f10f34d7b2e458a0ea01b124261a0c0fbbda4648e4452f277254aa6cc3250367`)
and the exact `content/semantic/v1/learning_schema.json` bytes
(`a156c592414d0d4838c5423e2cb471fc49a4450f21d62e5e5c198ba948ae7034`);
the suite compiler and runner abort if either differs. Result data and the
completed report land in a later commit. If any prediction, threshold, split,
label, model dimension, optimizer, budget, compiler hash, or learning-schema
hash changes after the barrier, the report calls it an amendment and the run
is not primary evidence until that amendment has its own pre-run commit.

### 2. Generate a small authoritative suite

Add these artifacts:

- `experiments/katas/structural-semantic-katas-v1.source.json`: synthetic,
  minimal ability programs in the existing reviewed semantic source grammar.
- `experiments/katas/structural-semantic-katas-v1.json`: canonical compiled
  records, exact labels, split assignments, structural metadata, and hashes.
- `experiments/workloads/structural-semantic-katas-v1.json`: the immutable
  experimental contract.
- `scripts/compile_structural_semantic_katas.py`: generate or `--check` the
  suite using `manabot.semantic.compiler.compile_source` and the current
  learning schema.

The programs are synthetic diagnostics, not new admitted cards. The existing
compiler remains authoritative for opcode lowering, shape validation, target
role validation, and unknown-opcode rejection. The static catalog is
viewer-safe because ability definitions and their typed operations are public,
immutable content; it contains no `Observation`, hand/library contents, chosen
objects, runtime counters, legal offer, or other match fact. In production the
existing viewer projection controls which visible object's definition is
bound. This offline suite exercises the public definition program directly and
does not bypass that future visibility boundary with runtime state.

The compiler needs synthetic registry and semantic keys to validate a source
document, but those are provenance only. The program encoder receives no
`registry_name`, semantic key, `CardDefId`, definition/program index, pair ID,
split hash, query ID, nuisance ID, or label. The five katas also forbid
`definition_ref` instructions and tokens. This is necessary because the
current `SymbolicProgramBinder` represents a definition-reference payload with
its referenced semantic key; W2-214 neither tests nor nominates that referenced-
definition identity/semantics path. The model vocabulary for this suite is
restricted to typed structures, fields, program kinds, opcodes, primitive
values/enums, and local target/choice role IDs. Opaque identity features remain
absent. The probe wrapper receives only the five-valued family enum needed to
select the corresponding diagnostic head, as specified below; it is never
embedded into or concatenated with the program representation.

Generate 80 matched pairs for each of five families: 400 pairs and 800 programs
total. Each program remains at most 64 projected tokens in the feasibility
prototype; the checked generator fails rather than truncates if any program
exceeds the pre-registered 72-token diagnostic budget. Nuisance operands,
conditions, selectors, and primitive effects vary deterministically from seed
`21400`, while every pair differs in one semantically decisive structural
relation.

| Kata | Minimal paired change | Exact binary query | Required information |
|---|---|---|---|
| order | Swap two primitive instructions. | Does probe effect A execute before B? | preorder position |
| hierarchy | Move the same leaf/subtree between outer and inner branch arms. | Is probe effect A owned by the selected outer arm? | parent/ancestor and arm relation |
| field role | Swap the same two integer leaves between `power` and `toughness`. | Is the compiled `power` delta greater than `toughness`? | field-owner relation and value type |
| argument binding | Swap two local role references between `tap` and `untap`, leaving declarations unchanged. | Does `tap` bind to the target declared with `creature_you_control`? | declaration-to-reference link |
| target/choice role | Swap which declared target role is consumed by `for_each_target` and by the sibling effect. | Does the iterated role permit multiple selected targets? | role link plus target cardinality/choice role |

Labels come from small, deterministic queries over the compiler-lowered typed
IR. Hand-written oracle fixtures pair reviewed source fragments with their
expected predicate values before tokenization; tests compile those fragments
and compare the IR query result to the fixture. Neither the fixture nor label
oracle imports the token builder, `SymbolicProgramBinder`, structural
projector, or emitted model tensors. This preserves independence between the
authority that decides the answer and the projection the model reads. These
are static queries: no observation, runtime object, legal offer, private state,
or selected target identity enters the suite.

For every pair, compilation must prove all of the following before a split is
assigned:

1. both programs are valid under the current closed opcode and role schema;
2. the `(token_kind, token_value)` multisets are exactly equal;
3. the ordered token sequences are different;
4. token counts are equal and under budget;
5. labels are opposite;
6. no opaque identity or source metadata is tensorized; and
7. neither program contains a definition reference.

The current compiler/tokenizer has already validated one concrete pair for
each family. Their lengths are 27, 57, 28, 53, and 63 tokens respectively; all
five have equal token multisets and unequal sequences. This removes the risk
that canonical field sorting or delimiter emission makes the requested
contrast impossible.

### 3. Make splits pair-safe and content-addressed

Normalize each compiled program to
`{kind, cost, targets, trigger, instructions}` before hashing so program and
definition identities do not influence content identity. A pair ID is the
SHA-256 of its family, oracle query, nuisance configuration, and two sorted
normalized program hashes. A nuisance signature separately hashes every
generator choice that is supposed to be label-independent: primitive and
condition selections, scalar operands, selectors, cardinalities, and the named
family skeleton variant, with pair orientation and the structural relation
under test removed.

Within each kata, rank pairs by
`SHA256("structural-semantic-katas-v1" || pair_id)` and assign exactly:

- 48 pairs / 96 programs to train;
- 16 pairs / 32 programs to validation;
- 16 pairs / 32 programs to test.

Both members of a pair always remain in the same split. This prevents the
strongest leakage mode: training on one member and evaluating on its nearly
identical inverse. Every family and split is exactly label-balanced. The suite
generator also rejects any repeated normalized program hash, nuisance
signature, or pair-template signature before assignment, so train, validation,
and test have disjoint nuisance configurations by construction.

The checked artifact and result receipt report exact train/validation/test
intersection counts for:

- normalized program hashes (required `0/0/0`);
- nuisance signatures (required `0/0/0`);
- pair-template signatures containing the family skeleton plus all
  label-independent generator fields (required `0/0/0`); and
- literal-erased family skeleton IDs (expected to overlap in all three splits,
  with exactly the five declared kata IDs).

The last overlap is deliberate: W2-214 tests fresh nuisance instances of five
known relations, not unseen program compositions. W2-213 owns novel
composition/template and card-identity holdouts.

For every generator field exposed in the artifact, the audit builds an exact
label contingency table. Each label-independent field/value must occur equally
often with labels 0 and 1, and pair members must differ only in the one declared
structural relation. Token length, token multiset hash, opcode counts, value
histograms, selector histograms, and skeleton ID receive the same audit. Any
unexpected duplicate, split overlap, unequal contingency table, or undeclared
side-correlated field invalidates the suite before training.

The suite stores per-program, per-pair, per-split, source, exact compiler-file,
learning-schema, and full-suite digests. `--check` verifies the pinned compiler
and schema hashes, recompiles, and byte-compares canonical JSON.

This split tests whether an encoder learns the five declared structural
relations on fresh nuisance instances. It is not the W2-213 held-out
composition or held-out-card benchmark and must not be described as semantic
recombination.

### 4. Preserve the bag as the negative control

The `bag_v1` arm uses the landed `TransferPolicy` semantic-only path with
`hidden_dim=32`, masked mean/max token pooling, and the same context projection.
Its encoder output is projected to the common `probe_dim=24`. It gets token IDs
and the validity mask only. It never receives positions or structural
relations.

The five families answer five different predicates, so both arms use the same
explicit probe interface rather than one unconditioned two-label head:

```text
KataProbe.forward(
    program_tokens,
    token_mask,
    structural_relations_or_none,
    family: KataFamily,
    candidate_order,
) -> two candidate logits
```

`KataFamily` selects one of five independent two-candidate dot-product heads:
`order`, `hierarchy`, `field_role`, `argument_binding`, or
`target_choice_role`. Each head has the same `probe_dim=24` architecture,
initialization rule, optimizer treatment, and candidate-order permutation in
both arms; corresponding bag and structural heads start from byte-identical
parameters for a given seed and are then trained separately. Family is a typed
head route, not a learned embedding or encoder feature. A batch may contain
multiple families; routing and gathering logits occur inside the timed model
call.

All five head parameter counts, the bag's 32-to-24 output projection, routing,
and head inference cost are included in arm capacity and performance matching.
The structural arm uses the identical five-head wrapper over its 24-wide
summary representation. This resolves task identification without giving one
encoder a query channel the other lacks.

Because each pair has the same multiset and opposite labels, this arm has a
mathematical 50% ceiling per pair regardless of training data or parameter
count. The runner audits the mechanism directly: paired logits must differ by
at most `1e-6`, paired predictions must disagree 0 times, and accuracy must be
exactly 50% for every kata and seed. A bag result above 55%, any paired
prediction disagreement, or input-multiset mismatch invalidates the instrument
instead of counting as a better model.

The negative-control rationale follows the permutation-invariant set-function
boundary formalized in [Deep Sets](https://arxiv.org/abs/1703.06114). The
experiment does not ask a larger bag model to recover information its pooling
operation deletes.

### 5. Test one explicitly relational encoder

The candidate `relational_semantic_encoder_v1` is one small, pre-norm,
relation-aware self-attention block:

- token embedding over the same symbolic vocabulary as `bag_v1`;
- explicit token-kind embedding;
- deterministic sinusoidal preorder-position and tree-depth encodings;
- `d_model=24`, 2 attention heads, `d_ff=28`, one block, no dropout;
- one learned summary token followed by a 24-wide tanh context projection,
  feeding the identical five-head `KataProbe` wrapper used by the bag arm;
- additive per-head attention biases for clipped relative sequence distance,
  directed parent/child and ancestor relations, directed field-owner edges,
  and local target/choice-role declaration/reference edges.

The structural projector derives relations from the already-emitted canonical
token stream without changing the production observation/action ABI:

- matched begin/end delimiters define the parent stack and depth;
- a value token attaches to its active typed field token;
- the local role declared by `target.role` is the unique declaration node for
  later equal-role references.

Malformed delimiter stacks, ambiguous role declarations, dangling references,
unknown relation types, and any W2-214 program carrying a `definition_ref` fail
closed. Tests compare the projector's token IDs to the existing
`SymbolicProgramBinder` for every admitted program without a definition
reference and the full kata suite; separate fixtures prove that referenced-
definition programs are rejected from this experiment. The candidate's local
role-link result therefore cannot be restated as evidence about referenced
card definitions.

This is deliberately smaller than a generic graph library. Relation-aware
self-attention is justified because it extends attention to directed labeled
graphs ([Shaw, Uszkoreit, and Vaswani](https://arxiv.org/abs/1803.02155)), and
Graphormer's result identifies explicit structural encoding as the key change
for graph inputs ([Ying et al.](https://arxiv.org/abs/2106.05234)). A plain
positional Transformer is not sufficient evidence for the hierarchy and link
requirements.

Before training, the runner prints and checks trainable parameter counts. With
the checked vocabulary, the fixed dimensions above must place the structural
arm within 5% of `bag_v1`, including the common output width, all five family
heads, and bag projection; otherwise the contract is invalid and no training
starts. Do not resize either model after seeing a result.

### 6. Fix data, optimization, seeds, and compute

Use model seeds `[21401, 21402, 21403, 21404, 21405]`. Five seeds are cheap and
make seed-level uncertainty more useful than the minimum requirement of three.
For both arms and every seed:

- CPython 3.12 only; abort on any other minor version;
- CPU only, one Torch thread, deterministic algorithms enabled;
- AdamW, learning rate `0.003`, zero weight decay;
- batch size 64, deterministically shuffled from the model seed;
- maximum 800 optimizer steps, validation every 20 steps;
- select the lowest validation NLL checkpoint with earliest-step tie-break;
- identical train/validation/test examples, candidate-order permutations,
  optimizer budget, and stopping rule across arms;
- identical per-family loss weighting: each batch is family-balanced and the
  five head losses are averaged with weight `0.2` each;
- no pretrained embeddings, identity features, label smoothing, augmentation,
  or model-specific hyperparameter search.

The primary run cap is 8,000 optimizer steps total
`(2 arms × 5 seeds × 800)`, at most 512,000 presented training examples, one
CPU core, and 30 wall-clock minutes. The runner aborts the entire experiment at
the first cap exceeded; it does not continue with a partial seed table. A failed
trainability diagnostic may nominate a separately pre-registered optimizer
study, but this run does not adapt its learning rate or increase model size.

Before interpreting test accuracy, require the structural arm to reach at
least 99% train accuracy and 95% validation accuracy on every seed. Failure is
classified as optimization/capacity unresolved, not evidence that structural
semantics is impossible.

### 7. Pre-register predictions and gates

Predictions:

- `bag_v1` will produce zero paired prediction disagreements and exactly 50%
  accuracy for every kata and seed; aggregate Brier score will be approximately
  0.25 and NLL approximately `ln(2)`.
- `relational_semantic_encoder_v1` will reach at least 95% aggregate test
  accuracy and at least 90% on every kata in the across-seed mean.
- Its paired uplift over `bag_v1` will be at least 40 percentage points for
  every kata.
- Aggregate Brier score will be at most 0.10 and NLL at most 0.35. Calibration
  is a prediction, not a post-hoc temperature-scaled result.
- Trainable parameters will differ by no more than 5%; structural batch-1 p95
  model latency will be no more than 2.5 times the bag arm and batch-128
  throughput will be at least 40% of the bag arm on the same CPU process.

The nomination gate requires all of the following:

1. every compiler, hash, split, leakage, budget, and fail-closed invariant is
   green, including pinned compiler/schema hashes, zero normalized-program,
   nuisance, and pair-template overlap, exact nuisance-label balance, and
   definition-reference exclusion;
2. the bag symmetry audit is green and bag accuracy is exactly 50%;
3. every structural seed clears the trainability diagnostic;
4. structural mean aggregate test accuracy is at least 95%, every per-kata
   mean is at least 90%, and every per-kata paired uplift is at least 40 points;
5. the across-seed 95% t-interval lower bound for aggregate structural-minus-
   bag accuracy is above 35 points;
6. aggregate Brier is at most 0.10 and NLL at most 0.35;
7. parameter and performance gates pass.

Result branches are fixed in advance:

- **Nominate:** all gates pass. Record
  `relational_semantic_encoder_v1` only as the candidate encoder for W2-213's
  recombination and dynamic-binding work, with no gameplay or recombination
  claim from W2-214 itself. Gameplay integration remains prohibited until the
  full Semantic Representation Katas Project and the independent Search
  Teacher & Distillation Project both produce their required useful evidence.
- **Instrument invalid:** bag symmetry, equal-multiset, label balance, split,
  identity exclusion, or hash checks fail. Kill the result, repair the suite,
  and re-pre-register before training again.
- **Teacher/label error:** hand oracle fixtures or compiler parity fail. Kill
  the result and repair authority before touching the model.
- **Optimization/capacity unresolved:** structural training or validation
  accuracy misses its diagnostic. Do not scale the model in this task; propose
  a bounded optimizer/architecture diagnostic.
- **Missing structural relation:** training succeeds but one or two katas miss
  the test gate. Use the per-kata failure to redesign the corresponding
  projection relation; do not average it away.
- **Encoder redesign:** training succeeds but aggregate accuracy fails, or
  three or more katas fail. Reject the one-block relational Transformer and
  next test a typed tree/message-passing encoder under a new contract.
- **Cost redesign:** static semantic gates pass but
  parameter/latency/throughput gates fail. Keep the semantic finding, withhold
  nomination, and next test a cached typed-tree encoder. Do not hide cost by
  excluding the structural projector.

### 8. Report mechanism and uncertainty, not just an aggregate

The JSON receipt and Markdown report include, for every arm and seed:

- train, validation, aggregate test, and per-kata accuracy;
- NLL, Brier score, and fixed five-bin ECE, aggregate and per kata;
- paired prediction disagreements and paired structural-minus-bag outcomes;
- exact normalized-program, nuisance-signature, pair-template, and intentional
  family-skeleton overlap matrices, plus every nuisance-field label contingency
  table;
- trainable parameter count and bytes, broken out by encoder, common output
  projection, and all five family heads;
- model-only batch-1 p50/p95 latency after 200 warmups over 2,000 fixed-order
  samples;
- catalog-projector-plus-model batch-1 p50/p95 latency;
- batch-128 examples/second and tokens/second;
- optimizer steps, selected checkpoint step, wall time, and presented examples;
- Python, Torch, platform, CPU/thread, pinned compiler-file hash, pinned
  learning-schema hash, source, suite, split, contract, and pre-registration
  provenance.

Treat the five training seeds as the independent units. Report mean, sample
standard deviation, min/max, and a two-sided 95% t interval across seeds for
each primary metric and paired arm difference. Example-level intervals may be
included only as checkpoint-description noise and are never substituted for
seed uncertainty.

## De-risking

| Question | Finding | Impact on design |
|---|---|---|
| Can the existing bag ever solve equal-token opposite-label pairs? | No. Its masked mean/max pooling is permutation invariant; the limitation is architectural, not a shortage of examples. | Make exact paired symmetry the negative-control mechanism audit and treat any bag uplift as leakage. |
| Can the current compiler and tokenizer actually express all five equal-multiset contrasts? | Yes. Concrete compiler-lowered prototypes for order, hierarchy, field role, argument binding, and target/choice role all have equal token multisets, unequal sequences, and lengths 27–63. | Use the authoritative compiler/tokenizer rather than a parallel toy grammar. |
| Does the current projection retain enough raw information for a structural model? | Yes. It emits ordered typed tokens, begin/end structures, field markers, and local role IDs; the bag discards those relationships only during pooling. | Add position, hierarchy, field-owner, and local-role relations without changing opcodes or the gameplay ABI. Exclude definition references from this claim. |
| Is position alone enough? | It can recover order, but it makes hierarchy, field ownership, and nonlocal role references implicit and leaves a null result ambiguous. | Use one relation-aware block with explicit directed relations. |
| Could train/test twins leak the answer? | Yes, if pair members are split apart; a model could memorize one member and infer its inverse. | Assign whole pairs to splits, content-address the assignment, and balance within every family/split. |
| Could generator templates leak the answer without exact duplicate programs? | Yes. Label-correlated operands, selectors, lengths, or nuisance reuse could make a structural result spurious. | Make normalized programs, nuisance signatures, and pair-template signatures disjoint across splits; require exact per-field label balance and report all overlap matrices. |
| Could labels or identity leak through metadata? | The compiler requires names and keys, and `SymbolicProgramBinder` uses a semantic key as a `definition_ref` payload. | Exclude registry names, semantic keys, CardDefId, indices, pair/split/query IDs, and all definition references from model tensors. Route only the typed family enum to identical per-family heads. |
| Can one unconditioned head answer five different predicates? | No. Without a query it would infer the task from family-correlated content, confounding encoder quality with task identification. | Give both arms five identical family-specific probe heads. Family selects a head but is never embedded into the program representation; include every head and routing cost in the match. |
| Could a result be blamed on extra structural-model capacity? | The proposed 24-wide block is close to the landed 32-wide bag count for the expected vocabulary. | Refuse to train unless exact trainable counts are within 5%; keep data and optimizer budgets identical. |
| Could a failure merely be bad optimization? | Yes, especially with five distinct relation katas. | Require per-seed train/validation diagnostics and branch to an optimizer study rather than calling a representational null. |
| Is calibration estimable on a tiny suite? | Per-example ECE is noisy, while Brier/NLL are proper scores and 160 test programs provide only modest bin counts. | Make Brier/NLL primary, fix ECE at five bins, and express uncertainty across seeds. |
| Are the labels independent of model tokenization? | Yes by design: hand-written source/answer fixtures are checked against compiled-IR queries, and label code cannot import the token builder or projector. | Pin compiler and learning-schema hashes so neither the authority nor input contract can drift silently. |
| Does this create a new gameplay world? | No observation/action tensor, admitted ContentPack, or checkpoint ABI changes. This is an offline diagnostic representation. | Keep `WORLDS.md` unchanged; a later production semantic input contract owns any world change. |
| Can the environment silently use the wrong Python? | Yes. During kickoff, unconstrained `uv run` initially selected CPython 3.14 because `pyproject.toml` allows it, despite the repo's PyO3 pin. The disposable venv was corrected to 3.12.12. | Runner aborts unless Python is 3.12; all documented commands remain `uv run ...`. |

## Alternatives considered

| Approach | Tradeoff | Why not |
|---|---|---|
| One shared two-label head plus a content-addressed typed query input | Makes the predicate explicit and could generalize to future probes. | It adds a second learned input whose interaction with the encoder becomes another causal variable. Five identical routed heads are simpler for five fixed diagnostics and keep query semantics out of the program representation. |
| Plain Transformer with absolute positions | Small and likely solves instruction order and adjacent field/value swaps. | A failure on hierarchy or role binding would not distinguish inadequate training from missing graph relations; it does not explicitly satisfy the link contract. |
| Typed TreeLSTM or message-passing tree encoder | Strong hierarchy bias and linear complexity. | Role references and execution order are cross-tree/directional edges, so it needs extra machinery anyway. Keep it as the declared redesign if relation-aware attention fails or costs too much. |
| Large generic Graphormer stack | Rich relation modeling and familiar implementation. | Unnecessary for sequences under 72 tokens, violates the bounded matched-capacity intent, and could hide a bad instrument with scale. |
| Add more invariant MLP capacity to the bag | Produces a cleaner raw parameter match. | No invariant function can separate opposite labels on identical multisets; capacity cannot restore deleted structure. |
| Train on the 31 real admitted definitions | Reuses production content. | The current pack does not provide controlled, balanced equal-multiset minimal pairs; identity and opcode correlations would dominate. |
| Join runtime objects and legal offers now | Would exercise real target and command selection. | That is the separate dynamic-binding KR. Mixing it here would make a null result uninterpretable and violate the W2-214 directive. |

## Key decisions

- The suite proves structural *discriminability*, not held-out composition,
  card transfer, gameplay strength, or executable rules parity.
- Synthetic programs use the authoritative closed typed grammar; they do not
  broaden card coverage or become a ContentPack.
- Pair membership is the causal unit for the instrument; training seed is the
  independent unit for method uncertainty.
- Five kata families remain separate in every table and use five identical
  family-specific heads in both arms. An aggregate cannot hide a missing
  hierarchy or role-link mechanism, and the encoder never has to guess which
  predicate its logits answer.
- The structural candidate is one relation-aware block with explicit position,
  hierarchy, token type, field, and local-role link information. Definition
  references are excluded. It is a candidate for W2-213, not a foregone
  production architecture.
- Normalized programs, nuisance configurations, and pair templates are
  disjoint across splits. Literal-erased family skeletons intentionally remain
  shared because W2-213, not this task, owns unseen composition.
- The bag arm is expected to fail exactly. The experiment succeeds when that
  failure is mechanistically demonstrated and the structural arm either clears
  every declared gate or yields a precise redesign boundary.
- No result-contingent model growth or optimizer tuning occurs inside the
  primary run.

## Wild success

The one-block encoder clears all five katas at calibrated confidence while
remaining near 10k parameters and within 2.5 times the bag's batch-1 p95. The
surprising practical win is that static card programs can be encoded once per
immutable catalog definition and cached; future per-decision cost is then the
cheap object-to-definition binding already measured by W2-215, not a repeated
parse of every visible card. The result nominates a crisp static encoder
candidate for W2-213's recombination and dynamic-binding tests. It does not
unlock gameplay integration.

## Wild failure

Six months later this work is discarded because the generated labels captured
generator templates rather than semantic relations, a positional model
memorized nuisance patterns, or a static encoder was prematurely described as
gameplay understanding. Pair-safe splits, identity exclusion, per-kata tables,
proper calibration, explicit claim boundaries, and the prohibition on dynamic
integration are the defenses. W2-213 remains responsible for genuine
recombination evidence; a pass here cannot substitute for it.

## Scope

- In scope: a committed pre-registration; deterministic compiler-validated
  equal-token paired katas for order, hierarchy, field role, argument binding,
  and target/choice role; exact static labels; content-addressed pair-safe
  nuisance-disjoint splits and anti-template audits; the landed bag negative
  control; identical per-family probe heads; one matched-capacity relational
  encoder; five seeds; accuracy, calibration, cost, and decision evidence.
- Out of scope: runtime target identity, legal offers or commands, choices
  beyond 32, dynamic match facts, W2-213 compositional/card holdouts, unknown
  opcode handling beyond fail-closed admission, definition-reference
  semantics, full gameplay policy or search integration, PPO, new cards, rules
  changes, model scaling, and win rate.

## Done when

The implementation is complete when:

1. the pre-registration commit predates every training timestamp;
2. the checked suite compiles byte-identically and all pair/hash/leakage/oracle
   invariants pass;
3. focused checks pass:

   ```bash
   uv run scripts/compile_structural_semantic_katas.py --check
   uv run --extra dev pytest -q \
     tests/semantic/test_structural_katas.py \
     tests/semantic/test_structural_encoder.py
   uv run --extra dev ruff check \
     manabot/semantic \
     experiments/runners/run_structural_semantic_katas.py \
     scripts/compile_structural_semantic_katas.py \
     tests/semantic/test_structural_katas.py \
     tests/semantic/test_structural_encoder.py
   ```

4. the primary five-seed command completes inside the hard cap and writes a
   complete machine-readable receipt plus the result report;
5. the report contains every per-kata, calibration, parameter, latency,
   throughput, overlap/leakage, pinned-provenance, and seed-uncertainty field;
   and
6. the report ends with either the nominated
   `relational_semantic_encoder_v1` candidate for W2-213 or one explicit
   redesign branch. W2-214 never nominates gameplay integration; that remains
   gated on completed useful evidence from both the full Semantic
   Representation Katas Project and Search Teacher & Distillation Project.

## Measure

Baseline before training is a theorem-backed and audited 50% per-pair ceiling
for `bag_v1`, plus its measured parameter and CPU cost. The structural arm is
better only if it crosses the full nomination gate above at matched data and
capacity. The headline chart is five per-kata paired uplifts with seed-level
95% intervals; aggregate accuracy is secondary to all-five coverage.
