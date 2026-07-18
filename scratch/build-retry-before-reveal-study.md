# Retry Before Reveal Study Vertical Slice

## Problem

Etude Fantasia has the separate foundations of the first Study moment: Game
records every deliberate decision under a viewer-safe canonical replay address,
Rules can fork one retained historical root and execute an ordinary structured
command without mutating the recording, and the shared Study contract keeps
played, policy, search, robustness, uncertainty, and provenance evidence
distinct. The player still cannot experience those pieces as one honest loop.

The immediate product problem is to let the player open the complete decision
timeline for the selected completed UR Lessons versus GW Allies match, restore
one exact decision, try a legal line before seeing any policy or search judgment,
then compare three plainly labelled plans on the same semantic table and return
to the identical recorded position.

The final Intelligence provider is not on this Task base. The only Intelligence
export symbol is
`manabot.sim.study_evidence.build_study_artifact(audit, ...)`. It accepts a
Teacher-1 trajectory audit, hard-codes `source_replay_id` to
`int-4-trajectory-audit-v1`, and derives its ordinal from that audit chronology.
It accepts no `CanonicalReplayV1`, trace ID, `ReplayDecisionAddress`, restored
decision, or retained Study root. Its policy mass is also a caller-supplied
mapping rather than a historical model inference. Therefore it cannot produce
the policy-and-search evidence for the Game-issued `replay.<match_id>` selected
by the player. The only checked-in artifact that does bind
`replay.pinned-curated-match` is explicitly fixture
evidence (`producer: canonical-replay-fixture`, `analysis_budget.id:
fixture-only`, one node); using it at runtime would falsely label placeholder
numbers as policy and search judgment.

That gap does not block the reviewed Retry/return substrate. This PR defines a
typed historical-evidence request and response boundary, implements the exact
branch lifecycle and player interaction around it, and installs an unavailable
production provider that fails closed with “Study evidence is unavailable for
this recording.” A fixture-backed provider may be injected only by tests to
exercise the post-reveal UI and semantic preview path. Normal `./scripts/play`
must never synthesize policy/search values, rewrite artifact identity, or serve
fixture evidence. Intelligence can later plug in one callable or stored lookup
that returns a validated artifact bound to the exact Game replay without
changing the Study interaction.

## The demo

Run `./scripts/play`, finish the pinned UR Lessons versus GW Allies matchup,
open `/replay`, and choose a human decision from the complete Score. The exact
table position opens with only canonical Retry choices. After one choice is
accepted, Reveal becomes available and Return remains available; with the
default unavailable provider, Reveal fails closed without losing the branch,
and “Return to score” restores the original frame, offer, event cursor, and
timeline selection in one action.

The real-stack test injects the checked exact-match fixture provider through the
server constructor—not configuration or an environment variable—and proves the
future provider path: Reveal shows separately labelled Played, Policy, and
Search cards; selecting a card focuses authoritative table objects and applies
exactly one command on a fresh fork to produce one bounded
`PresentationEvent` sequence before returning to the unchanged recording.

The same flow works with pointer or keyboard, uses a short non-animated beat in
reduced-motion mode, and stacks the timeline, table, and plan cards into a
touch-friendly single column on a phone.

## Approach

### 1. Treat canonical replay as navigation truth

When a trace is selected, fetch `/api/traces/{trace_id}/decisions`, validate the
`CanonicalReplayProjectionResponseV1`, and render every player-zero row in
global ordinal order. The Score changes from legacy trace-event navigation to
canonical decision navigation for Study; ordinary frame-by-frame replay remains
available and unchanged.

Each decision button shows turn, prompt kind, and the authoritative decision
ordinal; it does not print the played offer before reveal. Activating it fetches
`/api/traces/{trace_id}/decisions/{address}` and renders the returned
`ExperienceFrame.projection` directly. No client code reconstructs a frame,
legal action, hidden card, or presentation episode from trace snapshots.

### 2. Make Retry and reveal a server-enforced state machine

Game owns a small in-memory `StudyAttempt` keyed by an unpredictable attempt
ID and bound to `(trace_id, address, viewer, session expiry)`. It is available
only while the completed match's retained `StudyForkProvider` remains in the
live `GameSession`; an old or expired trace continues to support replay but
reports Retry as unavailable instead of pretending to reconstruct a rules root.

