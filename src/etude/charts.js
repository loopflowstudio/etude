// What a report page and the index share: formatting, tooltips, uPlot line charts, and
// the model of an experiment's page data (`etude.page.Page`): its jobs over seeds and the
// value of each checkpoint, so the index's headline is the report's own curve.
"use strict";
const WALL = "wall_seconds";

// ---------------------------------------------------------------- helpers
const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const mean = xs => xs.reduce((a, b) => a + b, 0) / xs.length;
const num = v => v == null ? "—" : Math.abs(v) >= 1e9 ? (v / 1e9).toPrecision(3) + "G" : Math.abs(v) >= 1e6 ? (v / 1e6).toPrecision(3) + "M" : Math.abs(v) >= 1e4 ? (v / 1e3).toPrecision(3) + "k" : String(+(+v).toPrecision(4));
const fmt = v => v == null ? "—" : v !== 0 && Math.abs(v) < 1e-3 ? v.toExponential(2) : num(v);
const dur = s => s >= 3600 ? (s / 3600).toFixed(2) + " h" : s >= 60 ? (s / 60).toFixed(1) + " min" : s.toFixed(s < 1 ? 2 : 1) + " s";
const when = ms => new Date(ms).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
const ago = (ms, now) => { const s = Math.max(0, (now - ms) / 1000); return s < 120 ? Math.round(s) + " s before" : s < 7200 ? Math.round(s / 60) + " min before" : s < 172800 ? Math.round(s / 3600) + " h before" : Math.round(s / 86400) + " d before"; };
const splitKey = o => JSON.stringify(Object.keys(o).sort().map(k => [k, o[k]]));
const show = v => typeof v === "boolean" ? (v ? "yes" : "no") : String(v);
const mixHex = (a, b, t) => { const h = x => x.replace("#", "").match(/../g).map(v => parseInt(v, 16)); const pa = h(a), pb = h(b); return `rgb(${pa.map((c, i) => Math.round(c + (pb[i] - c) * t)).join(",")})`; };
const inkOn = c => { const [r, g, b] = c.match(/\d+/g).map(Number); return 0.299 * r + 0.587 * g + 0.114 * b > 150 ? "#0b0b0b" : "#ffffff"; };
const alpha = (hex, a) => { const [r, g, b] = hex.replace("#", "").match(/../g).map(v => parseInt(v, 16)); return `rgba(${r},${g},${b},${a})`; };

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

