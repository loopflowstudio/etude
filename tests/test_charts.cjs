// Headless report behavior: node --test tests/test_charts.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {test} = require("node:test");
const context = vm.createContext({});
vm.runInContext(fs.readFileSync("src/etude/charts.js", "utf8") + "\nthis.model = model; this.isolated = isolated; this.tickLabels = tickLabels; this.runMember = runMember;", context);

function player(name, line, updates, run = null, ordinal = null) {
  return {name, line, at: {updates}, config: "{}", run, ordinal, placed: true};
}

function page(players, cells, training = {jobs: [], runs: [], checkpoints: [], clocks: []}) {
  return {
    players, cells: cells.map(([subject, opponent, value]) => ({
      subject, opponent, value, selection: 0, score: 0, split: {}, fixed: false,
      interval: [value - 0.1, value + 0.1], rows: 4, matches: 4,
    })),
    training, report: {clock: "updates"}, coverage: [], points: [],
    selections: [{kind: "outside"}], drawn: [{seated: false, tournaments: []}], panel_selections: [],
  };
}

const points = (m, job, opponent = "") => m.jobPoints(job, "updates", m.evalValue(0, 0, {}, opponent));

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
    report: {evals: [{kind: "curve", title: "Score"}], perf: [], ml: [], ...data.report},
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
  return {text: document.getElementById("evals-body").textContent, plots, display: id => document.getElementById(id).style.display};
}

test("a section the report gives no panel is hidden, header and controls with it", () => {
  const data = page([player("u100", "learner", 100)], [[0, null, 0.5]]);
  const none = renderPage(data);
  assert.deepEqual([none.display("evals"), none.display("perf"), none.display("ml")], [undefined, "none", "none"]);
  const perf = renderPage({...data, report: {clock: "updates", perf: [{kind: "lines", title: "Speed", keys: ["sps"]}]}});
  assert.deepEqual([perf.display("perf"), perf.display("ml")], ["", "none"]);
});

test("a curve whose checkpoints all play two variants says so once, naming the variants", () => {
  const training = {
    jobs: [{name: "a", launch: "a", label: "A", runs: ["a1"]}], runs: [{name: "a1"}], clocks: [{name: "updates"}],
    checkpoints: [100, 200, 300].map((u, k) => ({run: "a1", ordinal: k + 1, align: u, at: {updates: u}})),
  };
  const players = [100, 200, 300].flatMap((u, k) => ['{"search":0}', '{"search":9}'].map(config => ({...player(`u${u}`, "a1", u, "a1", k + 1), config})));
  const data = page(players, players.map((_, i) => [i, null, 0.5]), training);
  data.report = {clock: "updates", evals: [{kind: "curve", title: "Score"}]};
  const rendered = renderPage(data);
  assert.equal(rendered.plots.length, 0);
  assert.equal(rendered.text.match(/multiple inference variants/g).length, 1);
  assert.match(rendered.text, / A: multiple inference variants: \{"search":0\} and \{"search":9\}; choose a seating/);
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
        jobs: [{name: "a", launch: "a", runs: ["a"]}], runs: [{name: "a"}], clocks: [{name: "updates"}],
        checkpoints: players.map(p => ({run: p.run, ordinal: p.ordinal, at: p.at, align: 400})),
      };
    }
    const m = context.model(page(players, [[0, 1, 0.25], [1, 0, 0.75]], training));
    const [gap] = points(m, m.JOBS[0], "");
    assert.equal(gap.missing, true);
    assert.equal(gap.y, undefined);
    assert.match(gap.meta, /first.*second.*stood at updates 400/);
    // Every panel uses the same resolver, even if only one candidate has games.
    const mark = m.marks.get(m.JOBS[0].members[0].key)[0];
    const subject = m.subjectAt(mark, i => i === 0);
    assert.equal(subject.index, undefined);
    assert.match(subject.gap, /first.*second.*stood at updates 400/);
  }
});

test("a dense line marks the points it has no segment for", () => {
  const ys = [0.5, 0.6, null, 0.7, null, undefined, 0.8, 0.9, null, 0.4];
  assert.deepEqual([...context.isolated(ys)], [3, 9]);
  assert.deepEqual([...context.isolated([0.5])], [0]);
});

