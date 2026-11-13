// What a report page and the index share: formatting, tooltips, uPlot line charts, and
// the model of an experiment's page data (`etude.page.Page`): its jobs over seeds and the
// value of each checkpoint, so the index's headline is the report's own curve.
"use strict";
// Etude's clocks, in hours, on every training row and checkpoint (`BUILT_IN_CLOCKS`).
const BUILT_IN = ["running_hours", "hours_since_start"];

// ---------------------------------------------------------------- helpers
const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const mean = xs => xs.reduce((a, b) => a + b, 0) / xs.length;
const num = v => v == null ? "—" : Math.abs(v) >= 1e9 ? (v / 1e9).toPrecision(3) + "G" : Math.abs(v) >= 1e6 ? (v / 1e6).toPrecision(3) + "M" : Math.abs(v) >= 1e4 ? (v / 1e3).toPrecision(3) + "k" : String(+(+v).toPrecision(4));
const fmt = v => v == null ? "—" : v !== 0 && Math.abs(v) < 1e-3 ? v.toExponential(2) : num(v);
const scoreFormat = v => v == null ? "—" : Math.abs(v) >= 0.01 && Math.abs(v) < 100 ? v.toFixed(2) : fmt(v);
const scoreCount = (c, score) => `${c.rows} ${score?.unit || "rows"} · ${c.matches} matches`;
// Keep partial-seed notices; format full seed means with the chart's precision.
const pointMeta = (p, format = fmt) => p.meta ?? (p.seeds ? `mean of ${p.seeds.length} seeds: ${p.seeds.map(format).join(", ")}` : "");
const pointText = (p, format = fmt) => format(p.y) + (p.lo != null ? ` · ${p.intervalLabel || (p.seeds ? "seed range" : "95% interval")} ${format(p.lo)}–${format(p.hi)}` : "");
const dur = s => s >= 3600 ? (s / 3600).toFixed(2) + " h" : s >= 60 ? (s / 60).toFixed(1) + " min" : s.toFixed(s < 1 ? 2 : 1) + " s";
const when = ms => new Date(ms).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
const ago = (ms, now) => { const s = Math.max(0, (now - ms) / 1000); return s < 120 ? Math.round(s) + " s before" : s < 7200 ? Math.round(s / 60) + " min before" : s < 172800 ? Math.round(s / 3600) + " h before" : Math.round(s / 86400) + " d before"; };
const splitKey = o => JSON.stringify(Object.keys(o).sort().map(k => [k, o[k]]));
const show = v => typeof v === "boolean" ? (v ? "yes" : "no") : String(v);
const mixHex = (a, b, t) => { const h = x => x.replace("#", "").match(/../g).map(v => parseInt(v, 16)); const pa = h(a), pb = h(b); return `rgb(${pa.map((c, i) => Math.round(c + (pb[i] - c) * t)).join(",")})`; };
const inkOn = c => { const [r, g, b] = c.match(/\d+/g).map(Number); return 0.299 * r + 0.587 * g + 0.114 * b > 150 ? "#0b0b0b" : "#ffffff"; };
const alpha = (hex, a) => { const [r, g, b] = hex.replace("#", "").match(/../g).map(v => parseInt(v, 16)); return `rgba(${r},${g},${b},${a})`; };

// Tick labels as `format` writes them, unless two ticks would read the same ("0.51" twice
// on a narrow range): then each with the decimals its increment needs.
function tickLabels(values, format, increment) {
  const labels = values.map(format);
  if (new Set(labels).size === labels.length || !(increment > 0)) return labels;
  const decimals = Math.min(12, Math.max(0, Math.ceil(-Math.log10(increment) - 1e-9)));
  return values.map(v => v.toFixed(decimals));
}
// Why a seating's point has no score (`Page.points[].gap`, an eval `Gap`): what its relative
// seat looked for, in words: "small has no checkpoint at updates 8,000 yet".
function missingText(g) {
  const seat = g.seat, whose = seat.job || "its line", kind = seat.trained ? "trained checkpoint" : "checkpoint", yet = g.missing === "not_yet";
  if (seat.clock == null) return yet ? `${whose} has not ended yet` : `${whose} ended with no checkpoint`;
  if (g.value == null) return `the held checkpoint has no ${seat.clock} position`;
  const where = `${seat.clock} ${(+g.value.toPrecision(6)).toLocaleString("en-US")}`;
  if (seat.at_or_before) return yet ? `${whose} has not saved past ${where} yet` : `${whose} has no ${kind} at or before ${where}`;
  return `${whose} has no ${kind} at ${where} ${yet ? "yet" : "and never will"}`;
}

