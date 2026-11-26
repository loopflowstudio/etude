// Headless report behavior: node --test tests/test_charts.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {test} = require("node:test");
const context = vm.createContext({});
vm.runInContext(fs.readFileSync("src/etude/charts.js", "utf8") + "\nthis.model = model; this.isolated = isolated; this.tickLabels = tickLabels; this.runMember = runMember; this.curveScore = curveScore;", context);

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
function chartBrowser(hash = "", overview = null) {
  class Element {
    constructor() { this.children = []; this.style = {}; this.text = ""; }
    set textContent(text) { this.text = String(text); this.children = []; }
    get textContent() { return this.text + this.children.map(c => typeof c === "string" ? c : c.textContent).join(" "); }
    append(...children) { this.children.push(...children); }
    appendChild(child) { this.append(child); return child; }
    replaceChildren(...children) { this.text = ""; this.children = children; }
    setAttribute(key, value) { (this.attributes ??= {})[key] = value; }
    addEventListener() {}
  }
  const elements = new Map(), plots = [];
  const document = {
    createElement: () => new Element(),
    querySelectorAll: () => [],
    querySelector: selector => selector === ".run-overview" && overview
      ? {querySelector: () => ({dataset: {lastCheckpoint: JSON.stringify(overview)}})} : null,
    getElementById: id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  };
  const location = new URL("http://localhost/runs/test?tab=charts" + hash);
  const browser = vm.createContext({
    document, devicePixelRatio: 1, URLSearchParams, location,
    history: {replaceState(_state, _title, url) { location.href = new URL(url, location).href; }}, addEventListener() {},
    getComputedStyle: () => ({getPropertyValue: () => "#112233"}),
    IntersectionObserver: class { observe() {} },
    uPlot: class {
      constructor(options, values) { this.over = new Element(); plots.push({options, values}); }
      destroy() {}
    },
  });
  vm.runInContext(fs.readFileSync("src/etude/charts.js", "utf8"), browser);
  return {browser, document, location, plots};
}

function renderPage(data, hash = "", overview = null) {
  const {browser, document, location, plots} = chartBrowser(hash, overview);
  const full = {
    experiment: "test", notes: [], environments: [], commits: [], failures: [], absent: [], generated_ms: 0,
    scores: [{table: "games", value: "win", unit: "games"}], ...data,
    report: {evals: [{kind: "curve", title: "Score"}], perf: [], ml: [], ...data.report},
    training: {metrics: [], series: {}, bands: {}, timers: [], ...data.training, runs: data.training.runs.map(r => ({rows: 0, writers: [], ...r}))},
  };
  document.getElementById("data").textContent = JSON.stringify(full);
  const script = fs.readFileSync("src/etude/page.html", "utf8").split("<script>\n")[1].split("</script>")[0];
  vm.runInContext(script, browser);
  function click(section, text) {
    const find = el => el.onclick && el.textContent === text ? el : el.children.filter(c => typeof c !== "string").map(find).find(Boolean);
    find(document.getElementById(section + "-controls")).onclick();
  }
  return {location, click, controls: id => document.getElementById(id + "-controls").textContent, text: document.getElementById("evals-body").textContent, plots, sectionText: id => document.getElementById(id + "-body").textContent, display: id => document.getElementById(id).style.display};
}

test("experiment card captions name the metric once and keep its clock, units and scale", () => {
  const {browser, document} = chartBrowser();
  const host = document.createElement("div"), data = document.createElement("script");
  const curves = {loss: {owner: "a", x: [598], mean: [299], last: 300}};
  data.textContent = JSON.stringify([
    {title: "loss", clock: "updates", curves},
    {title: "Training", clock: "updates", unit: "nats", log: true, curves},
  ]);
  host.classList = {contains: () => false};
  host.querySelector = () => data;
  browser.drawExperimentCurves({querySelectorAll: () => [host]});
  const captions = host.children.slice(1).map(figure => figure.children[0]);
  assert.deepEqual(captions.map(c => [c.text, c.children[0].textContent]), [
    ["loss", "updates"], ["Training", "loss · nats · log scale · updates"],
  ]);
  for (const figure of host.children.slice(1)) {
    assert.equal(figure.children[1].attributes["aria-label"], "a: 300, last plotted bucket at updates 598");
  }
});

