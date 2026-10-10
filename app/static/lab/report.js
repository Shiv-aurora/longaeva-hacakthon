/*
 * Report: fills the technical report (/report) from the built JSON files.
 *
 * No figure is typed into the prose. Each one is an empty element bound to a
 * JSON field under /static/data/ and filled here when the page loads:
 *
 *   <span data-bind="fare_clock:smooth.through_latest.one_year_share.estimate" data-format="pct0"></span>
 *
 * Bind grammar: FILE:SEGMENT(.SEGMENT)*
 *   key            an object key, or an index into a list (-1 is the last item)
 *   key[f=v]       the first item of the list `key` whose field f equals v;
 *                  conditions can be chained: scores[model=fitted][split=test]
 *   @count         the length of a list (or the number of keys of an object)
 * Two binds separated by a comma give a pair, for the range formats.
 * data-scale multiplies a number before it is formatted (a slope per unit of
 * fuel becomes a move per 10% with data-scale="0.1").
 *
 * If the file or the field is missing the element reads "not yet computed" in a
 * muted style. A block with data-needs="a b" is hidden until every named file
 * is built, and the line marked data-pending-for with the same names is shown
 * in its place.
 *
 * Charts come from LabCharts (charts.js); the one chart it lacks, the range by
 * airline, is drawn here with the same vendored Observable Plot and colors.
 *
 * The section list beside the text is HookSidebar (hook-sidebar.js). This file
 * tells it which section is being read, and on narrow screens folds it into a
 * bar that opens.
 *
 * The binder (parseBind, resolvePath, evaluate) uses no DOM, so the tests run
 * it under node against the real files.
 */