// ---------------------------------------------------------------- tooltips
let tipNode;
function showTip(evt, rows, title) {
  if (!tipNode) { tipNode = node("div", "tip"); document.body.appendChild(tipNode); }
  const t = tipNode; t.replaceChildren();
  if (title) t.appendChild(node("div", "tip-title", title));
  for (const r of rows) {
    const line = node("div", "tip-row"), k = node("span");
    if (r.color) { k.className = "sw" + (r.dotted ? " dot" : ""); k.style.cssText = `display:inline-block;width:14px;height:3px;background:${r.dotted ? "" : r.color};color:${r.color}`; }
    line.append(k, node("strong", null, r.value), node("span", "tip-label", r.label || ""));
    t.appendChild(line);
  }
  t.style.display = "block";
  const pad = 14, w = t.offsetWidth, h = t.offsetHeight;
  let left = evt.clientX + pad, top = evt.clientY + pad;
  if (left + w > innerWidth - 8) left = Math.max(8, evt.clientX - w - pad);
  if (top + h > innerHeight - 8) top = Math.max(8, evt.clientY - h - pad);
  t.style.left = left + "px"; t.style.top = top + "px";
}
const hideTip = () => { if (tipNode) tipNode.style.display = "none"; };

// ---------------------------------------------------------------- line charts (uPlot)
// The indices of `ys` holding a value with no value beside it on either side: a line draws
// nothing for them, so they need a marker.
function isolated(ys) {
  const found = [];
  for (let i = 0; i < ys.length; i++) if (ys[i] != null && ys[i - 1] == null && ys[i + 1] == null) found.push(i);
  return found;
}

