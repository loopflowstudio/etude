# Study wave — opening vertical slice

## What to build

After one recorded creator-selected match, `Study this game` restores one
viewer-safe decision, lets the player retry it before reveal, compares the
played command with policy and search alternatives, demonstrates one short
semantic continuation, and supports an ephemeral branch with one-click return.

## The demo

Finish the pinned UR Lessons versus GW Allies release scenario and choose
`Study this game`. The review jumps to one recorded priority, targeting, or
combat landmark. The player retries the decision, reveals `You played`,
`Policy instinct`, and `Deeper search`, selects an alternative on the table,
watches its semantic beats, takes control briefly, and returns to the original
match without state drift or hidden-information leakage.

## Core records

```ts
type StudyIdentity = {
  packHash: string;
  engineHash: string;
  modelHash: string;
  stateHash: string;
  viewer: PlayerRef;
  decisionId: string;
  promptId: string;
  offerId: string;
  analysisBudget: AnalysisBudget;
};

type StudyLandmark = {
  identity: StudyIdentity;
  frame: ExperienceFrame;
  offer: InteractionOffer;
  played: CommandRef;
  reasons: LandmarkReason[];
  evidence?: DecisionEvidence;
};

type DecisionEvidence = {
  policy?: { command: CommandRef; mass: number };
  search?: { command: CommandRef; value: number; visits: number };
  worlds?: {
    count: number;
    robustCount: number;
    interval?: [number, number];
    assumptions: string[];
  };
  provenance: AnalysisProvenance;
};

type StudyBranch = {
  origin: StudyIdentity;
  commands: CommandRef[];
  stateHashes: string[];
  status: 'ephemeral' | 'saved';
};
```

The spelling may follow existing protocol conventions. The required invariants
are exact identity, viewer-safe evidence, separately labelled quantities, and
canonical commands/events rather than display-derived rules.

## Interaction sequence

1. The terminal result offers `Study this game`.
2. A short decision timeline opens on one precomputed landmark. Turn and phase
   remain visible; routine automatic transitions are not independent landmarks.
3. The original frame and offer are restored under the historical viewer's
   knowledge boundary.
4. The UI asks the player to retry. It does not reveal engine candidates yet.
5. After submission, reveal the played, policy, and search commands. If values
   are not calibrated, use `Worth considering`, not mistake/blunder labels.
6. Selecting an alternative highlights involved table objects and plays a
   bounded semantic continuation from canonical `PresentationEvent`s.
7. `Try this line` creates an exact ephemeral branch. A persistent ribbon shows
   `Match → decision → branch` with Return, Restart, Save, and Discard.
8. Return restores the recorded frame, timeline cursor, and presentation state
   exactly.

## Constraints

- The default analysis may use only information available to the historical
  viewer. The recorded opponent hand is not a shortcut for evaluation.
- Policy mass, search value, visits, robustness, and uncertainty are different
  fields with different labels.
- No client-side legality, snapshot-diff narration, or fabricated principal
  variation.
- One short landmark is enough for this slice. General landmark ranking,
  annotations, collaboration, and model-history comparison belong to later
  Tasks.
- Branches are ephemeral unless explicitly saved and never alter the source
  trace.
- Existing Match, Replay, and learning observation behavior remains compatible.

## Done when

- A deterministic release-stack browser test performs the full demo by pointer
  and keyboard.
- Rust/Python/TypeScript fixtures agree on study identity, evidence, and branch
  records.
- Tests prove hidden historical cards do not enter default evidence or shared
  payloads.
- Retry, reveal, branch, and return preserve the expected state hashes and
  semantic event order.
- Policy/search quantities display their provenance and analysis budget.
- Reduced-motion and mobile-table layouts retain the complete interaction.

## Measure

Record time from result to restored landmark, time to provisional and final
analysis, branch creation/return latency, analysis payload bytes, peak memory,
and accessibility/visual evidence. Correctness gates are zero illegal commands,
zero hidden-information leaks, exact return hash, and deterministic replay of
the selected continuation.

