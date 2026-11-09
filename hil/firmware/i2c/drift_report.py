"""
A self-contained HTML report of I2C sensor event timing, for the drift
benchmark in test_i2c_sensors.py: when the broker received each sensor's
i2c.Events, against an ideal clock at the sensor's period.

    write_report(path, title, subtitle, sensors, early, late)

sensors is a list of {"name", "address", "period", "times", "summary",
"failure", "slot"}: times when the broker received each event (seconds since the
epoch), summary the _timing() numbers (or None), failure why it failed
(or None). early/late bound an interval around the period
(INTERVAL_EARLY/INTERVAL_LATE).

The page draws, with no network access:
- Drift: how far each sensor's events are behind an ideal clock started
  at its first event, per interval, all sensors on one chart.
- Intervals: each sensor's interval lengths, a small multiple per sensor
  on a shared scale, with the period and the allowed band.
- A table of the numbers, which is also the page's text alternative.
"""

import html
import json
from pathlib import Path


# Categorical slots 1-8 (light, dark), in fixed order: a sensor keeps its
# slot by its position in the test's SENSORS, whichever fail. Validated
# adjacent-pair CVD and normal-vision safe in both modes; in light mode
# aqua, yellow and magenta are under 3:1 on the surface, so the table
# carries every value.
SERIES = [("#2a78d6", "#3987e5"), ("#eb6834", "#d95926"),
          ("#1baf7a", "#199e70"), ("#eda100", "#c98500"),
          ("#e87ba4", "#d55181"), ("#008300", "#008300"),
          ("#6250d6", "#9085e9"), ("#e34948", "#e66767")]


def _series_css(scheme):
    index = 0 if scheme == "light" else 1
    return "\n".join(f"  --series-{i + 1}: {pair[index]};"
                     for i, pair in enumerate(SERIES))


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
.viz-root {
  color-scheme: light;
  --surface-1: #fcfcfb;
  --page: #f9f9f7;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --text-muted: #898781;
  --grid: #e1e0d9;
  --axis: #c3c2b7;
  --band: rgba(11, 11, 11, 0.05);
  --border: rgba(11, 11, 11, 0.10);
__SERIES_LIGHT__
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --page: #0d0d0d;
    --text-primary: #f0efec;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --band: rgba(255, 255, 255, 0.06);
    --border: rgba(255, 255, 255, 0.10);
__SERIES_DARK__
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --surface-1: #1a1a19;
  --page: #0d0d0d;
  --text-primary: #f0efec;
  --text-secondary: #c3c2b7;
  --text-muted: #898781;
  --grid: #2c2c2a;
  --axis: #383835;
  --band: rgba(255, 255, 255, 0.06);
  --border: rgba(255, 255, 255, 0.10);
__SERIES_DARK__
}
body { margin: 0; }
.viz-root {
  font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--page); color: var(--text-primary);
  padding: 24px; min-height: 100vh; box-sizing: border-box;
}
h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; }
h2 { font-size: 16px; font-weight: 600; margin: 0 0 2px; }
h3 { font-size: 13px; font-weight: 600; margin: 0; }
.sub { color: var(--text-secondary); margin: 0 0 16px; }
.card {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px; margin: 0 0 16px; max-width: 960px;
  position: relative;
}
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; margin: 8px 0;
          color: var(--text-secondary); font-size: 13px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.key { width: 16px; height: 2px; border-radius: 1px; display: inline-block; }
svg { display: block; width: 100%; height: auto; overflow: visible; }
svg text { fill: var(--text-muted); font-size: 11px;
           font-variant-numeric: tabular-nums; }
.grid line { stroke: var(--grid); stroke-width: 1; }
.baseline { stroke: var(--axis); stroke-width: 1; }
.series { fill: none; stroke-width: 2; stroke-linejoin: round;
          stroke-linecap: round; }
.dot { stroke: var(--surface-1); stroke-width: 2; }
.band { fill: var(--band); }
.ref { stroke: var(--text-muted); stroke-width: 1; }
.cross { stroke: var(--text-muted); stroke-width: 1; visibility: hidden; }
.multiples { display: grid; gap: 16px;
             grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); }
.multiples .panel { position: relative; }
.multiples .meta { color: var(--text-secondary); font-size: 12px;
                   margin: 0 0 4px; }