// series: [{label, color, dotted, opacity, width, faint, pointFill, points: [{x, y, lo, hi, missing, skip, gap, meta}]}]
// A missing point is null to uPlot, so its line breaks: an evaluation that did not happen is
// never bridged. A skipped point is no point of the line (undefined, which uPlot joins over).
// A point with a `gap` is marked along the bottom: ○ not yet played, × failed or some seeds
// only, | not evaluated. `lo`/`hi` are a band, or whiskers on a line of five points or fewer.
// o: {height, compact, xLabel, yFormat, xFormat, ref, refs: [{y, label}], log, band, sync, dots, spark}.
// Charts in one `sync` group share a cursor; dragging zooms, a double click zooms out.
const charts = [];
function destroyCharts() { for (const u of charts) u.destroy(); charts.length = 0; }
function line(host, series, o = {}) {
  host.replaceChildren();
  const width = Math.max(120, host.clientWidth || 600), height = o.height || 200;
  const drawn = p => !p.missing && !p.skip && p.x != null && p.y != null && (!o.log || p.y > 0);
  if (!series.some(s => s.points.some(drawn))) {
    const notes = series.flatMap(s => {
      const reasons = new Set(s.points.filter(p => p.missing && p.meta).map(p => p.meta));
      return [...reasons].map(reason => `${s.label}: ${reason}`);
    });
    for (const text of notes.length ? notes : [o.empty || "Nothing recorded for this selection"]) host.appendChild(node("div", "na chart-empty", text));
    return;
  }
  const xs = [...new Set(series.flatMap(s => s.points.filter(p => p.x != null).map(p => p.x)))].sort((a, b) => a - b);
  const maps = series.map(s => new Map(s.points.filter(p => p.x != null).map(p => [p.x, p])));
  const column = (m, f) => xs.map(x => { const p = m.get(x); if (!p || p.skip) return undefined; return drawn(p) ? f(p) : null; });
  const muted = css("--muted"), grid = css("--grid"), ink2 = css("--ink-2");
  const data = [xs], spec = [{}], bands = [], whiskers = [];
  series.forEach((s, i) => {
    const pts = s.points.filter(drawn), sparse = pts.length <= 5 && !o.spark;
    const color = s.opacity != null && s.opacity < 1 ? alpha(s.color, s.opacity) : s.color;
    data.push(column(maps[i], p => p.y));
    spec.push({label: s.label, stroke: color, width: s.width ?? (o.spark ? 1.5 : 2), dash: s.dotted ? [1, 4] : undefined, cap: s.dotted ? "round" : "butt",
      // A dense line marks only its isolated points, which have no segment to show them.
      points: {show: true, size: sparse ? 7 : 5, fill: s.pointFill ?? color, stroke: color, filter: sparse || o.dots ? null : (u, k) => isolated(u.data[k])}});
    const banded = o.band !== false && pts.some(p => p.lo != null);
    if (banded && !sparse) {
      data.push(column(maps[i], p => p.hi ?? p.y), column(maps[i], p => p.lo ?? p.y));
      spec.push({stroke: "transparent", points: {show: false}}, {stroke: "transparent", points: {show: false}});
      bands.push({series: [spec.length - 2, spec.length - 1], fill: alpha(s.color, 0.13)});
    } else if (banded) whiskers.push([i, s.color]);
  });
  // Reference lines are flat series: the score's even value, and anchors' scores.
  const refs = [...(o.ref != null ? [{y: o.ref, label: null}] : []), ...(o.refs || [])];
  for (const r of refs) { data.push(xs.map(() => r.y)); spec.push({label: r.label || "reference", stroke: r.label ? ink2 : muted, width: 1, dash: r.label ? [6, 3] : [2, 3], points: {show: false}}); }
  const yf = o.yFormat || fmt, xf = o.xFormat || num;
  const axis = {stroke: muted, grid: {stroke: grid, width: 1}, ticks: {stroke: grid, width: 1, size: 4}, font: "11px ui-sans-serif, -apple-system, sans-serif", labelFont: "11px ui-sans-serif, -apple-system, sans-serif"};
  const toPx = v => v * devicePixelRatio;
  const opts = {
    width, height, legend: {show: false}, bands, series: spec,
    scales: {x: {time: false}, y: o.log ? {distr: 3, log: 10} : {}},
    axes: o.spark ? [{show: false}, {show: false}] : [
      {...axis, label: o.compact ? null : o.xLabel, labelSize: o.compact ? 0 : 18, values: (u, vs, a, space, incr) => tickLabels(vs, xf, incr), space: o.compact ? 50 : 70},
      // A log axis labels its powers of ten; the ticks between stay unlabelled.
      {...axis, values: (u, vs, a, space, incr) => o.log ? vs.map(v => Math.abs(Math.log10(v) % 1) < 1e-9 ? yf(v) : "") : tickLabels(vs, yf, incr), size: o.compact ? 40 : 52, space: o.compact ? 24 : 32},
    ],
    cursor: o.spark ? {show: false} : {sync: o.sync ? {key: o.sync} : undefined, drag: {x: true, y: false}, points: {size: 6}},
    hooks: {
      draw: [u => {
        const c = u.ctx, base = u.bbox.top + u.bbox.height - toPx(6);
        c.save(); c.lineWidth = toPx(1.5);
        // Whiskers: a sparse line's own intervals.
        for (const [i, color] of whiskers) for (const p of series[i].points.filter(drawn)) {
          if (p.lo == null) continue;
          const x = u.valToPos(p.x, "x", true); c.strokeStyle = alpha(color, 0.6);
          c.beginPath(); c.moveTo(x, u.valToPos(p.lo, "y", true)); c.lineTo(x, u.valToPos(p.hi, "y", true)); c.stroke();
        }
        series.forEach(s => { for (const p of s.points) {
          if (!p.gap || p.x == null) continue;
          const x = u.valToPos(p.x, "x", true), r = toPx(3);
          if (x < u.bbox.left - 1 || x > u.bbox.left + u.bbox.width + 1) continue;
          c.beginPath();
          if (p.gap === "pending") { c.strokeStyle = s.color; c.arc(x, base, r, 0, 2 * Math.PI); }
          else if (p.gap === "failed" || p.gap === "partial") { c.strokeStyle = p.gap === "failed" ? css("--bad") : s.color; c.moveTo(x - r, base - r); c.lineTo(x + r, base + r); c.moveTo(x + r, base - r); c.lineTo(x - r, base + r); }
          else { c.strokeStyle = muted; c.lineWidth = toPx(1); c.moveTo(x, base - r); c.lineTo(x, base + r); }
          c.stroke(); c.lineWidth = toPx(1.5);
        } });
        // Anchor labels at the right end, each on the page's colour and stacked upward from
        // the lowest, so close anchors never cover one another or each other's lines.
        if (!o.compact) {
          c.font = `${toPx(11)}px ui-sans-serif, -apple-system, sans-serif`; c.textAlign = "right"; c.textBaseline = "bottom";
          const right = u.bbox.left + u.bbox.width - toPx(2), step = toPx(15), surface = css("--surface");
          let above = Infinity;
          for (const r of [...(o.refs || [])].sort((a, b) => a.y - b.y)) {
            const y = Math.min(u.valToPos(r.y, "y", true) - toPx(2), above - step); above = y;
            const w = c.measureText(r.label).width + toPx(6);
            c.fillStyle = surface; c.fillRect(right - w + toPx(3), y - toPx(13), w, toPx(13));
            c.fillStyle = ink2; c.fillText(r.label, right, y - toPx(1));
          }
        }
        c.restore();
      }],
      setCursor: [u => {
        const i = u.cursor.idx;
        if (i == null || u.cursor.left < 0 || !u.over.matches(":hover")) { hideTip(); return; }
        const xv = xs[i], box = u.over.getBoundingClientRect();
        const rows = series.flatMap((s, k) => {
          const p = maps[k].get(xv);
          if (s.faint || !p) return [];
          const meta = pointMeta(p, yf);
          return [{color: s.color, dotted: s.dotted, label: s.label + (meta ? " · " + meta : ""), value: !drawn(p) ? "—" : pointText(p, yf), y: drawn(p) ? p.y : -Infinity}];
        })
          .concat((o.refs || []).map(r => ({value: yf(r.y), label: r.label + " (anchor)", y: r.y})))
          .sort((a, b) => b.y - a.y);
        showTip({clientX: box.left + u.cursor.left, clientY: box.top + u.cursor.top}, rows.length ? rows : [{value: "no point here", label: ""}], (o.xLabel || "x") + " " + xf(xv));
      }],
    },
  };
  const u = new uPlot(opts, data, host);
  u.over.addEventListener("pointerleave", hideTip);
  charts.push(u);
}