The state sequence is:

```text
recorded decision -> retry command accepted -> reveal enabled -> plan preview
        ^                                                       |
        +--------------------- return --------------------------+
```

Return is available from every Study state. Leaving before reveal simply
closes the branch; it never reveals implicitly. Reveal has no bypass: the
server rejects it until the attempt records one accepted ordinary command.

`POST /api/traces/{trace_id}/decisions/{address}/retry` accepts a normal
protocol `Command`. The server restores the address, forks a fresh
`StudyBranch`, publishes its structured offers, verifies that the submitted
command binds the restored match/revision/prompt and one currently published
offer, and calls `StudyBranch.submit`. The response contains only the
viewer-safe branch projection, the bounded events caused by that command, the
attempt ID, and the exact recorded return identity. It contains no
`StudyArtifact`, policy mass, visits, values, robustness, uncertainty, model,
budget, or provenance.

The provider boundary is explicit and Game-owned at the join:

```python
@dataclass(frozen=True)
class HistoricalStudyEvidenceRequest:
    projection: CanonicalReplayProjectionV1
    source_replay_sha256: str
    address: str
    restored: RestoredReplayDecision

class HistoricalStudyEvidenceProvider(Protocol):
    def artifact_for(
        self, request: HistoricalStudyEvidenceRequest
    ) -> StudyArtifact: ...
```

Game adds one canonical semantic digest helper for the complete viewer
projection and supplies that digest rather than trusting a provider string. The
default provider raises a typed `StudyEvidenceUnavailableError`. It is wired by
normal server construction and cannot be replaced by an environment variable
or client input. Tests may inject a fixture provider directly into the server
or `GameSession` constructor; this keeps fixture evidence mechanically absent
from the player runtime. The replay fixture generator switches to the same
digest helper, and the Rust, Python, and TypeScript Study fixture tests certify
the regenerated value.

Only after one retry command is accepted may
`POST /api/study-attempts/{attempt_id}/reveal` ask the injected provider for
evidence. Game validates the returned `StudyArtifact` on the server, compares
its source digest with the Game-computed viewer-projection digest, and then
explicitly joins the requested address to exactly one landmark. The landmark's
frame, offer, played command, source replay, match, content, asset manifest,
viewer, and address must equal the restored Game objects byte-for-semantic-byte.
An unavailable provider returns typed `study_evidence_unavailable`; malformed,
fixture-only in production, missing, or drifted evidence returns a typed
fail-closed error. None of these errors closes the attempt, so the player can
still Return. The TypeScript consumer validates a successful artifact again.

`POST /api/study-attempts/{attempt_id}/return` closes the branch through
`return_to_recorded`, deletes the attempt, and returns the original
`RestoredReplayDecision`. Starting another attempt for the same address forks a
fresh immutable root. Study attempts never append trace events, replay rows,
accepted live commands, or presentation history.

### 3. Project one semantic, bounded plan continuation

Game reuses the existing `PresentationProjector` authority adapter for branch
previews. It stages the exact selected `InteractionOffer`, consumes the
committed branch observation's `recent_events`, and drains only the single
command transition as protocol `PresentationEvent` objects. It never compares
before/after snapshots. The recorded Played plan uses the already canonical
`RestoredReplayDecision.continuation`, bounded at the next decision for the
same viewer. Every Policy or Search plan preview—whether or not it agrees with
Played—forks the same retained root fresh, applies exactly that plan's ordinary
command, emits only that committed transition's events, calls
`return_to_recorded`, and discards the preview fork. A preview never reuses the
player's Retry branch or carries state into the next preview.

The first product slice chooses one evidence-backed landmark whose selected
offer or one comparison offer produces at least one authored semantic event.
If the provider supplies no such landmark for the selected matchup, the server
reports the exact lack of previewable evidence rather than expanding the
presentation vocabulary or narrating a snapshot diff in this Task.

### 4. Keep three judgments visibly distinct

After reveal, derive three display plans from one validated landmark:

- **Played** is the exact historical `played` command.
- **Policy** is the alternative with greatest `policy_mass.probability`, with
  original frame-offer order as the stable tie break.
- **Search** is the alternative with greatest viewer-perspective
  `expected_match_points`, then visits, then lower uncertainty, then frame-offer
  order.

