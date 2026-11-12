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
const points = (m, job, opponent = earlier) => m.jobPoints(job, "updates", m.evalValue(0, 0, {}, opponent));

// Run the shipped page and chart renderer; only the browser DOM and plotting
// library are replaced. Assertions see displayed text and the plotted values.
function renderPage(data, hash = "") {
  class Element {
    constructor() { this.children = []; this.style = {}; this.text = ""; }
    set textContent(text) { this.text = String(text); this.children = []; }
    get textContent() { return this.text + this.children.map(c => typeof c === "string" ? c : c.textContent).join(" "); }
    append(...children) { this.children.push(...children); }
    appendChild(child) { this.append(child); return child; }
    replaceChildren(...children) { this.text = ""; this.children = children; }
    setAttribute() {}
    addEventListener() {}
  }
  const elements = new Map(), plots = [];
  const document = {
    createElement: () => new Element(), querySelectorAll: () => [],
    getElementById: id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  };
  const full = {
    experiment: "test", notes: [], environments: [], commits: [], failures: [], generated_ms: 0,
    scores: [{table: "games", value: "win", unit: "games"}], ...data,
    report: {evals: [{kind: "head_to_head", title: "Head to head"}], perf: [], ml: [], ...data.report},
    training: {...data.training, metrics: [], runs: data.training.runs.map(r => ({rows: 0, attempts: [], ...r}))},
  };
  document.getElementById("data").textContent = JSON.stringify(full);
  const browser = vm.createContext({
    document, URLSearchParams, location: {hash}, history: {replaceState() {}}, addEventListener() {},
    getComputedStyle: () => ({getPropertyValue: () => "#112233"}),
    IntersectionObserver: class { observe() {} },
    uPlot: class {
      constructor(options, values) { this.over = new Element(); plots.push({options, values}); }
      destroy() {}
    },
  });
  const script = fs.readFileSync("src/etude/page.html", "utf8").split("<script>\n")[1].split("</script>")[0];
  vm.runInContext(fs.readFileSync("src/etude/charts.js", "utf8") + "\n" + script, browser);
  return {text: document.getElementById("evals-body").textContent, plots};
}

function pairedSeeds() {
  const training = {
    jobs: [{key: "a", name: "a", label: "A", runs: ["a1", "a2"]}, {key: "b", name: "b", label: "B", runs: ["b1", "b2"]}],
    runs: ["a1", "a2", "b1", "b2"].map(name => ({name})), clocks: [{name: "updates"}],
    checkpoints: ["a1", "a2", "b1", "b2"].map(run => ({run, ordinal: 1, align: 100, at: {updates: 100}})),
  };
  return page(training.runs.map(r => player("u100", r.name, 100, r.name, 1)), [[0, 2, 0.25], [1, 3, 0.75]], training);
}

test("HeadToHead displays ambiguity instead of averaging scientific variants across subject seeds", () => {
  const data = pairedSeeds();
  data.players[0].config = '{"search":0}'; data.players[1].config = '{"search":9}';
  const rendered = renderPage(data);
  assert.equal(rendered.plots.length, 0);
  assert.match(rendered.text, /A vs B.*seeds played different inference variants/);
  assert.doesNotMatch(rendered.text, /No games|Nothing recorded/);
});

test("HeadToHead carries opposing scientific identity across subject seeds", () => {
  const data = pairedSeeds();
  data.players[2].config = '{"search":0}'; data.players[3].config = '{"search":9}';
  const rendered = renderPage(data);
  assert.equal(rendered.plots.length, 0);
  assert.match(rendered.text, /A vs B.*different inference variants/);
});

test("a curve against a named opponent preserves its scientific identity across seeds", () => {
  const data = pairedSeeds();
  data.report.evals = [{kind: "curve", title: "Score", subject: "a", opponent: "u100"}];
  data.players[2].config = '{"search":0}'; data.players[3].config = '{"search":9}';
  const ambiguous = renderPage(data);
  assert.equal(ambiguous.plots.length, 0);
  assert.match(ambiguous.text, /different inference variants/);

  data.players[3].config = data.players[2].config;
  const consistent = renderPage(data);
  assert.equal(consistent.plots.length, 1);
  assert.equal(consistent.plots[0].values[1][0], 0.5);
});