.tip {
  position: absolute; pointer-events: none; visibility: hidden;
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 6px; padding: 6px 8px; font-size: 12px; z-index: 2;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12); white-space: nowrap;
}
.tip .head { color: var(--text-secondary); margin-bottom: 2px; }
.tip .row { display: flex; align-items: center; gap: 6px; }
.tip .row strong { font-variant-numeric: tabular-nums; }
.tip .row span:last-child { color: var(--text-secondary); }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: right; padding: 6px 8px;
         border-bottom: 1px solid var(--grid);
         font-variant-numeric: tabular-nums; }
th { color: var(--text-secondary); font-weight: 600; }
th:first-child, td:first-child { text-align: left; }
td.fail { text-align: left; color: var(--text-secondary); }
.note { color: var(--text-secondary); }
.note strong { color: var(--text-primary); }
.toggle { position: absolute; top: 24px; right: 24px; font: inherit;
          background: var(--surface-1); color: var(--text-primary);
          border: 1px solid var(--border); border-radius: 6px;
          padding: 4px 10px; cursor: pointer; }
</style>
</head>
<body>
<div class="viz-root">
<button class="toggle" type="button" id="theme">Toggle theme</button>
<h1>__TITLE__</h1>
<p class="sub">__SUBTITLE__</p>
<div class="card note" id="excluded" hidden></div>

<div class="card" id="drift-card">
  <h2>Drift behind an ideal clock</h2>
  <p class="sub" id="drift-sub"></p>
  <div class="legend" id="drift-legend"></div>
  <svg id="drift" role="img" aria-labelledby="drift-desc"></svg>
  <p class="sub" id="drift-desc" hidden></p>
  <div class="tip" id="drift-tip"></div>
</div>

<div class="card">
  <h2>Interval between events</h2>
  <p class="sub" id="int-sub"></p>
  <div class="multiples" id="multiples"></div>
</div>

<div class="card">
  <h2>Numbers</h2>
  <table id="table"></table>
</div>
</div>