// ---------------------------------------------------------------- an experiment's page data
// Curves are jobs, runs across seeds, and one per line of placed checkpoints no training run
// saved, with one more for those of no line. A curve's members are its runs, or its line; each
// member has marks, a run's checkpoints or a line's positions, each with a `key` that names it.
// A line (`Placement.line`) is a learner: the crosstable compares a checkpoint only with its
// own line's. A model is bound to its run's checkpoint by key, and drawn where
// `Placement.at` says; the page computed both, and which players are `placed`.
const runMember = run => ({kind: "run", name: run, key: "run:" + run});
const lineMember = line => ({kind: "line", name: line, key: "line:" + line});
const runMark = (run, ordinal) => JSON.stringify(["run", run, ordinal]);
const lineMark = (line, at) => JSON.stringify(["line", line, splitKey(at)]);
function model(D) {
  const T = D.training, R = D.report;
  const indices = D.players.map((a, i) => i);
  const placed = i => D.players[i].placed;
  const lineOf = i => D.players[i].line ?? "";
  const positioned = indices.filter(i => D.players[i].run == null && placed(i));
  const atClocks = [...new Set(positioned.flatMap(i => Object.keys(D.players[i].at)))];
  // The clock a line's checkpoints are keyed by: a job's first, else the one the report names.
  const alignClock = T.clocks.find(c => !BUILT_IN.includes(c.name))?.name ?? (atClocks.includes(R.clock) ? R.clock : atClocks[0]);
  // Named lines in order, then the checkpoints of no line. A colour follows the curve's position, never its rank.
  const named = [...new Set(positioned.map(lineOf))].sort((a, b) => (a === "") - (b === "") || a.localeCompare(b));
  // A curve's key identifies it on the page: a job's name, or its line. Only external
  // curves carry a line; the empty string is the unnamed learner.
  const curves = [...T.jobs.map(j => ({...j, key: j.name, members: j.runs.map(runMember)})), ...named.map(line => {
    const name = line || "checkpoints with no line";
    return {key: "~line:" + line, name, launch: name, label: name, runs: [], members: [lineMember(line)], line};
  })];
  // A report names a job by its full name, or by its launch name for every config launched under it.
  const names = (j, n) => n === j.name || n === j.launch;
  const JOBS = curves.map((j, i) => {
    // A full-name label wins; a launch-name label keeps the config digest ("PPO 1a2b3c").
    const shown = R.labels?.[j.name] ?? (R.labels?.[j.launch] == null ? null : R.labels[j.launch] + j.name.slice(j.launch.length));
    const seeds = j.line == null ? ` · ${j.runs.length} seed${j.runs.length === 1 ? "" : "s"}` : "";
    return {...j, label: shown == null ? j.label : shown + seeds,
      index: i, color: `--j${i % 8 + 1}`, baseline: names(j, R.baseline)};
  });
  // Marks by member key: a run's checkpoints, and a line's positions, one mark per position.
  const marks = new Map();
  for (const m of T.checkpoints) {
    const member = runMember(m.run).key;
    if (!marks.has(member)) marks.set(member, []);
    marks.get(member).push({...m, key: runMark(m.run, m.ordinal)});
  }
  const lineMarks = new Map();
  for (const i of positioned) {
    const at = D.players[i].at, line = lineOf(i), member = lineMember(line).key;
    if (at[alignClock] == null) continue;
    const key = lineMark(line, at);
    if (!lineMarks.has(member)) lineMarks.set(member, new Map());
    lineMarks.get(member).set(key, {key, line, at, align: at[alignClock], elapsed: null, pace: {}});
  }
  for (const [member, got] of lineMarks) marks.set(member, [...got.values()].sort((a, b) => a.align - b.align));
  const markOf = (run, ordinal) => (marks.get(runMember(run).key) || []).find(m => m.ordinal === ordinal);
  // A value's own position (`at`, from `Player.at`) wins over its mark's, so no point disagrees with the page.
  const xAt = (m, v, c) => v.at?.[c] ?? m.at[c];
  // Per checkpoint, `value(mark)` gives {y, lo, hi, meta}, {missing, meta}, {skip, meta}
  // for a checkpoint that is no point of this line, or nothing; `gap` says why a value is absent.
  // A job's point needs every seed: seeds align on where each checkpoint stood on the run's
  // first clock, and average; the band is their range, or a single seed's own interval.
  // `some` keeps points only some seeds have, such as direct games one seed played.
  function jobPoints(job, clock, value, some = false) {
    const per = job.members.map(member => new Map((marks.get(member.key) || []).map(m => [m.align, [m, value(m)]]).filter(([, [, v]]) => v)));
    const keys = [...new Set(per.flatMap(p => [...p.keys()]))].sort((a, b) => a - b);
    return keys.map(key => {
      // A point one seed played for its whole job (`seeds="one"`) needs no other seed.
      const all = per.map(p => p.get(key)).filter(Boolean), whole = all.some(([, v]) => v.whole);
      const got = whole ? all.filter(([, v]) => v.whole) : all;
      const xs = got.map(([m, v]) => xAt(m, v, clock)).filter(x => x != null);
      const x = xs.length ? mean(xs) : null;
      const partial = got.length < job.members.length && !whole;
      const short = `${got.length} of ${job.members.length} seeds evaluated`;
      if (partial && !some) {
        // A seed without this checkpoint yet: the others' gap, if they have one, is still shown.
        const present = got.map(([, v]) => v), why = present.find(v => v.missing && v.gap);
        if (present.every(v => v.skip)) return {x, key, skip: true, gap: present[0].gap, meta: present[0].meta};
        return {x, key, missing: true, gap: why?.gap, meta: why ? `${why.meta} · ${short}` : short};
      }
      const point = pointOf(got, x, key);
      return partial ? {...point, meta: point.meta ? `${point.meta} · ${short}` : short} : point;
    });
  }
  function pointOf(got, x, key) {
    const bad = got.find(([, v]) => v.missing);
    if (bad) return {x, key, missing: true, gap: bad[1].gap, meta: bad[1].meta};
    const skipped = got.filter(([, v]) => v.skip);
    if (skipped.length === got.length) return {x, key, skip: true, gap: skipped[0][1].gap, meta: skipped[0][1].meta};
    if (skipped.length) return {x, key, missing: true, gap: "partial", meta: `${got.length - skipped.length} of ${got.length} seeds evaluated`};
    if (new Set(got.map(([, v]) => v.inference).filter(v => v != null)).size > 1)
      return {x, key, missing: true, gap: "none", meta: "seeds played different inference variants; choose a seating"};
    const ys = got.map(([, v]) => v.y), y = mean(ys);
    return got.length > 1
      ? {x, key, y, lo: Math.min(...ys), hi: Math.max(...ys), seeds: ys}
      : {x, key, y, lo: got[0][1].lo, hi: got[0][1].hi, meta: got[0][1].meta};
  }

  // ---- evals: what compare computed
  const cellIndex = new Map();
  // "Against all" is left out where it repeats a subject's only opponent: that opponent's cell stands in.
  for (const c of D.cells) if (c.opponent != null) cellIndex.set(`${c.selection}|${c.score}|${c.subject}||${+c.fixed}|${splitKey(c.split)}`, c);
  for (const c of D.cells) cellIndex.set(`${c.selection}|${c.score}|${c.subject}|${c.opponent ?? ""}|${+c.fixed}|${splitKey(c.split)}`, c);
  const cellOf = (sel, score, subject, opponent, fixed, split) => cellIndex.get(`${sel}|${score}|${subject}|${opponent ?? ""}|${+fixed}|${splitKey(split)}`);
  // A checkpoint can play several scientific variants. The requested matches choose
  // one; execution setup lives on results and never creates another candidate.
  const bound = new Map();
  function bind(key, i) {
    if (!bound.has(key)) bound.set(key, []);
    bound.get(key).push(i);
  }
  D.players.forEach((a, i) => { if (a.run != null) bind(runMark(a.run, a.ordinal), i); });
  for (const i of positioned) bind(lineMark(lineOf(i), D.players[i].at), i);
  const fixedPlayers = indices.filter(i => !placed(i));
  const coverage = new Map(D.coverage.map(c => [`${c.run}|${c.ordinal}|${c.tournament}`, c]));
  const jobOfRun = run => JOBS.find(j => j.runs.includes(run));
  // The players a curve draws: its runs' checkpoints, or its line's.
  const curvePlayers = j => j.line != null ? positioned.filter(i => lineOf(i) === j.line) : indices.filter(i => j.runs.includes(D.players[i].run));
  // The lines a curve's checkpoints belong to: a job's seeds, or a line curve's own.
  const linesOf = j => [...new Set(curvePlayers(j).map(lineOf))].sort();
  const lineMembers = line => indices.filter(i => placed(i) && lineOf(i) === line);
  // A position must identify one checkpoint within its learner and config, even across jobs.
  const positions = new Map();
  D.players.forEach((a, i) => {
    if (!placed(i) || a.at[alignClock] == null) return;
    const key = JSON.stringify([lineOf(i), a.config, a.at[alignClock]]);
    if (!positions.has(key)) positions.set(key, []);
    positions.get(key).push(i);
  });
  const variants = new Map();
  for (const a of D.players) {
    if (!variants.has(a.name)) variants.set(a.name, new Set());
    variants.get(a.name).add(a.config);
  }
  const playerName = i => {
    const a = D.players[i], name = variants.get(a.name).size > 1 ? `${a.name} ${a.config}` : a.name;
    return a.run != null ? `${a.run} ${name}` : a.line != null ? `${a.line} ${name}` : name;
  };
  function subjectAt(m, played) {
    const candidates = bound.get(m.key) || [];
    const recorded = candidates.filter(played);
    const found = recorded.length ? recorded : candidates;
    const i = found[0];
    if (i == null) return {};
    const a = D.players[i];
    const subject = {at: a.at};
    if (found.some(k => D.players[k].config !== a.config))
      // Named by config, not checkpoint: the point's position says which one, and an empty chart says it once.
      return {...subject, gap: `multiple inference variants: ${[...new Set(found.map(k => D.players[k].config))].join(" and ")}; choose a seating`};
    // Position uniqueness belongs to checkpoint resolution, for every panel.
    const same = positions.get(JSON.stringify([lineOf(i), a.config, a.at[alignClock]])) || found;
    if (same.length > 1)
      return {...subject, gap: `${same.map(playerName).join(" and ")} stood at ${alignClock} ${num(a.at[alignClock])}`};
    return {...subject, index: i};
  }
  function selName(s) {
    if (Array.isArray(s)) return s.map(selName).join(" + ");
    const detail = s.seating ?? (s.tag != null ? `tagged ${s.tag}` : null);
    if (s.kind === "outside") return "matches outside a tournament";
    return s.tournament == null ? detail ?? "every tournament" : detail == null ? s.tournament : `${s.tournament} · ${detail}`;
  }
  // A page offers each tournament and each tag within one; a seating only where a panel names it.
  const choices = D.selections.flatMap((s, i) => !Array.isArray(s) && s.seating == null ? [[String(i), selName(s)]] : []);
  // The selection eval panel `k` of the report draws, which the page resolved: with
  // `control`, the one the section's Matches control has `selected`.
  const panelSelection = (k, selected) => {
    const p = (D.panel_selections || [])[k];
    return p == null || p.control ? selected : p.selection;
  };
  const usesSelection = (D.panel_selections || []).some(p => p.control);
  function scheduleSeries(panel, jobs = JOBS) {
    return jobs.flatMap(job => {
      const samples = panel.jobs[job.name] ?? panel.jobs[job.launch];
      if (!samples) return [];
      const points = samples.map(p => ({x: p.at, y: p.value}));
      const markers = job.runs.flatMap(run => {
        const x = T.runs.find(r => r.name === run)?.at?.[panel.clock];
        if (x == null || x < points[0].x || x > points.at(-1).x) return [];
        const right = points.findIndex(p => p.x >= x), b = points[right], a = points[Math.max(0, right - 1)];
        const y = x === b.x ? b.y : a.y + (b.y - a.y) * (x - a.x) / (b.x - a.x);
        return [{run, x, y, meta: `recorded ${panel.clock} · ${run}`}];
      });
      return [{job, points, markers}];
    });
  }
  // Opponents are the anchors checkpoints met, by name; checkpoints met are head-to-head games.
  function opponents(score, sel) {
    const seen = new Set(D.cells.filter(c => c.selection === sel && c.score === score && !c.fixed && placed(c.subject)).map(c => c.opponent));
    const anchors = [...seen].filter(o => o != null && !placed(o)).sort((a, b) => D.players[a].name.localeCompare(D.players[b].name));
    return [...anchors.map(o => [String(o), D.players[o].name]), ...(seen.has(null) || !anchors.length ? [["", "all opponents"]] : [])];
  }
  // Why a checkpoint has no value here. Failed and not yet played break a line; a checkpoint
  // nothing played, or played only outside this selection, is no point of it and is marked.
  function gapOf(sel, m) {
    const tournaments = m.run == null ? [] : D.drawn[sel]?.tournaments ?? [];
    const held = tournaments.map(t => coverage.get(`${m.run}|${m.ordinal}|${t}`)).filter(Boolean);
    const c = held.reduce((a, b) => ({played: a.played + b.played, failed: a.failed + b.failed, unplayed: a.unplayed + b.unplayed}), {played: 0, failed: 0, unplayed: 0});
    const s = n => n > 1 ? "es" : "", played = c.played ? `, ${c.played} played` : "";
    if (c.failed) return {missing: true, gap: "failed", meta: `${c.failed} match${s(c.failed)} failed${played}`};
    if (c.unplayed) return {missing: true, gap: "pending", meta: `not yet played: ${c.unplayed} match${s(c.unplayed)} eval(state) wants${played}`};
    if (c.played) return {skip: true, meta: "not in these matches"};
    return {skip: true, gap: "none", meta: "not evaluated"};
  }
  // A selection of seatings draws its points (`Page.points`): each checkpoint a seating was
  // held for, where it stood, with its first seat's score against its second, which the page
  // computed, or why its relative seat found no checkpoint.
  const where = p => p.run != null ? runMark(p.run, p.ordinal) : lineMark(p.line ?? "", p.at);
  const scored = new Map(), unseated = new Map();
  for (const p of D.points || []) {
    if (p.value == null) unseated.set(`${p.selection}|${where(p)}`, p);
    else scored.set(`${p.selection}|${p.score}|${splitKey(p.split)}|${where(p)}`, p);
  }
  const seated = sel => sel != null && !!D.drawn[sel]?.seated;
  function seatedValue(score, sel, split) {
    return m => {
      const p = scored.get(`${sel}|${score}|${splitKey(split)}|${m.key}`);
      if (p) {
        const several = p.opponents.length > 1 ? `mean of ${p.opponents.length} · ` : "";
        return {y: p.value, lo: p.interval?.[0], hi: p.interval?.[1], at: p.at, whole: p.whole, meta: `${scoreCount(p, D.scores?.[score])} · ${several}vs ${p.opponents.map(playerName).join(", ")}`};
      }
      const u = unseated.get(`${sel}|${m.key}`);
      if (u) return {missing: true, gap: u.gap.missing === "not_yet" ? "pending" : "none", at: u.at, meta: missingText(u.gap)};
      return gapOf(sel, m);
    };
  }
  // Each checkpoint's value in the requested matches: as seated, for a selection of seatings;
  // else against the opponent given, an anchor's index or "" for all. Scientific variants are
  // selected by their results, never averaged together behind a curve.
  const evalValue = (score, sel, split, opponent) => {
    if (seated(sel)) return seatedValue(score, sel, split);
    return m => {
      const o = opponent === "" ? null : +opponent;
      const subject = subjectAt(m, i => !!cellOf(sel, score, i, o, false, split));
      if (subject.gap) return {missing: true, gap: "none", at: subject.at, meta: subject.gap};
      const i = subject.index;
      if (i == null || sel == null) return gapOf(sel, m);
      const a = D.players[i], c = cellOf(sel, score, i, o, false, split);
      if (!c) return gapOf(sel, m);
      return {y: c.value, lo: c.interval?.[0], hi: c.interval?.[1], at: a.at, inference: JSON.stringify([a.config, o == null ? null : D.players[o].config]), meta: scoreCount(c, D.scores?.[score])};
    };
  };
  const evaluated = pts => pts.filter(p => !p.missing && !p.skip);
  return {T, R, JOBS, marks, markOf, jobPoints, cellOf, subjectAt, placed, positioned, atClocks, alignClock, fixedPlayers, usesSelection, panelSelection, seated, scheduleSeries, coverage, jobOfRun, linesOf, lineMembers, playerName, selName, choices, opponents, evalValue, evaluated};
}