test("HeadToHead names opposing variants instead of pooling them as checkpoints", () => {
  for (const position of [100, 200]) {
    const data = page([player("a", "a", 100), player("b", "b", 100), player("b", "b", position)], [[0, 1, 0.25], [0, 2, 0.75]]);
    data.players[1].config = '{"search":0}'; data.players[2].config = '{"search":9}';
    const rendered = renderPage(data);
    assert.equal(rendered.plots.length, 0);
    assert.match(rendered.text, /a vs b.*opponent.*inference variants.*search.*choose a matchup/);
    assert.doesNotMatch(rendered.text, /No games|Nothing recorded/);
  }
});

test("HeadToHead preserves ambiguity for partially evaluated jobs", () => {
  const data = pairedSeeds();
  data.players.push({...data.players[0], config: '{"search":9}'});
  data.cells = [data.cells[0], {...data.cells[0], subject: 4, value: 0.75}];
  const rendered = renderPage(data);
  assert.equal(rendered.plots.length, 0);
  assert.match(rendered.text, /A vs B.*multiple inference variants.*1 of 2 seeds evaluated/);
});

test("a curve whose checkpoints all play two variants says so once, naming the variants", () => {
  const training = {
    jobs: [{key: "a", name: "a", label: "A", runs: ["a1"]}], runs: [{name: "a1"}], clocks: [{name: "updates"}],
    checkpoints: [100, 200, 300].map((u, k) => ({run: "a1", ordinal: k + 1, align: u, at: {updates: u}})),
  };
  const players = [100, 200, 300].flatMap((u, k) => ['{"search":0}', '{"search":9}'].map(config => ({...player(`u${u}`, "a1", u, "a1", k + 1), config})));
  const data = page(players, players.map((_, i) => [i, null, 0.5]), training);
  data.report = {clock: "updates", evals: [{kind: "curve", title: "Score"}]};
  const rendered = renderPage(data);
  assert.equal(rendered.plots.length, 0);
  assert.equal(rendered.text.match(/multiple inference variants/g).length, 1);
  assert.match(rendered.text, / A: multiple inference variants: \{"search":0\} and \{"search":9\}; choose a matchup or set Curve.inference/);
});

test("HeadToHead keeps legitimate means over seeds and distinct opponent positions", () => {
  const data = pairedSeeds();
  data.players.push(player("u200", "b1", 200, "b1", 2));
  data.training.checkpoints.push({run: "b1", ordinal: 2, align: 200, at: {updates: 200}});
  data.cells.push({...data.cells[0], opponent: 4, value: 0.5});
  const {plots} = renderPage(data);
  assert.equal(plots.length, 1);
  assert.equal(plots[0].options.series[1].label, "A vs B");
  assert.deepEqual(Array.from(plots[0].values[0]), [100]);
  assert.deepEqual(Array.from(plots[0].values[1]), [(0.375 + 0.75) / 2]);
});

test("HeadToHead selection resolves variants and missing games remain an empty panel", () => {
  const data = page([player("a", "a", 100), player("b", "b", 100), player("b", "b", 100)], [[0, 1, 0.25], [0, 2, 0.75]]);
  data.players[1].config = '{"search":0}'; data.players[2].config = '{"search":9}';
  data.selections.push({matchup: "search"}, {matchup: "empty"});
  data.cells[1].selection = 1;
  data.report.evals = [{kind: "head_to_head", matches: {matchup: "search"}}];
  const {plots} = renderPage(data);
  assert.equal(plots.length, 1);
  assert.equal(plots[0].values[1][0], 0.75);
  data.report.evals[0].matches = {matchup: "empty"};
  const empty = renderPage(data);
  assert.equal(empty.plots.length, 0);
  assert.match(empty.text, /No games between checkpoints/);
  assert.doesNotMatch(empty.text, /inference variants/);
});

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
    // Head-to-head uses the same resolver, even if only one candidate has games.
    const run = m.JOBS[0].runs[0], mark = m.marks.get(run)[0];
    const subject = m.subjectAt(run, mark, i => i === 0);
    assert.equal(subject.index, undefined);
    assert.match(subject.gap, /first.*second.*stood at updates 400/);
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