(function (global) {
  "use strict";

  var MISSING = "not yet computed";
  var MINUS = "−";
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

  // ------------------------------------------------------------------ paths

  var BIND = /^([a-z][a-z0-9_]*):(\S+)$/;
  var SEGMENT = /^([A-Za-z0-9_-]+)?((?:\[[A-Za-z0-9_]+=[A-Za-z0-9_-]+\])*)$/;
  var CONDITION = /\[([A-Za-z0-9_]+)=([A-Za-z0-9_-]+)\]/g;

  /** "file:a.b[c=d].e" -> {file, segments} or null when it is not well formed. */
  function parseBind(spec) {
    var m = BIND.exec(String(spec == null ? "" : spec).trim());
    if (!m) return null;
    var parts = m[2].split(".");
    var segments = [];
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i];
      if (part === "@count") {
        if (i !== parts.length - 1) return null;
        segments.push({ count: true });
        continue;
      }
      var s = part ? SEGMENT.exec(part) : null;
      if (!s || (s[1] == null && !s[2])) return null;
      var where = [];
      var c;
      CONDITION.lastIndex = 0;
      while ((c = CONDITION.exec(s[2] || ""))) where.push([c[1], c[2]]);
      segments.push({ key: s[1] == null ? null : s[1], where: where });
    }
    return { file: m[1], segments: segments };
  }

  /** Walks the segments from a root value; undefined when any step is absent. */
  function resolvePath(root, segments) {
    var node = root;
    for (var i = 0; i < segments.length; i++) {
      var seg = segments[i];
      if (node == null) return undefined;
      if (seg.count) {
        if (Array.isArray(node)) return node.length;
        return typeof node === "object" ? Object.keys(node).length : undefined;
      }
      if (seg.key != null) {
        if (Array.isArray(node)) {
          if (!/^-?\d+$/.test(seg.key)) return undefined;
          var index = parseInt(seg.key, 10);
          node = node[index < 0 ? node.length + index : index];
        } else if (typeof node === "object") {
          node = Object.prototype.hasOwnProperty.call(node, seg.key) ? node[seg.key] : undefined;
        } else {
          return undefined;
        }
      }
      if (seg.where.length) {
        if (!Array.isArray(node)) return undefined;
        var found;
        for (var j = 0; j < node.length && found === undefined; j++) {
          var item = node[j];
          if (item && typeof item === "object" && seg.where.every(function (w) { return String(item[w[0]]) === w[1]; })) found = item;
        }
        node = found;
      }
    }
    return node;
  }

  // ---------------------------------------------------------------- formats

  function isNum(v) {
    return typeof v === "number" && isFinite(v);
  }

  function body(v, digits) {
    return Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function isZero(text) {
    return Number(text.replace(/,/g, "")) === 0;
  }

  function plain(digits, scale, suffix) {
    return function (v) {
      if (!isNum(v)) return null;
      var text = body(v * (scale || 1), digits);
      return (v < 0 && !isZero(text) ? MINUS : "") + text + (suffix || "");
    };
  }

  function signed(digits, scale, suffix) {
    return function (v) {
      if (!isNum(v)) return null;
      var text = body(v * (scale || 1), digits);
      return (isZero(text) ? "" : v < 0 ? MINUS : "+") + text + (suffix || "");
    };
  }

  function unsigned(digits, scale, suffix) {
    return function (v) {
      return isNum(v) ? body(v * (scale || 1), digits) + (suffix || "") : null;
    };
  }

  function usd(digits, suffix) {
    return function (v) {
      if (!isNum(v)) return null;
      var text = body(v, digits);
      return (v < 0 && !isZero(text) ? MINUS : "") + "$" + text + (suffix || "");
    };
  }

  function dayParts(v) {
    var m = /^(\d{4})-(\d{2})(?:-(\d{2}))?/.exec(typeof v === "string" ? v : "");
    if (!m || +m[2] < 1 || +m[2] > 12) return null;
    return { year: m[1], month: MONTHS[+m[2] - 1], day: m[3] ? String(+m[3]) : null };
  }

  // Simple fractions a reader hears more easily than a percentage.
  var FRACTIONS = [
    [0.1, "a tenth"], [0.2, "a fifth"], [0.25, "a quarter"], [1 / 3, "a third"], [0.4, "two-fifths"], [0.5, "half"],
    [0.6, "three-fifths"], [2 / 3, "two-thirds"], [0.75, "three-quarters"], [0.8, "four-fifths"], [0.9, "nine-tenths"]
  ];

  var pct0 = plain(0, 100, "%");

  var FORMATS = {
    // Words as the builder wrote them. Some files give a summary as one string, others as a list of sentences.
    text: function (v) {
      if (Array.isArray(v)) {
        return v.length && v.every(function (s) { return typeof s === "string" && s; }) ? v.join(" ") : null;
      }
      return typeof v === "string" && v ? v : isNum(v) ? String(v) : null;
    },
    int: plain(0),
    num1: plain(1),
    num2: plain(2),
    signed1: signed(1),
    signed2: signed(2),
    // A fraction shown as a percentage.
    pct0: pct0,
    pct1: plain(1, 100, "%"),
    pct2: plain(2, 100, "%"),
    pctabs0: unsigned(0, 100, "%"),
    pctabs1: unsigned(1, 100, "%"),
    // A number already in percent.
    pctraw0: plain(0, 1, "%"),
    pctraw1: plain(1, 1, "%"),
    pctrawabs0: unsigned(0, 1, "%"),
    pctrawabs1: unsigned(1, 1, "%"),
    usd2: usd(2),
    usd0: usd(0),
    // Millions of dollars.
    usd0m: usd(0, "m"),
    // Dollars shown as whole cents, without a sign: "9 cents".
    cents0: function (v) {
      if (!isNum(v)) return null;
      var cents = Math.round(Math.abs(v) * 100);
      return cents.toLocaleString("en-US") + (cents === 1 ? " cent" : " cents");
    },
    date: function (v) {
      var p = dayParts(v);
      return p && p.day ? p.month + " " + p.day + ", " + p.year : null;
    },
    monthyear: function (v) {
      var p = dayParts(v);
      return p ? p.month + " " + p.year : null;
    },
    year: function (v) {
      var m = /^(\d{4})(?:\D|$)/.exec(typeof v === "string" ? v : isNum(v) ? String(v) : "");
      return m ? m[1] : null;
    },
    // "2026Q3" -> "Q3 2026"
    period: function (v) {
      var m = /^(\d{4})Q([1-4])$/.exec(typeof v === "string" ? v : "");
      return m ? "Q" + m[2] + " " + m[1] : null;
    },
    // "about three-quarters" when a simple fraction is close, the percentage otherwise.
    words: function (v) {
      if (!isNum(v)) return null;
      var best = FRACTIONS[0];
      FRACTIONS.forEach(function (f) {
        if (Math.abs(v - f[0]) < Math.abs(v - best[0])) best = f;
      });
      var gap = Math.abs(v - best[0]);
      if (gap > 0.03) return pct0(v);
      return (gap < 0.0005 ? "" : "about ") + best[1];
    },
    hash: function (v) { return typeof v === "string" && /^[0-9a-f]{16,}$/i.test(v) ? v : null; }
  };

  function pair(inner, value) {
    if (!Array.isArray(value) || value.length !== 2) return null;
    var lo = inner(value[0]);
    var hi = inner(value[1]);
    return lo == null || hi == null ? null : lo + " to " + hi;
  }

  /** "pct0", "range:pct0" (a pair) or "flex:usd2" (a number, a pair or words as given). */
  function formatter(name) {
    var m = /^(?:(range|flex):)?([a-z0-9]+)$/.exec(String(name || "text"));
    if (!m || !Object.prototype.hasOwnProperty.call(FORMATS, m[2])) return null;
    var inner = FORMATS[m[2]];
    if (m[1] === "range") return function (v) { return pair(inner, v); };
    if (m[1] === "flex") {
      return function (v) {
        if (Array.isArray(v)) return pair(inner, v);
        return typeof v === "string" ? v || null : inner(v);
      };
    }
    return inner;
  }

  function scaled(value, k) {
    if (Array.isArray(value)) return value.map(function (v) { return scaled(v, k); });
    return isNum(value) ? value * k : value;
  }

  /**
   * evaluate(spec, format, scale, lookup) -> {ok: true, text, value} | {ok: false, reason}
   * lookup(name) returns {status: "ok", data} for a loaded file, anything else when it is not there.
   */
  function evaluate(spec, format, scale, lookup) {
    var render = formatter(format);
    if (!render) return { ok: false, reason: "unknown format: " + format };
    var specs = String(spec == null ? "" : spec).split(",");
    if (specs.length > 2) return { ok: false, reason: "more than two binds: " + spec };
    var values = [];
    for (var i = 0; i < specs.length; i++) {
      var bind = parseBind(specs[i]);
      if (!bind) return { ok: false, reason: "malformed bind: " + specs[i] };
      var file = lookup(bind.file);
      if (!file || file.status !== "ok") return { ok: false, reason: bind.file + ".json is not built", file: bind.file };
      var value = resolvePath(file.data, bind.segments);
      if (value === undefined || value === null) return { ok: false, reason: "no value at " + specs[i].trim(), file: bind.file };
      values.push(value);
    }
    var out = values.length === 1 ? values[0] : values;
    if (scale != null && scale !== "") {
      var k = Number(scale);
      if (!isFinite(k)) return { ok: false, reason: "data-scale is not a number: " + scale };
      out = scaled(out, k);
    }
    var text = render(out);
    if (text == null) return { ok: false, reason: "the value at " + spec + " does not suit the format " + (format || "text") };
    return { ok: true, text: text, value: out };
  }

  var api = {
    MISSING: MISSING,
    FORMAT_NAMES: Object.keys(FORMATS),
    parseBind: parseBind,
    resolvePath: resolvePath,
    formatter: formatter,
    evaluate: evaluate
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.LabReport = api;
  if (!global.document) return;

  // ------------------------------------------------------------------- page

  var doc = global.document;
  var FIXTURES = /(?:^|[?&])fixtures=1(?:&|$)/.test(global.location.search || "");
  var BASE = "/static/data/" + (FIXTURES ? "fixtures/" : "");
  var PRINT_WIDTH = 660;

  function all(selector, root) {
    return Array.prototype.slice.call((root || doc).querySelectorAll(selector));
  }

  function listed(el, attribute) {
    return (el.getAttribute(attribute) || "").split(/\s+/).filter(Boolean);
  }

  function hasFixtureFlag(data) {
    if (Array.isArray(data)) return data.some(function (d) { return d && d.fixture === true; });
    return !!data && data.fixture === true;
  }

  function load(name) {
    var url = BASE + name + ".json";
    return global.fetch(url, { cache: "no-cache", headers: { Accept: "application/json" } }).then(function (response) {
      if (response.status === 404) return { status: "missing", name: name, url: url };
      if (!response.ok) return { status: "error", name: name, url: url };
      return response.json().then(function (data) {
        // Placeholder files are never shown as results.
        if (!FIXTURES && hasFixtureFlag(data)) return { status: "error", name: name, url: url };
        return { status: "ok", name: name, url: url, data: data };
      }, function () {
        return { status: "error", name: name, url: url };
      });
    }, function () {
      return { status: "error", name: name, url: url };
    });
  }

  function fileNames() {
    var names = {};
    all("[data-bind]").forEach(function (el) {
      el.getAttribute("data-bind").split(",").forEach(function (spec) {
        var bind = parseBind(spec);
        if (bind) names[bind.file] = true;
      });
    });
    all("[data-needs], [data-pending-for]").forEach(function (el) {
      listed(el, "data-needs").concat(listed(el, "data-pending-for")).forEach(function (name) { names[name] = true; });
    });
    return Object.keys(names);
  }

  function built(files, names) {
    return names.every(function (name) { return files[name] && files[name].status === "ok"; });
  }

  // A block appears once every file it needs is built; until then its one-line note stands in.
  function applyNeeds(files) {
    all("[data-needs]").forEach(function (el) {
      el.hidden = !built(files, listed(el, "data-needs"));
    });
    all("[data-pending-for]").forEach(function (el) {
      el.hidden = built(files, listed(el, "data-pending-for"));
    });
  }

  function fillBinds(files) {
    var lookup = function (name) { return files[name]; };
    all("[data-bind]").forEach(function (el) {
      var result = evaluate(el.getAttribute("data-bind"), el.getAttribute("data-format") || "text", el.getAttribute("data-scale"), lookup);
      el.textContent = result.ok ? result.text : MISSING;
      el.classList.toggle("nc", !result.ok);
      if (result.ok) el.removeAttribute("title");
      else el.setAttribute("title", result.reason);
    });
  }

  // ----------------------------------------------------------------- charts

  function note(text) {
    var p = doc.createElement("p");
    p.className = "fig-missing";
    p.textContent = text;
    return p;
  }

  /**
   * The one-year share for each airline and for the three pooled: the estimate as a
   * dot, the range of the resamples as a bar. A range that runs off the scale is cut
   * at the edge and marked with an arrow; its numbers are printed beside it.
   */
  function carrierRanges(data, L) {
    var Plot = global.Plot;
    var C = L.colors;
    var pct = function (v) { return L.fmt.pct(v, 0); };
    var rows = ((data.by_carrier || {}).table || []).filter(function (r) {
      return r && r.one_year_share && isNum(r.one_year_share.estimate);
    }).map(function (r) {
      return { name: r.name || r.carrier, estimate: r.one_year_share.estimate, lo: r.one_year_share.p05, hi: r.one_year_share.p95, n: r.n_rows, pooled: false };
    });
    var pooled = (data.pooled || {}).one_year_share;
    if (pooled && isNum(pooled.estimate)) {
      rows.push({ name: "All pooled", estimate: pooled.estimate, lo: pooled.p05, hi: pooled.p95, n: data.pooled.n_rows, pooled: true });
    }
    rows = rows.filter(function (r) { return isNum(r.lo) && isNum(r.hi); });
    return {
      legend: [
        { label: "Least-squares estimate", key: "dot", color: C.ink },
        { label: "Range of the resamples, 5% to 95%", key: "line", color: C.context },
        { label: "The three airlines pooled", key: "line", color: C.ours }
      ],
      render: function (width) {
        if (!rows.length) return note("The estimates by airline are not in this file yet.");
        var narrow = width < 560;
        // The scale is set by the pooled range; a much wider range is cut at the edges.
        var anchor = rows.filter(function (r) { return r.pooled; })[0] || rows[0];
        var span = Math.max(anchor.hi - anchor.lo, 0.4);
        var domain = [Math.min(0, anchor.lo) - span * 0.35, Math.max(1, anchor.hi) + span * 0.35];
        var drawn = rows.map(function (r) {
          return {
            name: r.name, pooled: r.pooled, estimate: r.estimate,
            x1: Math.max(r.lo, domain[0]), x2: Math.min(r.hi, domain[1]),
            offLeft: r.lo < domain[0], offRight: r.hi > domain[1],
            inside: r.estimate >= domain[0] && r.estimate <= domain[1],
            label: pct(r.estimate) + (narrow ? "" : " (" + pct(r.lo) + " to " + pct(r.hi) + ")"),
            tip: r.name + "\nAfter one year: " + pct(r.estimate) + "\nRange: " + pct(r.lo) + " to " + pct(r.hi) + (isNum(r.n) ? "\n" + r.n + " quarters" : "")
          };
        });
        var tone = function (d) { return d.pooled ? C.ours : C.context; };
        var marks = [
          Plot.gridX({ ticks: narrow ? 4 : 6, stroke: C.grid, strokeOpacity: 1 }),
          Plot.axisX({ ticks: narrow ? 4 : 6, tickSize: 4, stroke: C.axis, tickFormat: pct,
            label: "Share of a fuel cost change made up after one year", labelAnchor: "left", labelArrow: "none", labelOffset: 34 }),
          Plot.axisY({ tickSize: 0, label: null, tickPadding: 10 }),
          Plot.ruleX([0, 1], { stroke: C.axis }),
          Plot.text([[0, "No response"], [1, "Full recovery"]], { x: "0", text: "1", frameAnchor: "top", dy: -10, fill: C.label,
            fontFamily: "var(--font-sans)", fontSize: 11 }),
          Plot.ruleY(drawn, { y: "name", x1: "x1", x2: "x2", stroke: tone, strokeWidth: 3 }),
          Plot.text(drawn.filter(function (d) { return d.offLeft; }), { y: "name", x: "x1", text: function () { return "◂"; },
            textAnchor: "end", dx: 1, fill: tone, fontSize: 12 }),
          Plot.text(drawn.filter(function (d) { return d.offRight; }), { y: "name", x: "x2", text: function () { return "▸"; },
            textAnchor: "start", dx: -1, fill: tone, fontSize: 12 }),
          Plot.dot(drawn.filter(function (d) { return d.inside; }), { y: "name", x: "estimate", r: 4.5, fill: C.ink, stroke: C.surface, strokeWidth: 2 }),
          Plot.text(drawn, { y: "name", x: function () { return domain[1]; }, dx: 14, textAnchor: "start", text: "label",
            fill: C.label, fontFamily: "var(--font-sans)", fontSize: 11.5 }),
          Plot.tip(drawn, Plot.pointerY({ y: "name", x: function (d) { return (d.x1 + d.x2) / 2; }, title: "tip",
            fill: C.surface, stroke: C.bandEdge, fontSize: 12, lineHeight: 1.35, textPadding: 10 }))
        ];
        return L.plot({
          width: width, height: drawn.length * 38 + 66,
          marginLeft: 92, marginRight: narrow ? 52 : 150, marginTop: 24, marginBottom: 42,
          x: { axis: null, domain: domain },
          y: { axis: null, type: "band", domain: drawn.map(function (d) { return d.name; }), padding: 0 },
          marks: marks
        });
      }
    };
  }

  var points = signed(2);

  // name -> function (files, LabCharts) returning a chart spec ({render, legend}).
  var CHARTS = {
    cost: function (d, L) { return L.costClock(d.cost_clock); },
    recovery: function (d, L) { return L.recoveryCurve(d.fare_clock); },
    capacity: function (d, L) {
      return L.bandLine({
        rows: d.capacity_response.curve, x: "quarters_after", y: "p50", lo: "p10", hi: "p90",
        name: "Change in capacity growth, middle estimate", bandName: "Its 10–90% range",
        xLabel: "Quarters after a 10% rise in jet fuel",
        // The axis may place ticks between quarters; only whole quarters are named.
        xFormat: function (q) { return q !== Math.round(q) ? "" : q === 0 ? "0" : "+" + q; },
        tipX: function (q) { return q === 0 ? "Same quarter" : q + (q === 1 ? " quarter later" : " quarters later"); },
        yFormat: function (v) { return points(v); }
      });
    },
    demand: function (d, L) { return L.demandSignal({ daily: d.demand_signal.daily || [] }); },
    market: function (d, L) { return L.eventPath({ path: (d.market_clock.event_study || {}).path || [] }); },
    fares: function (d, L) { return L.dailyFares({ daily: d.daily_fares_2022.daily || [] }); },
    carriers: function (d, L) { return carrierRanges(d.peer_recovery, L); },
    track: function (d, L) {
      var quarters = d.track_record.quarters || [];
      var hasTest = quarters.some(function (q) { return q.split === "test"; });
      return L.trackRecord(d.track_record, hasTest ? { only: "test" } : {});
    }
  };

  var HEIGHTS = { recovery: 320, cost: 260, capacity: 240, market: 250 };
  var figures = [];

  function mountFigure(fig, files) {
    var name = fig.getAttribute("data-chart");
    var box = fig.querySelector("[data-plot]");
    if (!box) return;
    var L = global.LabCharts;
    var data = {};
    Object.keys(files).forEach(function (key) { data[key] = files[key].data; });
    var spec = null;
    try {
      if (L && global.Plot && CHARTS[name]) spec = CHARTS[name](data, L);
    } catch (error) {
      if (global.console) console.error(error);
    }
    if (!spec || typeof spec.render !== "function") {
      box.replaceChildren(note("This chart could not be drawn. The numbers in the text do not depend on it."));
      return;
    }
    if (spec.legend && spec.legend.length && typeof L.legendList === "function" && !fig.querySelector(".fig-legend")) {
      fig.insertBefore(L.legendList(spec.legend), box);
    }
    var last = 0;
    function draw(limit) {
      var width = Math.floor(box.clientWidth);
      if (limit) width = Math.min(width || limit, limit);
      if (!width) return;
      last = width;
      try {
        box.replaceChildren(spec.render(width, {}, HEIGHTS[name]));
      } catch (error) {
        box.replaceChildren(note("This chart could not be drawn. The numbers in the text do not depend on it."));
        if (global.console) console.error(error);
      }
    }
    draw();
    figures.push(draw);
    if (global.ResizeObserver) {
      var pending = 0;
      new global.ResizeObserver(function () {
        if (Math.floor(box.clientWidth) === last) return;
        global.cancelAnimationFrame(pending);
        pending = global.requestAnimationFrame(function () { draw(); });
      }).observe(box);
    }
  }

  function drawFigures(files) {
    all("figure[data-chart]").forEach(function (fig) {
      if (fig.closest("[hidden]")) return;
      mountFigure(fig, files);
    });
  }

  // Paper is light and narrower than the screen, and it cannot be clicked: switch to the
  // light theme, open every closed detail and draw the charts at the page's width before
  // printing, then put all three back.
  var themeBeforePrint;
  var closedBeforePrint = [];
  function printing(on) {
    var root = doc.documentElement;
    if (on) {
      if (themeBeforePrint !== undefined) return;
      themeBeforePrint = root.getAttribute("data-theme");
      root.setAttribute("data-theme", "light");
      closedBeforePrint = all("details").filter(function (el) { return !el.open; });
      closedBeforePrint.forEach(function (el) { el.open = true; });
      figures.forEach(function (draw) { draw(PRINT_WIDTH); });
    } else if (themeBeforePrint !== undefined) {
      if (themeBeforePrint === null) root.removeAttribute("data-theme");
      else root.setAttribute("data-theme", themeBeforePrint);
      themeBeforePrint = undefined;
      closedBeforePrint.forEach(function (el) { el.open = false; });
      closedBeforePrint = [];
      figures.forEach(function (draw) { draw(); });
    }
  }

  // --------------------------------------------------------------- sections

  // A section becomes the current one once its heading is within this many pixels below
  // the place a link to it would scroll it to.
  var SPY_SLACK = 48;
  // Scrolling has ended when no scroll event has arrived for this long.
  var SETTLE_MS = 140;

  /**
   * The section list: mounts the sidebar, keeps its active item on the section being
   * read, and on narrow screens runs the bar that opens it. The links are ordinary
   * anchors, so the browser does the scrolling and writes the address.
   */
  function sectionNav() {
    var nav = doc.getElementById("sections");
    if (!nav || !global.HookSidebar) return null;
    var side = nav.closest(".report-side") || nav;
    var links = all(".hook-item", nav);
    var targets = links.map(function (link) { return doc.getElementById((link.getAttribute("href") || "").replace(/^#/, "")); });
    if (!links.length || targets.indexOf(null) !== -1) return null;
    var toggle = doc.getElementById("sections-toggle");
    var here = doc.getElementById("sections-here");
    var root = doc.scrollingElement || doc.documentElement;
    var bar = null;
    var held = -1;   // the item a person chose: it stays current until the scroll it started has ended
    var timer = 0;
    var queued = 0;

    function px(el, property) {
      return parseFloat(global.getComputedStyle(el)[property]) || 0;
    }

    function atBottom() {
      return root.scrollTop > 0 && root.scrollTop + global.innerHeight >= root.scrollHeight - 2;
    }

    // The last section whose heading has reached the top of the reading area.
    function reading() {
      if (atBottom()) return targets.length - 1;
      var pad = px(doc.documentElement, "scrollPaddingTop");
      var index = 0;
      targets.forEach(function (target, i) {
        if (target.getBoundingClientRect().top <= pad + px(target, "scrollMarginTop") + SPY_SLACK) index = i;
      });
      return index;
    }

    function show(index, jump) {
      bar.setActive(index, jump ? { jump: true } : undefined);
      if (here && links[index]) here.textContent = links[index].textContent;
    }

    function release() {
      var chosen = held;
      held = -1;
      // Near the end the page cannot bring a short section to the top; the choice then stands.
      var now = reading();
      if (now !== chosen && !atBottom()) show(now);
    }

    function settle(ms) {
      global.clearTimeout(timer);
      timer = global.setTimeout(release, ms);
    }

    function hold(index, jump) {
      held = index;
      show(index, jump);
      settle(SETTLE_MS * 3);
    }

    function onScroll() {
      if (held !== -1) {
        settle(SETTLE_MS);
        return;
      }
      if (queued) return;
      queued = global.requestAnimationFrame(function () {
        queued = 0;
        if (held === -1) show(reading());
      });
    }

    function named() {
      var id = "";
      try {
        id = decodeURIComponent((global.location.hash || "").slice(1));
      } catch (error) {
        id = "";
      }
      return id ? doc.getElementById(id) : null;
    }

    // The section an element sits in, -1 for none.
    function sectionOf(el) {
      for (var i = 0; el && i < targets.length; i++) {
        if (targets[i] === el || targets[i].contains(el)) return i;
      }
      return -1;
    }

    function open(on) {
      side.classList.toggle("is-open", on);
      if (toggle) toggle.setAttribute("aria-expanded", String(on));
    }

    var first = sectionOf(named());
    bar = global.HookSidebar.mount(nav, {
      active: first >= 0 ? first : 0,
      current: "location",
      onChange: function (index) {
        hold(index);
        // The list folds away once the link has done its work, not while it is being followed.
        global.setTimeout(function () { open(false); }, 0);
      }
    });
    show(bar.active, true);

    if (toggle) {
      toggle.hidden = false;
      side.classList.add("is-ready");
      toggle.addEventListener("click", function () { open(!side.classList.contains("is-open")); });
      doc.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && side.classList.contains("is-open")) {
          open(false);
          toggle.focus();
        }
      });
      doc.addEventListener("click", function (event) {
        if (side.classList.contains("is-open") && !side.contains(event.target)) open(false);
      });
    }
    global.addEventListener("scroll", onScroll, { passive: true });
    global.addEventListener("resize", onScroll);
    global.addEventListener("hashchange", function () {
      var index = sectionOf(named());
      if (index >= 0) hold(index);
    });

    return {
      // The page grows as its figures arrive, so a place named in the address is brought back
      // into view once they are in; without one the list follows where the reader is.
      align: function () {
        var el = named();
        var index = sectionOf(el);
        if (index < 0) {
          show(reading(), true);
          return;
        }
        var place = function () { el.scrollIntoView({ block: "start", behavior: "instant" }); };
        place();
        hold(index, true);
        // Once more after the next frame, when the charts and the bar above have settled.
        if (global.requestAnimationFrame) global.requestAnimationFrame(place);
      }
    };
  }

  function start() {
    global.addEventListener("beforeprint", function () { printing(true); });
    global.addEventListener("afterprint", function () { printing(false); });
    var printer = doc.getElementById("print");
    if (printer) {
      printer.addEventListener("click", function () {
        printing(true);
        global.print();
      });
    }
    var banner = doc.getElementById("fixture-pill");
    if (banner) banner.hidden = !FIXTURES;
    var sections = null;
    try {
      sections = sectionNav();
    } catch (error) {
      if (global.console) console.error(error);
    }

    return Promise.all(fileNames().map(load)).then(function (results) {
      var files = {};
      results.forEach(function (r) { files[r.name] = r; });
      applyNeeds(files);
      fillBinds(files);
      drawFigures(files);
      doc.documentElement.setAttribute("data-report", "ready");
      if (sections) sections.align();
      return files;
    });
  }

  api.start = start;
  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", start);
  else start();
})(typeof window !== "undefined" ? window : globalThis);
