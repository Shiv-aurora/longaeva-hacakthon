/*
 * LabCharts: formatters, the figure frame and every chart on the lab site.
 *
 * Nothing here fetches data or knows about a page. Each chart function takes
 * plain data (the shapes in docs/lab_contract.md) and returns a "spec":
 *
 *   { render(width, state) -> Element,      draws at a given pixel width
 *     legend:   [{label, key, color}],      optional
 *     controls: [{id, label, options, value}], optional; values arrive in state
 *     table:    {columns: [{label, value(row), num}], rows} }   the table view
 *
 * LabCharts.mount(host, frame, spec) wraps a spec in the standard figure:
 * title (the builder's headline), legend, plot, "how to read this", source,
 * download link and table view. Colors are CSS variables from tokens.css, so
 * a theme change never needs a redraw.
 *
 * Depends on the vendored globals d3 (7.9.0) and Plot (0.6.17).
 */
(function (global) {
  "use strict";

  var d3 = global.d3;
  var Plot = global.Plot;

  var C = {
    ink: "var(--chart-ink)",
    label: "var(--chart-label)",
    grid: "var(--chart-grid)",
    axis: "var(--chart-axis)",
    surface: "var(--chart-surface)",
    ours: "var(--chart-ours)",
    management: "var(--chart-management)",
    costs: "var(--chart-costs)",
    context: "var(--chart-context)",
    band: "var(--chart-band)",
    bandEdge: "var(--chart-band-edge)",
    ord: ["var(--chart-ord-1)", "var(--chart-ord-2)", "var(--chart-ord-3)"]
  };

  var MINUS = "−";
  var DASH = "–";
  var MISSING = "–";

  // ---------------------------------------------------------------- formats

  function isNum(v) {
    return typeof v === "number" && isFinite(v);
  }

  function fixed(v, digits) {
    var s = Math.abs(v).toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    });
    return (v < 0 && Number(Math.abs(v).toFixed(digits)) !== 0 ? MINUS : "") + s;
  }

  function parseDay(iso) {
    if (iso instanceof Date) return iso;
    if (typeof iso !== "string") return null;
    var d = new Date(iso.length <= 10 ? iso + "T00:00:00Z" : iso);
    return isNaN(d) ? null : d;
  }

  var dayShort = d3.utcFormat("%-d %b");
  var dayMedium = d3.utcFormat("%-d %b %Y");
  var dayLong = d3.utcFormat("%-d %B %Y");
  var monthYear = d3.utcFormat("%b %Y");
  var monthOnly = d3.utcFormat("%b");
  // Month ticks on two lines: the year sits under the first tick and under each January, so labels never collide.
  function monthTick(d, i) {
    return monthOnly(d) + (i === 0 || d.getUTCMonth() === 0 ? "\n" + d.getUTCFullYear() : "");
  }
  var stamp = d3.utcFormat("%-d %b %Y, %H:%M UTC");

  var fmt = {
    missing: MISSING,
    num: function (v, digits) {
      return isNum(v) ? fixed(v, digits == null ? 2 : digits) : MISSING;
    },
    int: function (v) {
      return isNum(v) ? fixed(v, 0) : MISSING;
    },
    usd: function (v, digits) {
      if (!isNum(v)) return MISSING;
      var body = fixed(Math.abs(v), digits == null ? 2 : digits);
      return (v < 0 && Number(body.replace(/,/g, "")) !== 0 ? MINUS : "") + "$" + body;
    },
    usdSigned: function (v, digits) {
      if (!isNum(v)) return MISSING;
      var body = fixed(Math.abs(v), digits == null ? 2 : digits);
      var zero = Number(body.replace(/,/g, "")) === 0;
      return (zero ? "" : v < 0 ? MINUS : "+") + "$" + body;
    },
    usdMillions: function (v) {
      return isNum(v) ? (v < 0 ? MINUS : "") + "$" + fixed(Math.abs(v), 0) + "m" : MISSING;
    },
    // Millions in, "$850m" or "$1.2bn" out.
    usdCompact: function (v) {
      if (!isNum(v)) return MISSING;
      var a = Math.abs(v);
      var body = a >= 1000 ? fixed(a / 1000, a >= 10000 ? 0 : 1) + "bn" : fixed(a, 0) + "m";
      return (v < 0 && a >= 0.5 ? MINUS : "") + "$" + body;
    },
    usdCompactSigned: function (v) {
      if (!isNum(v)) return MISSING;
      var body = fmt.usdCompact(Math.abs(v));
      return (Math.abs(v) < 0.5 ? "" : v < 0 ? MINUS : "+") + body;
    },
    pct: function (v, digits) {
      return isNum(v) ? fixed(v * 100, digits == null ? 0 : digits) + "%" : MISSING;
    },
    pctSigned: function (v, digits) {
      if (!isNum(v)) return MISSING;
      var body = fixed(Math.abs(v * 100), digits == null ? 1 : digits);
      var zero = Number(body) === 0;
      return (zero ? "" : v < 0 ? MINUS : "+") + body + "%";
    },
    range: function (lo, hi, f) {
      f = f || fmt.num;
      if (!isNum(lo) && !isNum(hi)) return MISSING;
      if (!isNum(hi) || lo === hi) return f(lo);
      if (!isNum(lo)) return f(hi);
      return f(lo) + DASH + f(hi);
    },
    pctRange: function (lo, hi) {
      if (isNum(lo) && isNum(hi) && lo !== hi) return fixed(lo * 100, 0) + DASH + fixed(hi * 100, 0) + "%";
      return fmt.pct(isNum(lo) ? lo : hi);
    },
    day: function (iso) {
      var d = parseDay(iso);
      return d ? dayMedium(d) : MISSING;
    },
    dayLong: function (iso) {
      var d = parseDay(iso);
      return d ? dayLong(d) : MISSING;
    },
    dayShort: function (iso) {
      var d = parseDay(iso);
      return d ? dayShort(d) : MISSING;
    },
    stamp: function (iso) {
      var d = parseDay(iso);
      return d ? stamp(d) : MISSING;
    },
    // "2026Q3" -> "Q3 2026"; anything else is returned unchanged.
    period: function (p) {
      var m = /^(\d{4})Q([1-4])$/.exec(String(p || ""));
      return m ? "Q" + m[2] + " " + m[1] : String(p == null ? MISSING : p);
    },
    periodShort: function (p) {
      var m = /^(\d{4})Q([1-4])$/.exec(String(p || ""));
      return m ? "Q" + m[2] + " \u2019" + m[1].slice(2) : String(p);
    },
    periodAxis: function (p) {
      var m = /^(\d{4})Q([1-4])$/.exec(String(p || ""));
      return m ? m[1] + " Q" + m[2] : String(p);
    },
    hashShort: function (h) {
      return typeof h === "string" && h.length > 20 ? h.slice(0, 12) : h || MISSING;
    }
  };

  // ------------------------------------------------------------ DOM helpers

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v == null || v === false) return;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "dataset") Object.assign(node.dataset, v);
        else if (k.slice(0, 2) === "on" && typeof v === "function") node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? "" : v);
      });
    }
    (children || []).forEach(function (c) {
      if (c == null || c === false) return;
      node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    });
    return node;
  }

  var SVGNS = "http://www.w3.org/2000/svg";

  function svgEl(tag, attrs) {
    var node = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) {
      node.setAttribute(k, attrs[k]);
    });
    return node;
  }

  // Legend keys mirror the mark they stand for.
  function keyMark(kind, color) {
    var wrap = el("span", { class: "key", "aria-hidden": "true" });
    var svg = svgEl("svg", { viewBox: "0 0 14 10", focusable: "false" });
    var add = function (tag, attrs) {
      svg.appendChild(svgEl(tag, attrs));
    };
    if (kind === "line") add("line", { x1: 0, y1: 5, x2: 14, y2: 5, stroke: color, "stroke-width": 2, "stroke-linecap": "round" });
    else if (kind === "thin") add("line", { x1: 0, y1: 5, x2: 14, y2: 5, stroke: color, "stroke-width": 1.25 });
    else if (kind === "band") add("rect", { x: 0, y: 1, width: 14, height: 8, fill: color, rx: 1 });
    else if (kind === "wash") add("rect", { x: 0, y: 1, width: 14, height: 8, fill: color, "fill-opacity": 0.2, rx: 1 });
    else if (kind === "dot") add("circle", { cx: 7, cy: 5, r: 4, fill: color });
    else if (kind === "ring") add("circle", { cx: 7, cy: 5, r: 3.25, fill: "none", stroke: color, "stroke-width": 1.5 });
    else if (kind === "square") add("rect", { x: 3.5, y: 1.5, width: 7, height: 7, fill: color });
    else if (kind === "diamond") add("path", { d: "M7 0.5 L11.5 5 L7 9.5 L2.5 5 Z", fill: color });
    else if (kind === "tick") add("line", { x1: 7, y1: 0, x2: 7, y2: 10, stroke: color, "stroke-width": 2.5 });
    else if (kind === "range") {
      add("line", { x1: 0, y1: 5, x2: 14, y2: 5, stroke: color, "stroke-width": 2 });
      add("circle", { cx: 7, cy: 5, r: 3.5, fill: color });
    }
    wrap.appendChild(svg);
    return wrap;
  }

  function legendList(items) {
    return el("ul", { class: "fig-legend" }, items.map(function (item) {
      return el("li", null, [keyMark(item.key, item.color), item.label]);
    }));
  }

  function table(columns, rows, caption) {
    var head = el("tr", null, columns.map(function (c) {
      var th = el("th", { scope: "col", class: c.num ? "num" : null }, [c.label]);
      if (c.note) th.appendChild(el("span", { class: "th-note", text: c.note }));
      return th;
    }));
    var body = rows.map(function (row, i) {
      var tr = el("tr", { class: columns.rowClass ? columns.rowClass(row) : null }, columns.map(function (c, j) {
        var value = c.value(row, i);
        var cell = el(j === 0 ? "th" : "td", { class: c.num ? "num" : null, scope: j === 0 ? "row" : null });
        if (value instanceof Node) cell.appendChild(value);
        else cell.textContent = value == null ? MISSING : String(value);
        return cell;
      }));
      return tr;
    });
    var t = el("table", { class: "data" }, [el("thead", null, [head]), el("tbody", null, body)]);
    if (caption) t.insertBefore(el("caption", { text: caption }), t.firstChild);
    return t;
  }

  // ----------------------------------------------------------------- states

  /**
   * state(host, kind, detail): the three non-data states every component has.
   * kind is "loading", "missing" (file not built yet) or "error".
   */
  function state(host, kind, detail) {
    detail = detail || {};
    var titles = { loading: "Loading", missing: "Not computed yet", error: "Could not load this" };
    var box = el("div", { class: "state is-" + kind + (detail.compact ? " is-compact" : ""), role: kind === "error" ? "alert" : "status" }, [
      el("b", { text: detail.title || titles[kind] })
    ]);
    if (kind === "loading") box.appendChild(el("span", { class: "loading-bar", "aria-hidden": "true" }));
    if (detail.text) box.appendChild(el("span", { text: detail.text }));
    if (detail.file) box.appendChild(el("code", { text: detail.file }));
    if (host) host.replaceChildren(box);
    return box;
  }

  // ----------------------------------------------------------- figure frame

  /**
   * mount(host, frame, spec) -> {redraw(), setSpec(spec), el}
   * frame: {kicker, title, headline (used as the title when given), subtitle, read, source, dataHref, dataLabel, headingLevel, extra}
   */
  function mount(host, frame, spec) {
    frame = frame || {};
    var values = {};
    (spec.controls || []).forEach(function (c) {
      values[c.id] = c.value;
    });

    host.classList.add("fig");
    host.replaceChildren();

    if (frame.kicker) host.appendChild(el("p", { class: "eyebrow fig-kicker", text: frame.kicker }));
    if (frame.headline || frame.title) host.appendChild(el(frame.headingLevel || "h3", { class: "fig-title", text: frame.headline || frame.title }));
    if (frame.subtitle) host.appendChild(el("p", { class: "fig-sub", text: frame.subtitle }));

    var plotBox = el("div", { class: "fig-plot" });

    if (spec.controls && spec.controls.length) {
      var controls = el("div", { class: "fig-controls" });
      spec.controls.forEach(function (c) {
        var name = "ctl-" + Math.random().toString(36).slice(2, 8);
        var group = el("div", { class: "segmented", role: "radiogroup", "aria-label": c.label });
        c.options.forEach(function (o) {
          var input = el("input", { type: "radio", name: name, value: o.value });
          input.checked = o.value === c.value;
          input.addEventListener("change", function () {
            values[c.id] = o.value;
            draw();
          });
          group.appendChild(el("label", null, [input, o.label]));
        });
        controls.appendChild(el("span", { class: "control-label", text: c.label }));
        controls.appendChild(group);
      });
      host.appendChild(controls);
    }

    if (spec.legend && spec.legend.length) host.appendChild(legendList(spec.legend));
    host.appendChild(plotBox);
    if (frame.extra) host.appendChild(frame.extra);

    var foot = el("div", { class: "fig-foot" });
    if (frame.read) {
      foot.appendChild(el("p", { class: "fig-read" }, [el("b", { text: "How to read this. " }), frame.read]));
    }
    var meta = el("div", { class: "fig-meta" });
    if (frame.source) meta.appendChild(el("span", { class: "fig-source" }, [el("b", { text: "Source: " }), frame.source]));
    var actions = el("span", { class: "fig-actions" });
    var tableBox = null;
    if (spec.table) {
      var toggle = el("button", { type: "button", class: "linklike", "aria-expanded": "false", text: "Show as a table" });
      tableBox = el("div", { class: "fig-table", hidden: true, tabindex: "0", role: "region", "aria-label": "Chart data as a table" });
      toggle.addEventListener("click", function () {
        var open = tableBox.hidden;
        if (open && !tableBox.firstChild) tableBox.appendChild(table(spec.table.columns, spec.table.rows));
        tableBox.hidden = !open;
        toggle.setAttribute("aria-expanded", String(open));
        toggle.textContent = open ? "Hide the table" : "Show as a table";
      });
      actions.appendChild(toggle);
    }
    if (frame.dataHref) {
      actions.appendChild(el("a", { href: frame.dataHref, download: true, text: frame.dataLabel || "Download the data (JSON)" }));
    }
    if (actions.firstChild) meta.appendChild(actions);
    if (meta.firstChild) foot.appendChild(meta);
    if (foot.firstChild) host.appendChild(foot);
    if (tableBox) host.appendChild(tableBox);

    var lastWidth = 0;
    function draw() {
      var width = Math.floor(plotBox.clientWidth);
      if (!width) return;
      lastWidth = width;
      try {
        plotBox.replaceChildren(spec.render(width, values));
      } catch (error) {
        state(plotBox, "error", { text: "This chart could not be drawn: " + (error && error.message ? error.message : error) });
        if (global.console) console.error(error);
      }
    }
    draw();

    if (global.ResizeObserver) {
      var pending = 0;
      new ResizeObserver(function () {
        if (Math.floor(plotBox.clientWidth) === lastWidth) return;
        cancelAnimationFrame(pending);
        pending = requestAnimationFrame(draw);
      }).observe(plotBox);
    }
    return { el: host, redraw: draw };
  }

  // ------------------------------------------------------------ plot basics

  function plot(options) {
    var base = { className: "lab-plot", style: { background: "transparent", overflow: "visible" } };
    return Plot.plot(Object.assign(base, options));
  }

  function gridY(options) {
    return Plot.gridY(Object.assign({ stroke: C.grid, strokeOpacity: 1 }, options));
  }

  function gridX(options) {
    return Plot.gridX(Object.assign({ stroke: C.grid, strokeOpacity: 1 }, options));
  }

  function axisY(options) {
    return Plot.axisY(Object.assign({ tickSize: 0, label: null, tickPadding: 8 }, options));
  }

  function axisX(options) {
    return Plot.axisX(Object.assign({ tickSize: 4, label: null, stroke: C.axis }, options));
  }

  // A text label with a halo in the surface colour so it survives crossing a line.
  function label(data, options) {
    return Plot.text(data, Object.assign({
      fill: C.ink, stroke: C.surface, strokeWidth: 4, fontSize: 12, fontFamily: "var(--font-sans)", lineAnchor: "middle"
    }, options));
  }

  function tip(data, pointer, options) {
    return Plot.tip(data, pointer(Object.assign({
      fill: C.surface, stroke: C.bandEdge, fontSize: 12, lineHeight: 1.35, textPadding: 10, pointerSize: 8, maxRadius: 48
    }, options)));
  }

  function padded(values, fraction, floorSpan) {
    var lo = d3.min(values);
    var hi = d3.max(values);
    var span = Math.max(hi - lo, floorSpan || 1e-9);
    return [lo - span * fraction, hi + span * fraction];
  }

  function dateTicks(width) {
    return width < 420 ? 3 : width < 720 ? 5 : 7;
  }

  // ------------------------------------------- fuel against the planning price

  /**
   * fuelAgainstPlan({daily: [[iso, usd]], assumed, since})
   * Daily spot jet fuel from a start date against the price management planned on.
   */
  function fuelAgainstPlan(data) {
    var rows = (data.daily || []).filter(function (d) {
      return isNum(d[1]) && (!data.since || d[0] >= data.since);
    }).map(function (d) {
      return { iso: d[0], date: parseDay(d[0]), value: d[1] };
    });
    var assumed = data.assumed;
    var last = rows[rows.length - 1];
    var legend = [{ label: "Jet fuel, daily spot price (US Gulf Coast)", key: "line", color: C.ink }];
    if (isNum(assumed)) legend.push({ label: "Price United planned on in its guidance", key: "line", color: C.management });
    return {
      legend: legend,
      table: {
        columns: [
          { label: "Date", value: function (r) { return fmt.day(r.iso); } },
          { label: "Spot price, $ per gallon", num: true, value: function (r) { return fmt.usd(r.value, 3); } },
          { label: "Against the assumed price", num: true, value: function (r) { return isNum(assumed) ? fmt.usdSigned(r.value - assumed, 3) : MISSING; } }
        ],
        rows: rows
      },
      render: function (width, values, height) {
        var narrow = width < 560;
        var values = rows.map(function (r) { return r.value; });
        if (isNum(assumed)) values.push(assumed);
        var domain = padded(values, 0.14, 0.2);
        var marks = [
          gridY({ ticks: 5 }),
          axisY({ ticks: 5, tickFormat: function (v) { return fmt.usd(v); } }),
          axisX({ ticks: dateTicks(width), tickFormat: function (d) { return dayShort(d); } })
        ];
        if (isNum(assumed)) {
          marks.push(Plot.ruleY([assumed], { stroke: C.management, strokeWidth: 2 }));
          marks.push(label([assumed], {
            y: function (d) { return d; }, frameAnchor: "left", dx: 6, dy: -11, textAnchor: "start",
            text: function (d) { return "Planned on " + fmt.usd(d); }
          }));
        }
        marks.push(Plot.lineY(rows, { x: "date", y: "value", stroke: C.ink, strokeWidth: 2, strokeLinejoin: "round", strokeLinecap: "round" }));
        if (last) {
          marks.push(Plot.dot([last], { x: "date", y: "value", r: 4.5, fill: C.ink, stroke: C.surface, strokeWidth: 2 }));
          marks.push(label([last], {
            x: "date", y: "value", dx: 10, textAnchor: "start", fontWeight: 500,
            text: function (d) { return fmt.usd(d.value) + (narrow ? "" : "\n" + dayShort(d.date)); }
          }));
        }
        marks.push(Plot.ruleX(rows, Plot.pointerX({ x: "date", stroke: C.axis })));
        marks.push(Plot.dot(rows, Plot.pointerX({ x: "date", y: "value", r: 4, fill: C.ink, stroke: C.surface, strokeWidth: 2 })));
        marks.push(tip(rows, Plot.pointerX, {
          x: "date", y: "value",
          title: function (d) {
            return fmt.usd(d.value, 3) + " per gallon\n" + dayMedium(d.date) +
              (isNum(assumed) ? "\n" + fmt.usdSigned(d.value - assumed, 3) + " against the assumed " + fmt.usd(assumed) : "");
          }
        }));
        return plot({
          width: width, height: height || (narrow ? 260 : 340),
          marginLeft: 48, marginRight: narrow ? 52 : 72, marginTop: 20, marginBottom: 30,
          x: { type: "utc", axis: null },
          y: { axis: null, domain: domain },
          marks: marks
        });
      }
    };
  }

  // -------------------------------------------------- 02 the track record

  var SPLITS = [
    ["test", "Locked test", "quarters the method never saw while it was being built"],
    ["select", "Selection period", "used to choose between versions of the method"],
    ["fit", "Fitting period", "used to estimate the method"]
  ];

  function trackRow(q, mode) {
    var calls = q.calls || {};
    var fitted = calls.fitted || {};
    var base = mode === "miss" ? q.actual_eps : 0;
    var shift = function (v) { return isNum(v) && isNum(base) ? v - base : null; };
    return {
      period: q.period, split: q.split, guided_on: q.guided_on, raw: q,
      lo: shift(q.guidance_low), hi: shift(q.guidance_high), mid: shift(calls.guidance_mid),
      costs: shift(calls.costs_only), management: shift(calls.management),
      p10: shift(fitted.p10), p50: shift(fitted.p50), p90: shift(fitted.p90),
      actual: shift(q.actual_eps)
    };
  }

  function trackTip(r, mode) {
    var q = r.raw;
    var calls = q.calls || {};
    var fitted = calls.fitted || {};
    var f = mode === "miss" ? function (v) { return fmt.usdSigned(isNum(v) && isNum(q.actual_eps) ? v - q.actual_eps : null); } : fmt.usd;
    var lines = [fmt.period(q.period) + (q.guided_on ? ", guided " + fmt.day(q.guided_on) : "")];
    lines.push("Reported: " + fmt.usd(q.actual_eps));
    lines.push("Guidance: " + fmt.range(q.guidance_low, q.guidance_high, f) + ", middle " + f(calls.guidance_mid));
    lines.push("No response: " + f(calls.costs_only));
    lines.push("As management says: " + f(calls.management));
    lines.push("As history shows: " + f(fitted.p50) + " (" + fmt.range(fitted.p10, fitted.p90, f) + ")");
    return lines.join("\n");
  }

  function trackMarks(rows, mode) {
    return [
      Plot.barX(rows, { y: "period", x1: "lo", x2: "hi", fill: C.band, insetTop: 7, insetBottom: 7, rx: 2 }),
      Plot.tickX(rows, { y: "period", x: "mid", stroke: C.context, strokeWidth: 1.5, insetTop: 7, insetBottom: 7 }),
      Plot.ruleY(rows, { y: "period", x1: "p10", x2: "p90", stroke: C.ours, strokeWidth: 2 }),
      Plot.dot(rows, { y: "period", x: "costs", symbol: "square", r: 4.5, fill: C.costs, stroke: C.surface, strokeWidth: 1.5 }),
      Plot.dot(rows, { y: "period", x: "management", symbol: "diamond", r: 4.5, fill: C.management, stroke: C.surface, strokeWidth: 1.5 }),
      Plot.dot(rows, { y: "period", x: "p50", r: 4.5, fill: C.ours, stroke: C.surface, strokeWidth: 1.5 }),
      Plot.tickX(rows, { y: "period", x: "actual", stroke: C.ink, strokeWidth: 2.5, insetTop: 3, insetBottom: 3 }),
      tip(rows, Plot.pointerY, { y: "period", x: mode === "miss" ? "p50" : "actual", title: function (r) { return trackTip(r, mode); } })
    ];
  }

  /**
   * trackRecord({quarters}, {collapse: ["select", "fit"]})
   * One row per past guided quarter: the guidance range, each rival's call and the result.
   * Also draws a single row (the Delta check) when given one quarter and {single: true}.
   */
  function trackRecord(data, options) {
    options = options || {};
    var quarters = (data.quarters || []).slice();
    var collapse = options.collapse || ["select", "fit"];
    var f = fmt.usd;
    var legend = [
      { label: "United's guidance range", key: "band", color: C.bandEdge },
      { label: "Middle of the guidance", key: "tick", color: C.context },
      { label: "No response (costs only)", key: "square", color: C.costs },
      { label: "As management says", key: "diamond", color: C.management },
      { label: "As history shows, with its 10–90% range", key: "range", color: C.ours },
      { label: "What was reported", key: "tick", color: C.ink }
    ];
    return {
      legend: legend,
      controls: options.single ? null : [{
        id: "mode", label: "Scale", value: "level",
        options: [{ value: "level", label: "Dollars per share" }, { value: "miss", label: "Miss against the result" }]
      }],
      table: {
        columns: [
          { label: "Quarter", value: function (q) { return fmt.period(q.period); } },
          { label: "Period", value: function (q) { return q.split || MISSING; } },
          { label: "Guidance", num: true, value: function (q) { return fmt.range(q.guidance_low, q.guidance_high, f); } },
          { label: "Guidance middle", num: true, value: function (q) { return f((q.calls || {}).guidance_mid); } },
          { label: "No response", num: true, value: function (q) { return f((q.calls || {}).costs_only); } },
          { label: "As management says", num: true, value: function (q) { return f((q.calls || {}).management); } },
          { label: "History, 10%", num: true, value: function (q) { return f(((q.calls || {}).fitted || {}).p10); } },
          { label: "History, middle", num: true, value: function (q) { return f(((q.calls || {}).fitted || {}).p50); } },
          { label: "History, 90%", num: true, value: function (q) { return f(((q.calls || {}).fitted || {}).p90); } },
          { label: "Reported", num: true, value: function (q) { return f(q.actual_eps); } }
        ],
        rows: quarters.slice().reverse()
      },
      render: function (width, values, height) {
        var mode = (values && values.mode) || "level";
        var all = quarters.map(function (q) { return trackRow(q, mode); }).filter(function (r) {
          return mode !== "miss" || isNum(r.actual);
        });
        var numbers = [];
        all.forEach(function (r) {
          ["lo", "hi", "mid", "costs", "management", "p10", "p50", "p90", "actual"].forEach(function (k) {
            if (isNum(r[k])) numbers.push(r[k]);
          });
        });
        if (!numbers.length) return state(null, "missing", { text: "No past quarters are in this file yet." });
        var domain = padded(numbers, 0.06, 0.5);
        var narrow = width < 520;
        var tickFormat = mode === "miss" ? function (v) { return fmt.usdSigned(v); } : function (v) { return fmt.usd(v); };

        if (options.only) all = all.filter(function (r) { return r.split === options.only; });
        if (options.only && !all.length) return state(null, "missing", { text: "No quarters in this period." });
        function draw(rows, showAxisLabel, room) {
          rows = rows.slice().sort(function (a, b) { return a.period < b.period ? 1 : -1; });
          var rowHeight = room ? Math.max(20, Math.min(44, Math.floor((room - 40) / rows.length))) : 30;
          var marks = [
            gridX({ ticks: narrow ? 4 : 8 }),
            Plot.axisX({ anchor: "top", ticks: narrow ? 4 : 8, tickSize: 0, tickFormat: tickFormat,
              label: showAxisLabel ? (mode === "miss" ? "Call minus the reported number, $ per share" : "Adjusted earnings per share") : null,
              labelAnchor: "left", labelArrow: "none" }),
            Plot.axisY({ tickSize: 0, label: null, tickFormat: fmt.periodAxis, tickPadding: 10 })
          ];
          if (mode === "miss") marks.push(Plot.ruleX([0], { stroke: C.axis }));
          return plot({
            width: width, height: rows.length * rowHeight + 40,
            marginLeft: 68, marginRight: 12, marginTop: 34, marginBottom: 6,
            x: { axis: null, domain: domain },
            y: { axis: null, type: "band", domain: rows.map(function (r) { return r.period; }), padding: 0 },
            marks: marks.concat(trackMarks(rows, mode))
          });
        }

        if (options.single || options.only) return draw(all, true, height);

        var box = el("div");
        var known = SPLITS.map(function (s) { return s[0]; });
        var groups = SPLITS.slice();
        all.forEach(function (r) {
          if (known.indexOf(r.split) < 0) {
            known.push(r.split);
            groups.push([r.split, String(r.split || "Other"), ""]);
          }
        });
        var first = true;
        groups.forEach(function (g) {
          var rows = all.filter(function (r) { return r.split === g[0]; });
          if (!rows.length) return;
          var head = el("div", { class: "group-head" }, [
            el("b", { text: g[1] }),
            el("span", { text: rows.length + (rows.length === 1 ? " quarter" : " quarters") + (g[2] ? ": " + g[2] : "") })
          ]);
          if (!first && collapse.indexOf(g[0]) >= 0) {
            var details = el("details", { class: "group" }, [el("summary", null, [head])]);
            var holder = el("div");
            details.appendChild(holder);
            details.addEventListener("toggle", function () {
              if (details.open && !holder.firstChild) holder.appendChild(draw(rows, true));
            });
            box.appendChild(details);
          } else {
            box.appendChild(head);
            box.appendChild(draw(rows, true));
          }
          first = false;
        });
        return box;
      }
    };
  }

  // ------------------------------------------------------- 02 cost clock

  /**
   * costClock({daily_fuel: [[iso, usd]], reported: [{period_end, usd_gal}]})
   * Spot jet fuel (daily) against the price United reported paying (quarter average).
   */
  function costClock(data) {
    var spot = (data.daily_fuel || []).filter(function (d) { return isNum(d[1]); }).map(function (d) {
      return { iso: d[0], date: parseDay(d[0]), value: d[1] };
    });
    var paid = (data.reported || []).filter(function (r) { return isNum(r.usd_gal); }).map(function (r) {
      var end = parseDay(r.period_end);
      var start = new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth() - 2, 1));
      return { start: start, end: end, mid: new Date((start.getTime() + end.getTime()) / 2), value: r.usd_gal, period_end: r.period_end };
    });
    if (spot.length && paid.length) {
      var from = spot[0].date;
      paid = paid.filter(function (p) { return p.end >= from; });
    }
    var lastSpot = spot[spot.length - 1];
    var lastPaid = paid[paid.length - 1];
    return {
      legend: [
        { label: "Spot price, daily", key: "thin", color: C.context },
        { label: "Price United reported paying, quarter average", key: "line", color: C.ink }
      ],
      table: {
        columns: [
          { label: "Quarter ending", value: function (r) { return fmt.day(r.period_end); } },
          { label: "Price paid, $ per gallon", num: true, value: function (r) { return fmt.usd(r.value); } },
          { label: "Average spot in the quarter", num: true, value: function (r) {
            var inside = spot.filter(function (s) { return s.date >= r.start && s.date <= r.end; });
            return inside.length ? fmt.usd(d3.mean(inside, function (s) { return s.value; })) : MISSING;
          } }
        ],
        rows: paid.slice().reverse()
      },
      render: function (width, values, height) {
        var values = spot.map(function (s) { return s.value; }).concat(paid.map(function (p) { return p.value; }));
        if (!values.length) return state(null, "missing", { text: "No fuel prices are in this file yet." });
        var marks = [
          gridY({ ticks: 4 }),
          axisY({ ticks: 4, tickFormat: function (v) { return fmt.usd(v); } }),
          axisX({ ticks: width < 420 ? 4 : 6, tickFormat: function (d) { return d3.utcFormat("%Y")(d); } }),
          Plot.lineY(spot, { x: "date", y: "value", stroke: C.context, strokeWidth: 1.25 }),
          Plot.link(paid, { x1: "start", x2: "end", y1: "value", y2: "value", stroke: C.ink, strokeWidth: 2.5, strokeLinecap: "butt" })
        ];
        if (lastSpot) marks.push(label([lastSpot], { x: "date", y: "value", text: ["Spot"], dx: 6, textAnchor: "start", fill: C.label }));
        if (lastPaid) marks.push(label([lastPaid], { x: "mid", y: "value", text: ["Paid"], dy: -11, dx: -4, textAnchor: "end", fontWeight: 500 }));
        marks.push(Plot.ruleX(spot, Plot.pointerX({ x: "date", stroke: C.axis })));
        marks.push(tip(spot, Plot.pointerX, {
          x: "date", y: "value",
          title: function (d) {
            var q = paid.filter(function (p) { return d.date >= p.start && d.date <= p.end; })[0];
            return fmt.usd(d.value) + " spot\n" + dayMedium(d.date) + (q ? "\n" + fmt.usd(q.value) + " paid on average that quarter" : "");
          }
        }));
        return plot({
          width: width, height: height || 250,
          marginLeft: 46, marginRight: 34, marginTop: 18, marginBottom: 28,
          x: { type: "utc", axis: null },
          y: { axis: null, domain: padded(values, 0.1, 0.2) },
          marks: marks
        });
      }
    };
  }

  // ------------------------------------------------------- 02 fare clock

  /**
   * recoveryCurve({curve: [{quarters_after, p10, p50, p90}], rivals: {costs_only, management, published}, peer})
   * The share of a fuel cost shock recovered through revenue, by quarter, against the rivals.
   *   rivals.management  [{quarters_after, low, high, period}] are drawn at their quarter with one label;
   *                      entries without `quarters_after` are drawn as levels across the chart.
   *   rivals.published   are not drawn (a mark with no name explains nothing); the spec returns them
   *                      as `published` for a list beside the chart.
   *   peer               optional {name, curve: [{quarters_after, value, lo, hi}]}: a second estimate, as a thin line.
   */
  function recoveryCurve(data) {
    var curve = (data.curve || []).filter(function (c) { return isNum(c.p50); });
    var rivals = data.rivals || {};
    var costs = (rivals.costs_only || []).map(function (v, i) {
      return { q: curve[i] ? curve[i].quarters_after : i, value: v };
    }).filter(function (c) { return isNum(c.value); });
    var promises = (rivals.management || []).filter(function (m) { return isNum(m.low) || isNum(m.high); }).map(function (m) {
      var low = isNum(m.low) ? m.low : m.high;
      var high = isNum(m.high) ? m.high : m.low;
      return { period: m.period, low: low, high: high, mid: (low + high) / 2, q: isNum(m.quarters_after) ? m.quarters_after : null };
    });
    var published = (rivals.published || []).filter(function (p) { return p && p.label; }).map(function (p, i) {
      return { n: i + 1, label: p.label, value: p.value, url: p.url };
    });
    var peer = data.peer && Array.isArray(data.peer.curve) ? data.peer.curve.filter(function (c) { return isNum(c.value); }) : [];
    var peerName = (data.peer && data.peer.name) || "Other airlines";
    var legend = [
      { label: "Measured from United's history, middle", key: "range", color: C.ours },
      { label: "Its 10–90% range", key: "wash", color: C.ours }
    ];
    if (promises.length) legend.push({ label: "What management has said", key: "diamond", color: C.management });
    if (peer.length) legend.push({ label: peerName + ", measured the same way", key: "thin", color: C.context });
    if (costs.length) legend.push({ label: "No response (costs only)", key: "line", color: C.costs });
    var xName = function (q) { return q === 0 ? "Same quarter" : q + (q === 1 ? " quarter later" : " quarters later"); };
    var at = function (list, q, key) { var hit = list.filter(function (r) { return r[key] === q; })[0]; return hit || null; };
    return {
      legend: legend,
      published: published,
      table: {
        columns: [
          { label: "Quarters after the shock", value: function (c) { return xName(c.quarters_after); } },
          { label: "10%", num: true, value: function (c) { return fmt.pct(c.p10); } },
          { label: "Middle", num: true, value: function (c) { return fmt.pct(c.p50); } },
          { label: "90%", num: true, value: function (c) { return fmt.pct(c.p90); } }
        ].concat(promises.some(function (p) { return p.q != null; }) ? [{ label: "Management", num: true, value: function (c) {
          var m = at(promises, c.quarters_after, "q");
          return m ? fmt.pctRange(m.low, m.high) : MISSING;
        } }] : []).concat(peer.length ? [{ label: peerName, num: true, value: function (c) {
          var r = at(peer, c.quarters_after, "quarters_after");
          return r ? fmt.pct(r.value) : MISSING;
        } }] : []),
        rows: curve
      },
      render: function (width, values, height) {
        if (!curve.length) return state(null, "missing", { text: "The recovery curve is not in this file yet." });
        var narrow = width < 460;
        var H = height || (narrow ? 300 : 360);
        var mTop = 18;
        var mBottom = 42;
        var qs = curve.map(function (c) { return c.quarters_after; });
        var qMin = d3.min(qs);
        var qMax = d3.max(qs);
        var x1 = qMin - 0.25;
        var x2 = qMax + 0.2;
        var ys = [0, 1];
        curve.forEach(function (c) { ys.push(c.p10, c.p90); });
        costs.forEach(function (c) { ys.push(c.value); });
        promises.forEach(function (p) { ys.push(p.low, p.high); });
        peer.forEach(function (r) { ys.push(r.value); });
        ys = ys.filter(isNum);
        var domain = padded(ys, 0.1, 0.5);
        var px = function (v) { return mTop + (domain[1] - v) / (domain[1] - domain[0]) * (H - mTop - mBottom); };
        var unpx = function (y) { return domain[1] - (y - mTop) / (H - mTop - mBottom) * (domain[1] - domain[0]); };
        var last = curve[curve.length - 1];

        var marks = [
          gridY({ ticks: 6, x1: x1, x2: x2 }),
          axisY({ ticks: 6, tickFormat: function (v) { return fmt.pct(v); } }),
          axisX({ ticks: qs, tickFormat: function (q) { return q === 0 ? "0" : "+" + q; },
            label: "Quarters after the fuel shock", labelAnchor: "left", labelArrow: "none", labelOffset: 34 }),
          Plot.ruleY([0], { x1: x1, x2: x2, stroke: C.axis })
        ];

        // Statements with no quarter attached: a level across the chart, named where it sits.
        promises.filter(function (p) { return p.q == null; }).forEach(function (p) {
          if (p.high > p.low) marks.push(Plot.rect([p], { x1: x1, x2: x2, y1: "low", y2: "high", fill: C.management, fillOpacity: 0.16 }));
          else marks.push(Plot.ruleY([p.low], { x1: x1, x2: x2, stroke: C.management, strokeWidth: 1.5 }));
          marks.push(label([p], { x: x1, y: "high", dx: 6, dy: -9, textAnchor: "start",
            text: function (d) { return "Management, " + fmt.period(d.period) + ": " + fmt.pctRange(d.low, d.high); } }));
        });

        marks.push(Plot.areaY(curve, { x: "quarters_after", y1: "p10", y2: "p90", fill: C.ours, fillOpacity: 0.12 }));
        if (costs.length) marks.push(Plot.lineY(costs, { x: "q", y: "value", stroke: C.costs, strokeWidth: 1.5 }));
        if (peer.length) marks.push(Plot.lineY(peer, { x: "quarters_after", y: "value", stroke: C.context, strokeWidth: 1.5 }));
        marks.push(Plot.lineY(curve, { x: "quarters_after", y: "p50", stroke: C.ours, strokeWidth: 2 }));
        marks.push(Plot.dot(curve, { x: "quarters_after", y: "p50", r: 4, fill: C.ours, stroke: C.surface, strokeWidth: 2 }));

        // Statements tied to a quarter: a range at that quarter, a diamond at its middle.
        var placed = promises.filter(function (p) { return p.q != null && p.q >= qMin && p.q <= qMax; });
        if (placed.length) {
          marks.push(Plot.ruleX(placed.filter(function (p) { return p.high > p.low; }), { x: "q", y1: "low", y2: "high", stroke: C.management, strokeWidth: 3 }));
          marks.push(Plot.dot(placed, { x: "q", y: "mid", symbol: "diamond", r: 5, fill: C.management, stroke: C.surface, strokeWidth: 1.5 }));
          // Named once, beside the last thing management said.
          marks.push(label([placed[placed.length - 1]], { x: "q", y: "high", dy: -13, text: ["Management"] }));
        }

        // End-of-line names on the right, nudged apart so none overlap.
        var ends = [{ y: px(last.p50), text: (data.name || "United") + " " + fmt.pct(last.p50), strong: true }];
        if (peer.length) ends.push({ y: px(peer[peer.length - 1].value), text: peerName + " " + fmt.pct(peer[peer.length - 1].value) });
        if (costs.length) ends.push({ y: px(costs[costs.length - 1].value), text: "No response" });
        spread(ends, 15, mTop + 6, H - mBottom - 4);
        marks.push(Plot.text(ends, { x: x2, y: function (d) { return unpx(d.at); }, dx: 8, textAnchor: "start", text: "text",
          fill: function (d) { return d.strong ? C.ink : C.label; }, fontFamily: "var(--font-sans)", fontSize: 12 }));

        marks.push(tip(curve, Plot.pointerX, {
          x: "quarters_after", y: "p50",
          title: function (d) {
            var said = at(placed, d.quarters_after, "q");
            var other = at(peer, d.quarters_after, "quarters_after");
            return xName(d.quarters_after) + "\nMeasured: " + fmt.pct(d.p50) + " recovered\n10–90% range: " + fmt.pctRange(d.p10, d.p90) +
              (said ? "\nManagement: " + fmt.pctRange(said.low, said.high) + (said.period ? " (" + fmt.period(said.period) + ")" : "") : "") +
              (other ? "\n" + peerName + ": " + fmt.pct(other.value) : "");
          }
        }));

        return plot({
          width: width, height: H,
          marginLeft: 44, marginRight: narrow ? 112 : 132, marginTop: mTop, marginBottom: mBottom,
          x: { axis: null, domain: [x1, x2] },
          y: { axis: null, domain: domain },
          marks: marks
        });
      }
    };
  }

  // ------------------------------------------------------ 02 travel demand

  /**
   * demandSignal({daily: [{date, travelers_7d, yoy_7d}]})
   * Two aligned strips: the seven-day average of airport checkpoint travelers,
   * and its change on the same week a year earlier.
   */
  function demandSignal(data) {
    var rows = (data.daily || []).map(function (d) {
      return { date: d.date, day: parseDay(d.date), travelers: d.travelers_7d, yoy: d.yoy_7d };
    }).filter(function (d) { return d.day; });
    var people = function (v) { return isNum(v) ? fixed(v / 1e6, 2) + "m" : MISSING; };
    return {
      table: {
        columns: [
          { label: "Date", value: function (r) { return fmt.day(r.date); } },
          { label: "Travelers a day, 7-day average", num: true, value: function (r) { return fmt.int(r.travelers); } },
          { label: "Against a year earlier", num: true, value: function (r) { return fmt.pctSigned(r.yoy, 1); } }
        ],
        rows: rows
      },
      render: function (width, values, height) {
        if (!rows.length) return state(null, "missing", { text: "The daily traveler counts are not in this file yet." });
        var last = rows[rows.length - 1];
        var xScale = { type: "utc", axis: null, domain: [rows[0].day, last.day] };
        var box = el("div");
        var room = height ? Math.max(180, height - 56) : 310;
        function strip(title, key, format, zero, showAxis, stripHeight) {
          var values = rows.map(function (r) { return r[key]; }).filter(isNum);
          if (!values.length) return;
          if (zero) values.push(0);
          var end = rows.filter(function (r) { return isNum(r[key]); }).pop();
          var m = [gridY({ ticks: 4 }), axisY({ ticks: 4, tickFormat: format })];
          if (zero) m.push(Plot.ruleY([0], { stroke: C.axis }));
          if (showAxis) m.push(axisX({ ticks: width < 420 ? 4 : 6, tickFormat: monthTick }));
          m.push(Plot.lineY(rows, { x: "day", y: key, stroke: C.ink, strokeWidth: 1.75, strokeLinejoin: "round" }));
          m.push(Plot.dot([end], { x: "day", y: key, r: 3.5, fill: C.ink, stroke: C.surface, strokeWidth: 2 }));
          m.push(label([end], { x: "day", y: key, dx: 9, textAnchor: "start", fontWeight: 500, text: function (d) { return format(d[key]); } }));
          m.push(Plot.ruleX(rows, Plot.pointerX({ x: "day", stroke: C.axis })));
          m.push(tip(rows, Plot.pointerX, { x: "day", y: key, title: function (r) {
            return dayMedium(r.day) + "\n" + people(r.travelers) + " travelers a day\n" + fmt.pctSigned(r.yoy, 1) + " against a year earlier";
          } }));
          box.appendChild(el("p", { class: "mini-title", text: title }));
          box.appendChild(plot({
            width: width, height: stripHeight, marginLeft: 50, marginRight: 56, marginTop: 8, marginBottom: showAxis ? 34 : 6,
            x: xScale, y: { axis: null, domain: padded(values, 0.14, 1e-6) }, marks: m
          }));
        }
        strip("Travelers a day, 7-day average", "travelers", people, false, false, Math.round(room * 0.52));
        strip("Against a year earlier", "yoy", function (v) { return fmt.pctSigned(v, 1); }, true, true, Math.round(room * 0.48));
        return box;
      }
    };
  }

  // ---------------------------------------------------- band-and-line charts

  /**
   * bandLine({rows, x, y, lo, hi, xLabel, xFormat, yFormat, xTicks, zeroX, zeroXLabel, name, bandName, tipX, height})
   * A middle line with a shaded band. Used by the market clock and the industry lags.
   */
  function bandLine(o) {
    var rows = (o.rows || []).filter(function (r) { return isNum(r[o.y]); });
    var yFormat = o.yFormat || fmt.num;
    var xFormat = o.xFormat || String;
    return {
      legend: [
        { label: o.name || "Average", key: "line", color: C.ours },
        { label: o.bandName || "Range of uncertainty", key: "wash", color: C.ours }
      ],
      table: {
        columns: [
          { label: o.xLabel || "x", num: true, value: function (r) { return xFormat(r[o.x]); } },
          { label: o.name || "Average", num: true, value: function (r) { return yFormat(r[o.y]); } },
          { label: "Low", num: true, value: function (r) { return yFormat(r[o.lo]); } },
          { label: "High", num: true, value: function (r) { return yFormat(r[o.hi]); } }
        ],
        rows: rows
      },
      render: function (width, values, height) {
        if (!rows.length) return state(null, "missing", { text: "This series is not in the file yet." });
        var ys = [0];
        rows.forEach(function (r) { ys.push(r[o.y], r[o.lo], r[o.hi]); });
        ys = ys.filter(isNum);
        var marks = [
          gridY({ ticks: 5 }),
          axisY({ ticks: 5, tickFormat: yFormat }),
          axisX({ ticks: o.xTicks || (width < 420 ? 5 : 8), tickFormat: xFormat, label: o.xLabel, labelAnchor: "left", labelArrow: "none", labelOffset: 34 }),
          Plot.ruleY([0], { stroke: C.axis })
        ];
        if (o.zeroX != null) {
          marks.push(Plot.ruleX([o.zeroX], { stroke: C.axis }));
          if (o.zeroXLabel) marks.push(label([o.zeroX], { x: function (d) { return d; }, frameAnchor: "top", dy: -9, text: [o.zeroXLabel], fill: C.label, fontSize: 11 }));
        }
        marks.push(Plot.areaY(rows, { x: o.x, y1: o.lo, y2: o.hi, fill: C.ours, fillOpacity: 0.14 }));
        marks.push(Plot.lineY(rows, { x: o.x, y: o.y, stroke: C.ours, strokeWidth: 2 }));
        marks.push(Plot.ruleX(rows, Plot.pointerX({ x: o.x, stroke: C.axis })));
        marks.push(Plot.dot(rows, Plot.pointerX({ x: o.x, y: o.y, r: 4, fill: C.ours, stroke: C.surface, strokeWidth: 2 })));
        marks.push(tip(rows, Plot.pointerX, {
          x: o.x, y: o.y,
          title: function (r) {
            return (o.tipX ? o.tipX(r[o.x]) : xFormat(r[o.x])) + "\n" + (o.name || "Average") + ": " + yFormat(r[o.y]) +
              "\nRange: " + yFormat(r[o.lo]) + " to " + yFormat(r[o.hi]);
          }
        }));
        return plot({
          width: width, height: height || o.height || 250,
          marginLeft: 50, marginRight: 12, marginTop: 22, marginBottom: 42,
          x: { axis: null },
          y: { axis: null, domain: padded(ys, 0.1, 0.004) },
          marks: marks
        });
      }
    };
  }

  /** eventPath({path: [{day, mean, lo, hi}]}): average stock path around large fuel moves. */
  function eventPath(data) {
    return bandLine({
      rows: data.path, x: "day", y: "mean", lo: "lo", hi: "hi",
      name: "Average move in the share price", bandName: "Range of uncertainty",
      xLabel: "Trading days from the fuel move", zeroX: 0, zeroXLabel: "Fuel move",
      xFormat: function (d) { return d > 0 ? "+" + d : String(d).replace("-", MINUS); },
      tipX: function (d) { return d === 0 ? "Day of the fuel move" : Math.abs(d) + (Math.abs(d) === 1 ? " trading day " : " trading days ") + (d > 0 ? "after" : "before"); },
      yFormat: function (v) { return fmt.pctSigned(v, 1); }
    });
  }

  /** industryLags({rows: [{lag_months, coef, lo, hi}]}): monthly industry fare response by lag. */
  function industryLags(data) {
    return bandLine({
      rows: data.rows, x: "lag_months", y: "coef", lo: "lo", hi: "hi",
      name: "Response of air fares", xLabel: "Months after the fuel move", height: 220,
      xFormat: function (d) { return String(d); },
      tipX: function (d) { return d + (d === 1 ? " month after" : " months after"); },
      yFormat: function (v) { return fmt.num(v, 2); }
    });
  }

  // -------------------------------------------------- 03 EPS distribution

  // Monotone cubic interpolation of the quantile points, read as a cumulative curve.
  function cumulative(points) {
    var xs = [];
    var ps = [];
    points.slice().sort(function (a, b) { return a[0] - b[0]; }).forEach(function (pt) {
      if (!isNum(pt[0]) || !isNum(pt[1])) return;
      if (xs.length && pt[1] <= xs[xs.length - 1]) return;
      ps.push(pt[0]);
      xs.push(pt[1]);
    });
    var n = xs.length;
    if (n < 2) return null;
    var h = [];
    var d = [];
    for (var i = 0; i < n - 1; i++) {
      h.push(xs[i + 1] - xs[i]);
      d.push((ps[i + 1] - ps[i]) / h[i]);
    }
    var m = [d[0]];
    for (var k = 1; k < n - 1; k++) {
      if (d[k - 1] * d[k] <= 0) m.push(0);
      else {
        var w1 = 2 * h[k] + h[k - 1];
        var w2 = h[k] + 2 * h[k - 1];
        m.push((w1 + w2) / (w1 / d[k - 1] + w2 / d[k]));
      }
    }
    m.push(d[n - 2]);
    function locate(x) {
      var j = d3.bisectRight(xs, x) - 1;
      return Math.max(0, Math.min(n - 2, j));
    }
    return {
      n: n, min: xs[0], max: xs[n - 1], pMin: ps[0], pMax: ps[n - 1],
      below: function (x) {
        if (x <= xs[0]) return ps[0];
        if (x >= xs[n - 1]) return ps[n - 1];
        var j = locate(x);
        var t = (x - xs[j]) / h[j];
        var t2 = t * t;
        var t3 = t2 * t;
        return (2 * t3 - 3 * t2 + 1) * ps[j] + (t3 - 2 * t2 + t) * h[j] * m[j] + (-2 * t3 + 3 * t2) * ps[j + 1] + (t3 - t2) * h[j] * m[j + 1];
      },
      density: function (x) {
        if (x < xs[0] || x > xs[n - 1]) return 0;
        var j = locate(x);
        var t = (x - xs[j]) / h[j];
        var t2 = t * t;
        return Math.max(0, (6 * t2 - 6 * t) / h[j] * ps[j] + (3 * t2 - 4 * t + 1) * m[j] + (-6 * t2 + 6 * t) / h[j] * ps[j + 1] + (3 * t2 - 2 * t) * m[j + 1]);
      }
    };
  }

  /**
   * epsDistribution({quantiles: [[p, eps]], guidance: [low, high], consensus, median})
   * The shape of the estimate, split into below / inside / above the guidance range.
   * Needs at least seven quantiles to draw a shape; with fewer it draws the points it has.
   */
  function epsDistribution(data) {
    var quantiles = (data.quantiles || []).filter(function (q) { return Array.isArray(q) && isNum(q[0]) && isNum(q[1]); });
    var cdf = cumulative(quantiles);
    var guidance = data.guidance || [];
    var lo = guidance[0];
    var hi = guidance[1];
    var hasRange = isNum(lo) && isNum(hi);
    var consensus = data.consensus;
    var median = isNum(data.median) ? data.median : null;
    var shape = cdf && cdf.n >= 7;
    var legend = shape ? [
      { label: "Below United's guidance range", key: "band", color: C.ord[0] },
      { label: "Inside the range", key: "band", color: C.ord[1] },
      { label: "Above the range", key: "band", color: C.ord[2] }
    ] : [
      { label: "Points of the estimate", key: "dot", color: C.ours },
      { label: "United's guidance range", key: "band", color: C.bandEdge }
    ];
    if (isNum(consensus)) legend.push({ label: "Analyst consensus", key: "tick", color: C.ink });
    return {
      legend: legend,
      shape: shape,
      table: {
        columns: [
          { label: "Chance of coming in below", num: true, value: function (q) { return fmt.pct(q[0]); } },
          { label: "Adjusted earnings per share", num: true, value: function (q) { return fmt.usd(q[1]); } }
        ],
        rows: quantiles.slice().sort(function (a, b) { return a[0] - b[0]; })
      },
      render: function (width, values, height) {
        if (!cdf) return state(null, "missing", { text: "The estimate's distribution is not in this file yet." });
        var narrow = width < 520;
        var xsAll = [cdf.min, cdf.max];
        if (hasRange) xsAll.push(lo, hi);
        if (isNum(consensus)) xsAll.push(consensus);
        var domain = padded(xsAll, 0.05, 0.2);
        var mid = (domain[0] + domain[1]) / 2;
        var marks = [];
        var top = 1;

        if (shape) {
          var N = 200;
          var xs = d3.range(N + 1).map(function (i) { return cdf.min + (cdf.max - cdf.min) * i / N; });
          if (hasRange) [lo, hi].forEach(function (b) { if (b > cdf.min && b < cdf.max) xs.push(b); });
          xs.sort(d3.ascending);
          var pts = xs.map(function (x) { return { x: x, y: cdf.density(x), below: cdf.below(x) }; });
          top = d3.max(pts, function (p) { return p.y; }) || 1;
          var regions = hasRange ? [
            pts.filter(function (p) { return p.x <= lo; }),
            pts.filter(function (p) { return p.x >= lo && p.x <= hi; }),
            pts.filter(function (p) { return p.x >= hi; })
          ] : [[], pts, []];
          regions.forEach(function (region, i) {
            if (region.length > 1) marks.push(Plot.areaY(region, { x: "x", y: "y", fill: C.ord[i], curve: "linear" }));
          });
          if (hasRange) marks.push(Plot.ruleX([lo, hi].filter(function (b) { return b > cdf.min && b < cdf.max; }), { y1: 0, y2: top * 1.02, stroke: C.surface, strokeWidth: 2 }));
          if (median != null) marks.push(Plot.ruleX([median], { y1: 0, y2: cdf.density(median), stroke: C.surface, strokeWidth: 1.5 }));
          marks.push(Plot.ruleX(pts, Plot.pointerX({ x: "x", y1: 0, y2: top * 1.05, stroke: C.ink, strokeWidth: 1 })));
          marks.push(tip(pts, Plot.pointerX, {
            x: "x", y: "y",
            title: function (p) {
              return fmt.usd(p.x) + " per share\n" + fmt.pct(p.below) + " chance of coming in below this\n" + fmt.pct(1 - p.below) + " chance of coming in above";
            }
          }));
        } else {
          var rows = quantiles.map(function (q) { return { p: q[0], x: q[1] }; });
          if (hasRange) marks.push(Plot.rect([0], { x1: lo, x2: hi, y1: 0, y2: top, fill: C.band }));
          marks.push(Plot.link([0], { x1: cdf.min, x2: cdf.max, y1: top * 0.45, y2: top * 0.45, stroke: C.ours, strokeWidth: 2 }));
          marks.push(Plot.dot(rows, { x: "x", y: top * 0.45, r: 5, fill: C.ours, stroke: C.surface, strokeWidth: 2 }));
          marks.push(label(rows, { x: "x", y: top * 0.45, dy: 16, text: function (r) { return fmt.pct(r.p) + "\n" + fmt.usd(r.x); }, lineAnchor: "top", fontSize: 11 }));
        }

        // The two vertical markers are labelled in rows above the shape; the guidance
        // range is bracketed below the axis, so no line ever crosses a label.
        var rowA = top * 1.3;
        var rowB = top * 1.12;
        if (isNum(consensus)) {
          marks.push(Plot.ruleX([consensus], { y1: 0, y2: rowA, stroke: C.ink, strokeWidth: 1.5 }));
          marks.push(label([0], { x: consensus, y: rowA, dx: consensus > mid ? -6 : 6, textAnchor: consensus > mid ? "end" : "start",
            text: ["Analyst consensus " + fmt.usd(consensus)], fontWeight: 500 }));
        }
        if (median != null) {
          marks.push(Plot.ruleX([median], { y1: top * (shape ? 1.0 : 0.55), y2: rowB, stroke: C.axis }));
          marks.push(label([0], { x: median, y: rowB, dx: median > mid ? -6 : 6, textAnchor: median > mid ? "end" : "start",
            text: ["Middle estimate " + fmt.usd(median)] }));
        }

        marks.push(Plot.ruleY([0], { stroke: C.axis }));
        marks.push(axisX({ ticks: narrow ? 5 : 9, tickFormat: function (v) { return fmt.usd(v); } }));
        if (hasRange) {
          marks.push(function (index, scales, values, dimensions) {
            var x1 = scales.x(lo);
            var x2 = scales.x(hi);
            var y = dimensions.height - dimensions.marginBottom + 30;
            var g = svgEl("g", { "aria-label": "guidance range" });
            g.appendChild(svgEl("path", { d: "M" + x1 + "," + (y - 5) + "V" + y + "H" + x2 + "V" + (y - 5), fill: "none", stroke: C.ink, "stroke-width": 1 }));
            var text = svgEl("text", { x: (x1 + x2) / 2, y: y + 15, "text-anchor": "middle", fill: C.ink, "font-family": "var(--font-sans)", "font-size": 12 });
            text.textContent = (narrow ? "Guidance " : "United's guidance range ") + fmt.range(lo, hi, fmt.usd);
            g.appendChild(text);
            return g;
          });
        }

        return plot({
          width: width, height: height || (narrow ? 290 : 350),
          marginLeft: 8, marginRight: 8, marginTop: 14, marginBottom: 58,
          x: { axis: null, domain: domain },
          y: { axis: null, domain: [0, top * 1.38] },
          marks: marks
        });
      }
    };
  }

  // ---------------------------------------------------- 03 daily estimate

  /**
   * dailyEstimate({daily: [{date, fuel, eps_p10, eps_p50, eps_p90, stock_excess_since_guidance, gap}],
   *                guidance: [low, high], consensus})
   * Three aligned strips on one time axis: the estimate with its band, fuel, and the stock.
   */
  function dailyEstimate(data, options) {
    options = options || {};
    var rows = (data.daily || []).map(function (d) {
      return Object.assign({}, d, { day: parseDay(d.date) });
    }).filter(function (d) { return d.day; });
    var guidance = data.guidance || [];
    var lo = guidance[0];
    var hi = guidance[1];
    var hasRange = isNum(lo) && isNum(hi);
    var consensus = data.consensus;
    var legend = [
      { label: "Middle estimate", key: "line", color: C.ours },
      { label: "Its 10–90% range", key: "wash", color: C.ours }
    ];
    if (hasRange) legend.push({ label: "United's guidance range", key: "band", color: C.bandEdge });
    if (isNum(consensus)) legend.push({ label: "Analyst consensus", key: "thin", color: C.ink });
    return {
      legend: legend,
      table: {
        columns: [
          { label: "Date", value: function (r) { return fmt.day(r.date); } },
          { label: "10%", num: true, value: function (r) { return fmt.usd(r.eps_p10); } },
          { label: "Middle", num: true, value: function (r) { return fmt.usd(r.eps_p50); } },
          { label: "90%", num: true, value: function (r) { return fmt.usd(r.eps_p90); } },
          { label: "Fuel, $ per gallon", num: true, value: function (r) { return fmt.usd(r.fuel, 3); } },
          { label: "Stock against the market", num: true, value: function (r) { return fmt.pctSigned(r.stock_excess_since_guidance, 1); } },
          { label: "Gap", num: true, value: function (r) { return fmt.num(r.gap, 3); } }
        ],
        rows: rows
      },
      render: function (width, values, height) {
        if (!rows.length) return state(null, "missing", { text: "The daily estimate is not in this file yet." });
        var narrow = width < 560;
        var left = 50;
        var right = narrow ? 52 : 64;
        var last = rows[rows.length - 1];
        var xScale = { type: "utc", axis: null, domain: [rows[0].day, last.day] };
        var box = el("div");

        // Strip 1: the estimate.
        var ys = [];
        rows.forEach(function (r) { ys.push(r.eps_p10, r.eps_p50, r.eps_p90); });
        if (hasRange) ys.push(lo, hi);
        if (isNum(consensus)) ys.push(consensus);
        ys = ys.filter(isNum);
        var marks = [gridY({ ticks: 5 }), axisY({ ticks: 5, tickFormat: function (v) { return fmt.usd(v); } })];
        if (hasRange) marks.push(Plot.rect([0], { x1: rows[0].day, x2: last.day, y1: lo, y2: hi, fill: C.band }));
        if (isNum(consensus)) marks.push(Plot.ruleY([consensus], { stroke: C.ink, strokeWidth: 1.25 }));
        marks.push(Plot.areaY(rows, { x: "day", y1: "eps_p10", y2: "eps_p90", fill: C.ours, fillOpacity: 0.16 }));
        marks.push(Plot.lineY(rows, { x: "day", y: "eps_p50", stroke: C.ours, strokeWidth: 2, strokeLinejoin: "round" }));
        marks.push(Plot.dot([last], { x: "day", y: "eps_p50", r: 4.5, fill: C.ours, stroke: C.surface, strokeWidth: 2 }));
        marks.push(label([last], { x: "day", y: "eps_p50", dx: 10, textAnchor: "start", fontWeight: 500, text: function (d) { return fmt.usd(d.eps_p50); } }));
        if (hasRange) marks.push(label([0], { x: rows[0].day, y: hi, dx: 6, dy: -9, textAnchor: "start", text: ["Top of United's guidance range"], fill: C.label }));
        if (hasRange) marks.push(label([0], { x: rows[0].day, y: lo, dx: 6, dy: 10, textAnchor: "start", text: ["Bottom of the range"], fill: C.label }));
        if (isNum(consensus)) marks.push(label([0], { x: last.day, y: consensus, dx: -6, dy: -9, textAnchor: "end", text: ["Analyst consensus " + fmt.usd(consensus)] }));
        marks.push(Plot.ruleX(rows, Plot.pointerX({ x: "day", stroke: C.axis })));
        marks.push(Plot.dot(rows, Plot.pointerX({ x: "day", y: "eps_p50", r: 4, fill: C.ours, stroke: C.surface, strokeWidth: 2 })));
        marks.push(tip(rows, Plot.pointerX, {
          x: "day", y: "eps_p50",
          title: function (r) {
            return dayMedium(r.day) + "\nMiddle estimate: " + fmt.usd(r.eps_p50) + "\n10–90% range: " + fmt.range(r.eps_p10, r.eps_p90, fmt.usd) +
              "\nJet fuel: " + fmt.usd(r.fuel) + " per gallon" +
              (isNum(r.stock_excess_since_guidance) ? "\nStock against the market: " + fmt.pctSigned(r.stock_excess_since_guidance, 1) : "");
          }
        }));
        if (!options.compact) box.appendChild(el("p", { class: "mini-title", text: "Estimate of adjusted earnings per share" }));
        else marks.push(axisX({ ticks: dateTicks(width), tickFormat: function (d) { return dayShort(d); } }));
        box.appendChild(plot({
          width: width, height: height ? Math.max(120, height - (options.compact ? 26 : 2 * 26 + 26 + 100 + 126)) : (narrow ? 240 : 300),
          marginLeft: left, marginRight: right, marginTop: 10, marginBottom: options.compact ? 28 : 8,
          x: xScale, y: { axis: null, domain: padded(ys, 0.08, 0.2) }, marks: marks
        }));

        function strip(title, key, format, color, zero, showAxis) {
          var values = rows.map(function (r) { return r[key]; }).filter(isNum);
          if (!values.length) return;
          if (zero) values.push(0);
          var m = [gridY({ ticks: 3 }), axisY({ ticks: 3, tickFormat: format })];
          if (zero) m.push(Plot.ruleY([0], { stroke: C.axis }));
          if (showAxis) m.push(axisX({ ticks: dateTicks(width), tickFormat: function (d) { return dayShort(d); } }));
          m.push(Plot.lineY(rows, { x: "day", y: key, stroke: color, strokeWidth: 1.75, strokeLinejoin: "round" }));
          m.push(Plot.dot([last], { x: "day", y: key, r: 3.5, fill: color, stroke: C.surface, strokeWidth: 2 }));
          m.push(label([last], { x: "day", y: key, dx: 9, textAnchor: "start", text: function (d) { return format(d[key]); } }));
          m.push(Plot.ruleX(rows, Plot.pointerX({ x: "day", stroke: C.axis })));
          m.push(tip(rows, Plot.pointerX, { x: "day", y: key, title: function (r) { return dayMedium(r.day) + "\n" + format(r[key]); } }));
          box.appendChild(el("p", { class: "mini-title", text: title }));
          box.appendChild(plot({
            width: width, height: showAxis ? 126 : 100, marginLeft: left, marginRight: right, marginTop: 8, marginBottom: showAxis ? 30 : 6,
            x: xScale, y: { axis: null, domain: padded(values, 0.18, 0.02) }, marks: m
          }));
        }
        if (options.compact) return box;
        strip("Jet fuel spot price, $ per gallon", "fuel", function (v) { return fmt.usd(v); }, C.ink, false, false);
        strip("United shares against the market since the guidance date", "stock_excess_since_guidance",
          function (v) { return fmt.pctSigned(v, 1); }, C.ink, true, true);
        return box;
      }
    };
  }

  // -------------------------------------------------------- 02 simulation

  var MODES = [
    { id: "costs_only", name: "No response", short: "No response", note: "Fares do not follow the fuel bill. Costs only.", color: C.costs },
    { id: "management", name: "As management says", short: "Management", note: "Fares recover what United has said they will.", color: C.management },
    { id: "measured", name: "As history shows", short: "History", note: "Fares recover at the speed measured from United's past.", color: C.ours }
  ];

  var METRICS = [
    { id: "operating_income_musd", label: "Operating income", format: function (v) { return fmt.usdCompact(v); } },
    { id: "operating_margin", label: "Margin", format: function (v) { return fmt.pct(v, 1); } },
    { id: "fcf_musd", label: "Free cash flow", format: function (v) { return fmt.usdCompact(v); } },
    { id: "eps_change", label: "EPS change", format: function (v) { return fmt.usdSigned(v); } }
  ];

  function modeQuarters(modes, id) {
    var mode = modes && modes[id];
    var quarters = mode && Array.isArray(mode.quarters) ? mode.quarters : [];
    return quarters;
  }

  /**
   * scenarioMultiples({modes: {costs_only, management, measured}}, {metrics})
   * Small multiples on one shared scale: the outcome over the horizon under each
   * response mode, middle line with its 10-90% band. The other modes' middle lines
   * are repeated in gray so the only difference on screen is the response.
   * A mode without quarters draws a "not computed yet" panel; the others still render.
   */
  function scenarioMultiples(data, options) {
    options = options || {};
    var modes = data.modes || {};
    var metrics = options.metrics || METRICS;
    var rowsFor = function (id, metric) {
      return modeQuarters(modes, id).map(function (q) {
        var v = q[metric] || {};
        return { period: q.period, p10: v.p10, p50: v.p50, p90: v.p90 };
      }).filter(function (r) { return isNum(r.p50); });
    };
    var tableRows = [];
    MODES.forEach(function (m) {
      modeQuarters(modes, m.id).forEach(function (q) { tableRows.push({ mode: m, q: q }); });
    });
    var columns = [
      { label: "Behavior", value: function (r) { return r.mode.name; } },
      { label: "Quarter", value: function (r) { return fmt.period(r.q.period); } }
    ];
    metrics.forEach(function (metric) {
      ["p10", "p50", "p90"].forEach(function (k) {
        columns.push({ label: metric.label + ", " + (k === "p50" ? "middle" : k === "p10" ? "10%" : "90%"), num: true,
          value: function (r) { return metric.format((r.q[metric.id] || {})[k]); } });
      });
    });
    return {
      controls: [{ id: "metric", label: "Outcome", value: metrics[0].id,
        options: metrics.map(function (m) { return { value: m.id, label: m.label }; }) }],
      legend: [
        { label: "Middle outcome, by quarter", key: "range", color: C.ink },
        { label: "10–90% range", key: "wash", color: C.ink },
        { label: "The other two behaviors, for comparison", key: "thin", color: C.context }
      ],
      table: { columns: columns, rows: tableRows },
      render: function (width, values, height) {
        var metric = metrics.filter(function (m) { return m.id === (values && values.metric); })[0] || metrics[0];
        var series = {};
        var ys = [];
        var periods = [];
        MODES.forEach(function (m) {
          series[m.id] = rowsFor(m.id, metric.id);
          series[m.id].forEach(function (r) {
            ys.push(r.p10, r.p50, r.p90);
            if (periods.indexOf(r.period) < 0) periods.push(r.period);
          });
        });
        ys = ys.filter(isNum);
        periods.sort();
        var columnsCount = width >= 720 ? 3 : 1;
        var gap = 24;
        var panelWidth = Math.floor((width - gap * (columnsCount - 1)) / columnsCount);
        var domain = ys.length ? padded(ys.concat(d3.min(ys) > 0 ? [] : [0]), 0.1, 1e-6) : [0, 1];
        var box = el("div", { class: "multiples" });
        box.style.display = "grid";
        box.style.gridTemplateColumns = "repeat(" + columnsCount + ", minmax(0, 1fr))";
        box.style.gap = (columnsCount === 1 ? 28 : gap) + "px";

        MODES.forEach(function (m) {
          var rows = series[m.id];
          var panel = el("div", { class: "multiple" }, [
            el("p", { class: "multiple-title" }, [keyMark("line", m.color), el("b", { text: m.name })]),
            el("p", { class: "multiple-note", text: m.note })
          ]);
          if (!rows.length) {
            panel.appendChild(state(null, "missing", { text: "This behavior has not been built on this server yet. The other two are unaffected." }));
            box.appendChild(panel);
            return;
          }
          var last = rows[rows.length - 1];
          var marks = [
            gridY({ ticks: 5 }),
            axisY({ ticks: 5, tickFormat: metric.format }),
            Plot.axisX({ tickSize: 0, label: null, tickPadding: 8, tickFormat: function (p, i) {
              var m = /^(\d{4})Q([1-4])$/.exec(String(p));
              return m ? "Q" + m[2] + (i === 0 || m[2] === "1" ? "\n" + m[1] : "") : String(p);
            } })
          ];
          if (domain[0] < 0) marks.push(Plot.ruleY([0], { stroke: C.axis }));
          MODES.forEach(function (other) {
            if (other.id !== m.id && series[other.id].length) {
              marks.push(Plot.lineY(series[other.id], { x: "period", y: "p50", stroke: C.context, strokeWidth: 1.25, strokeOpacity: 0.7 }));
            }
          });
          marks.push(Plot.areaY(rows, { x: "period", y1: "p10", y2: "p90", fill: m.color, fillOpacity: 0.16 }));
          marks.push(Plot.lineY(rows, { x: "period", y: "p50", stroke: m.color, strokeWidth: 2 }));
          marks.push(Plot.dot(rows, { x: "period", y: "p50", r: 4, fill: m.color, stroke: C.surface, strokeWidth: 2 }));
          marks.push(label([last], { x: "period", y: "p50", dx: 9, textAnchor: "start", fontWeight: 500, text: function (d) { return metric.format(d.p50); } }));
          marks.push(Plot.ruleX(rows, Plot.pointerX({ x: "period", stroke: C.axis })));
          marks.push(tip(rows, Plot.pointerX, {
            x: "period", y: "p50",
            title: function (r) {
              return fmt.period(r.period) + ", " + m.name.toLowerCase() + "\n" + metric.label + ": " + metric.format(r.p50) +
                "\n10–90% range: " + metric.format(r.p10) + " to " + metric.format(r.p90);
            }
          }));
          panel.appendChild(plot({
            width: panelWidth, height: columnsCount === 1 ? 204 : 262,
            marginLeft: 54, marginRight: 58, marginTop: 10, marginBottom: 38,
            x: { axis: null, type: "point", domain: periods, padding: 0.3 },
            y: { axis: null, domain: domain },
            marks: marks
          }));
          box.appendChild(panel);
        });
        return box;
      }
    };
  }

  /**
   * bridge({steps: [{step, value}]})
   * A per-share waterfall. Each step starts where the last one ended; a step named
   * "net" (or "total") is drawn from zero as the result.
   */
  function bridge(data) {
    var running = 0;
    var skipped = (data.steps || []).filter(function (s) { return !isNum(s.value); }).map(function (s) { return String(s.step); });
    var rows = (data.steps || []).filter(function (s) { return isNum(s.value); }).map(function (s) {
      var total = /^(net|total)\b/i.test(String(s.step));
      var start = total ? 0 : running;
      var end = total ? s.value : running + s.value;
      if (!total) running = end;
      var name = String(s.step);
      return { step: name.charAt(0).toUpperCase() + name.slice(1), value: s.value, start: start, end: end, total: total };
    });
    return {
      skipped: skipped,
      table: {
        columns: [
          { label: "Step", value: function (r) { return r.step; } },
          { label: "$ per share", num: true, value: function (r) { return fmt.usdSigned(r.value); } },
          { label: "Running total", num: true, value: function (r) { return fmt.usdSigned(r.end); } }
        ],
        rows: rows
      },
      render: function (width, values, height) {
        if (!rows.length) return state(null, "missing", { compact: true, text: "The per-share bridge is not in this response." });
        var xs = [0];
        rows.forEach(function (r) { xs.push(r.start, r.end); });
        var links = rows.slice(0, -1).map(function (r, i) { return { x: r.end, y1: r.step, y2: rows[i + 1].step }; })
          .filter(function (l, i) { return !rows[i + 1].total; });
        var marks = [
          gridX({ ticks: width < 420 ? 4 : 6 }),
          Plot.axisX({ anchor: "top", ticks: width < 420 ? 4 : 6, tickSize: 0, label: null, tickFormat: function (v) { return fmt.usdSigned(v); } }),
          Plot.axisY({ tickSize: 0, label: null, tickPadding: 10, fontFamily: "var(--font-sans)", fontSize: 12.5 }),
          Plot.ruleX([0], { stroke: C.axis }),
          Plot.link(links, { x1: "x", x2: "x", y1: "y1", y2: "y2", stroke: C.axis }),
          Plot.barX(rows, { y: "step", x1: "start", x2: "end", insetTop: height && height < 200 ? 5 : 12, insetBottom: height && height < 200 ? 5 : 12, rx: 2,
            fill: function (r) { return r.total ? C.ink : C.context; } }),
          label(rows, { y: "step", frameAnchor: "right", dx: 62, textAnchor: "end", stroke: null,
            fontWeight: function (r) { return r.total ? 500 : 400; },
            text: function (r) { return fmt.usdSigned(r.value); } }),
          tip(rows, Plot.pointerY, { y: "step", x: function (r) { return (r.start + r.end) / 2; },
            title: function (r) { return r.step + ": " + fmt.usdSigned(r.value) + " per share" + (r.total ? "" : "\nRunning total: " + fmt.usdSigned(r.end)); } })
        ];
        return plot({
          width: width, height: rows.length * (height ? Math.max(15, Math.min(38, Math.floor((height - 30) / rows.length))) : 38) + 30,
          marginLeft: 104, marginRight: 66, marginTop: 26, marginBottom: 4,
          x: { axis: null, domain: padded(xs, 0.04, 0.2) },
          y: { axis: null, type: "band", domain: rows.map(function (r) { return r.step; }), padding: 0 },
          marks: marks
        });
      }
    };
  }

  // ------------------------------------------------------------ app tiles

  /** segmented(control, values, onChange): a radio group for {id, label, options, value}. */
  function segmented(control, values, onChange) {
    var name = "ctl-" + Math.random().toString(36).slice(2, 8);
    var group = el("div", { class: "segmented", role: "radiogroup", "aria-label": control.label });
    control.options.forEach(function (o) {
      var input = el("input", { type: "radio", name: name, value: o.value });
      input.checked = o.value === values[control.id];
      input.addEventListener("change", function () {
        values[control.id] = o.value;
        onChange(control.id, o.value);
      });
      group.appendChild(el("label", null, [input, o.label]));
    });
    return group;
  }

  var drawerNode = null;

  /**
   * openDrawer(label, nodes): the side drawer used for sources and for panel details.
   * The dialog is created on first use, so any lab page gets it without markup.
   */
  function openDrawer(label, nodes) {
    if (!drawerNode) {
      var close = el("button", { type: "button", class: "icon-btn", "aria-label": "Close" });
      close.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9"/></svg>';
      drawerNode = el("dialog", { class: "drawer", "aria-labelledby": "drawer-label" }, [
        el("div", { class: "drawer-head" }, [el("p", { class: "eyebrow", id: "drawer-label" }), close]),
        el("div", { class: "drawer-body" })
      ]);
      close.addEventListener("click", function () { drawerNode.close(); });
      drawerNode.addEventListener("click", function (event) {
        if (event.target === drawerNode) drawerNode.close();
      });
      document.body.appendChild(drawerNode);
    }
    drawerNode.querySelector(".eyebrow").textContent = label || "Details";
    var body = drawerNode.querySelector(".drawer-body");
    body.replaceChildren.apply(body, (nodes || []).filter(Boolean));
    if (!drawerNode.open) {
      if (typeof drawerNode.showModal === "function") drawerNode.showModal();
      else drawerNode.setAttribute("open", "");
    }
    body.scrollTop = 0;
    return body;
  }

  function fits() {
    return !!global.matchMedia && global.matchMedia("(min-width: 1100px) and (min-height: 700px)").matches;
  }

  /**
   * panel(host, frame, spec) -> {redraw(), values, el}
   * The app tile: one title (the builder's headline, one line), small tools, the chart.
   * Everything explanatory sits behind the "i" button, in the drawer.
   * frame: {title (a short name), headline (the builder's full sentence, shown in the drawer),
   *         key (one line with the key number: a string or a node), kicker, read, source,
   *         details() -> [nodes], dataHref, dataLabel,
   *         tools: [nodes], legend: false to hide, scroll: true to let the plot scroll, onChange(id, value),
   *         height: fixed plot height in px, bare: true to leave the host unstyled, info: false for no "i"}
   * In the desktop app the chart is drawn at the tile's own height; elsewhere at its default height.
   */
  function panel(host, frame, spec) {
    frame = frame || {};
    var values = {};
    (spec.controls || []).forEach(function (c) { values[c.id] = c.value; });
    if (!frame.bare) host.classList.add("tile");
    host.replaceChildren();

    var plotBox = el("div", { class: "tile-plot" + (frame.scroll ? " is-scroll" : "") });
    if (frame.height) plotBox.style.height = frame.height + "px";
    var head = el("header", { class: "tile-head" });
    if (frame.title) head.appendChild(el(frame.headingLevel || "h2", { class: "tile-title", text: frame.title, title: frame.title }));
    var tools = el("div", { class: "tile-tools" });
    (spec.controls || []).forEach(function (c) {
      tools.appendChild(segmented(c, values, function (id, value) {
        draw();
        if (frame.onChange) frame.onChange(id, value);
      }));
    });
    (frame.tools || []).forEach(function (t) { tools.appendChild(t); });

    function details() {
      var nodes = [];
      if (frame.headline || frame.title) nodes.push(el("h3", { text: frame.headline || frame.title }));
      if (spec.legend && spec.legend.length) nodes.push(legendList(spec.legend));
      if (frame.read) nodes.push(el("div", null, [el("p", { class: "drawer-label", text: "How to read it" }), el("p", { class: "drawer-text", text: frame.read })]));
      (frame.details ? frame.details() : []).forEach(function (n) { if (n) nodes.push(n); });
      if (frame.source) nodes.push(el("div", null, [el("p", { class: "drawer-label", text: "Source" }), el("p", { class: "drawer-text", text: frame.source })]));
      var actions = el("p", { class: "drawer-actions" });
      if (frame.dataHref) actions.appendChild(el("a", { href: frame.dataHref, download: true, text: frame.dataLabel || "Download the data (JSON)" }));
      if (actions.firstChild) nodes.push(actions);
      if (spec.table && spec.table.rows && spec.table.rows.length) {
        nodes.push(el("div", null, [el("p", { class: "drawer-label", text: "The numbers" }),
          el("div", { class: "fig-table", tabindex: "0", role: "region", "aria-label": "Chart data as a table" }, [table(spec.table.columns, spec.table.rows)])]));
      }
      return nodes;
    }

    if (frame.info !== false) {
      var info = el("button", { type: "button", class: "info-btn", "aria-label": "About this panel: how to read it, sources and data", title: "How to read it, sources, data", text: "i" });
      info.addEventListener("click", function () { openDrawer(frame.kicker || "About this panel", details()); });
      tools.appendChild(info);
    }
    if (tools.firstChild) head.appendChild(tools);
    if (head.firstChild) host.appendChild(head);
    if (frame.key) host.appendChild(typeof frame.key === "string" ? el("p", { class: "tile-key", text: frame.key }) : frame.key);
    if (frame.legend !== false && spec.legend && spec.legend.length) {
      var legend = legendList(spec.legend);
      legend.classList.add("tile-legend");
      host.appendChild(legend);
    }
    host.appendChild(plotBox);
    if (frame.foot) host.appendChild(frame.foot);

    var last = "";
    function draw() {
      var width = Math.floor(plotBox.clientWidth);
      if (!width) return;
      var height = frame.height || (fits() ? Math.floor(plotBox.clientHeight) : 0);
      last = width + "x" + (frame.height ? 0 : height);
      try {
        plotBox.replaceChildren(spec.render(width, values, height > 60 ? height : undefined));
      } catch (error) {
        state(plotBox, "error", { compact: true, text: "This chart could not be drawn: " + (error && error.message ? error.message : error) });
        if (global.console) console.error(error);
      }
    }
    draw();
    if (global.ResizeObserver) {
      var pending = 0;
      new ResizeObserver(function () {
        var now = Math.floor(plotBox.clientWidth) + "x" + (fits() ? Math.floor(plotBox.clientHeight) : 0);
        if (now === last) return;
        cancelAnimationFrame(pending);
        pending = requestAnimationFrame(draw);
      }).observe(plotBox);
    }
    return { el: host, redraw: draw, values: values, details: details };
  }

  // Push labels apart vertically so none overlap; positions are pixels.
  function spread(items, gap, lo, hi) {
    var sorted = items.slice().sort(function (a, b) { return a.y - b.y; });
    sorted.forEach(function (item, i) {
      item.at = i ? Math.max(item.y, sorted[i - 1].at + gap) : Math.max(item.y, lo);
    });
    for (var i = sorted.length - 1; i >= 0; i--) {
      var limit = i === sorted.length - 1 ? hi : sorted[i + 1].at - gap;
      if (sorted[i].at > limit) sorted[i].at = limit;
    }
    return items;
  }

  function quarterTick(p, i) {
    var m = /^(\d{4})Q([1-4])$/.exec(String(p));
    return m ? "Q" + m[2] + (i === 0 || m[2] === "1" ? "\n" + m[1] : "") : String(p);
  }

  /**
   * scenarioFan({modes}, options): the outcome over the horizon, three behaviors on one scale.
   * options (read at draw time, so the caller can change them and redraw):
   *   metric   one of LabCharts.metrics ids
   *   visible  {costs_only, management, measured: boolean}
   *   focus    the behavior whose 10-90% band is drawn (default: measured, else management, else costs only)
   * A behavior without quarters is left out; `missing` on the spec lists those.
   */
  function scenarioFan(data, options) {
    options = options || {};
    var modes = data.modes || {};
    var metrics = options.metrics || METRICS;
    var rowsFor = function (id, metric) {
      return modeQuarters(modes, id).map(function (q) {
        var v = q[metric] || {};
        var ref = (q.no_shock || {})[metric] || {};
        return { period: q.period, p10: v.p10, p50: v.p50, p90: v.p90, reference: ref.p50 };
      }).filter(function (r) { return isNum(r.p50); });
    };
    var tableRows = [];
    MODES.forEach(function (m) {
      modeQuarters(modes, m.id).forEach(function (q) { tableRows.push({ mode: m, q: q }); });
    });
    var columns = [
      { label: "Behavior", value: function (r) { return r.mode.name; } },
      { label: "Quarter", value: function (r) { return fmt.period(r.q.period); } }
    ];
    metrics.forEach(function (metric) {
      ["p10", "p50", "p90"].forEach(function (k) {
        columns.push({ label: metric.label + ", " + (k === "p50" ? "middle" : k === "p10" ? "10%" : "90%"), num: true,
          value: function (r) { return metric.format((r.q[metric.id] || {})[k]); } });
      });
    });
    return {
      missing: MODES.filter(function (m) { return !modeQuarters(modes, m.id).length; }),
      legend: MODES.map(function (m) { return { label: m.name + ": " + m.note, key: "range", color: m.color }; })
        .concat([{ label: "10–90% of simulated paths, for the behavior in focus", key: "wash", color: C.ours }, { label: "No shock, middle path", key: "thin", color: C.context }]),
      table: { columns: columns, rows: tableRows },
      render: function (width, values, height) {
        var metric = metrics.filter(function (m) { return m.id === options.metric; })[0] || metrics[0];
        var shown = MODES.filter(function (m) { return !options.visible || options.visible[m.id] !== false; });
        var series = {};
        var ys = [];
        var periods = [];
        var reference = null;
        shown.forEach(function (m) {
          series[m.id] = rowsFor(m.id, metric.id);
          series[m.id].forEach(function (r) {
            ys.push(r.p10, r.p50, r.p90, r.reference);
            if (periods.indexOf(r.period) < 0) periods.push(r.period);
          });
          if (!reference && series[m.id].some(function (r) { return isNum(r.reference); })) reference = series[m.id];
        });
        ys = ys.filter(isNum);
        periods.sort();
        if (!ys.length) return state(null, "missing", { compact: true, text: "Nothing to draw: no behavior is switched on, or none has been computed." });
        var H = height || (width < 560 ? 280 : 360);
        var narrow = width < 560;
        var mTop = 14;
        var mBottom = 40;
        var mRight = narrow ? 104 : 132;
        var domain = padded(ys.concat(d3.min(ys) > 0 ? [] : [0]), 0.08, 1e-6);
        var px = function (v) { return mTop + (domain[1] - v) / (domain[1] - domain[0]) * (H - mTop - mBottom); };
        var marks = [
          gridY({ ticks: 5 }),
          axisY({ ticks: 5, tickFormat: metric.format }),
          Plot.axisX({ tickSize: 0, label: null, tickPadding: 8, tickFormat: quarterTick })
        ];
        if (domain[0] < 0) marks.push(Plot.ruleY([0], { stroke: C.axis }));
        if (reference) marks.push(Plot.lineY(reference, { x: "period", y: "reference", stroke: C.context, strokeWidth: 1.25 }));
        // One soft band, for the behavior in focus; three overlapping bands read as mud.
        var focus = shown.filter(function (m) { return m.id === options.focus && series[m.id].length; })[0] ||
          ["measured", "management", "costs_only"].map(function (id) { return shown.filter(function (m) { return m.id === id && series[m.id].length; })[0]; }).filter(Boolean)[0];
        if (focus) marks.push(Plot.areaY(series[focus.id], { x: "period", y1: "p10", y2: "p90", fill: focus.color, fillOpacity: 0.12 }));
        var ends = [];
        shown.forEach(function (m) {
          var rows = series[m.id];
          if (!rows.length) return;
          marks.push(Plot.lineY(rows, { x: "period", y: "p50", stroke: m.color, strokeWidth: 2 }));
          marks.push(Plot.dot(rows, { x: "period", y: "p50", r: 3.5, fill: m.color, stroke: C.surface, strokeWidth: 2 }));
          var end = rows[rows.length - 1];
          ends.push({ period: end.period, value: end.p50, y: px(end.p50), text: m.short + " " + metric.format(end.p50), bold: true });
        });
        if (reference) {
          var refEnd = reference[reference.length - 1];
          ends.push({ period: refEnd.period, value: refEnd.reference, y: px(refEnd.reference), text: "No shock " + metric.format(refEnd.reference), bold: false });
        }
        spread(ends, 15, mTop + 6, H - mBottom - 4);
        // Plot takes a constant dy only, so the nudged position goes back into data units.
        var unpx = function (at) { return domain[1] - (at - mTop) / (H - mTop - mBottom) * (domain[1] - domain[0]); };
        marks.push(Plot.text(ends, { x: "period", y: function (d) { return unpx(d.at); }, dx: 10, textAnchor: "start",
          text: "text", fill: function (d) { return d.bold ? C.ink : C.label; },
          fontFamily: "var(--font-sans)", fontSize: 12 }));
        var tipRows = periods.map(function (p) {
          var lines = [fmt.period(p) + ", " + metric.label.toLowerCase()];
          var y = null;
          shown.forEach(function (m) {
            var r = series[m.id].filter(function (x) { return x.period === p; })[0];
            if (!r) return;
            if (y == null) y = r.p50;
            lines.push(m.name + ": " + metric.format(r.p50) + " (" + metric.format(r.p10) + " to " + metric.format(r.p90) + ")");
          });
          var ref = reference && reference.filter(function (x) { return x.period === p; })[0];
          if (ref && isNum(ref.reference)) lines.push("No shock: " + metric.format(ref.reference));
          return { period: p, y: y, title: lines.join("\n") };
        }).filter(function (r) { return r.y != null; });
        marks.push(Plot.ruleX(tipRows, Plot.pointerX({ x: "period", stroke: C.axis })));
        marks.push(tip(tipRows, Plot.pointerX, { x: "period", y: "y", title: "title" }));
        return plot({
          width: width, height: H, marginLeft: 56, marginRight: mRight, marginTop: mTop, marginBottom: mBottom,
          x: { axis: null, type: "point", domain: periods, padding: 0.25 },
          y: { axis: null, domain: domain },
          marks: marks
        });
      }
    };
  }

  /**
   * gridHeat({cells: [{x, y, prob, p50}], boundary: [{x, y}]}, {xLabel, yLabel, xFormat, yFormat, marker: {x, y}, share})
   * Two drivers against each other. Each cell is one tested point, shaded by the share of
   * simulated runs on the stated side of the threshold; the line is the boundary.
   */
  // The boundary arrives as a set of crossing points. Walk it nearest point to nearest
  // point (in cell units) and start a new run where the next point is more than a cell away.
  function chain(points, xs, ys) {
    if (points.length < 2) return [];
    var sx = (xs[xs.length - 1] - xs[0]) / Math.max(1, xs.length - 1) || 1;
    var sy = (ys[ys.length - 1] - ys[0]) / Math.max(1, ys.length - 1) || 1;
    var distance = function (a, b) { return Math.hypot((a.x - b.x) / sx, (a.y - b.y) / sy); };
    var left = points.slice().sort(function (a, b) { return a.x - b.x || a.y - b.y; });
    // Start from an end of the line: the point whose nearest neighbours all lie to one side is hard to
    // know in general, so start at the point farthest from the centre of the set.
    var cx = d3.mean(left, function (p) { return p.x; });
    var cy = d3.mean(left, function (p) { return p.y; });
    var start = d3.greatestIndex(left, function (p) { return distance(p, { x: cx, y: cy }); });
    var current = left.splice(start, 1)[0];
    var run = 0;
    var out = [{ x: current.x, y: current.y, run: run }];
    while (left.length) {
      var next = d3.leastIndex(left, function (p) { return distance(p, current); });
      if (distance(left[next], current) > 1.6) run += 1;
      current = left.splice(next, 1)[0];
      out.push({ x: current.x, y: current.y, run: run });
    }
    return out;
  }

  function gridHeat(data, options) {
    options = options || {};
    var cells = (data.cells || []).filter(function (c) { return isNum(c.x) && isNum(c.y) && isNum(c.prob); });
    var boundary = (data.boundary || []).filter(function (b) { return isNum(b.x) && isNum(b.y); });
    var xFormat = options.xFormat || String;
    var yFormat = options.yFormat || String;
    var share = options.share || "Share of runs";
    function edges(values) {
      var out = [];
      values.forEach(function (v, i) {
        var before = i ? (v + values[i - 1]) / 2 : values.length > 1 ? v - (values[1] - v) / 2 : v - 0.5;
        var after = i < values.length - 1 ? (v + values[i + 1]) / 2 : values.length > 1 ? v + (v - values[i - 1]) / 2 : v + 0.5;
        out.push([before, after]);
      });
      return out;
    }
    return {
      legend: [0.1, 0.5, 0.9].map(function (p) {
        return { label: fmt.pct(p) + " of runs", key: "band", color: "color-mix(in srgb, var(--chart-ours) " + Math.round(p * 100) + "%, var(--chart-surface))" };
      }).concat(boundary.length ? [{ label: "Boundary: half of runs", key: "line", color: C.ink }] : [])
        .concat(options.marker ? [{ label: "The scenario on the left", key: "ring", color: C.ink }] : []),
      table: {
        columns: [
          { label: options.xLabel || "x", num: true, value: function (c) { return xFormat(c.x); } },
          { label: options.yLabel || "y", num: true, value: function (c) { return yFormat(c.y); } },
          { label: share, num: true, value: function (c) { return fmt.pct(c.prob); } },
          { label: "Middle outcome", num: true, value: function (c) { return options.valueFormat ? options.valueFormat(c.p50) : fmt.num(c.p50, 1); } }
        ],
        rows: cells
      },
      render: function (width, values, height) {
        if (!cells.length) return state(null, "missing", { compact: true, text: "No tested points came back." });
        var xs = Array.from(new Set(cells.map(function (c) { return c.x; }))).sort(d3.ascending);
        var ys = Array.from(new Set(cells.map(function (c) { return c.y; }))).sort(d3.ascending);
        var xe = edges(xs);
        var ye = edges(ys);
        var rects = cells.map(function (c) {
          var i = xs.indexOf(c.x);
          var j = ys.indexOf(c.y);
          return { x: c.x, y: c.y, prob: c.prob, p50: c.p50, x1: xe[i][0], x2: xe[i][1], y1: ye[j][0], y2: ye[j][1] };
        });
        var H = height || (width < 560 ? 300 : 380);
        var cellWidth = (width - 70) / xs.length;
        var marks = [
          Plot.rect(rects, { x1: "x1", x2: "x2", y1: "y1", y2: "y2", inset: 1, rx: 2,
            fill: function (d) { return "color-mix(in srgb, var(--chart-ours) " + Math.round(Math.max(0, Math.min(1, d.prob)) * 100) + "%, var(--chart-surface))"; },
            stroke: C.grid, strokeWidth: 1 })
        ];
        if (cellWidth >= 34) {
          marks.push(Plot.text(rects, { x: "x", y: "y", text: function (d) { return fmt.pct(d.prob); },
            fill: function (d) { return d.prob > 0.55 ? "var(--chart-on-fill)" : C.ink; }, fontSize: 11, fontFamily: "var(--font-mono)" }));
        }
        var runs = chain(boundary, xs, ys);
        if (runs.length > 1) {
          marks.push(Plot.line(runs, { x: "x", y: "y", z: "run", stroke: C.surface, strokeWidth: 4.5 }));
          marks.push(Plot.line(runs, { x: "x", y: "y", z: "run", stroke: C.ink, strokeWidth: 1.5 }));
        }
        var m = options.marker;
        if (m && isNum(m.x) && isNum(m.y) && m.x >= xe[0][0] && m.x <= xe[xe.length - 1][1] && m.y >= ye[0][0] && m.y <= ye[ye.length - 1][1]) {
          marks.push(Plot.dot([m], { x: "x", y: "y", r: 7, fill: C.surface, stroke: C.ink, strokeWidth: 2 }));
          marks.push(Plot.dot([m], { x: "x", y: "y", r: 2.5, fill: C.ink }));
        }
        marks.push(Plot.axisX({ ticks: xs, tickSize: 0, tickPadding: 8, tickFormat: xFormat, label: options.xLabel, labelAnchor: "center", labelArrow: "none", labelOffset: 36 }));
        marks.push(Plot.axisY({ ticks: ys, tickSize: 0, tickPadding: 8, tickFormat: yFormat, label: options.yLabel, labelAnchor: "top", labelArrow: "none" }));
        marks.push(tip(rects, Plot.pointer, { x: "x", y: "y", title: function (d) {
          return (options.xLabel || "x") + " " + xFormat(d.x) + ", " + (options.yLabel || "y").toLowerCase() + " " + yFormat(d.y) +
            "\n" + share + ": " + fmt.pct(d.prob) + (isNum(d.p50) ? "\nMiddle outcome: " + (options.valueFormat ? options.valueFormat(d.p50) : fmt.num(d.p50, 1)) : "");
        } }));
        return plot({
          width: width, height: H, marginLeft: 58, marginRight: 12, marginTop: 24, marginBottom: 46,
          x: { axis: null, domain: [xe[0][0], xe[xe.length - 1][1]] },
          y: { axis: null, domain: [ye[0][0], ye[ye.length - 1][1]] },
          marks: marks
        });
      }
    };
  }

  /**
   * dailyFares({daily: [{date, fuel, index_ua, index_dl, index_aa, index_all}]})
   * Daily fare indexes for United and its peers above the fuel price, on one date axis.
   */
  function dailyFares(data) {
    var rows = (data.daily || []).map(function (d) { return Object.assign({}, d, { day: parseDay(d.date) }); }).filter(function (d) { return d.day; });
    var lines = [
      { key: "index_all", name: "All airlines", color: C.context, width: 1.25 },
      { key: "index_dl", name: "Delta", color: C.context, width: 1.25 },
      { key: "index_aa", name: "American", color: C.context, width: 1.25 },
      { key: "index_ua", name: "United", color: C.ink, width: 2 }
    ].filter(function (l) { return rows.some(function (r) { return isNum(r[l.key]); }); });
    return {
      legend: [{ label: "United", key: "line", color: C.ink }, { label: "Delta, American and all airlines", key: "thin", color: C.context }],
      table: {
        columns: [{ label: "Date", value: function (r) { return fmt.day(r.date); } }].concat(lines.slice().reverse().map(function (l) {
          return { label: l.name + ", fare index", num: true, value: function (r) { return fmt.num(r[l.key], 1); } };
        })).concat([{ label: "Jet fuel, $ per gallon", num: true, value: function (r) { return fmt.usd(r.fuel); } }]),
        rows: rows
      },
      render: function (width, values, height) {
        if (!rows.length || !lines.length) return state(null, "missing", { compact: true, text: "The daily fare series is not in this file yet." });
        var room = height ? Math.max(170, height - 52) : 300;
        var xScale = { type: "utc", axis: null, domain: [rows[0].day, rows[rows.length - 1].day] };
        var box = el("div");
        var ys = [];
        rows.forEach(function (r) { lines.forEach(function (l) { if (isNum(r[l.key])) ys.push(r[l.key]); }); });
        var top = Math.round(room * 0.62);
        var domain = padded(ys, 0.08, 1e-6);
        var px = function (v) { return 8 + (domain[1] - v) / (domain[1] - domain[0]) * (top - 14); };
        var marks = [gridY({ ticks: 4 }), axisY({ ticks: 4, tickFormat: function (v) { return fmt.num(v, 0); } })];
        var ends = [];
        lines.forEach(function (l) {
          var series = rows.filter(function (r) { return isNum(r[l.key]); });
          marks.push(Plot.lineY(series, { x: "day", y: l.key, stroke: l.color, strokeWidth: l.width, strokeLinejoin: "round" }));
          var end = series[series.length - 1];
          ends.push({ day: end.day, value: end[l.key], y: px(end[l.key]), text: l.name, bold: l.key === "index_ua" });
        });
        spread(ends, 13, 10, top - 8);
        var unpx = function (at) { return domain[1] - (at - 8) / (top - 14) * (domain[1] - domain[0]); };
        marks.push(Plot.text(ends, { x: "day", y: function (d) { return unpx(d.at); }, dx: 8, textAnchor: "start", text: "text",
          fill: function (d) { return d.bold ? C.ink : C.label; }, fontFamily: "var(--font-sans)", fontSize: 11.5 }));
        marks.push(Plot.ruleX(rows, Plot.pointerX({ x: "day", stroke: C.axis })));
        marks.push(tip(rows, Plot.pointerX, { x: "day", y: lines[lines.length - 1].key, title: function (r) {
          return dayMedium(r.day) + "\n" + lines.slice().reverse().map(function (l) { return l.name + ": " + fmt.num(r[l.key], 1); }).join("\n") +
            (isNum(r.fuel) ? "\nJet fuel: " + fmt.usd(r.fuel) : "");
        } }));
        box.appendChild(el("p", { class: "mini-title", text: "Fare index" }));
        box.appendChild(plot({ width: width, height: top, marginLeft: 44, marginRight: 86, marginTop: 8, marginBottom: 6, x: xScale, y: { axis: null, domain: domain }, marks: marks }));
        var fuel = rows.filter(function (r) { return isNum(r.fuel); });
        if (fuel.length) {
          box.appendChild(el("p", { class: "mini-title", text: "Jet fuel, $ a gallon" }));
          box.appendChild(plot({
            width: width, height: room - top, marginLeft: 44, marginRight: 86, marginTop: 8, marginBottom: 34, x: xScale,
            y: { axis: null, domain: padded(fuel.map(function (r) { return r.fuel; }), 0.12, 1e-6) },
            marks: [gridY({ ticks: 3 }), axisY({ ticks: 3, tickFormat: function (v) { return fmt.usd(v); } }),
              axisX({ ticks: width < 420 ? 3 : 5, tickFormat: monthTick }),
              Plot.lineY(fuel, { x: "day", y: "fuel", stroke: C.ink, strokeWidth: 1.5 })]
          }));
        }
        return box;
      }
    };
  }

  // ------------------------------------------------------- HTML components

  /**
   * rangeStrip({domain: [min, max], p10, p50, p90, management})
   * A one-line range: P10 to P90 as a bar, P50 as a dot, management's number as a tick or band.
   */
  function rangeStrip(o) {
    var d0 = o.domain[0];
    var d1 = o.domain[1];
    var pos = function (v) { return Math.max(0, Math.min(100, (v - d0) / (d1 - d0) * 100)) + "%"; };
    var width = function (a, b) { return Math.max(0, Math.min(100, (b - a) / (d1 - d0) * 100)) + "%"; };
    var strip = el("div", { class: "strip", "aria-hidden": "true" }, [el("span", { class: "strip-track" })]);
    var m = o.management;
    if (Array.isArray(m) && isNum(m[0]) && isNum(m[1])) {
      var band = el("span", { class: "strip-band" });
      band.style.left = pos(m[0]);
      band.style.width = width(m[0], m[1]);
      strip.appendChild(band);
    }
    if (isNum(o.p10) && isNum(o.p90)) {
      var range = el("span", { class: "strip-range" + (o.ghost ? " is-ghost" : "") });
      range.style.left = pos(o.p10);
      range.style.width = width(o.p10, o.p90);
      strip.appendChild(range);
    }
    if (isNum(m)) {
      var mark = el("span", { class: "strip-mark" });
      mark.style.left = pos(m);
      strip.appendChild(mark);
    }
    if (isNum(o.p50)) {
      var dot = el("span", { class: "strip-dot" });
      dot.style.left = pos(o.p50);
      strip.appendChild(dot);
    }
    return strip;
  }

  /** oddsBar([{label, value, note}]): three ordered shares of one whole, with their numbers. */
  function oddsBar(parts) {
    var total = d3.sum(parts, function (p) { return isNum(p.value) ? p.value : 0; }) || 1;
    var bar = el("div", { class: "odds-bar", role: "img", "aria-label": parts.map(function (p) { return p.label + ": " + fmt.pct(p.value); }).join(", ") });
    parts.forEach(function (p, i) {
      var seg = el("span");
      seg.style.flex = (isNum(p.value) ? p.value / total : 0) + " 0 0";
      seg.style.background = C.ord[i];
      bar.appendChild(seg);
    });
    var list = el("dl", { class: "odds-list" }, parts.map(function (p, i) {
      var swatch = el("i");
      swatch.style.background = C.ord[i];
      var dd = el("dd", null, [fmt.pct(p.value)]);
      if (p.note) dd.appendChild(el("small", { text: p.note }));
      return el("div", null, [el("dt", null, [swatch, p.label]), dd]);
    }));
    return el("div", null, [bar, list]);
  }

  /** oddsLine([{label, value}]): the same three shares as one line of numbers, for a short tile. */
  function oddsLine(parts) {
    return el("p", { class: "inline-stats" }, parts.map(function (p, i) {
      var swatch = el("i", { class: "dot" });
      swatch.style.background = C.ord[i];
      return el("span", null, [swatch, p.label + " ", el("b", { text: fmt.pct(p.value) })]);
    }));
  }

  /** meter({label, value, note, ours, color}): one probability on a 0 to 100% track. */
  function meter(o) {
    var fill = el("i");
    fill.style.width = (isNum(o.value) ? Math.max(0, Math.min(1, o.value)) * 100 : 0) + "%";
    if (o.color) fill.style.background = o.color;
    return el("div", { class: "meter" + (o.ours ? " is-ours" : "") }, [
      el("div", { class: "meter-head" }, [el("span", { text: o.label }), el("b", { text: fmt.pct(o.value) })]),
      el("div", { class: "meter-track", role: "img", "aria-label": o.label + ": " + fmt.pct(o.value) }, [fill]),
      o.note ? el("p", { class: "meter-note" }, Array.isArray(o.note) ? o.note : [o.note]) : null
    ]);
  }

  global.LabCharts = {
    colors: C,
    fmt: fmt,
    el: el,
    isNum: isNum,
    parseDay: parseDay,
    keyMark: keyMark,
    legendList: legendList,
    table: table,
    state: state,
    mount: mount,
    plot: plot,
    // chart specs
    fuelAgainstPlan: fuelAgainstPlan,
    trackRecord: trackRecord,
    costClock: costClock,
    recoveryCurve: recoveryCurve,
    demandSignal: demandSignal,
    bandLine: bandLine,
    eventPath: eventPath,
    industryLags: industryLags,
    epsDistribution: epsDistribution,
    dailyEstimate: dailyEstimate,
    scenarioFan: scenarioFan,
    scenarioMultiples: scenarioMultiples,
    gridHeat: gridHeat,
    dailyFares: dailyFares,
    panel: panel,
    openDrawer: openDrawer,
    segmented: segmented,
    metrics: METRICS,
    bridge: bridge,
    modes: MODES,
    // HTML components
    rangeStrip: rangeStrip,
    oddsBar: oddsBar,
    oddsLine: oddsLine,
    meter: meter
  };
})(window);