test("each job draws its requested results without a global config filter", () => {
  const data = page([player("a", "a", 100), player("b", "b", 100)], [[0, null, 0.25], [1, null, 0.625]]);
  data.players[0].config = '{"search":0}';
  data.players[1].config = '{"search":9}';
  const m = context.model(data);
  assert.deepEqual(Array.from(m.JOBS, j => points(m, j, "")[0].y), [0.25, 0.625]);
});

test("requested seatings select scientific variants without pooling their scores", () => {
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
  assert.match(gap.meta, /multiple inference variants.*search.*choose a seating/);
});

test("search against no search keeps both intentional sides selectable", () => {
  const data = page([player("u100", "a", 100), player("u100", "a", 100)], [[0, 1, 0.25], [1, 0, 0.75]]);
  data.players[0].config = '{"search":0}';
  data.players[1].config = '{"search":9}';
  const m = context.model(data);
  assert.equal(points(m, m.JOBS[0], "1")[0].y, 0.25);
  assert.equal(points(m, m.JOBS[0], "0")[0].y, 0.75);
});

test("seeds playing different scientific variants are not averaged together", () => {
  const training = {
    jobs: [{name: "job", launch: "job", runs: ["a", "b"]}],
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

test("selections are named for what they count", () => {
  const m = context.model(page([player("a", "a", 100)], [[0, null, 0.625]]));
  assert.equal(m.selName([{tag: "anchor"}, {seating: "head-to-head"}]), "tagged anchor + head-to-head");
  assert.equal(m.selName({tournament: "played", tag: "anchor"}), "played · tagged anchor");
  assert.equal(m.selName({kind: "outside"}), "matches outside a tournament");
  assert.equal(m.selName({tournament: null, seating: null, tag: "anchor"}), "tagged anchor");
});

test("a panel draws the selection the page resolved for it, or the control's", () => {
  const data = page([player("a", "a", 100)], [[0, null, 0.625]]);
  data.selections = [{tournament: "empty", tag: "anchor"}, {tournament: "played", tag: "anchor"}];
  data.drawn = data.selections.map(s => ({seated: false, tournaments: [s.tournament]}));
  data.panel_selections = [{selection: 1, control: false}, {selection: null, control: true}, {selection: null, control: false}];
  const m = context.model(data);
  assert.deepEqual([0, 1, 2, 3].map(k => m.panelSelection(k, 0)), [1, 0, null, 0]);
  assert.equal(m.usesSelection, true);
});

test("union curves keep jobs separate", () => {
  const data = page([player("a", "small", 100), player("b", "large", 100)], [[0, 1, 0.375], [1, 0, 0.625]]);
  const union = [{tournament: "paired", tag: "small"}, {tournament: "paired", tag: "large"}];
  data.selections = [...union, union];
  data.drawn = data.selections.map(() => ({seated: false, tournaments: ["paired"]}));
  data.cells.forEach(c => { c.selection = 2; });
  data.report.evals = [{kind: "curve", matches: union}];
  data.panel_selections = [{selection: 2, control: false}];
  data.report.labels = {large: "Large model"};
  const m = context.model(data), sel = m.panelSelection(0, 0);
  assert.equal(sel, 2);
  const ys = m.JOBS.map(j => m.jobPoints(j, "updates", m.evalValue(0, sel, {}, ""))[0].y);
  assert.deepEqual(Array.from(ys), [0.625, 0.375]); // external learner order is lexical
  assert.equal(m.JOBS[0].label, "Large model");
  assert.equal(m.usesSelection, false);
  data.scores = [{unit: "games"}];
  const [large] = context.headline(data, m);
  assert.equal(large.job.name, "large");
  assert.equal(large.latest.y, 0.625);
  assert.equal(large.against, "all opponents");
  data.report.evals.push({kind: "curve"});
  data.panel_selections.push({selection: null, control: true});
  assert.equal(context.model(data).usesSelection, true);
});

test("job labels retain seed counts and baseline identity", () => {
  const data = page([], [], {jobs: [{name: "large", launch: "large", label: "large · 2 seeds", runs: ["a", "b"]}], runs: [], checkpoints: [], clocks: []});
  data.report.labels = {large: "Large model"}; data.report.baseline = "large";
  const [job] = context.model(data).JOBS;
  assert.equal(job.label, "Large model · 2 seeds");
  assert.equal(job.name, "large"); assert.equal(job.key, "large"); assert.equal(job.baseline, true);
});

test("a launch name addresses every config job launched under it; a full name addresses one", () => {
  const jobs = [
    {name: "ppo 1a2b3c", launch: "ppo", label: "ppo 1a2b3c · 3 seeds", runs: ["a", "b", "c"]},
    {name: "ppo 4d5e6f", launch: "ppo", label: "ppo 4d5e6f · 1 seed", runs: ["d"]},
    {name: "dqn", launch: "dqn", label: "dqn · 1 seed", runs: ["e"]},
  ];
  const data = page([], [], {jobs, runs: [], checkpoints: [], clocks: []});
  data.report.labels = {ppo: "PPO", dqn: "DQN"}; data.report.baseline = "ppo";
  const m = context.model(data);
  assert.deepEqual(Array.from(m.JOBS.slice(0, 2), j => j.label), ["PPO 1a2b3c · 3 seeds", "PPO 4d5e6f · 1 seed"]);
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
    {members: ["a", "b", "c"].map(context.runMember)}, "updates", () => ({y: 0.625}), true,
  )[0];
  assert.equal(context.pointMeta(partial, context.scoreFormat), "2 of 3 seeds evaluated");
});

test("configured curves interpolate at recorded absolute clocks without extrapolation", () => {
  const training = {jobs: [{name: "large", launch: "large", runs: ["a", "b"]}],
    runs: [{name: "a", at: {updates: 1200}}, {name: "b", at: {updates: 900}}], checkpoints: [], clocks: []};
  const data = page([], [], training);
  const schedule = {clock: "updates", jobs: {large: [{at: 1000, value: 1}, {at: 1400, value: 0.2}]}};
  const [s] = context.model(data).scheduleSeries(schedule);
  assert.deepEqual(Array.from(s.points, p => p.x), [1000, 1400]);
  assert.equal(s.markers.length, 1); assert.equal(s.markers[0].x, 1200);
  assert.ok(Math.abs(s.markers[0].y - 0.6) < 1e-10);
  assert.match(s.markers[0].meta, /recorded updates/);
  training.runs[0].at.updates = 1400;
  assert.equal(context.model(data).scheduleSeries(schedule)[0].markers[0].y, 0.2);
  delete training.runs[0].at.updates;
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

// A seating held for checkpoints of line "learner": each one's point (`Page.points`), scored
// by the page or missing because its relative seat found no checkpoint.
const NEVER = {seating: "half", checkpoint: {run: "learner", name: "u100", address: null}, missing: "never", position: 50, seat: {kind: "fraction", clock: "updates", value: 0.5, job: null, at_or_before: false, trained: false}};
function seated(points, cells = []) {
  const data = page([player("u100", "learner", 100), player("u200", "learner", 200), player("u400", "learner", 400)], cells);
  data.selections = [{tournament: "t", seating: "half"}];
  data.drawn = [{seated: true, tournaments: ["t"]}];
  data.points = points.map(p => ({selection: 0, run: null, ordinal: null, line: "learner", whole: false, commit: null,
    ...(p.value == null ? {} : {score: 0, split: {}, interval: null, rows: 4, matches: 2}), ...p}));
  return data;
}

test("a seating draws each point's score where its checkpoint stood, and says why one is missing", () => {
  const data = seated([
    {at: {updates: 400}, value: 0.6, interval: [0.5, 0.7], opponents: [1]},
    {at: {updates: 200}, value: 0.55, opponents: [0]},
    {at: {updates: 100}, gap: NEVER},
  ]);
  const m = context.model(data), at = Object.fromEntries(points(m, m.JOBS[0]).map(p => [p.x, p]));
  assert.equal(at[400].y, 0.6); assert.equal(at[400].lo, 0.5);
  assert.equal(at[200].y, 0.55);
  assert.match(at[200].meta, /2 matches · vs .*u100/);
  assert.equal(at[100].missing, true);
  assert.equal(at[100].gap, "none");
  assert.equal(at[100].meta, "its line has no checkpoint at updates 50 and never will");
});

test("a seat not filled yet is a pending gap, and a point over several lines says so", () => {
  const data = seated([
    {at: {updates: 400}, value: 0.6, opponents: [0, 1]},
    {at: {updates: 200}, gap: {...NEVER, missing: "not_yet", position: 8000, seat: {...NEVER.seat, kind: "aligned", job: "small"}}},
    {at: {updates: 100}, gap: {...NEVER, seat: {...NEVER.seat, at_or_before: true, trained: true}}},
  ]);
  const m = context.model(data), at = Object.fromEntries(points(m, m.JOBS[0]).map(p => [p.x, p]));
  assert.equal(at[400].y, 0.6);
  assert.match(at[400].meta, /mean of 2 · vs .*u100, .*u200/);
  assert.equal(at[200].gap, "pending");
  assert.equal(at[200].meta, "small has no checkpoint at updates 8,000 yet");
  assert.equal(at[100].meta, "its line has no trained checkpoint at or before updates 50");
  const rendered = renderPage({...data, report: {clock: "updates", evals: [{kind: "curve", title: "Half", matches: {seating: "half"}}]}, panel_selections: [{selection: 0, control: false}]});
  assert.equal(rendered.plots.length, 1);
  assert.match(rendered.text, /first seat against the second, as seated/);
});

test("a point is its own view's: another score or split of the same checkpoint is not drawn for it", () => {
  const data = seated([
    {at: {updates: 400}, value: 0.6, opponents: [1]},
    {at: {updates: 400}, value: 0.9, opponents: [1], split: {on_play: true}},
  ]);
  const m = context.model(data);
  const whole = m.jobPoints(m.JOBS[0], "updates", m.evalValue(0, 0, {}, "")).find(p => p.x === 400);
  const split = m.jobPoints(m.JOBS[0], "updates", m.evalValue(0, 0, {on_play: true}, "")).find(p => p.x === 400);
  assert.deepEqual([whole.y, split.y], [0.6, 0.9]);
});

test("ticks a narrow range would label alike are written with the decimals their step needs", () => {
  const two = v => v.toFixed(2);
  assert.deepEqual([...context.tickLabels([0.5, 0.55, 0.6], two, 0.05)], ["0.50", "0.55", "0.60"]);
  assert.deepEqual([...context.tickLabels([0.505, 0.51, 0.515], two, 0.005)], ["0.505", "0.510", "0.515"]);
});

test("a one-seed seating draws a job's point from the seed that played it, and waits for every seed otherwise", () => {
  const training = {
    jobs: [{name: "job", launch: "job", runs: ["a", "b"]}],
    runs: [{name: "a"}, {name: "b"}], clocks: [{name: "updates"}],
    checkpoints: ["a", "b"].map(run => ({run, ordinal: 1, align: 100, at: {updates: 100}})),
  };
  const data = page([player("a100", "a", 100, "a", 1), player("b100", "b", 100, "b", 1), player("rival", null, 0)], [[1, 2, 0.75]], training);
  data.players[2].at = {}; data.players[2].placed = false;
  data.selections = [{tournament: "t", seating: "rival"}];
  data.drawn = [{seated: true, tournaments: ["t"]}];
  // Held only for seed b's checkpoint: with seeds="one" that point is the job's.
  data.points = [{selection: 0, run: "b", ordinal: 1, line: "b", at: {updates: 100}, whole: true, commit: null, score: 0, split: {}, value: 0.75, interval: null, rows: 4, matches: 4, opponents: [2]}];
  const one = points(context.model(data), context.model(data).JOBS[0])[0];
  assert.equal(one.y, 0.75);
  assert.doesNotMatch(one.meta, /seeds evaluated/);
  data.points[0].whole = false;
  const every = points(context.model(data), context.model(data).JOBS[0])[0];
  assert.equal(every.missing, true);
  assert.match(every.meta, /1 of 2 seeds evaluated/);
});
