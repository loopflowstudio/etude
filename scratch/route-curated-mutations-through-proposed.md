# W2-190: Route curated mutations through proposed events

## Outcome

Curated gameplay mutations no longer change match state before replacement and
prevention effects can inspect them. Damage, direct life changes, destruction,
+1/+1 counter changes, and zone movement enter one typed internal pipeline:

1. propose an event against exact object identity;
2. collect matching immutable replacement definitions;
3. apply each candidate once in deterministic order;
4. commit the surviving event and emit the existing factual `GameEvent`s.

Players and agents observe the same action, observation, and presentation
shapes for existing cards. Driver fixtures additionally demonstrate prevented
damage and permanents entering tapped or with counters.

## Source of truth

`GameState` remains the authoritative mutable match. `ProposedEvent` is an
ephemeral typed command, never a second event log. Replacement meaning lives in
immutable `CardDefinition` data shared through `ContentPack`; committed
`GameEvent`s remain the trigger/presentation/training projection.

Live permanent targets and sources use W2-180 `ObjectRef`s. A zone proposal may
have no departing ref only when an entity is genuinely being created outside
all zones (the existing token-creation seam). Stale refs are rejected without
mutation; battlefield departure still records W2-180 LKI before incarnation is
advanced.

## Curated contract

The initial proposed variants are:

- damage from an optional exact source to a player or exact permanent;
- signed life delta for one player;
- destruction of one exact permanent;
- signed +1/+1 counter delta for one exact permanent;
- one card/entity move from its observed zone to another, including
  battlefield-entry facts.

The initial declarative replacements are deliberately narrow:

- prevent N damage that would be dealt to the source's controller or their
  permanent;
- double that damage;
- this object enters tapped;
- this object enters with N +1/+1 counters.

Applicable battlefield replacements are sorted by `(source ObjectRef,
definition index)`. Intrinsic entry replacements use the entering object's
definition order. Each collected candidate applies at most once. This is the
replay contract for this slice.

Damage to a player commits its derived life loss inside the damage commit so
prevention happens exactly once. Direct gain/loss and lifelink use the life
proposal. Destruction commits by proposing the consequent battlefield-to-
graveyard zone move, allowing the zone authority to retain incarnation, LKI,
trigger, and presentation behavior.

## End-to-end proof

One trace places deterministic prevention and doubling drivers on a player's
battlefield, resolves Lightning Bolt through the ordinary cast/priority path,
and asserts the ordered transformed life total plus the unchanged committed
`DamageDealt`/`LifeChanged` observation shapes. Repeating the seeded trace must
produce identical state and event output. Companion traces cover a nonmatching
target, full prevention, intrinsic enters-tapped/with-counters behavior, stale
identity rejection, and the existing no-replacement Bolt trace.

Focused proof command:

`cd managym && cargo test --test rules_tests cr_614_replacement`

The full Rust suite plus existing Python observation/protocol tests provide the
compatibility proof before landing.

## Affected surfaces and consumers

- `state/card.rs`: immutable replacement definitions; empty definitions skip
  serialization so the current content fingerprint and card observation ABI do
  not change.
- `flow/proposed_event.rs`: internal propose/replace-or-prevent/commit authority.
- `flow/damage.rs`, `flow/zones.rs`, `flow/resolution.rs`, `flow/sba.rs`, and
  combat callers: adapters into the authority rather than direct mutation.
- Trigger collection, `GameEvent`, observation encoding, Python bindings,
  structured offers, presentation protocol, and fixed action indices remain
  consumers with unchanged shapes.

Scenario-only direct state injection remains an explicit test/measurement
bypass and is not routed through gameplay events.

## Absent and error states

- zero/negative damage and zero counter/life deltas are no-ops;
- a fully prevented event commits no damage/life mutation and emits no factual
  damage/life event;
- missing entities, stale incarnations, wrong-zone objects, or changed
  from-zones reject the proposal without partial mutation;
- empty replacement sets commit exactly the pre-task behavior;
- invalid replacement parameters are rejected by debug assertions and have no
  content registrations in the default pack.

## Operational boundary

Candidate collection is allocation-bounded by the current battlefield and
small per-card replacement vectors. It uses deterministic dense iteration and
sorting, with no subprocess, network, random choice, state clone, or new player
decision. The pipeline adds no mutable replacement journal in this slice.

## Exclusions

- affected-player/controller ordering choices and all other CR 616 choice
  branches;
- replacement applicability recomputation, self-replacement loops, dependency
  ordering, and simultaneous-event replacement;
- consumable "next N damage" shields, prevention that redirects damage, and
  replacement-generated decisions;
- draw/token/cost replacement families, indestructible, and new production
  card registrations;
- broad rollback/undo or trigger/SBA fixpoint redesign.