<script type="application/json" id="data">__DATA__</script>
<script>
(() => {
  const data = JSON.parse(document.getElementById("data").textContent);
  const NS = "http://www.w3.org/2000/svg";
  const root = document.documentElement;
  document.getElementById("theme").addEventListener("click", () => {
    const dark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
  });

  const el = (tag, attrs, parent) => {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
  };
  const html = (tag, cls, text, parent) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  };
  const color = (s) => `var(--series-${s.slot + 1})`;
  const fmt = (v, digits = 3) => (v >= 0 ? "+" : "−") +
                                 Math.abs(v).toFixed(digits) + " s";
  // Clean tick values covering [lo, hi]
  const ticks = (lo, hi, count = 5) => {
    const raw = (hi - lo) / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag)
      .find((s) => s >= raw);
    // Outward to whole steps, so the data never runs past the last tick
    const first = Math.floor(lo / step + 1e-9) * step;
    const last = Math.ceil(hi / step - 1e-9) * step;
    const out = [];
    for (let v = first; v <= last + step * 1e-6; v += step) {
      out.push(+v.toFixed(6));
    }
    return out;
  };

  // Per sensor: intervals, and drift[n] = (t_n - t_0) - n * period
  const series = data.sensors.filter((s) => s.times.length > 1).map((s) => {
    const t0 = s.times[0];
    return {
      ...s,
      intervals: s.times.slice(1).map((t, i) => t - s.times[i]),
      drift: s.times.map((t, n) => (t - t0) - n * s.period),
    };
  });
  const periods = [...new Set(series.map((s) => s.period))];
  const periodText = periods.length === 1 ? `${periods[0]} s` :
    "each sensor's own";

  // A crosshair + tooltip over an SVG plot, snapping to interval n
  function hover(svg, tip, card, x, nMax, rows, plot) {
    const cross = el("line", {class: "cross", y1: plot.top,
                              y2: plot.bottom}, svg);
    const hit = el("rect", {x: plot.left, y: plot.top,
                            width: plot.right - plot.left,
                            height: plot.bottom - plot.top,
                            fill: "transparent"}, svg);
    const show = (evt) => {
      const box = svg.getBoundingClientRect();
      const vx = (evt.clientX - box.left) * (plot.width / box.width);
      const n = Math.max(0, Math.min(nMax, Math.round(x.invert(vx))));
      cross.setAttribute("x1", x(n));
      cross.setAttribute("x2", x(n));
      cross.style.visibility = "visible";
      tip.replaceChildren();
      html("div", "head", `Interval ${n}`, tip);
      for (const [s, value] of rows(n)) {
        const row = html("div", "row", undefined, tip);
        const key = html("span", "key", undefined, row);
        key.style.background = color(s);
        html("strong", "", value, row);
        html("span", "", s.name, row);
      }
      tip.style.visibility = "visible";
      const cardBox = card.getBoundingClientRect();
      let left = evt.clientX - cardBox.left + 12;
      if (left + tip.offsetWidth > cardBox.width) {
        left -= tip.offsetWidth + 24;
      }
      tip.style.left = `${left}px`;
      tip.style.top = `${evt.clientY - cardBox.top + 12}px`;
    };
    const hide = () => {
      cross.style.visibility = "hidden";
      tip.style.visibility = "hidden";
    };
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerleave", hide);
  }

  function scale(d0, d1, r0, r1) {
    const f = (v) => r0 + (v - d0) / (d1 - d0 || 1) * (r1 - r0);
    f.invert = (p) => d0 + (p - r0) / (r1 - r0) * (d1 - d0);
    return f;
  }

  // --- Drift chart --------------------------------------------------
  {
    const W = 720, H = 316;
    const plot = {left: 52, right: W - 16, top: 8, bottom: H - 44,
                  width: W};
    const svg = document.getElementById("drift");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const nMax = Math.max(1, ...series.map((s) => s.drift.length - 1));
    const all = series.flatMap((s) => s.drift).concat([0]);
    const yt = ticks(Math.min(...all), Math.max(...all));
    const x = scale(0, nMax, plot.left, plot.right);
    const y = scale(yt[0], yt[yt.length - 1], plot.bottom, plot.top);
    const grid = el("g", {class: "grid"}, svg);
    for (const v of yt) {
      el("line", {x1: plot.left, x2: plot.right, y1: y(v), y2: y(v)}, grid);
      el("text", {x: plot.left - 8, y: y(v) + 4, "text-anchor": "end"}, svg)
        .textContent = `${v} s`;
    }
    el("line", {class: "baseline", x1: plot.left, x2: plot.right,
                y1: y(0), y2: y(0)}, svg);
    for (const n of ticks(0, nMax, Math.min(nMax, 10))) {
      if (n % 1) continue;
      el("text", {x: x(n), y: plot.bottom + 18, "text-anchor": "middle"},
         svg).textContent = n;
    }
    el("text", {x: (plot.left + plot.right) / 2, y: H - 4,
                "text-anchor": "middle"}, svg).textContent = "Interval";
    for (const s of series) {
      el("polyline", {class: "series", stroke: color(s),
                      points: s.drift.map((d, n) => `${x(n)},${y(d)}`)
                        .join(" ")}, svg);
      const n = s.drift.length - 1;
      el("circle", {class: "dot", fill: color(s), r: 4, cx: x(n),
                    cy: y(s.drift[n])}, svg);
    }
    const legend = document.getElementById("drift-legend");
    for (const s of series) {
      const item = html("span", "", undefined, legend);
      html("span", "key", undefined, item).style.background = color(s);
      item.appendChild(document.createTextNode(s.name));
    }
    document.getElementById("drift-sub").textContent =
      `Seconds each sensor's events are behind a ${periodText} clock ` +
      "started at its first event, by interval. Flat is on time; a " +
      "rising line loses time every period.";
    document.getElementById("drift-desc").textContent =
      "Drift by interval: " + series.map((s) =>
        `${s.name} ${fmt(s.drift[s.drift.length - 1], 2)} after ` +
        `${s.drift.length - 1} intervals`).join("; ");
    hover(svg, document.getElementById("drift-tip"),
          document.getElementById("drift-card"), x, nMax,
          (n) => series.filter((s) => n < s.drift.length)
            .map((s) => [s, fmt(s.drift[n])]), plot);
  }

  // --- Interval small multiples ------------------------------------
  {
    document.getElementById("int-sub").textContent =
      "Each event's interval since the one before, on one scale. The " +
      `line marks the period, the band the allowed ` +
      `−${data.early} s to +${data.late} s.`;
    const lo = Math.min(...series.flatMap((s) =>
      [s.period - data.early, ...s.intervals]));
    const hi = Math.max(...series.flatMap((s) =>
      [s.period + data.late, ...s.intervals]));
    const yt = ticks(lo, hi, 4);
    const host = document.getElementById("multiples");
    for (const s of series) {
      const panel = html("div", "panel", undefined, host);
      html("h3", "", s.name, panel);
      const mean = s.intervals.reduce((a, b) => a + b, 0) /
                   s.intervals.length;
      html("p", "meta", `every ${s.period} s, mean ` +
           `${mean.toFixed(3)} s (${fmt(mean - s.period)})`, panel);
      const W = 320, H = 160;
      const plot = {left: 40, right: W - 10, top: 6, bottom: H - 22,
                    width: W};
      const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, role: "img",
                             "aria-label": `${s.name} intervals`}, panel);
      const nMax = Math.max(1, s.intervals.length);
      const x = scale(1, nMax, plot.left, plot.right);
      const y = scale(yt[0], yt[yt.length - 1], plot.bottom, plot.top);
      const grid = el("g", {class: "grid"}, svg);
      for (const v of yt) {
        el("line", {x1: plot.left, x2: plot.right, y1: y(v), y2: y(v)},
           grid);
        el("text", {x: plot.left - 6, y: y(v) + 4, "text-anchor": "end"},
           svg).textContent = `${v} s`;
      }
      el("rect", {class: "band", x: plot.left,
                  y: y(s.period + data.late),
                  width: plot.right - plot.left,
                  height: y(s.period - data.early) -
                          y(s.period + data.late)}, svg);
      el("line", {class: "ref", x1: plot.left, x2: plot.right,
                  y1: y(s.period), y2: y(s.period)}, svg);
      el("text", {x: plot.left, y: H - 4}, svg).textContent = "1";
      el("text", {x: plot.right, y: H - 4, "text-anchor": "end"}, svg)
        .textContent = nMax;
      el("polyline", {class: "series", stroke: color(s),
                      points: s.intervals.map((v, i) =>
                        `${x(i + 1)},${y(v)}`).join(" ")}, svg);
      s.intervals.forEach((v, i) => el("circle", {
        class: "dot", fill: color(s), r: 4, cx: x(i + 1), cy: y(v)}, svg));
      const tip = html("div", "tip", undefined, panel);
      const xi = (n) => x(n);
      xi.invert = (p) => x.invert(p);
      hover(svg, tip, panel, xi, nMax,
            (n) => n >= 1 ? [[s, `${s.intervals[n - 1].toFixed(3)} s`]]
                          : [], plot);
    }
  }

  // --- Excluded sensors ------------------------------------------------
  {
    const names = Object.keys(data.excluded);
    const note = document.getElementById("excluded");
    if (names.length) {
      note.hidden = false;
      html("strong", "", "Not in this run: ", note);
      note.appendChild(document.createTextNode(names.map((n) =>
        `${n} (${data.excluded[n]})`).join("; ")));
    }
  }

  // --- Table ---------------------------------------------------------
  {
    const table = document.getElementById("table");
    const head = html("tr", "", undefined, html("thead", "", undefined,
                                                 table));
    for (const h of ["Sensor", "Address", "Period", "Intervals", "Mean",
                     "Min", "Max", "Jitter (σ)", "Drift / interval",
                     "Drift"]) html("th", "", h, head);
    const body = html("tbody", "", undefined, table);
    for (const [name, why] of Object.entries(data.excluded)) {
      const tr = html("tr", "", undefined, body);
      html("td", "", name, tr);
      const td = html("td", "fail", `Excluded: ${why}`, tr);
      td.colSpan = 9;
    }
    for (const s of data.sensors) {
      const tr = html("tr", "", undefined, body);
      html("td", "", s.name, tr);
      html("td", "", "0x" + s.address.toString(16).padStart(2, "0"), tr);
      html("td", "", `${s.period} s`, tr);
      const m = s.summary;
      if (!m) {
        const td = html("td", "fail", s.failure || "no events", tr);
        td.colSpan = 7;
        continue;
      }
      for (const v of [m.intervals, `${m.mean.toFixed(3)} s`,
                       `${m.min.toFixed(3)} s`, `${m.max.toFixed(3)} s`,
                       `${m.jitter.toFixed(3)} s`,
                       fmt(m.drift_per_interval), fmt(m.drift)]) {
        html("td", "", String(v), tr);
      }
    }
  }
})();
</script>
</body>
</html>
"""


def write_report(path, title, subtitle, sensors, early, late, excluded=None):
    """Write the report to path; returns it. excluded is {name: why} for
    sensors left out of the run on purpose, listed in a note and the
    table."""
    data = {"early": early, "late": late, "excluded": excluded or {},
            "sensors": [dict(s, slot=s.get("slot", i) % len(SERIES))
                        for i, s in enumerate(sensors)]}
    # Inside <script>, only "</" can end the element early
    payload = json.dumps(data).replace("</", "<\\/")
    page = (_PAGE.replace("__SERIES_LIGHT__", _series_css("light"))
            .replace("__SERIES_DARK__", _series_css("dark"))
            .replace("__TITLE__", html.escape(title))
            .replace("__SUBTITLE__", html.escape(subtitle))
            .replace("__DATA__", payload))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page)
    return path