test("single-series headers show the logged endpoint and grouped seeds keep their mean", () => {
  const data = page([], [], {
    jobs: [{name: "a", label: "Solo", runs: ["a"]}, {name: "b", label: "Pair", runs: ["b1", "b2"]}],
    runs: ["a", "b1", "b2"].map(name => ({name})), checkpoints: [], clocks: [{name: "updates"}],
    metrics: ["value"], series: {updates: {value: [{owner: "a", x: [598], mean: [299], last: 300}]}},
    bands: {updates: {value: [{owner: "b", x: [598], mean: [150], low: [140], high: [160], seeds: [2]}]}},
  });
  for (const scale of ["linear", "log"]) {
    data.report = {clock: "updates", ml: [{kind: "series", title: "Value", keys: ["value"], scale}]};
    const view = renderPage({...data, run_charts: true});
    assert.match(view.sectionText("charts"), /Solo 300.*Pair 150/);
    assert.ok(view.plots[0].values.some(line => line.includes(299)));
  }
  const trace = data.training.series.updates.value[0];
  Object.assign(trace, {x: [100, 598], mean: [40, -1], last: 0});
  assert.match(renderPage({...data, run_charts: true}).sectionText("charts"), /Solo 40/);
});

test("a section the report gives no panel is hidden, header and controls with it", () => {
  const data = page([player("u100", "learner", 100)], [[0, null, 0.5]]);
  const none = renderPage(data);
  assert.deepEqual([none.display("evals"), none.display("perf"), none.display("ml")], [undefined, "none", "none"]);
  const perf = renderPage({...data, report: {clock: "updates", perf: [{kind: "lines", title: "Speed", keys: ["sps"]}]}});
  assert.deepEqual([perf.display("perf"), perf.display("ml")], ["", "none"]);
});

test("Anchor lines is available only for a curve with a reference to toggle", () => {
  const data = page([player("u100", "learner", 100), ...["alpha", "beta"].map(name =>
    ({name, placed: false, config: "{}", at: {}}))], [[0, 1, 0.25], [0, 2, 0.75]]);
  assert.doesNotMatch(renderPage(data).controls("evals"), /Anchor lines/);

  data.cells.push({...data.cells[0], subject: 2, opponent: 1, value: 0.5});
  const view = renderPage(data);
  const hasReference = () => view.plots.at(-1).options.series.some(s => s.label === "beta");
  assert.match(view.controls("evals"), /Anchor lines/);
  assert.ok(hasReference());
  view.click("evals", "Off");
  assert.ok(!hasReference());
  view.click("evals", "On");
  assert.ok(hasReference());
  view.click("evals", "beta");
  assert.doesNotMatch(view.controls("evals"), /Anchor lines/);

  data.drawn[0].seated = true;
  assert.doesNotMatch(renderPage(data).controls("evals"), /Anchor lines/);
});

test("an empty curve split cannot toggle anchor lines", () => {
  const players = [player("u100", "learner", 100), ...["alpha", "beta"].map(name =>
    ({name, placed: false, config: "{}", at: {}}))];
  const data = page(players, [[0, 1, 0.25], [2, 1, 0.5]]);
  data.report.evals = [{kind: "curve", title: "Score", split: "side"}];
  data.cells[1].split = {side: "first"};
  const empty = renderPage(data);
  assert.equal(empty.plots.length, 0);
  assert.doesNotMatch(empty.controls("evals"), /Anchor lines/);

  data.cells.push({...data.cells[0], split: {side: "first"}});
  const view = renderPage(data);
  assert.match(view.controls("evals"), /Anchor lines/);
  assert.ok(view.plots.at(-1).options.series.some(s => s.label === "beta"));
});

