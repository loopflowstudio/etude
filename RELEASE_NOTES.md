# v0.4.4

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.4.4 makes evaluation curves easier to compare across panels. A score can define its y-axis range so the same vertical distance represents the same change whenever panels use that range. ([#133](https://github.com/loopflowstudio/etude/pull/133))

## Compare rates on a common scale

A land-play rate moving from 0.85 to 1.00 can look like a full-height swing when the chart fits the observed values. With a fixed 0-to-1 scale, that move takes up 15% of the chart height. ([#133](https://github.com/loopflowstudio/etude/pull/133))

- Set `range=(0, 1)` on a rate’s `Score` to use those bounds for its evaluation curves. Scores without a range still scale to the displayed values. ([#133](https://github.com/loopflowstudio/etude/pull/133))
- Etude validates the range when you create the score. The lower bound must be strictly less than the upper bound. ([#133](https://github.com/loopflowstudio/etude/pull/133))