The cards remain separate even when two or all three select the same offer; in
that case they say “Same line” instead of implying independent choices. Policy
probability is displayed only as probability. Search value, visits,
sampled-world robustness, and uncertainty retain their own labels and units;
none is collapsed into a generic confidence score.

Selecting or focusing a plan resolves its command's `offer_id` back through the
restored `frame.offers` and passes that offer's authoritative `focus` IDs to
`GameBoard`. No plan carries client-authored object IDs. The existing
`PresentationPlayer` plays the returned bounded sequence and retains its Skip,
Fast-forward, Finish, and reduced-motion behavior.

### 5. Make the one moment complete on each input surface

- The decision timeline and Retry offers are native buttons. Opening a
  decision moves focus to the Retry choices; an accepted retry moves focus to
  Reveal; reveal moves focus to the Played plan. Escape from plan comparison
  and “Return to score” both restore focus to the originating timeline button.
- Plan cards form a labelled radio group with arrow-key movement, Enter/Space
  activation, `aria-checked`, and a polite live summary of the selected plan
  and current semantic beat. Pointer hover and keyboard focus use the same
  authoritative board highlight path.
- At widths below 640px, the Score is a capped scroll region above the table,
  controls and plan cards are one column, and every button retains the existing
  44-pixel minimum touch height. No operation depends on hover.
- `prefers-reduced-motion: reduce` uses the existing 100 ms presentation beat,
  removes scrolling/transform animation, and keeps the final committed board
  and text summary readable after the beat.

## De-risking

| Question | Finding | Impact on design |
|----------|---------|-----------------|
| Does GAM-3 expose the complete viewer-safe timeline and exact restore? | Yes. `GET /api/traces/{trace}/decisions` projects every authorized row with an `erd1` address; the address endpoint restores the exact frame, offer, command, cursor, and bounded continuation. Python, Rust debug, and TypeScript replay tests pass. | Use this API as the only timeline and recorded-continuation truth. Do not build Study navigation from legacy trace frames or the ranked landmark list. |
| Can Rules fork and execute the exact historical decision without changing replay? | Yes, while the completed `GameSession` retains its private roots. `StudyForkProvider.fork`, `StudyBranch.structured_offers`, `submit`, and `return_to_recorded` pass their exact-root/non-mutation tests. Roots are intentionally not persisted in public traces. | Add only a Game HTTP/session adapter around the merged provider. Old or expired recordings honestly lose Retry but retain replay. Do not persist or expose engine roots. |
| Can Intelligence provide evidence for the selected Game replay? | No. `build_study_artifact` accepts only a Teacher-1 audit, hard-codes `int-4-trajectory-audit-v1`, and has no replay/address parameter. A direct probe found 48 viewer decisions in `replay.pinned-curated-match` and no accepted replay, trace, address, or decision argument. | Proceed with Retry/return and a typed `HistoricalStudyEvidenceProvider`. The production default fails closed; an exact Intelligence provider can replace it without changing Game interaction or authority. |
| Does `source_replay_sha256` have a runtime join algorithm? | Not yet. The pinned artifact hashes the checked-in pretty-printed viewer projection bytes, while `build_study_artifact` trusts a caller-provided digest and the Study validators do not compare it with a loaded replay. | Game owns canonical replay, so this PR adds a canonical semantic viewer-projection digest helper and passes its result into the provider request. Reveal rejects any returned digest mismatch. |
| Does the checked-in matching Study fixture unblock runtime? | No. `study-curated-decision.json` binds the pinned replay but declares `canonical-replay-fixture`, `fixture-only`, one node, uniform probability, and zero values. | Inject it only in unit and real-stack tests through a constructor-owned provider. Never make it selectable by client, environment, or normal runtime configuration. |
| Is evidence sealed if the client merely hides a prefetched artifact? | No. Prefetching exposes judgment in the network response and devtools before Retry. | The server does not serialize or fetch the artifact into the attempt response. Reveal is a separate endpoint allowed only after an accepted retry command. |
| Can plan theater be produced without snapshot diffs? | Yes for the slice: the branch returns committed engine `recent_events`; Game's existing projector turns committed semantic events plus the exact offer into protocol events. Recorded play already carries a canonical bounded continuation. | Reuse the presentation authority and limit alternatives to one command transition. Do not add textual diff narration. |
| Can a trace loaded after process/session expiry still Retry? | No. Canonical replay persists, authority-private cloned roots do not. Reconstructing from trace fields would require hidden authority or seed semantics absent from the public record. | Scope the first Retry moment to the just-completed retained session and expose a clear unavailable state for old traces. Durable branch restoration is a later Rules/storage decision. |
| Are the existing table and theater usable for the accessibility slice? | Yes. `ActionPanel` already focuses the first action on context changes; `GameBoard` consumes focus IDs; `PresentationStage` has live status, skip/finish controls, and reduced-motion timing; global mobile controls are 44 px high. | Reuse those behaviors, add deterministic focus handoffs and a labelled plan radio group, and cover them in Playwright rather than inventing a parallel Study renderer. |

