// Headless report behavior: node --test tests/test_charts.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {test} = require("node:test");
const context = vm.createContext({});
vm.runInContext(fs.readFileSync("src/etude/charts.js", "utf8") + "\nthis.model = model; this.isolated = isolated;", context);

function player(name, line, updates, run = null, ordinal = null) {
  return {name, line, at: {updates}, config: "{}", run, ordinal};
}

function page(players, cells, training = {jobs: [], runs: [], checkpoints: [], clocks: []}) {
  return {
    players, cells: cells.map(([subject, opponent, value]) => ({
      subject, opponent, value, selection: 0, score: 0, split: {}, fixed: false,
      interval: [value - 0.1, value + 0.1], rows: 4, matches: 4,
    })),
    training, report: {clock: "updates"}, coverage: [], selections: [{tournament: null}],
  };
}

const earlier = {kind: "earlier", clock: "updates", by: 400};
const points = (m, job, opponent = earlier) => m.jobPoints(job, "updates", m.evalValue(0, 0, {}, opponent, "{}"));

test("external learners sharing positions draw separate curves against their own earlier checkpoint", () => {
  const m = context.model(page([
    player("a0", "seed-1", 0), player("a400", "seed-1", 400),
    player("b0", "seed-2", 0), player("b400", "seed-2", 400),
  ], [[1, 0, 0.25], [3, 2, 0.75]]));
  assert.equal(m.JOBS.length, 2);
  for (const [j, value] of [[m.JOBS[0], 0.25], [m.JOBS[1], 0.75]]) {
    const drawn = points(m, j);
    assert.equal(drawn[0].missing, true);
    assert.match(drawn[0].meta, /no checkpoint at updates -400/);
    assert.equal(drawn[1].x, 400);
    assert.equal(drawn[1].y, value);
  }
});

test("a continuation compares and tabulates checkpoints across job boundaries", () => {
  const training = {
    jobs: [{key: "a", name: "a", runs: ["a"]}, {key: "b", name: "b", runs: ["b"]}],
    runs: [{name: "a"}, {name: "b"}], clocks: [{name: "updates"}],
    checkpoints: [
      {run: "a", ordinal: 1, align: 0, at: {updates: 0}},
      {run: "b", ordinal: 1, align: 400, at: {updates: 400}},
    ],
  };
  const m = context.model(page([
    player("parent", "learner", 0, "a", 1), player("child", "learner", 400, "b", 1),
  ], [[1, 0, 0.75]], training));
  assert.equal(points(m, m.JOBS[1])[0].y, 0.75);
  assert.equal(points(m, m.JOBS[1], {kind: "at", clock: "updates", value: 0})[0].y, 0.75);
  const learners = new Set(m.JOBS.flatMap(m.linesOf));
  assert.equal(learners.size, 1);
  const members = m.lineMembers([...learners][0]);
  assert.deepEqual(Array.from(members), [0, 1]);
  assert.equal(m.cellOf(0, 0, members[1], members[0], false, {}).value, 0.75);
});

test("duplicate positions are named gaps for both tracked and external checkpoints", () => {
  for (const tracked of [false, true]) {
    const players = [player("first", "learner", 400), player("second", "learner", 400)];
    // Extra clocks must not hide an ambiguity on the curve's alignment clock.
    players[1].at.tokens = 1000;
    let training;
    if (tracked) {
      players.forEach((p, i) => { p.run = "a"; p.ordinal = i + 1; });
      training = {
        jobs: [{key: "a", name: "a", runs: ["a"]}], runs: [{name: "a"}], clocks: [{name: "updates"}],
        checkpoints: players.map(p => ({run: p.run, ordinal: p.ordinal, at: p.at, align: 400})),
      };
    }
    const m = context.model(page(players, [[0, 1, 0.25], [1, 0, 0.75]], training));
    const [gap] = points(m, m.JOBS[0], "");
    assert.equal(gap.missing, true);
    assert.equal(gap.y, undefined);
    assert.match(gap.meta, /first.*second.*stood at updates 400/);
  }
});

test("an ambiguous relative opponent breaks the curve and names both candidates", () => {
  const m = context.model(page([
    player("first", "learner", 0), player("second", "learner", 0), player("later", "learner", 400),
  ], [[2, 0, 0.25], [2, 1, 0.75]]));
  const gap = points(m, m.JOBS[0]).find(p => p.x === 400);
  assert.equal(gap.missing, true);
  assert.match(gap.meta, /first.*second.*both stood at updates 0/);
});

test("a dense line marks the points it has no segment for", () => {
  const ys = [0.5, 0.6, null, 0.7, null, undefined, 0.8, 0.9, null, 0.4];
  assert.deepEqual([...context.isolated(ys)], [3, 9]);
  assert.deepEqual([...context.isolated([0.5])], [0]);
});