test("the Anchors panel lists unplaced players who have a strength or played each other", () => {
  const players = ["solo", "alpha", "beta"].map(name => ({name, placed: false, config: "{}", at: {}}));
  const data = page(players, [[0, 0, 0.5]]);
  data.report.evals = [{kind: "anchors", title: "Anchors"}];
  assert.match(renderPage(data).text, /No two unplaced players played each other/);

  data.cells.push({...data.cells[0], subject: 1, opponent: 2});
  const view = renderPage(data);
  assert.match(view.text, /2 anchors/);
  assert.match(view.text, /alpha/);
  assert.match(view.text, /beta/);
  assert.doesNotMatch(view.text, /solo/);

  data.cells.push({...data.cells[0], opponent: null, fixed: true});
  assert.match(renderPage(data).text, /3 anchors/);
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
  const many = Array.from({length: 160}, (_, i) => ({tournament: "half", seating: `s-${i + 1}`}));
  assert.equal(m.selName(many), "half · s-1 … half · s-160 (160 seatings)");
  const mixed = [{seating: "s-1"}, {tag: "anchor"}, {seating: "s-3"}, {kind: "outside"}];
  assert.equal(m.selName(mixed), "s-1 … matches outside a tournament (4 selections)");
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

test("the index headline uses the primary curve and its first named anchor", () => {
  const anchor = name => ({name, placed: false, config: "{}", at: {}});
  const data = page([player("u150", "learner", 150), anchor("Z"), anchor("alpha")],
    [[0, 1, 0.25], [0, 2, 0.75], [0, null, 0.5]]);
  data.scores = [{table: "scores", value: "won"}];
  data.report.evals = [{kind: "curve"}];
  const [head] = context.headline(data, context.model(data));
  assert.equal(head.against, "alpha");
  assert.equal(head.latest.y, 0.75);
  data.report.evals = [{kind: "anchors"}];
  assert.equal(context.headline(data, context.model(data))[0].latest, null);
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

test("refresh failure leaves the page visible and a later success reloads once", async () => {
  const status = {textContent: ""};
  const button = {};
  let reloads = 0, requests = 0, fail = true;
  const browser = vm.createContext({
    document: {getElementById: id => id === "refresh-report" ? button : status},
    location: {pathname: "/experiments/lab/report", reload: () => { reloads++; }},
    fetch: async () => { requests++; return {ok: !fail, text: async () => "source unavailable"}; },
  });
  vm.runInContext(fs.readFileSync("src/etude/refresh_report.js", "utf8"), browser);
  assert.equal(requests, 0); // loading the chart script never polls
  await button.onclick();
  assert.equal(requests, 1); assert.equal(reloads, 0);
  assert.match(status.textContent, /Refresh failed: source unavailable.*last successful generation/);
  assert.equal(button.disabled, false);
  fail = false;
  await button.onclick();
  assert.equal(requests, 2); assert.equal(reloads, 1);
});

// A seating held for checkpoints of line "learner": each one's point (`Page.points`), scored
// by the page or missing because its relative seat found no checkpoint.
const NEVER = {seating: "half", checkpoint: {run: "learner", name: "u100", address: null}, missing: "never", value: 50, seat: {kind: "fraction", clock: "updates", value: 0.5, job: null, at_or_before: false, trained: false}};
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
    {at: {updates: 200}, gap: {...NEVER, missing: "not_yet", value: 8000, seat: {...NEVER.seat, kind: "aligned", job: "small"}}},
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

  data.report.evals = [{kind: "curve", title: "Half", split: "on_play", matches: {seating: "half"}}];
  data.panel_selections = [{selection: 0, control: false}];
  const view = renderPage(data);
  assert.match(view.text, /on_play = yes/);
  assert.equal(view.plots[0].values[1].at(-1), 0.9);
});

test("a seated curve skips checkpoints only in the second seat and marks only held checkpoints pending or failed", () => {
  const training = {
    jobs: [{name: "a", launch: "a", label: "A", runs: ["a"]}],
    runs: [{name: "a"}],
    clocks: [{name: "updates", kind: "step"}],
    checkpoints: [400, 500, 600, 1000].map((u, i) => ({
      run: "a", ordinal: i + 1, align: u, at: {updates: u},
    })),
  };
  const data = page(
    [400, 500, 600, 1000].map((u, i) => player(`u${u}`, "a", u, "a", i + 1)),
    [],
    training,
  );
  data.selections = [{tournament: "half", seating: "vs half"}];
  data.drawn = [{seated: true, tournaments: ["half"]}];
  // Tournament coverage marks BOTH u500 (seat 2) and u1000 (seat 1) unplayed for u1000 vs u500.
  data.coverage = [
    {run: "a", ordinal: 1, tournament: "half", played: 1, failed: 0, unplayed: 0},
    {run: "a", ordinal: 2, tournament: "half", played: 0, failed: 0, unplayed: 1},
    {run: "a", ordinal: 3, tournament: "half", played: 1, failed: 0, unplayed: 0},
    {run: "a", ordinal: 4, tournament: "half", played: 0, failed: 0, unplayed: 1},
  ];
  // Only held checkpoints (u400, u600, u1000) get Points; u500 has none.
  data.points = [
    {selection: 0, run: "a", ordinal: 1, line: "a", at: {updates: 400}, whole: false, commit: null, score: 0, split: {}, value: 0.6, interval: null, rows: 4, matches: 1, opponents: [0]},
    {selection: 0, run: "a", ordinal: 3, line: "a", at: {updates: 600}, whole: false, commit: null, score: 0, split: {}, value: 0.7, interval: null, rows: 4, matches: 1, opponents: [0]},
    {selection: 0, run: "a", ordinal: 4, line: "a", at: {updates: 1000}, whole: false, commit: null, score: null, split: {}, value: null, interval: null, rows: 0, matches: 0, opponents: [], gap: null, failed: 0, unplayed: 1},
  ];
  data.report = {clock: "updates", evals: [{kind: "curve", title: "Half", matches: {tournament: "half", seating: "vs half"}}]};
  data.panel_selections = [{selection: 0, control: false}];

  const m = context.model(data);
  const at = Object.fromEntries(points(m, m.JOBS[0]).map(p => [p.x, p]));
  assert.equal(at[400].y, 0.6);
  assert.equal(at[500].skip, true);
  assert.equal(at[500].gap, undefined);
  assert.equal(at[600].y, 0.7);
  assert.equal(at[1000].missing, true);
  assert.equal(at[1000].gap, "pending");

  // Plotted column: u400=0.6, u500=undefined (bridges unbroken!), u600=0.7, u1000=null (pending).
  const rendered = renderPage(data);
  assert.deepEqual([...rendered.plots[0].values[0]], [400, 500, 600, 1000]);
  assert.deepEqual([...rendered.plots[0].values[1]], [0.6, undefined, 0.7, null]);

  // If u1000 fails instead of pending, u1000 is failed and u500 is still skipped.
  data.points[2].unplayed = 0;
  data.points[2].failed = 1;
  data.coverage[1].unplayed = 0;
  data.coverage[1].failed = 1;
  const m2 = context.model(data);
  const at2 = Object.fromEntries(points(m2, m2.JOBS[0]).map(p => [p.x, p]));
  assert.equal(at2[500].skip, true);
  assert.equal(at2[500].gap, undefined);
  assert.equal(at2[1000].missing, true);
  assert.equal(at2[1000].gap, "failed");
});

test("partially scored seated points keep the line unbroken while surfacing failed and unplayed counts", () => {
  const data = seated([
    {at: {updates: 200}, value: 0.5, opponents: [0], failed: 0, unplayed: 3},
    {at: {updates: 400}, value: 0.7, opponents: [0], failed: 2, unplayed: 5},
    {at: {updates: 600}, value: 0.8, opponents: [0], failed: 0, unplayed: 0},
  ]);
  const m = context.model(data);
  const at = Object.fromEntries(points(m, m.JOBS[0]).map(p => [p.x, p]));
  assert.equal(at[200].y, 0.5);
  assert.equal(at[200].missing, undefined);
  assert.equal(at[200].gap, undefined);
  assert.match(at[200].meta, /3 not yet played/);
  assert.equal(at[400].y, 0.7);
  assert.equal(at[400].missing, undefined);
  assert.equal(at[400].gap, "failed");
  assert.match(at[400].meta, /2 matches failed, 5 not yet played/);

  // Line stays unbroken across all three points, and u400 draws a red × failure marker.
  const rendered = renderPage({...data, report: {clock: "updates", evals: [{kind: "curve", title: "Half", matches: {seating: "half"}}]}, panel_selections: [{selection: 0, control: false}]});
  assert.deepEqual([...rendered.plots[0].values[1]], [undefined, 0.5, 0.7, 0.8]);
  const strokes = [], ctx = {
    save() {}, restore() {}, setLineDash() {}, beginPath() {},
    arc() {}, moveTo() {}, lineTo() {},
    stroke() { strokes.push(this.strokeStyle); },
    measureText: () => ({width: 10}), fillRect() {}, fillText() {},
  };
  rendered.plots[0].options.hooks.draw[0]({ctx, bbox: {top: 0, left: 0, width: 700, height: 200}, valToPos: x => x});
  assert.ok(strokes.length >= 1);

  // Multi-seed job points aggregate failed/unplayed notes and keep gap="failed".
  const twoSeed = page(
    [player("a400", "a", 400, "a", 1), player("b400", "b", 400, "b", 1)],
    [],
    {
      jobs: [{name: "j", launch: "j", label: "J", runs: ["a", "b"]}],
      runs: [{name: "a"}, {name: "b"}],
      clocks: [{name: "updates", kind: "step"}],
      checkpoints: [
        {run: "a", ordinal: 1, align: 400, at: {updates: 400}},
        {run: "b", ordinal: 1, align: 400, at: {updates: 400}},
      ],
    },
  );
  twoSeed.selections = [{tournament: "half", seating: "half"}];
  twoSeed.drawn = [{seated: true, tournaments: ["half"]}];
  twoSeed.points = [
    {selection: 0, run: "a", ordinal: 1, line: "a", at: {updates: 400}, whole: false, commit: null, score: 0, split: {}, value: 0.6, interval: null, rows: 2, matches: 1, opponents: [0], failed: 1, unplayed: 2},
    {selection: 0, run: "b", ordinal: 1, line: "b", at: {updates: 400}, whole: false, commit: null, score: 0, split: {}, value: 0.8, interval: null, rows: 2, matches: 1, opponents: [1], failed: 0, unplayed: 3},
  ];
  const m2 = context.model(twoSeed);
  const [combined] = points(m2, m2.JOBS[0]);
  assert.equal(combined.y, 0.7);
  assert.equal(combined.gap, "failed");
  assert.match(context.pointMeta(combined), /mean of 2 seeds:.*1 match failed, 5 not yet played/);
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


test("evaluation captions omit separators for absent score and selection text", () => {
  const data = page([player("u100", "learner", 100)], [[0, null, 0.5]]);
  data.scores = [];
  data.report = {evals: [{kind: "curve", title: "Score", matches: {seating: "missing"}}]};
  data.panel_selections = [{selection: null, control: false}];
  const rendered = renderPage(data);
  assert.doesNotMatch(rendered.text, /Score ·|·\s*·/);
  assert.match(rendered.text, /vs all opponents · band: 95% interval/);
});

test("explicit absent metric keys remain visible as report warnings", () => {
  const data = page([], [], {
    jobs: [{name: "a", launch: "a", runs: ["a"]}], runs: [{name: "a"}],
    checkpoints: [], clocks: [{name: "updates"}],
  });
  data.report = {ml: [{kind: "series", title: "Loss", keys: ["misspelt/loss"]}]};
  assert.match(renderPage(data).sectionText("ml"), /Named by a panel but not logged by these runs: misspelt\/loss/);
});


test("a live run leads with measured groups from either report section", () => {
  const data = page([], [], {
    jobs: [{name: "a", launch: "a", runs: ["a"]}], runs: [{name: "a"}],
    checkpoints: [], clocks: [{name: "steps"}, {name: "updates", kind: "step"}],
    metrics: ["loss", "accuracy"],
    series: {updates: {loss: [{owner: "a", x: [4], mean: [7]}], accuracy: [{owner: "a", x: [4], mean: [0.5]}]}},
  });
  data.report = {clock: "updates", perf: [
    {kind: "pace", title: "Pace"}, {kind: "elapsed", title: "Elapsed"},
    {kind: "attempts", title: "Writers"}, {kind: "flame", title: "Timers", prefix: "time/"},
    {kind: "series", title: "Training loss", keys: ["loss", "missing"], scale: "log", unit: "nats"},
  ]};
  const render = () => renderPage({...data, run_charts: true});
  const first = render();
  assert.match(first.sectionText("charts"), /Training loss.*nats.*log scale.*Other logged metrics.*accuracy/);
  assert.doesNotMatch(first.sectionText("charts"), /missing/);
  assert.deepEqual([...first.plots[0].values[1]], [7]);
  assert.equal(first.plots[0].options.axes[0].label, "updates");
  assert.equal(first.plots[0].options.scales.y.log, 10);
  assert.equal(first.display("performance"), "none");
  data.training.checkpoints.push({run: "a", ordinal: 1, align: 4, at: {updates: 4}, elapsed: 3, pace: {updates: null}});
  assert.match(render().sectionText("performance"), /Elapsed/);
  assert.doesNotMatch(render().sectionText("performance"), /Pace/);
  data.training.checkpoints.push({run: "a", ordinal: 2, align: 8, at: {updates: 8}, elapsed: 5, pace: {updates: 2}});
  assert.match(render().sectionText("performance"), /Pace.*Elapsed/);
});

test("run links retain only changed chart controls and preserve section anchors", () => {
  const data = page([], [], {
    jobs: [{name: "a", runs: ["a"]}], runs: [{name: "a"}], checkpoints: [],
    clocks: [{name: "updates"}, {name: "steps"}], metrics: ["loss"],
    series: {updates: {loss: [{owner: "a", x: [150], mean: [0.42]}]}},
  });
  data.run_charts = true;
  data.report = {clock: "updates", ml: [{kind: "series", title: "Losses", keys: ["loss"]}]};
  const view = renderPage(data);
  assert.equal(view.location.hash, "");
  assert.deepEqual([...view.plots[0].options.scales.x.range], [0, 150]);
  view.click("charts", "Raw");
  assert.equal(view.location.hash, "#smooth=off");
  view.click("charts", "steps");
  const restored = renderPage(data, view.location.hash + "&sel=3&hide=a");
  assert.equal(restored.location.hash, "#mlclock=steps&smooth=off");
  restored.click("charts", "Smoothed");
  restored.click("charts", "updates");
  assert.equal(restored.location.href, "http://localhost/runs/test?tab=charts");
  assert.equal(renderPage({...data, run_charts: false}, "#ml").location.hash, "#ml");
});

test("Overview draws the first two report panels and marks the last checkpoint on their clock", () => {
  const keys = ["return", "loss", "accuracy"];
  const data = page([], [], {
    jobs: [{name: "a", runs: ["a"]}], runs: [{name: "a"}], checkpoints: [], clocks: [{name: "steps"}],
    metrics: keys, series: {steps: Object.fromEntries(keys.map(key => [key, [{owner: "a", x: [10, 20], mean: [1, 2]}]]))},
  });
  data.run_charts = true;
  data.report = {clock: "steps", ml: keys.map(key => ({kind: "series", title: key, keys: [key]}))};
  const view = renderPage(data, "", {steps: 15});
  assert.equal(view.plots.length, 2);
  assert.match(view.sectionText("charts"), /return.*loss/);
  assert.doesNotMatch(view.sectionText("charts"), /accuracy/);
  for (const plot of view.plots) {
    assert.deepEqual([...plot.values[0]], [10, 15, 20]);
    const paths = [], ctx = {
      save() {}, restore() {}, setLineDash() {}, beginPath() {}, stroke() {},
      moveTo(x, y) { paths.push([x, y]); }, lineTo(x, y) { paths.push([x, y]); },
    };
    plot.options.hooks.draw[0]({ctx, bbox: {top: 2, left: 0, width: 20, height: 20}, valToPos: x => x});
    assert.deepEqual(paths, [[15, 2], [15, 22]]);
  }
  assert.equal(renderPage(data).plots.length, 3);
});

test("a timer-only run shows its measured breakdown under Performance", () => {
  const data = page([], [], {
    jobs: [{name: "a", launch: "a", label: "Learner", runs: ["a"]}], runs: [{name: "a", at: {steps: 1}}],
    checkpoints: [], clocks: [{name: "steps", kind: "step"}], metrics: ["time/train"],
    timers: [{run: "a", prefix: "time/", tree: {name: "active", seconds: 2, children: [
      {name: "train", seconds: 2, status: "recorded", children: []},
    ]}}],
  });
  data.report = {ml: [{kind: "flame", title: "Training timers", prefix: "time/"}]};
  const view = renderPage({...data, run_charts: true});
  assert.match(view.sectionText("charts"), /Timer metrics are shown under Performance/);
  assert.match(view.sectionText("performance"), /Training timers.*seconds logged under time\//);
});

test("declared clocks alone leave Charts with an empty message", () => {
  const data = page([], [], {runs: [{name: "a"}], jobs: [], clocks: [{name: "steps"}], checkpoints: []});
  const view = renderPage({...data, run_charts: true});
  assert.equal(view.display("charts"), "");
  assert.equal(view.sectionText("charts"), "No metric curves recorded.");
  assert.equal(view.display("performance"), "none");
});

test("pace plots restart boundaries as null gaps between measured intervals", () => {
  const data = page([], [], {
    jobs: [{name: "a", launch: "a", label: "A", runs: ["a"]}],
    runs: [{name: "a"}], clocks: [{name: "updates", kind: "step"}],
    checkpoints: [null, 10, null, 10].map((pace, i) => ({
      run: "a", ordinal: i + 1, align: i + 1, at: {updates: i + 1}, pace: {updates: pace},
    })),
  });
  data.report = {clock: "updates", perf: [{kind: "pace", title: "Pace"}]};
  for (const run_charts of [false, true]) {
    const rendered = renderPage({...data, run_charts});
    assert.equal(rendered.plots.length, 1);
    assert.deepEqual([...rendered.plots[0].values[1]], [null, 10, null, 10]);
    assert.equal(rendered.text === "", run_charts);
  }
});

test("a curve finds its score when the score carries a range", () => {
  const scores = [{table: "plays", value: "won", per: null, range: null},
                  {table: "plays", value: "won", per: "won", range: [0, 1]}];
  const D = {scores};
  assert.equal(context.curveScore(D, {score: {table: "plays", value: "won", per: "won", range: [0, 1]}}, 0), 1);
  assert.equal(context.curveScore(D, {score: {table: "plays", value: "won", per: "won", range: [0, 2]}}, 0), -1);
  assert.equal(context.curveScore(D, {}, 0), 0);
});

test("a visible run updates dates in place and reloads only when its content changes", async () => {
  const observation = {
    id: "run-observation", dataset: {refresh: "5000", version: "step-1"},
    textContent: "files observed 1 min ago",
  };
  const collected = {}, error = {};
  const nav = {
    id: "view-rail", contains: () => false,
    replaceWith: next => { visibleNav = next; }, scrollTo() {},
  };
  let visibleNav = nav;
  const document = {
    hidden: false,
    querySelector: () => observation,
    getElementById: id => ({"view-rail": visibleNav, "run-observation": observation, "console-collected": collected, "status-error": error})[id],
    // Supply the fragment the browser would parse from the successful response.
    createElement: () => ({content: {firstElementChild: next}}),
  };
  const next = {
    nextElementSibling: {...nav, textContent: "local and lab"},
    id: observation.id, dataset: {version: "step-1", collected: "Last collected 2 min ago"},
    textContent: "files observed 2 min ago",
  };
  const url = "/runs/lab/base@0";
  let tick, requests = [], navigations = 0, status = 503;
  const browser = vm.createContext({
    document,
    URLSearchParams, location: {pathname: url, search: "?tab=charts", reload: () => navigations++},
    fetch: async path => {
      requests.push(path);
      return {ok: status === 200, status, text: async () => ""};
    },
    setTimeout: (callback, delay) => {
      assert.equal(delay, 5000);
      tick = () => { tick = undefined; return callback(); };
    },
  });
  vm.runInContext(fs.readFileSync("src/etude/refresh_status.js", "utf8"), browser);

  await tick();
  assert.match(error.textContent, /503/);
  assert.equal(observation.textContent, "files observed 1 min ago");
  status = 200;
  await tick();
  assert.equal(error.textContent, "");
  assert.equal(visibleNav.textContent, "local and lab");
  assert.equal(observation.textContent, "files observed 2 min ago");
  assert.equal(collected.textContent, "Last collected 2 min ago");
  next.dataset.version = "step-2";
  document.hidden = true;
  await tick();
  assert.equal(navigations, 0);
  assert.equal(requests.length, 2);
  for (const request of requests) {
    const poll = new URL(request, "http://localhost");
    assert.equal(poll.pathname, url);
    assert.deepEqual(Object.fromEntries(poll.searchParams), {tab: "charts", observe: "1"});
  }
  document.hidden = false;
  await tick();
  assert.equal(navigations, 1);
  assert.equal(tick, undefined); // A slow navigation must finish before another check.
});