// series: [{label, color, dotted, opacity, width, faint, points: [{x, y, lo, hi, missing, skip, gap, meta}]}]
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
  if (!series.some(s => s.points.some(drawn))) { host.appendChild(node("div", "na chart-empty", o.empty || "Nothing recorded for this selection")); return; }
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
      points: {show: true, size: sparse ? 7 : 5, fill: color, stroke: color, filter: sparse || o.dots ? null : (u, k) => isolated(u.data[k])}});
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
      {...axis, label: o.compact ? null : o.xLabel, labelSize: o.compact ? 0 : 18, values: (u, vs) => vs.map(xf), space: o.compact ? 50 : 70},
      // A log axis labels its powers of ten; the ticks between stay unlabelled.
      {...axis, values: (u, vs) => vs.map(v => !o.log || Math.abs(Math.log10(v) % 1) < 1e-9 ? yf(v) : ""), size: o.compact ? 40 : 52, space: o.compact ? 24 : 32},
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
        const rows = series.filter(s => !s.faint).map((s, k) => ({s, p: maps[series.indexOf(s)].get(xv)})).filter(({p}) => p)
          .map(({s, p}) => ({color: s.color, dotted: s.dotted, label: s.label + (p.meta ? " · " + p.meta : ""), value: !drawn(p) ? "—" : yf(p.y) + (p.lo != null ? `  [${yf(p.lo)}–${yf(p.hi)}]` : ""), y: drawn(p) ? p.y : -Infinity}))
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
// saved, with one more for those of no line. A line (`Player.line`) is a learner: `Earlier`,
// `At` and the crosstable compare a checkpoint only with its own line's. A model is
// bound to its run's checkpoint by key, and drawn where `Player.at` says, which the page
// computed (its seat's position, else its run's).
const lineRun = line => "\u0001" + line;  // a line curve's one member, where a job has runs; no run is so named
function model(D) {
  const T = D.training, R = D.report;
  const indices = D.players.map((a, i) => i);
  const placed = i => D.players[i].run != null || Object.keys(D.players[i].at).length > 0;
  const lineOf = i => D.players[i].line ?? "";
  const positioned = indices.filter(i => D.players[i].run == null && placed(i));
  const posClocks = [...new Set(positioned.flatMap(i => Object.keys(D.players[i].at)))];
  // The clock a line's checkpoints are keyed by: a job's first, else the one the report names.
  const alignClock = T.clocks[0]?.name ?? (posClocks.includes(R.clock) ? R.clock : posClocks[0]);
  // Named lines in order, then the checkpoints of no line. A colour follows the curve's position, never its rank.
  const named = [...new Set(positioned.map(lineOf))].sort((a, b) => (a === "") - (b === "") || a.localeCompare(b));
  // Only external curves carry a line; the empty string is the unnamed learner.
  const curves = [...T.jobs, ...named.map(line => {
    const name = line || "checkpoints with no line";
    return {key: "~line:" + line, name, label: name, runs: [lineRun(line)], line};
  })];
  const JOBS = curves.map((j, i) => ({...j, index: i, color: `--j${i % 8 + 1}`, baseline: R.baseline != null && (R.baseline === j.name || R.baseline === j.key)}));
  const marks = new Map(); for (const m of T.checkpoints) { if (!marks.has(m.run)) marks.set(m.run, []); marks.get(m.run).push(m); }
  // A line curve has a mark per position, as a run has one per checkpoint.
  const lineMarks = new Map();
  for (const i of positioned) {
    const at = D.players[i].at, run = lineRun(lineOf(i));
    if (at[alignClock] == null) continue;
    const ordinal = splitKey(at);
    if (!lineMarks.has(run)) lineMarks.set(run, new Map());
    lineMarks.get(run).set(ordinal, {run, ordinal, at, align: at[alignClock], elapsed: null, pace: {}});
  }
  for (const [run, got] of lineMarks) marks.set(run, [...got.values()].sort((a, b) => a.align - b.align));
  const markOf = (run, ordinal) => (marks.get(run) || []).find(m => m.ordinal === ordinal);
  const xOf = (m, c) => c === WALL ? (m.elapsed == null ? null : m.elapsed / 3600) : m.at[c];
  // A value's own position (`at`, from `Player.at`) wins over its mark's, so no point disagrees with the page.
  const xAt = (m, v, c) => c !== WALL && v.at?.[c] != null ? v.at[c] : xOf(m, c);
  // Per checkpoint, `value(run, mark)` gives {y, lo, hi, meta}, {missing, meta}, {skip, meta}
  // for a checkpoint that is no point of this line, or nothing; `gap` says why a value is absent.
  // A job's point needs every seed: seeds align on where each checkpoint stood on the run's
  // first clock, and average; the band is their range, or a single seed's own interval.
  // `some` keeps points only some seeds have, such as direct games one seed played.
  function jobPoints(job, clock, value, some = false) {
    const per = job.runs.map(run => new Map((marks.get(run) || []).map(m => [m.align, [m, value(run, m)]]).filter(([, [, v]]) => v)));
    const keys = [...new Set(per.flatMap(p => [...p.keys()]))].sort((a, b) => a - b);
    return keys.map(key => {
      const got = per.map(p => p.get(key));
      const xs = got.filter(Boolean).map(([m, v]) => xAt(m, v, clock)).filter(x => x != null);
      const x = xs.length ? mean(xs) : null;
      const short = `${got.filter(Boolean).length} of ${got.length} seeds evaluated`;
      if (got.some(g => !g) && !some) {
        // A seed without this checkpoint yet: the others' gap, if they have one, is still shown.
        const present = got.filter(Boolean).map(([, v]) => v), why = present.find(v => v.missing && v.gap);
        if (present.length && present.every(v => v.skip)) return {x, key, skip: true, gap: present[0].gap, meta: present[0].meta};
        return {x, key, missing: true, gap: why?.gap, meta: why ? `${why.meta} · ${short}` : short};
      }
      if (some) { const kept = got.filter(Boolean); if (kept.length < got.length) return {...pointOf(kept, x, key), meta: short}; }
      return pointOf(got, x, key);
    });
  }
  function pointOf(got, x, key) {
    const bad = got.find(([, v]) => v.missing);
    if (bad) return {x, key, missing: true, gap: bad[1].gap, meta: bad[1].meta};
    const skipped = got.filter(([, v]) => v.skip);
    if (skipped.length === got.length) return {x, key, skip: true, gap: skipped[0][1].gap, meta: skipped[0][1].meta};
    if (skipped.length) return {x, key, missing: true, gap: "partial", meta: `${got.length - skipped.length} of ${got.length} seeds evaluated`};
    const ys = got.map(([, v]) => v.y), y = mean(ys);
    return got.length > 1
      ? {x, key, y, lo: Math.min(...ys), hi: Math.max(...ys), seeds: ys, meta: `mean of ${ys.length} seeds: ${ys.map(fmt).join(", ")}`}
      : {x, key, y, lo: got[0][1].lo, hi: got[0][1].hi, meta: got[0][1].meta};
  }

  // ---- evals: what compare computed
  const cellIndex = new Map();
  // "Against all" is left out where it repeats a subject's only opponent: that opponent's cell stands in.
  for (const c of D.cells) if (c.opponent != null) cellIndex.set(`${c.selection}|${c.score}|${c.subject}||${+c.fixed}|${splitKey(c.split)}`, c);
  for (const c of D.cells) cellIndex.set(`${c.selection}|${c.score}|${c.subject}|${c.opponent ?? ""}|${+c.fixed}|${splitKey(c.split)}`, c);
  const cellOf = (sel, score, subject, opponent, fixed, split) => cellIndex.get(`${sel}|${score}|${subject}|${opponent ?? ""}|${+fixed}|${splitKey(split)}`);
  const bound = new Map(); D.players.forEach((a, i) => { if (a.run != null) bound.set(`${a.run}|${a.ordinal}|${a.config}`, i); });
  for (const i of positioned) {
    const k = `${lineRun(lineOf(i))}|${splitKey(D.players[i].at)}|${D.players[i].config}`;
    bound.set(k, i);
  }
  const fixedPlayers = indices.filter(i => !placed(i));
  const configs = [...new Set(indices.filter(placed).map(i => D.players[i].config))].sort();
  const coverage = new Map(D.coverage.map(c => [`${c.run}|${c.ordinal}|${c.tournament}`, c]));
  const jobOfRun = run => JOBS.find(j => j.runs.includes(run));
  // The players a curve draws: its runs' checkpoints, or its line's.
  const members = j => j.line != null ? positioned.filter(i => lineOf(i) === j.line) : indices.filter(i => j.runs.includes(D.players[i].run));
  // The lines a curve's checkpoints belong to: a job's seeds, or a line curve's own.
  const linesOf = j => [...new Set(members(j).map(lineOf))].sort();
  const lineMembers = line => indices.filter(i => placed(i) && lineOf(i) === line);
  // A position must identify one checkpoint within its learner and config, even across jobs.
  const positions = new Map();
  D.players.forEach((a, i) => {
    if (!placed(i) || a.at[alignClock] == null) return;
    const key = JSON.stringify([lineOf(i), a.config, a.at[alignClock]]);
    if (!positions.has(key)) positions.set(key, []);
    positions.get(key).push(i);
  });
  const playerName = i => { const a = D.players[i]; return a.run != null ? `${a.run} ${a.name}` : a.line != null ? `${a.line} ${a.name}` : a.name; };
  const selName = s => s.tournament == null ? "matches outside a tournament" : s.matchup != null ? `${s.tournament} · ${s.matchup}` : s.tag != null ? `${s.tournament} · tagged ${s.tag}` : s.tournament;
  // A page offers each tournament and each tag within one; a matchup only where a panel names it.
  const choices = D.selections.map((s, i) => [String(i), selName(s)]).filter(([i]) => D.selections[+i].matchup == null);
  // Opponents are the anchors checkpoints met, by name; checkpoints met are head-to-head games.
  function opponents(score, sel) {
    const seen = new Set(D.cells.filter(c => c.selection === sel && c.score === score && !c.fixed && placed(c.subject)).map(c => c.opponent));
    const anchors = [...seen].filter(o => o != null && !placed(o)).sort((a, b) => D.players[a].name.localeCompare(D.players[b].name));
    return [...anchors.map(o => [String(o), D.players[o].name]), ...(seen.has(null) || !anchors.length ? [["", "all opponents"]] : [])];
  }
  // Why a checkpoint has no value here. Failed and not yet played break a line; a checkpoint
  // nothing played, or played only outside this selection, is no point of it and is marked.
  function gapOf(sel, run, m) {
    const c = sel == null ? null : coverage.get(`${run}|${m.ordinal}|${D.selections[sel].tournament}`);
    const s = n => n > 1 ? "es" : "", played = c && c.played ? `, ${c.played} played` : "";
    if (c && c.failed) return {missing: true, gap: "failed", meta: `${c.failed} match${s(c.failed)} failed${played}`};
    if (c && c.unplayed) return {missing: true, gap: "pending", meta: `not yet played: ${c.unplayed} match${s(c.unplayed)} eval(state) wants${played}`};
    if (c && c.played) return {skip: true, meta: "not in these matches"};
    return {skip: true, gap: "none", meta: "not evaluated"};
  }
  // A curve's own opponent for subject `i` (`etude.report.Curve.opponent`): {index} or {gap}.
  // By name, whoever of that name the subject met; by position, the checkpoint of the same
  // line and config standing exactly there.
  function opponentAt(i, spec, played) {
    if (spec.kind === "name") {
      const met = indices.filter(k => D.players[k].name === spec.name && played(k));
      if (met.length > 1) return {gap: `met ${met.map(playerName).join(" and ")}, each called ${spec.name}`};
      return met.length ? {index: met[0]} : {gap: `not played against ${spec.name}`};
    }
    const me = D.players[i], here = me.at[spec.clock];
    if (here == null) return {gap: `no position on ${spec.clock}`};
    const want = spec.kind === "earlier" ? here - spec.by : spec.value;
    const found = lineMembers(lineOf(i)).filter(k => D.players[k].config === me.config && D.players[k].at[spec.clock] === want);
    if (found.length > 1) return {gap: `${found.map(playerName).join(" and ")} both stood at ${spec.clock} ${num(want)}`};
    return found.length ? {index: found[0]} : {gap: `no checkpoint at ${spec.clock} ${num(want)}`};
  }
  // The value of each checkpoint of `config` against `opponent`, split by `split`: "" for all,
  // a player's index as a string, or a curve's own opponent ({kind: "name" | "earlier" | "at"}).
  const evalValue = (score, sel, split, opponent, config) => (run, m) => {
    const i = bound.get(`${run}|${m.ordinal}|${config}`);
    if (i == null || sel == null) return gapOf(sel, run, m);
    const a = D.players[i], both = positions.get(JSON.stringify([lineOf(i), config, a.at[alignClock]]));
    const missing = meta => ({missing: true, gap: "none", at: a.at, meta});
    if (both?.length > 1) return missing(`${both.map(playerName).join(" and ")} stood at ${alignClock} ${num(a.at[alignClock])}`);
    const own = typeof opponent === "object";
    let o = opponent === "" ? null : own ? null : +opponent;
    if (own) {
      const found = opponentAt(i, opponent, k => !!cellOf(sel, score, i, k, false, split));
      if (found.gap) return missing(found.gap);
      o = found.index;
    }
    const c = cellOf(sel, score, i, o, false, split);
    if (!c) return own ? missing(`not played against ${playerName(o)}`) : gapOf(sel, run, m);
    return {y: c.value, lo: c.interval?.[0], hi: c.interval?.[1], at: a.at, meta: `${c.rows} rows · ${c.matches} matches${own ? " · vs " + playerName(o) : ""}`};
  };
  const evaluated = pts => pts.filter(p => !p.missing && !p.skip);
  return {T, R, JOBS, marks, markOf, xOf, jobPoints, cellOf, bound, placed, positioned, posClocks, alignClock, fixedPlayers, configs, coverage, jobOfRun, members, linesOf, lineMembers, playerName, selName, choices, opponents, evalValue, evaluated};
}

// The report's own headline for each job: its score curve in the page's first view (the
// first selection, score and config, against the first opponent) on its first clock.
function headline(D, M) {
  const sel = M.choices.length ? +M.choices[0][0] : null;
  const opp = sel == null ? "" : (M.opponents(0, sel)[0] || [""])[0];
  const clock = M.alignClock || WALL;
  return M.JOBS.map(j => {
    const pts = sel == null || !D.scores.length ? [] : M.jobPoints(j, clock, M.evalValue(0, sel, {}, opp, M.configs[0] ?? ""));
    const done = M.evaluated(pts);
    return {job: j, points: pts, latest: done.length ? done[done.length - 1] : null, against: opp === "" ? "all opponents" : D.players[+opp].name};
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
