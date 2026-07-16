# Assumptions

- W2-275's "every historical player decision" is interpreted per historical
  viewer. For the pinned Study handoff that viewer is player 0, so the index
  contains every deliberate command player 0 made. Opponent-policy activity,
  priority auto-pass/F6 activity, and rules resolution remain authoritative
  continuation between those decisions rather than choices shown to player 0.
  Game can produce the same projection for player 1 when that viewer is
  authorized; it must never combine both players' private projections into one
  client artifact.
- A stable address is stable within an immutable, digest-pinned canonical
  replay. Regenerating or editing the replay changes its SHA-256 and therefore
  invalidates old addresses instead of silently resolving them against changed
  history.
- Legacy traces that did not persist an authoritative frame, selected offer,
  command, and presentation cursor are not canonicalizable. The implementation
  should return an explicit unavailable error for them rather than infer
  decisions from raw action indices or observation differences.