Validation performed during kickoff:

- `uv run --extra dev pytest -q tests/etude/test_replay_index.py tests/etude/test_study_branch.py tests/etude/test_study_protocol.py tests/etude/test_study_index.py tests/sim/test_study_evidence.py` — 33 passed.
- `cargo test --locked --manifest-path managym/Cargo.toml --test canonical_replay_tests --test study_protocol_tests --test study_index_protocol_tests` — 13 debug tests passed.
- `npm test -- --run src/lib/replay-index.test.ts src/lib/study-protocol.test.ts src/lib/presentation.test.ts` from `frontend/` — 22 passed.

## Alternatives considered

| Approach | Tradeoff | Why not |
|----------|----------|---------|
| Serve `protocol/fixtures/study-curated-decision.json` as the first Study result | Fastest visible UI and already binds the pinned replay | It is declared fixture evidence, not a manabot judgment. Shipping it would turn a conformance example into a false product claim. |
| Re-run the Teacher-1 audit and match decisions by ordinal or frame shape | Produces real search numbers with existing Intelligence code | The audit is a different match, replay identity, deck pairing, command chronology, and retained authority. Re-labelling it would violate the address and provenance contract. |
| Compute policy and search values directly in `etude.server` | Could bind the live Study root immediately | Game would duplicate Intelligence ownership, model/search semantics, and provenance. The directive explicitly forbids recreating cross-wave authority. |
| Send the artifact with restore and hide it until the player clicks Reveal | Simplest endpoint shape | The answer is already exposed before Retry and can influence the attempt. Retry-before-reveal must be enforced at the server boundary. |
| Build a generic branch/tree API now | Makes future multi-ply analysis possible | It expands a single expressive moment into tree lifecycle, persistence, and navigation before one player loop works. This Task needs one ephemeral command and one return. |
| Bind the UI to the ranked `StudyDecisionIndex` only | Gives a short, curated list | Landmarks are recommendations, not replay truth. It would hide valid historical decisions and violate the complete-timeline requirement. |

## Key decisions

1. **Build Retry/return now; fail closed on missing evidence.** The exact
   Intelligence provider is not required to land the canonical branch
   substrate. Normal runtime exposes no comparison when that provider is
   unavailable, while tests inject the exact-match fixture only to prove the
   typed post-reveal consumer path.
2. **Retry is mandatory before reveal.** An accepted ordinary command, not a
   hover or mock selection, unlocks Reveal. The player may Return without
   revealing, but cannot reveal without predicting.
3. **Sealing is a server property.** Policy and search fields do not cross the
   boundary before reveal.
4. **Replay remains the return truth.** Study branches are disposable; return
   reloads the exact `RestoredReplayDecision`, never a client snapshot.
5. **Plans are labels over authoritative commands.** Played, Policy, and Search
   may agree; their evidence remains separately named and never becomes a
   generic score.
6. **One fresh fork, one command, one semantic episode.** Every Policy/Search
   preview starts from the exact root, commits only that command, projects only
   that transition, returns, and discards the fork. It does not grow a tree,
   auto-play a branch, or invent narration.
7. **Expired roots fail visibly.** A replay without a retained Study root is
   still fully navigable, but Retry is disabled with a precise explanation.

Wild success is a player forgetting that three subsystems are involved: the
Score turns to the exact mistake, the table invites an unspoiled answer, the
reveal makes the manabot's different kinds of judgment legible, and one button
returns to the score without discontinuity. Wild failure is a beautiful panel
whose numbers came from another match, whose hidden prefetched answer biases
the retry, or whose alternate line is narrated from a snapshot diff. The
identity, sealing, and semantic-event decisions above are designed to make
those failures impossible rather than merely unlikely.