test("each job draws its requested results without a global config filter", () => {
  const data = page([player("a", "a", 100), player("b", "b", 100)], [[0, null, 0.25], [1, null, 0.625]]);
  data.players[0].config = '{"search":0}';
  data.players[1].config = '{"search":9}';
  const m = context.model(data);
  assert.deepEqual(Array.from(m.JOBS, j => points(m, j, "")[0].y), [0.25, 0.625]);
});

test("requested matchups select scientific variants without pooling their scores", () => {
  const data = page([player("u100", "a", 100), player("u100", "a", 100)], [[0, null, 0.25], [1, null, 0.75]]);
  data.players[0].config = '{"search":0}';
  data.players[1].config = '{"search":9}';
  data.cells[1].selection = 1;
  const m = context.model(data), j = m.JOBS[0];
  for (const [sel, y] of [[0, 0.25], [1, 0.75]]) {
    assert.equal(m.jobPoints(j, "updates", m.evalValue(0, sel, {}, ""))[0].y, y);
  }
  data.cells[1].selection = 0;
  const both = context.model(data), gap = points(both, both.JOBS[0], "")[0];
  assert.equal(gap.missing, true);
  assert.match(gap.meta, /multiple inference variants.*search.*Curve.inference/);
  assert.equal(both.jobPoints(both.JOBS[0], "updates", both.evalValue(0, 0, {}, "", {search: 9}))[0].y, 0.75);
});

test("search against no search keeps both intentional sides selectable", () => {
  const data = page([player("u100", "a", 100), player("u100", "a", 100)], [[0, 1, 0.25], [1, 0, 0.75]]);
  data.players[0].config = '{"search":0}';
  data.players[1].config = '{"search":9}';
  const m = context.model(data);
  for (const [search, y] of [[0, 0.25], [9, 0.75]]) {
    const p = m.jobPoints(m.JOBS[0], "updates", m.evalValue(0, 0, {}, "", {search}))[0];
    assert.equal(p.y, y);
  }
  assert.equal(points(m, m.JOBS[0], "1")[0].y, 0.25);
  assert.equal(points(m, m.JOBS[0], "0")[0].y, 0.75);
});

test("seeds playing different scientific variants are not averaged together", () => {
  const training = {
    jobs: [{key: "job", name: "job", runs: ["a", "b"]}],
    runs: [{name: "a"}, {name: "b"}], clocks: [{name: "updates"}],
    checkpoints: ["a", "b"].map(run => ({run, ordinal: 1, align: 100, at: {updates: 100}})),
  };
  const data = page([player("u100", "a", 100, "a", 1), player("u100", "b", 100, "b", 1)], [[0, null, 0.25], [1, null, 0.75]], training);
  data.players[0].config = '{"search":0}';
  data.players[1].config = '{"search":9}';
  const m = context.model(data), p = points(m, m.JOBS[0], "")[0];
  assert.equal(p.missing, true);
  assert.equal(p.y, undefined);
  assert.match(p.meta, /seeds played different inference variants/);
});

test("tag fallback prefers results while an explicit tournament remains exact", () => {
  const data = page([player("a", "a", 100)], [[0, null, 0.625]]);
  data.selections = [{tournament: "empty", tag: "anchor"}, {tournament: "played", tag: "anchor"}];
  data.cells[0].selection = 1;
  data.cells.push({...data.cells[0], selection: 0, score: 1});
  const m = context.model(data);
  assert.equal(m.selectMatches({tag: "anchor"}, 0), 1);
  assert.equal(m.selectMatches({tag: "anchor"}, 1, 1), 0);
  assert.equal(m.selectMatches({tag: "anchor"}, 1, 2), 1); // no result for this score
  assert.equal(m.selectMatches(null, 0), 0);
  assert.equal(m.selectMatches({tournament: "empty", tag: "anchor"}, 1), 0);
  assert.equal(m.selectMatches({tag: "missing"}, 0), null);
  assert.equal(m.selName([{tag: "anchor"}, {matchup: "head-to-head"}]), "tagged anchor + head-to-head");
  assert.equal(m.selName({tournament: "played", tag: "anchor"}), "played · tagged anchor");
  assert.equal(m.selName({tournament: null}), "matches outside a tournament");
});