// The first curve supplies the index headline, with the page's default score,
// result-aware selection, on its first clock.
function headline(D, M) {
  const sel = M.panelSelection((D.report.evals || []).findIndex(p => p.kind === "curve"), M.choices.length ? +M.choices[0][0] : null);
  const seated = M.seated(sel);
  const opp = seated || sel == null ? "" : (M.opponents(0, sel)[0] || [""])[0];
  const clock = M.alignClock || "hours_since_start";
  return M.JOBS.map(j => {
    const pts = sel == null || !D.scores.length ? [] : M.jobPoints(j, clock, M.evalValue(0, sel, {}, opp));
    const done = M.evaluated(pts);
    return {job: j, points: pts, latest: done.length ? done[done.length - 1] : null, against: seated ? "as seated" : opp === "" ? "all opponents" : D.players[+opp].name};
  });
}

// How far an experiment's evaluation has got, by checkpoint, and its failed matches. Without
// training runs, the checkpoints are those placed by their seats, each evaluated once played.
function progress(D, M) {
  if (!D.training.runs.length) {
    const played = new Set(D.cells.filter(c => M.positioned.includes(c.subject)).map(c => c.subject));
    return {checkpoints: M.positioned.length, evaluated: played.size, pending: 0, failed: D.failures.length};
  }
  const played = new Set(D.coverage.filter(c => c.played).map(c => `${c.run}|${c.ordinal}`));
  const pending = new Set(D.coverage.filter(c => c.unplayed).map(c => `${c.run}|${c.ordinal}`));
  return {checkpoints: D.training.checkpoints.length, evaluated: played.size, pending: pending.size, failed: D.failures.length};
}
const progressText = p => [`${p.evaluated} of ${p.checkpoints} evaluated`, ...(p.pending ? [`${p.pending} not yet played`] : []), ...(p.failed ? [`${p.failed} failed match${p.failed > 1 ? "es" : ""}`] : [])].join(", ");
// Seconds the runs' attempts were active, from each start to its last row.
const trained = D => D.training.runs.reduce((t, r) => t + r.attempts.reduce((a, s) => a + Math.max(0, (s.last_row ?? s.started) - s.started), 0), 0);
// The latest moment anything of the experiment was recorded, in epoch milliseconds.
function latestActivity(D) {
  const times = [D.latest_result_ms ?? 0];
  for (const r of D.training.runs) for (const s of r.attempts) times.push((r.started + (s.last_row ?? s.started)) * 1000);
  const started = new Map(D.training.runs.map(r => [r.name, r.started]));
  for (const m of D.training.checkpoints) times.push(((started.get(m.run) ?? 0) + m.elapsed) * 1000);
  return Math.max(...times) || null;
}

// A refresh is explicit, including when a timer was explicitly requested. Ordinary
// reloads only view cached HTML. On failure the current DOM and timestamp survive.
let refreshing = false;
async function refreshPage(served) {
  if (!served) { location.reload(); return; }
  if (refreshing) return;
  refreshing = true;
  const status = document.getElementById("refresh-status");
  if (status) status.textContent = "Refreshing…";
  try {
    const response = await fetch(location.pathname, {method: "POST"});
    if (!response.ok) throw new Error(await response.text());
    location.reload();
  } catch (error) {
    if (status) status.textContent = `Refresh failed: ${error.message}. Showing the last successful generation.`;
  } finally { refreshing = false; }
}
function refreshControl(host, served) {
  if (!served) return;
  const button = node("button", null, "Refresh"), status = node("span");
  status.id = "refresh-status"; status.setAttribute("role", "status");
  button.onclick = () => refreshPage(true);
  host.append(button, status);
}
