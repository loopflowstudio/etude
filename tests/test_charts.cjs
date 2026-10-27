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

test("a fraction of each checkpoint's position plays it against its own line's checkpoint exactly there", () => {
  const m = context.model(page([
    player("u100", "learner", 100), player("u200", "learner", 200),
    player("u3000", "learner", 3000), player("u5950", "learner", 5950), player("u6000", "learner", 6000),
  ], [[1, 0, 0.6], [4, 2, 0.55]]));
  const drawn = points(m, m.JOBS[0], {kind: "fraction", clock: "updates", value: 0.5});
  const at = Object.fromEntries(drawn.map(p => [p.x, p]));
  assert.equal(at[200].y, 0.6);
  assert.equal(at[6000].y, 0.55);
  assert.match(at[6000].meta, /vs .*u3000/);
  // No checkpoint stood at exactly half of 5,950: a gap that says where it looked.
  assert.equal(at[5950].missing, true);
  assert.match(at[5950].meta, /no checkpoint at updates 2,?975/);
});

test("a computed position matches a checkpoint by value, not by binary float", () => {
  const m = context.model(page([player("u63", "learner", 63), player("u90", "learner", 90)], [[1, 0, 0.4]]));
  const at90 = points(m, m.JOBS[0], {kind: "fraction", clock: "updates", value: 0.7}).find(p => p.x === 90);
  assert.equal(at90.y, 0.4);
  assert.match(at90.meta, /vs .*u63/);
});

test("a checkpoint never resolves to itself as its own opponent", () => {
  const m = context.model(page([player("u0", "learner", 0), player("u400", "learner", 400)], [[1, 0, 0.5]]));
  const first = points(m, m.JOBS[0], {kind: "fraction", clock: "updates", value: 0.5}).find(p => p.x === 0);
  assert.equal(first.missing, true);
  assert.match(first.meta, /no checkpoint at updates 0/);
  const own = points(m, m.JOBS[0], {kind: "at", clock: "updates", value: 400}).find(p => p.x === 400);
  assert.equal(own.missing, true);
  assert.match(own.meta, /no checkpoint at updates 400/);
});