## Scope

- In scope: the selected completed UR Lessons versus GW Allies matchup; the
  complete viewer-zero canonical decision timeline; exact decision restore;
  one ephemeral one-command Retry; exact one-action Return before or after
  reveal; a typed evidence-provider request and fail-closed default; explicit
  reveal only after retry; test-injected Played, Policy, and Search plan
  derivation from one validated `StudyArtifact`; authoritative focus highlights;
  one bounded semantic continuation; honest unavailable states; focused
  pointer, keyboard, reduced-motion, and mobile behavior; unit, contract, and
  real-stack browser coverage.
- Out of scope: client-side legality or rules; snapshot-diff narration;
  opponent-private projections; persisted or multi-ply branches; a generic
  analysis tree; annotations beyond the existing replay marginalia; sharing,
  chat, team-series orchestration, sideboarding, deck construction, Avatar Cube
  breadth, generic historical evidence generation inside Game, a production
  Intelligence provider, or durable restoration after the authority-private
  root expires.

## Done when

This design advances the Wave measures that “every historical player decision
in a completed game is addressable and restorable” and that a player can
“inspect evidence, retry an exact position, follow a canonical continuation,
and return without client-side rules or replay reconstruction.” This PR proves
the exact branch-and-return KR and the fail-closed consumer half of comparison;
it does not claim that normal runtime has honest Policy/Search evidence until
the Intelligence provider is installed. It is done when all of the following
are observable:

1. Selecting a completed trace renders every authorized canonical decision in
   order, and activating any row restores the exact server frame and cursor.
2. Before a retry command is accepted, no response or client state contains
   policy mass, search values, visits, robustness, uncertainty, model, budget,
   or evidence provenance. Reveal is disabled, while Return remains available.
3. A retry can submit only a command bound to the restored revision, prompt,
   and currently published canonical offer. The recorded trace and replay bytes
   remain unchanged.
4. The normal unavailable provider returns typed `study_evidence_unavailable`
   after retry without closing the attempt. Reveal succeeds only under an
   injected provider with a server-validated artifact whose source digest and
   landmark bind the selected replay/address. Drift and runtime fixture
   evidence fail closed.
5. Under the injected exact-match provider, Played, Policy, and Search remain
   separately labelled and preserve distinct evidence units. Every
   Policy/Search selection starts a fresh fork, highlights only IDs from the
   matching authoritative offer, commits exactly one command, plays only that
   bounded protocol presentation sequence, returns, and discards the fork.
6. Return restores equality of frame, offer, command, presentation cursor, and
   selected timeline address, then closes the branch.
7. Real-stack Playwright specs cover both normal unavailable behavior and a
   constructor-injected exact fixture. They prove pointer and keyboard flows,
   detect any pre-reveal evidence leak, verify fresh-fork previews and focus
   return, run under reduced motion, and repeat at a phone viewport without
   horizontal page overflow or sub-44-pixel controls.

If the exact Intelligence provider is still absent after these gates pass,
land this substrate as the current serial PR and keep GAM-4 open for evidence
integration with `lf pr land --next exact-study-evidence`. Do not use
`lf pr land -c` or claim the comparison KR until normal runtime passes the same
reveal proof with non-fixture evidence.

Verification commands:

```bash
cargo test --locked --manifest-path managym/Cargo.toml
uv run --extra dev pytest -q tests/etude
npm --prefix frontend run check
npm --prefix frontend test
npm --prefix frontend run test:e2e -- study.spec.ts
```

No Rust change is expected. If implementation nevertheless changes
`managym/src`, rebuild the CPython extension from the repository root with the
AGENTS.md command and rerun debug `cargo test` before landing.

## Measure

The primary measure is semantic integrity, not a synthetic engagement number:
zero evidence fields delivered before reveal; zero replay bytes changed by a
retry; exact recorded-state equality after return; and every authorized
canonical timeline row reachable. The browser proof records restore, retry,
reveal, plan-select, and return timings; on the pinned local stack, each
non-search UI transition should render within 100 ms at p95 over 20 repetitions
so the Study moment feels like one continuous table rather than a separate
analysis application.