test("union curves keep jobs separate and subject filtering omits the complementary side", () => {
  const data = page([player("a", "small", 100), player("b", "large", 100)], [[0, 1, 0.375], [1, 0, 0.625]]);
  const union = [{tournament: "paired", tag: "small"}, {tournament: "paired", tag: "large"}];
  data.selections = [...union, union];
  data.cells.forEach(c => { c.selection = 2; });
  data.report.evals = [{kind: "curve", matches: union}];
  data.report.labels = {large: "Large model"};
  const m = context.model(data), sel = m.selectMatches(union, 0);
  assert.equal(sel, 2);
  const ys = m.panelJobs({}).map(j => m.jobPoints(j, "updates", m.evalValue(0, sel, {}, ""))[0].y);
  assert.deepEqual(Array.from(ys), [0.625, 0.375]); // external learner order is lexical
  assert.equal(m.panelJobs({subject: "large"}).length, 1);
  assert.equal(m.panelJobs({subject: "large"})[0].label, "Large model");
  assert.equal(m.panelJobs({subject: "missing"}).length, 0);
  assert.equal(m.usesSelection, false);
  data.scores = [{unit: "games"}];
  data.report.evals[0].subject = "large";
  data.report.evals[0].opponent = "a";
  const [head] = context.headline(data, m);
  assert.equal(head.job.name, "large");
  assert.equal(head.latest.y, 0.625);
  assert.equal(head.against, "a");
  data.report.evals.push({kind: "curve"});
  assert.equal(context.model(data).usesSelection, true);
});

test("job labels retain seed counts and baseline identity", () => {
  const data = page([], [], {jobs: [{key: "id", name: "large", launch: "large", label: "large · 2 seeds", runs: ["a", "b"]}], runs: [], checkpoints: [], clocks: []});
  data.report.labels = {large: "Large model"}; data.report.baseline = "large";
  const [job] = context.model(data).JOBS;
  assert.equal(job.label, "Large model · 2 seeds");
  assert.equal(job.name, "large"); assert.equal(job.key, "id"); assert.equal(job.baseline, true);
});

test("a launch name addresses every config job launched under it; a full name addresses one", () => {
  const jobs = [
    {key: "ppo 1a2b3c", name: "ppo 1a2b3c", launch: "ppo", label: "ppo 1a2b3c · 3 seeds", runs: ["a", "b", "c"]},
    {key: "ppo 4d5e6f", name: "ppo 4d5e6f", launch: "ppo", label: "ppo 4d5e6f · 1 seed", runs: ["d"]},
    {key: "dqn", name: "dqn", launch: "dqn", label: "dqn · 1 seed", runs: ["e"]},
  ];
  const data = page([], [], {jobs, runs: [], checkpoints: [], clocks: []});
  data.report.labels = {ppo: "PPO", dqn: "DQN"}; data.report.baseline = "ppo";
  const m = context.model(data);
  assert.deepEqual(Array.from(m.panelJobs({subject: "ppo"}), j => j.label), ["PPO 1a2b3c · 3 seeds", "PPO 4d5e6f · 1 seed"]);
  assert.deepEqual(Array.from(m.panelJobs({subject: "ppo 4d5e6f"}), j => j.name), ["ppo 4d5e6f"]);
  assert.deepEqual(Array.from(m.JOBS, j => j.baseline), [true, true, false]);
  assert.equal(m.JOBS[2].label, "DQN · 1 seed");
  data.report.labels = {ppo: "PPO", "ppo 4d5e6f": "High learning rate"};
  assert.equal(context.model(data).JOBS[1].label, "High learning rate · 1 seed");
  const plan = value => [{at: 0, value}];
  const schedule = {clock: "updates", jobs: {ppo: plan(1), "ppo 4d5e6f": plan(2)}};
  assert.deepEqual(Array.from(m.scheduleSeries(schedule), s => [s.job.name, s.points[0].y]),
    [["ppo 1a2b3c", 1], ["ppo 4d5e6f", 2]]);
});

test("score tooltips name the interval and row unit without changing values", () => {
  vm.runInContext("this.pointText = pointText; this.pointMeta = pointMeta; this.scoreFormat = scoreFormat;", context);
  const data = page([player("a", "a", 100)], [[0, null, 0.625]]);
  data.cells[0].interval = [0.5313, 0.7191];
  data.cells[0].rows = 64; data.cells[0].matches = 16;
  data.scores = [{unit: "games"}];
  const m = context.model(data), [p] = points(m, m.JOBS[0], "");
  assert.equal(context.pointText(p, context.scoreFormat), "0.63 · 95% interval 0.53–0.72");
  assert.equal(p.meta, "64 games · 16 matches"); assert.equal(p.y, 0.625);
  data.cells[0].interval = null;
  const m2 = context.model(data), [single] = points(m2, m2.JOBS[0], "");
  assert.equal(context.pointText(single, context.scoreFormat), "0.63");
  assert.equal(context.pointText({y: 0.625, lo: 0.5, hi: 0.75, seeds: [0.5, 0.75]}, context.scoreFormat), "0.63 · seed range 0.50–0.75");
  assert.equal(context.pointMeta({y: 0.5625, seeds: [0.4375, 0.6875]}, context.scoreFormat), "mean of 2 seeds: 0.44, 0.69");
  const training = {jobs: [], clocks: [{name: "updates"}], runs: [], checkpoints:
    ["a", "b"].map(run => ({run, ordinal: 1, align: 100, at: {updates: 100}}))};
  const partial = context.model(page([], [], training)).jobPoints(
    {runs: ["a", "b", "c"]}, "updates", () => ({y: 0.625}), true,
  )[0];
  assert.equal(context.pointMeta(partial, context.scoreFormat), "2 of 3 seeds evaluated");
});

test("configured curves interpolate at recorded absolute clocks without extrapolation", () => {
  const training = {jobs: [{key: "id", name: "large", runs: ["a", "b"]}],
    runs: [{name: "a", reached: {updates: 1200}}, {name: "b", reached: {updates: 900}}], checkpoints: [], clocks: []};
  const data = page([], [], training);
  const schedule = {clock: "updates", jobs: {large: [{at: 1000, value: 1}, {at: 1400, value: 0.2}]}};
  const [s] = context.model(data).scheduleSeries(schedule);
  assert.deepEqual(Array.from(s.points, p => p.x), [1000, 1400]);
  assert.equal(s.markers.length, 1); assert.equal(s.markers[0].x, 1200);
  assert.ok(Math.abs(s.markers[0].y - 0.6) < 1e-10);
  assert.match(s.markers[0].meta, /recorded updates/);
  training.runs[0].reached.updates = 1400;
  assert.equal(context.model(data).scheduleSeries(schedule)[0].markers[0].y, 0.2);
  delete training.runs[0].reached.updates;
  assert.equal(context.model(data).scheduleSeries(schedule)[0].markers.length, 0);
});

test("refresh failure leaves the page visible and a later success reloads once", async () => {
  const status = {textContent: ""};
  let reloads = 0, requests = 0, fail = true;
  const browser = vm.createContext({
    document: {getElementById: () => status},
    location: {pathname: "/lab", reload: () => { reloads++; }},
    fetch: async () => { requests++; return {ok: !fail, text: async () => "source unavailable"}; },
  });
  vm.runInContext(fs.readFileSync("src/etude/charts.js", "utf8") + "\nthis.refreshPage = refreshPage;", browser);
  assert.equal(requests, 0); // loading the chart script never polls
  await browser.refreshPage(true);
  assert.equal(requests, 1); assert.equal(reloads, 0);
  assert.match(status.textContent, /Refresh failed: source unavailable.*last successful generation/);
  fail = false;
  await browser.refreshPage(true);
  assert.equal(requests, 2); assert.equal(reloads, 1);
  await browser.refreshPage(false);
  assert.equal(requests, 2); assert.equal(reloads, 2); // snapshots only reload
});
