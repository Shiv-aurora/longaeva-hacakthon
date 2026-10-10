/*
 * Lab: data loading, sources, the theme toggle and the application (index.html).
 *
 * Shared runtime (usable from any lab page, for example /report):
 *   Lab.fixtures            true when the page runs on ?fixtures=1
 *   Lab.load(name)          -> Promise<{status: "ok"|"missing"|"error", data, url, message}>
 *   Lab.ev(text, ids)       -> a clickable sourced number (plain text if no source is loaded)
 *   Lab.openEvidence(ids)   opens the drawer on those sources
 *   Lab.figures.<name>(ctx) -> {frame, spec} for LabCharts.panel or LabCharts.mount
 *   Lab.sim.set(request)    sets the simulator controls and runs
 *   Lab.view(name)          switches view: "simulator", "behaviour" or "reality"
 *   Lab.words()             visible word count of the current view
 *
 * Page code runs only when <body data-page="index">.
 * Rule: no finding is written here. Titles come from each file's `headline`;
 * every sentence with a number is assembled from the JSON.
 */
(function (global) {
  "use strict";

  var L = global.LabCharts;
  if (!L) return;
  var el = L.el;
  var fmt = L.fmt;
  var isNum = L.isNum;

  var params = new URLSearchParams(global.location.search);
  var FIXTURES = params.get("fixtures") === "1";
  var DATA = "/static/data/" + (FIXTURES ? "fixtures/" : "");
  var API = "/api/lab";
  var FILES = ["meta", "evidence", "cost_clock", "fare_clock", "demand_signal", "market_clock", "track_record", "q3_call", "signal"];
  // Shown when present, left out without a notice when not.
  var OPTIONAL = ["capacity_response", "daily_fares_2022", "peer_recovery"];
  var FILE_NAMES = {
    meta: "Guidance, report date and comparators", evidence: "Quoted sources", cost_clock: "Fuel price to fuel bill",
    fare_clock: "Fuel cost to revenue", demand_signal: "Travel demand", market_clock: "Share price reaction",
    track_record: "Replay on past quarters", q3_call: "The dated forward test", signal: "Daily estimate and backtest",
    daily_fares_2022: "Daily fares, 2022", capacity_response: "Capacity response", peer_recovery: "Recovery curve, several airlines pooled"
  };
  var reducedMotion = global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function $(id) {
    return document.getElementById(id);
  }

  // ---------------------------------------------------------------- loading

  function hasFixtureFlag(data) {
    if (Array.isArray(data)) return data.some(function (d) { return d && d.fixture === true; });
    return !!data && data.fixture === true;
  }

  // What the server says it has (GET /api/lab/status): which data files exist, whether a
  // language model is connected. Asked once; null when it cannot be had or on placeholder data.
  var serverStatus = FIXTURES ? Promise.resolve(null)
    : fetch(API + "/status", { headers: { Accept: "application/json" } }).then(function (response) {
      return response.ok ? response.json() : null;
    }).then(function (status) { return status && typeof status === "object" ? status : null; }, function () { return null; });

  var cache = {};
  function load(name) {
    if (cache[name]) return cache[name];
    var url = DATA + name + ".json";
    cache[name] = serverStatus.then(function (status) {
      // A file the server lists as not built is not asked for: no failed request, same state.
      if (status && status.data_files && status.data_files[name + ".json"] === false) return { status: "missing", name: name, url: url };
      return fetchData(name, url);
    });
    return cache[name];
  }

  function fetchData(name, url) {
    // "no-cache" revalidates with the server every time, so a rebuilt file is never served stale.
    return fetch(url, { cache: "no-cache", headers: { Accept: "application/json" } }).then(function (response) {
      if (response.status === 404) return { status: "missing", name: name, url: url };
      if (!response.ok) return { status: "error", name: name, url: url, message: "The server answered " + response.status + " for this file." };
      return response.json().then(function (data) {
        if (!FIXTURES && hasFixtureFlag(data)) {
          return { status: "error", name: name, url: url, message: "This file is marked as placeholder data, so it is not shown as a result." };
        }
        return { status: "ok", name: name, url: url, data: data };
      }, function () {
        return { status: "error", name: name, url: url, message: "This file is not valid JSON." };
      });
    }, function () {
      return { status: "error", name: name, url: url, message: "This file could not be fetched." };
    });
  }

  /** True when every result is usable; otherwise draws the right state in the tile. */
  function ready(host, results, what) {
    var bad = results.filter(function (r) { return !r || r.status !== "ok"; })[0];
    if (!bad) return true;
    host.classList.add("tile");
    var box = bad.status === "missing"
      ? L.state(null, "missing", { compact: true, text: (what || "This part") + " has not been built yet.", file: bad.url })
      : L.state(null, "error", { compact: true, text: bad.message, file: bad.url });
    host.replaceChildren(el("header", { class: "tile-head" }, [el("h2", { class: "tile-title", text: what || "" })]), box);
    return false;
  }

  function post(path, body, accept) {
    return fetch(API + path, {
      method: "POST", headers: { "Content-Type": "application/json", Accept: accept || "application/json" }, body: JSON.stringify(body)
    });
  }

  /** POST JSON and fold every failure into {__state, text}. */
  function call(path, body, what) {
    return post(path, body).then(function (response) {
      if (response.status === 404 || response.status === 405) return { __state: "missing", text: what + " is not available on this server yet." };
      if (response.status === 429) {
        // The server meters requests. Say so quietly and let the caller try again.
        var seconds = Number(response.headers.get("Retry-After"));
        return { __state: "busy", retryAfter: isNum(seconds) && seconds > 0 ? Math.min(60, Math.ceil(seconds)) : 5, text: what + " is limited to a few runs at a time." };
      }
      return response.json().then(function (json) {
        if (response.ok) return json;
        var detail = json && (typeof json.detail === "string" ? json.detail : Array.isArray(json.errors) ? json.errors.join(" ") : null);
        return { __state: "error", text: detail || what + " answered " + response.status + "." };
      }, function () {
        return { __state: "error", text: what + " answered " + response.status + " with something that is not JSON." };
      });
    }, function () {
      return { __state: "error", text: what + " could not be reached." };
    });
  }

  // ------------------------------------------------------------ shell

  function initTheme() {
    var button = $("theme-toggle");
    if (!button) return;
    var root = document.documentElement;
    button.addEventListener("click", function () {
      var current = root.getAttribute("data-theme") ||
        (global.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      var next = current === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("lab-theme", next); } catch (error) { /* not persisted; still applied */ }
    });
  }

  var VIEWS = ["simulator", "behaviour", "reality"];
  var viewNav = null;

  function currentHash() {
    return (global.location.hash || "").replace("#", "");
  }

  function showView(name, how) {
    if (VIEWS.indexOf(name) < 0) name = VIEWS[0];
    Array.prototype.forEach.call(document.querySelectorAll(".view"), function (view) {
      view.hidden = view.getAttribute("data-view") !== name;
    });
    document.body.setAttribute("data-current-view", name);
    if (viewNav) viewNav.setActive(VIEWS.indexOf(name), how);
  }

  /** Switch view from anywhere: a deep link, the console, another page's script. */
  function goToView(name) {
    if (VIEWS.indexOf(name) < 0) return;
    if (currentHash() === name) showView(name);
    else global.location.hash = "#" + name;
  }

  function initViews() {
    var host = $("view-nav");
    if (host && global.GooeyNav) {
      viewNav = global.GooeyNav.mount(host, {
        size: "sm", compact: { query: "(max-width: 339px)", size: "xs" },
        active: Math.max(0, VIEWS.indexOf(currentHash())),
        onChange: function (index) { goToView(VIEWS[index]); }
      });
    } else if (host) {
      // Without the animated nav the buttons still switch views.
      Array.prototype.forEach.call(host.querySelectorAll("button"), function (button, i) {
        button.addEventListener("click", function () { goToView(VIEWS[i]); });
      });
      viewNav = { setActive: function (index) {
        Array.prototype.forEach.call(host.querySelectorAll("li"), function (li, i) {
          li.classList.toggle("is-active", i === index);
          if (i === index) li.firstElementChild.setAttribute("aria-current", "true");
          else li.firstElementChild.removeAttribute("aria-current");
        });
      } };
    }
    global.addEventListener("hashchange", function () { showView(currentHash()); });
    showView(currentHash(), { jump: true });
  }

  /**
   * Words a visitor can see in the current view, top bar included. Text inside charts
   * (axis ticks, direct labels) is not counted; a title cut short by an ellipsis counts
   * only for the part that fits.
   */
  function words() {
    var view = document.querySelector(".view:not([hidden])");
    var isWord = function (w) { return /[A-Za-z]/.test(w); };
    var count = function (root) {
      if (!root) return 0;
      var total = 0;
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        var node = walker.currentNode;
        var parent = node.parentElement;
        if (!parent || parent.closest("svg, [hidden], .sr-only, script, style, noscript")) continue;
        var box = parent.getBoundingClientRect();
        if (!box.width || !box.height) continue;
        var n = node.nodeValue.split(/\s+/).filter(isWord).length;
        var clipped = parent.closest(".tile-title, .sim-status, .outcome-range, .outcome-delta, .chain-name");
        if (clipped && clipped.scrollWidth > clipped.clientWidth + 1) n = Math.round(n * clipped.clientWidth / clipped.scrollWidth);
        total += n;
      }
      return total;
    };
    return { view: document.body.getAttribute("data-current-view"), words: count(document.querySelector(".topbar")) + count(view) };
  }

  function openAbout(r) {
    var link = function (text, href, external) {
      return el("a", external ? { href: href, target: "_blank", rel: "noopener noreferrer", text: text } : { href: href, text: text });
    };
    var files = el("dl", { class: "kv" });
    FILES.concat(OPTIONAL).forEach(function (name) {
      var result = r && r[name];
      if (!result || (result.status !== "ok" && OPTIONAL.indexOf(name) >= 0)) return;
      var status = result.status === "ok" ? (result.data && typeof result.data.as_of === "string" ? "to " + fmt.day(result.data.as_of) : "loaded")
        : result.status === "missing" ? "not computed yet" : "could not be loaded";
      files.appendChild(el("div", null, [el("dt", { text: FILE_NAMES[name] || name }), el("dd", { text: status })]));
    });
    L.openDrawer("About", [
      el("h3", { text: "UAL Counterfactual Lab" }),
      el("p", { class: "drawer-text", text: "A forward simulation of United Airlines as a business. Set a scenario and the simulator runs it many times, each with different random fuel and demand, and shows the range of outcomes. How the airline responds inside it is measured from its history and checked against quarters the method never saw." }),
      labelled("The three views", el("ul", { class: "limits" }, [
        "Simulator: change fuel, demand, capacity or deliveries and read the outcomes.",
        "Behavior: the measured relationships that drive the simulator.",
        "Reality check: the same method replayed on past quarters, against simple rivals."
      ].map(function (t) { return el("li", { text: t }); }))),
      textBlock("Reading the numbers", "The large figure is the middle of the simulated paths. The range beneath it holds the middle 80% of them. The small \u201ci\u201d on each panel opens how to read it, its sources and its data."),
      FIXTURES ? textBlock("Placeholder data", "This session runs on made-up stand-in files (?fixtures=1). Nothing shown is a finding.") : null,
      labelled("Credits", el("ul", { class: "credits" }, [
        el("li", null, ["Navigation adapted from ", link("Rare UI", "https://rareui.com", true), " (Gooey Nav, \u00a9 2026 Swami Malode). ", link("License", "/static/lab/LICENSE-gooey-nav.txt")]),
        el("li", null, ["Charts drawn with Observable Plot and D3. ", link("Licenses", "/static/lab/vendor/VERSIONS")]),
        el("li", null, ["Set in Geist and Geist Mono. ", link("License", "/static/lab/fonts/LICENSE.txt")])
      ])),
      files.firstChild ? labelled("Data in this session", files) : null,
      el("p", { class: "drawer-actions" }, [link("Read the report", "/report")])
    ]);
  }

  // --------------------------------------------------------------- evidence

  var facts = {};

  function indexEvidence(result) {
    if (!result || result.status !== "ok") return;
    var data = result.data;
    var list = Array.isArray(data) ? data : null;
    if (!list && data) {
      ["items", "evidence", "facts", "records"].some(function (key) {
        if (Array.isArray(data[key])) list = data[key];
        return !!list;
      });
    }
    (list || []).forEach(function (fact) {
      if (fact && fact.id) facts[fact.id] = fact;
    });
  }

  function knownIds(ids) {
    return (Array.isArray(ids) ? ids : ids ? [ids] : []).filter(function (id) { return facts[id]; });
  }

  /** A number with a source behind it. Falls back to plain text when no source is loaded. */
  function ev(text, ids) {
    var known = knownIds(ids);
    if (!known.length) return el("span", { text: text });
    return el("button", {
      type: "button", class: "ev", text: text, title: "Show the source for this number",
      "aria-haspopup": "dialog", onclick: function () { openEvidence(known); }
    });
  }

  var CLAIMS = {
    fuel_assumed_usd_gal: "Fuel price assumed in the guidance",
    adj_eps_guidance: "Guidance for adjusted earnings per share",
    fuel_recovery_share: "Share of the fuel increase to be recovered through revenue"
  };
  var UNITS = {
    usd_per_gallon: function (v) { return fmt.usd(v) + " per gallon"; },
    usd_per_share: function (v) { return fmt.usd(v) + " per share"; },
    share: function (v) { return fmt.pct(v); },
    usd_m: function (v) { return fmt.usdMillions(v); }
  };

  function humanize(key) {
    var s = String(key || "").replace(/_/g, " ").trim();
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function factValue(fact) {
    var f = UNITS[fact.unit] || function (v) { return isNum(v) ? fmt.num(v, 2) + (fact.unit ? " " + humanize(fact.unit).toLowerCase() : "") : String(v); };
    if (Array.isArray(fact.value)) return fact.value.map(f).join(" to ");
    return fact.value == null ? fmt.missing : f(fact.value);
  }

  function copyButton(text) {
    var button = el("button", { type: "button", class: "btn btn-quiet", text: "Copy" });
    button.addEventListener("click", function () {
      var done = function () {
        button.textContent = "Copied";
        setTimeout(function () { button.textContent = "Copy"; }, 1400);
      };
      if (global.navigator.clipboard && global.navigator.clipboard.writeText) {
        global.navigator.clipboard.writeText(text).then(done, function () { button.textContent = "Copy failed"; });
      } else {
        button.textContent = "Select and copy";
      }
    });
    return button;
  }

  function factBlock(fact) {
    var picked = String(fact.picked_by || "");
    var pickedText = picked === "rules" ? "Fixed rules" :
      picked.indexOf(":") > 0 ? "A language model (" + picked + ") proposed it; the quote was then checked against the document" : picked || fmt.missing;
    var rows = [
      ["Value", factValue(fact)],
      ["Period", fmt.period(fact.period)],
      ["Document", fact.doc_url ? el("a", { href: fact.doc_url, target: "_blank", rel: "noopener noreferrer", text: "Open the source document" }) : fmt.missing],
      ["Document SHA-256", fact.doc_sha256 ? el("span", null, [el("span", { class: "hash", text: fact.doc_sha256 }), " ", copyButton(fact.doc_sha256)]) : fmt.missing],
      ["Position", isNum(fact.char_start) && isNum(fact.char_end) ? "Characters " + fmt.int(fact.char_start) + " to " + fmt.int(fact.char_end) : fmt.missing],
      ["Picked by", pickedText],
      ["Reference", el("span", { class: "hash", text: fact.id })]
    ];
    var dl = el("dl", { class: "facts" });
    rows.forEach(function (row) {
      dl.appendChild(el("dt", { text: row[0] }));
      dl.appendChild(el("dd", null, [row[1]]));
    });
    var check = fact.verified === true
      ? el("span", { class: "tag is-ok", text: "Checked: the quote appears word for word in the document" })
      : el("span", { class: "tag is-warn", text: "Not verified against the document" });
    return el("div", { class: "drawer-fact" }, [
      el("h3", { text: CLAIMS[fact.claim] || humanize(fact.claim) }),
      el("blockquote", { text: fact.quote ? "“" + fact.quote + "”" : "No quote is recorded for this number." }),
      el("p", null, [check]),
      dl
    ]);
  }

  function openEvidence(ids) {
    var known = knownIds(ids);
    L.openDrawer("Source", known.length
      ? known.map(function (id) { return factBlock(facts[id]); })
      : [L.state(null, "missing", { compact: true, text: "No source is recorded for this number." })]);
  }

  // ------------------------------------------------------------ small parts

  function labelled(title, node) {
    return node ? el("div", null, [el("p", { class: "drawer-label", text: title }), node]) : null;
  }

  function textBlock(title, text) {
    return text ? labelled(title, el("p", { class: "drawer-text", text: String(text) })) : null;
  }

  function listBlock(title, items, className) {
    if (!Array.isArray(items) || !items.length) return null;
    return labelled(title + " (" + items.length + ")", el("ul", { class: className || "limits" }, items.map(function (v) { return el("li", { text: String(v) }); })));
  }

  /** A builder's summary: a list of sentences stays a list, one string is a paragraph. */
  function summaryBlock(title, value) {
    var items = (Array.isArray(value) ? value : value ? [value] : []).filter(Boolean).map(String);
    if (!items.length) return null;
    return items.length === 1 ? textBlock(title, items[0])
      : labelled(title, el("ul", { class: "limits" }, items.map(function (v) { return el("li", { text: v }); })));
  }

  /** The one line under a tile's title: the number first, then the few words that say what it is. */
  function keyLine(value, text) {
    if (value == null || value === fmt.missing) return null;
    return el("p", { class: "tile-key" }, [el("b", { class: "num", text: value }), text ? el("span", { text: " " + text }) : null]);
  }

  function inputsBlock(data) {
    var inputs = data && Array.isArray(data.inputs) ? data.inputs : [];
    if (!inputs.length) return null;
    return labelled("Input files (" + inputs.length + ")", el("ul", { class: "limits" }, inputs.map(function (i) {
      return el("li", null, [el("span", { text: i.path + " " }), el("span", { class: "hash", text: i.sha256 || "" })]);
    })));
  }

  function stat(labelText, valueNodes) {
    return el("div", { class: "stat" }, [el("dt", { text: labelText }), el("dd", { class: "num" }, valueNodes)]);
  }

  function miniTable(columns, rows, caption) {
    return el("div", { class: "table-wrap" }, [L.table(columns, rows, caption)]);
  }

  var SPLIT_NAMES = { fit: "Fitting period", select: "Selection period", test: "Locked test" };

  function metricLabel(key) {
    var known = {
      n: "Observations", n_trades: "Trades", n_events: "Events", n_quarters: "Quarters",
      mae: "Average miss", mean_return_bps: "Average return per trade, in basis points (1 bp is 0.01%)",
      t_stat: "t-statistic (2 or more in size is unlikely to be chance)", t_nw: "t-statistic (2 or more in size is unlikely to be chance)",
      hit_rate: "Share of trades that made money", sharpe: "Return per unit of risk (Sharpe ratio)",
      p_value: "Chance of a result this large from noise alone (p-value)", recovery_4q: "Share recovered after four quarters"
    };
    return known[key] || humanize(key);
  }

  function metricValue(key, value) {
    if (!isNum(value)) return value == null ? fmt.missing : String(value);
    if (/^n(_|$)/.test(key)) return fmt.int(value);
    if (/(rate|share|recovery)/.test(key)) return fmt.pct(value);
    if (/bps$/.test(key)) return fmt.num(value, 1);
    return fmt.num(value, 2);
  }

  /** A table with one row per metric and one column per split, for by_split objects of any shape. */
  function splitTable(bySplit, caption) {
    if (!bySplit) return null;
    var splits = ["fit", "select", "test"].filter(function (s) { return bySplit[s] && typeof bySplit[s] === "object"; });
    var keys = [];
    splits.forEach(function (s) {
      Object.keys(bySplit[s]).forEach(function (k) {
        if (keys.indexOf(k) < 0 && typeof bySplit[s][k] !== "object") keys.push(k);
      });
    });
    if (!splits.length || !keys.length) return null;
    var columns = [{ label: "", value: function (k) { return metricLabel(k); } }].concat(splits.map(function (s) {
      return { label: SPLIT_NAMES[s], num: true, value: function (k) { return metricValue(k, bySplit[s][k]); } };
    }));
    return miniTable(columns, keys, caption);
  }

  function duration(ms) {
    if (!isNum(ms)) return "";
    return ms < 1000 ? Math.round(ms) + " ms" : fmt.num(ms / 1000, 1) + " s";
  }

  function signedPct(v) {
    return v === 0 ? "0%" : (v > 0 ? "+" : "−") + Math.abs(v) + "%";
  }

  // --------------------------------------------------------------- figures
  // Each entry returns {frame, spec}. ctx holds the loaded file contents by name.
  // frame.details() builds the drawer content; the app tile shows only the title.

  var figures = {
    fuel: function (ctx) {
      var meta = ctx.meta || {};
      var daily = ctx.cost_clock && ctx.cost_clock.daily_fuel;
      if (!daily && ctx.signal) daily = (ctx.signal.daily || []).map(function (d) { return [d.date, d.fuel]; });
      var g = meta.guidance || {};
      return {
        frame: {
          kicker: "Fuel against the plan",
          title: meta.fuel_headline || "Jet fuel since United's latest guidance, against the price it planned on",
          read: "The dark line is the market price of jet fuel each day. The flat line is the price United built into its guidance. This is the market price, not what United pays.",
          source: "US Energy Information Administration daily spot prices; United's guidance of " + fmt.day(g.made_on) + "."
        },
        spec: L.fuelAgainstPlan({ daily: daily || [], assumed: g.fuel_assumed_usd_gal, since: g.made_on })
      };
    },
    cost: function (ctx) {
      var d = ctx.cost_clock;
      return {
        frame: {
          kicker: "Fuel price to fuel bill",
          title: "Fuel price to fuel bill",
          headline: d.headline,
          key: isNum((d.fit || {}).slope) ? keyLine(fmt.usd(d.fit.slope), "paid per $1 of spot" + (isNum(d.fit.lag_days) ? " \u00b7 " + fmt.int(d.fit.lag_days) + "-day lag" : "")) : null,
          read: "The thin line is the daily market price. Each dark bar is the average price United reported paying over one quarter. The gap between them, and how long the bars take to follow the line, is what the simulator uses to turn a fuel price into a fuel bill.",
          source: "US Energy Information Administration daily spot prices; United's quarterly earnings releases.",
          details: function () {
            var fit = d.fit || {};
            var stats = el("dl", { class: "stat-row" });
            if (isNum(fit.lag_days)) stats.appendChild(stat("Typical delay", [fmt.int(fit.lag_days), el("small", { text: " days" })]));
            if (isNum(fit.basis_usd_gal)) stats.appendChild(stat("United pays above spot", [fmt.usdSigned(fit.basis_usd_gal), el("small", { text: " a gallon" })]));
            if (isNum(fit.slope)) stats.appendChild(stat("Passed through per $1 of spot", [fmt.usd(fit.slope), Array.isArray(fit.ci) ? el("small", { text: " (" + fmt.num(fit.ci[0]) + " to " + fmt.num(fit.ci[1]) + ")" }) : null]));
            if (isNum(fit.r2)) stats.appendChild(stat("Variation explained (R²)", [fmt.pct(fit.r2), isNum(fit.n) ? el("small", { text: " of " + fmt.int(fit.n) + " quarters" }) : null]));
            var mc = d.management_check;
            var check = mc ? miniTable([
              { label: "", value: function (row) { return row[0]; } },
              { label: "United said", num: true, value: function (row) { return row[1]; } },
              { label: "Rebuilt here", num: true, value: function (row) { return row[2]; } }
            ], [
              ["Extra fuel cost", fmt.usdMillions(mc.claimed_usd_m), fmt.usdMillions(mc.reproduced_usd_m)],
              ["Per share", fmt.usd(mc.claimed_eps), fmt.usd(mc.reproduced_eps)]
            ]) : null;
            return [
              summaryBlock("In plain words", d.plain_summary !== d.headline ? d.plain_summary : null),
              stats.firstChild ? labelled("The measured relationship", stats) : null,
              labelled("United's own arithmetic, rebuilt", check),
              labelled("Fit, period by period", splitTable(d.by_split, "Average miss in dollars per gallon")),
              listBlock("What this cannot tell you", Array.isArray(d.limits) ? d.limits : null),
              listBlock("Every version tried", d.variants_tried),
              inputsBlock(d)
            ];
          }
        },
        spec: L.costClock(d)
      };
    },
    fare: function (ctx) {
      var d = ctx.fare_clock;
      var rivals = d.rivals || {};
      // Management names calendar quarters. Where the builder has mapped its statements onto
      // quarters after the shock, they are drawn at those quarters; otherwise as levels.
      var said = Array.isArray(rivals.management) ? rivals.management : [];
      var pathName = (said.filter(function (m) { return m && m.path; })[0] || {}).path;
      var path = pathName && ((rivals.management_paths || {}).paths || {})[pathName];
      var mapped = path && Array.isArray(path.mapped) ? path.mapped.filter(function (m) { return !/^not stated/i.test(m.basis || ""); }) : [];
      // A second estimate, when its file exists: the same curve fitted on several airlines pooled.
      var peerFile = ctx.peer_recovery || null;
      var pooled = peerFile && peerFile.pooled && Array.isArray(peerFile.pooled.curve) ? peerFile.pooled.curve : [];
      var carriers = peerFile && peerFile.by_carrier && Array.isArray(peerFile.by_carrier.table) ? peerFile.by_carrier.table.length : 0;
      var peer = pooled.length ? {
        name: (carriers ? carriers + " airlines" : "Airlines") + " pooled",
        curve: pooled.map(function (c) { return { quarters_after: c.quarters_after, value: c.p50, lo: c.p10, hi: c.p90 }; })
      } : null;
      var spec = L.recoveryCurve(Object.assign({}, d, { rivals: Object.assign({}, rivals, mapped.length ? { management: mapped } : {}), peer: peer }));
      var last = (d.curve || []).filter(function (c) { return isNum(c.p50); }).pop();
      return {
        frame: {
          kicker: "Fuel cost to revenue",
          title: "Fuel cost to revenue",
          headline: d.headline,
          key: last ? keyLine(fmt.pct(last.p50), "won back after " + last.quarters_after + (last.quarters_after === 1 ? " quarter" : " quarters") + " \u00b7 " + fmt.pctRange(last.p10, last.p90)) : null,
          read: "Read left to right: of each extra dollar of fuel cost, the share won back through revenue in the same quarter and in each quarter after. The blue line is the middle estimate for United and the band its 10\u201390% range. Diamonds are what management has said, at the quarters it named. The gray line is the same curve fitted on several airlines together. The flat line at zero is no response at all.",
          source: "United's quarterly earnings releases; other airlines' releases for the pooled curve; published estimates as listed.",
          details: function () {
            var a = d.asymmetry || {};
            var stats = el("dl", { class: "stat-row" });
            if (isNum(a.up)) stats.appendChild(stat("Recovered when fuel rises", [fmt.pct(a.up)]));
            if (isNum(a.down)) stats.appendChild(stat("Given back when fuel falls", [fmt.pct(a.down)]));
            if (isNum(a.p_value)) stats.appendChild(stat("Chance the difference is noise", [fmt.pct(a.p_value)]));
            if (isNum(d.n)) stats.appendChild(stat("Quarters used", [fmt.int(d.n)]));
            var published = spec.published && spec.published.length ? el("ul", { class: "limits" }, spec.published.map(function (p) {
              return el("li", null, [
                p.url ? el("a", { href: p.url, target: "_blank", rel: "noopener noreferrer", text: p.label }) : el("span", { text: p.label }),
                isNum(p.value) ? el("span", { class: "num", text: " " + fmt.pct(p.value) }) : null
              ]);
            })) : null;
            var compare = peerFile && peerFile.comparison_with_united_only;
            var cross = el("dl", { class: "stat-row" });
            if (compare && isNum(compare.one_year_share_pooled)) cross.appendChild(stat("Pooled, a year on, best single fit", [fmt.pct(compare.one_year_share_pooled)]));
            if (compare && isNum(compare.one_year_share_united_only)) cross.appendChild(stat("United alone, best single fit", [fmt.pct(compare.one_year_share_united_only)]));
            var pooledRange = compare && compare.one_year_share_pooled_range;
            if (pooledRange && isNum(pooledRange.p05)) cross.appendChild(stat("Pooled, 90% range", [fmt.pctRange(pooledRange.p05, pooledRange.p95)]));
            var lags = null;
            if (Array.isArray(d.industry_monthly) && d.industry_monthly.length) {
              lags = el("div", { class: "tile-plot" });
              lags.appendChild(L.industryLags({ rows: d.industry_monthly }).render(404));
            }
            return [
              summaryBlock("In plain words", d.plain_summary !== d.headline ? d.plain_summary : null),
              textBlock("What \u201crecovered\u201d means here", d.recovery_definition),
              textBlock("The line and the band", d.curve_definition),
              mapped.length ? textBlock("Where management's numbers sit", (rivals.management_paths || {}).mapping) : null,
              peerFile ? textBlock("Cross-check on several airlines", peerFile.headline) : null,
              cross.firstChild ? cross : null,
              peerFile ? summaryBlock("The cross-check in plain words", peerFile.plain_summary !== peerFile.headline ? peerFile.plain_summary : null) : null,
              labelled("Published estimates", published),
              textBlock("About the published estimates", rivals.published_note),
              stats.firstChild ? labelled("Up against down", stats) : null,
              labelled("The same question on monthly industry fares", lags),
              labelled("Outside the fitting period", splitTable(d.by_split)),
              listBlock("What this cannot tell you", Array.isArray(d.limits) ? d.limits : null),
              listBlock("Every version tried", d.variants_tried),
              inputsBlock(d)
            ];
          }
        },
        spec: spec
      };
    },
    demand: function (ctx) {
      var d = ctx.demand_signal;
      var source = d.source || {};
      return {
        frame: {
          kicker: "Travel demand",
          title: "Travel demand",
          headline: d.headline,
          key: (function () {
            var latest = (d.daily || []).filter(function (row) { return isNum(row.yoy_7d); }).pop();
            return latest ? keyLine(fmt.pctSigned(latest.yoy_7d, 1), "against a year earlier \u00b7 " + fmt.dayShort(latest.date)) : null;
          })(),
          read: "Top: how many people pass through US airport security each day, smoothed over seven days. Bottom: the same number against the same week a year earlier, which takes out the seasons. This is all US air travel, not United alone.",
          source: (source.publisher || "US Transportation Security Administration") + ", " + (source.series || "daily checkpoint counts") + ".",
          details: function () {
            return [
              summaryBlock("In plain words", d.plain_summary),
              listBlock("What this signal cannot tell you", Array.isArray(d.limits) ? d.limits : d.limits ? [d.limits] : []),
              source.page ? el("p", { class: "drawer-actions" }, [el("a", { href: source.page, target: "_blank", rel: "noopener noreferrer", text: "Open the publisher's page" })]) : null,
              inputsBlock(d)
            ];
          }
        },
        spec: L.demandSignal({ daily: d.daily || [] })
      };
    },
    market: function (ctx) {
      var d = ctx.market_clock;
      var study = d.event_study || {};
      return {
        frame: {
          kicker: "Stock reaction",
          title: "Stock reaction",
          headline: d.headline,
          key: (function () {
            var united = (d.same_day || []).filter(function (row) { return row.ticker === "UAL" && isNum(row.beta); })[0];
            if (!united) return null;
            // The locked-test years when the file splits them out, otherwise every year.
            var test = united.by_split && united.by_split.test && isNum(united.by_split.test.beta) ? united.by_split.test : null;
            var years = test && test.start && test.end ? " \u00b7 " + String(test.start).slice(0, 4) + "\u2013" + String(test.end).slice(0, 4) : "";
            return keyLine(fmt.pctSigned((test || united).beta * 0.1, 1), "same day per 10% fuel rise" + years);
          })(),
          read: "Day 0 is a large one-day move in jet fuel. The line is the average move in United's shares, over and above the market, on the days around it. Where the band straddles zero, the move cannot be told apart from noise.",
          source: "Daily closing prices for airline shares and the S&P 500; US Energy Information Administration fuel prices.",
          details: function () {
            var sameDay = Array.isArray(d.same_day) && d.same_day.length ? miniTable([
              { label: "Airline", value: function (row) { return row.ticker; } },
              { label: "Move", note: "per 1% in fuel", num: true, value: function (row) { return fmt.num(row.beta, 2) + "%"; } },
              { label: "t-stat", num: true, value: function (row) { return fmt.num(row.t_nw, 1); } },
              { label: "Days", num: true, value: function (row) { return fmt.int(row.n); } }
            ], d.same_day, "A t-statistic of about 2 or more in size means the move is unlikely to be chance.") : null;
            var lagged = Array.isArray(d.lagged) && d.lagged.length ? miniTable([
              { label: "Trading days later", num: true, value: function (row) { return fmt.int(row.horizon_days); } },
              { label: "Further move per 1% in fuel", num: true, value: function (row) { return fmt.num(row.coef, 2) + "%"; } },
              { label: "t-stat", num: true, value: function (row) { return fmt.num(row.t_nw, 1); } }
            ], d.lagged) : null;
            return [
              summaryBlock("In plain words", d.plain_summary),
              isNum(study.n_events) ? textBlock("Events", fmt.int(study.n_events) + " large fuel moves" + (study.threshold ? ". A large move is: " + study.threshold : "") + ".") : null,
              labelled("Same-day move in each airline's shares", sameDay),
              labelled("Does the move continue on later days?", lagged),
              labelled("Period by period", splitTable(d.by_split)),
              listBlock("What this cannot tell you", Array.isArray(d.caveats) ? d.caveats : Array.isArray(d.limits) ? d.limits : null),
              listBlock("Every version tried", d.variants_tried),
              inputsBlock(d)
            ];
          }
        },
        spec: L.eventPath({ path: study.path || [] })
      };
    },
    fares: function (ctx) {
      var d = ctx.daily_fares_2022;
      return {
        frame: {
          kicker: "Daily fares, 2022",
          title: "Daily fares, 2022",
          headline: d.headline,
          key: (function () {
            var peak = (d.daily || []).filter(function (row) { return isNum(row.index_ua); }).sort(function (a, b) { return b.index_ua - a.index_ua; })[0];
            return peak ? keyLine(fmt.num(peak.index_ua, 0), "United fare index peak \u00b7 " + fmt.dayShort(peak.date)) : null;
          })(),
          read: "Top: daily fare indexes for United, its two largest rivals and all airlines through the 2022 fuel spike. Bottom: the jet fuel price on the same dates.",
          source: "Daily fare index as described in the file; US Energy Information Administration fuel prices.",
          details: function () {
            return [
              summaryBlock("In plain words", d.plain_summary),
              textBlock("Method", d.method),
              textBlock("The index", d.daily_note),
              listBlock("What this series cannot tell you", Array.isArray(d.limits) ? d.limits : d.limits ? [d.limits] : []),
              listBlock("Every version tried", d.variants_tried),
              inputsBlock(d)
            ];
          }
        },
        spec: L.dailyFares({ daily: d.daily || [] })
      };
    },
    capacity: function (ctx) {
      var d = ctx.capacity_response;
      var points = function (v) { return (v > 0 ? "+" : v < 0 ? "\u2212" : "") + fmt.num(Math.abs(v), 2); };
      return {
        frame: {
          kicker: "Capacity",
          title: "Capacity",
          headline: d.headline || "How United's capacity has moved after a fuel price change",
          key: (function () {
            var end = (d.curve || []).filter(function (c) { return isNum(c.p50); }).pop();
            return end ? keyLine(points(end.p50), "pts after " + end.quarters_after + " quarters \u00b7 " + points(end.p10) + " to " + points(end.p90)) : null;
          })(),
          read: "Read left to right: the change in United's capacity growth, in percentage points, for each 10% rise in the fuel price, in the same quarter and in each quarter after. The line is the middle estimate and the band its 10\u201390% range. Below zero means United flew less than it otherwise would have.",
          source: "United's quarterly capacity as reported; US Energy Information Administration fuel prices.",
          details: function () {
            return [
              summaryBlock("In plain words", d.plain_summary !== d.headline ? d.plain_summary : null),
              textBlock("The unit", d.unit),
              textBlock("In the simulator", "Off unless a run asks for it. When on, the simulator cuts capacity by this much after a fuel rise."),
              listBlock("What this cannot tell you", Array.isArray(d.limits) ? d.limits : d.limits ? [d.limits] : []),
              listBlock("Every version tried", d.variants_tried),
              inputsBlock(d)
            ];
          }
        },
        spec: L.bandLine({
          rows: d.curve || [], x: "quarters_after", y: "p50", lo: "p10", hi: "p90",
          name: "Middle estimate", bandName: "Its 10\u201390% range", xLabel: "Quarters after the fuel move",
          xTicks: (d.curve || []).map(function (c) { return c.quarters_after; }),
          xFormat: function (q) { return q === 0 ? "0" : "+" + q; },
          tipX: function (q) { return q === 0 ? "Same quarter" : q + (q === 1 ? " quarter later" : " quarters later"); },
          yFormat: points
        })
      };
    },
    track: function (ctx) {
      var d = ctx.track_record;
      return {
        frame: {
          kicker: "Replay on past quarters",
          title: "Replay on past quarters",
          headline: d.headline,
          read: "One row per quarter in which United gave guidance, most recent first. The gray bar is the range United guided to and the dark tick is what it then reported. Each colored mark is one forecaster's number made on the guidance date; the blue line is the 10–90% range of the simulator's measured behavior. The closer a mark sits to the dark tick, the better.",
          source: "United's guidance and earnings releases filed with the SEC. Every forecast uses only data published before its guidance date."
        },
        spec: L.trackRecord(d)
      };
    },
    distribution: function (ctx) {
      var d = ctx.q3_call;
      var meta = ctx.meta || {};
      var g = meta.guidance || {};
      var consensus = ((meta.comparators || {}).consensus_eps || {}).value;
      var eps = (d.chain || []).filter(function (c) { return c.link === "adj_eps_diluted"; })[0] || {};
      var spec = L.epsDistribution({ quantiles: d.eps_samples_quantiles, guidance: [g.eps_low, g.eps_high], consensus: consensus, median: eps.p50 });
      return {
        frame: {
          kicker: "The estimate",
          title: d.headline || "The estimate of " + fmt.period(g.period) + " earnings per share, against United's guidance",
          read: spec.shape
            ? "The shape is the whole estimate, not one number: where it is taller, that result is more likely. Its colors split at the edges of United's guidance range. The shape ends at the outermost points the estimate reports."
            : "The estimate reports too few points to draw a shape, so the points themselves are shown with United's guidance range behind them.",
          source: "The frozen estimate; United's guidance; analyst consensus as cited."
        },
        spec: spec
      };
    },
    daily: function (ctx, options) {
      var d = ctx.signal;
      var meta = ctx.meta || {};
      var g = meta.guidance || {};
      return {
        frame: {
          kicker: "Day by day",
          title: d.headline,
          read: "The middle estimate with its 10–90% range, recomputed each trading day with only the data available that day, against United's guidance range and the analyst consensus.",
          source: "Daily fuel and share prices; the measured fare response; United's guidance."
        },
        spec: L.dailyEstimate({ daily: d.daily, guidance: [g.eps_low, g.eps_high], consensus: ((meta.comparators || {}).consensus_eps || {}).value }, options)
      };
    }
  };

  function tile(host, name, ctx, result, extra) {
    var built = figures[name](ctx);
    return L.panel(host, Object.assign({ dataHref: result && result.url }, built.frame, extra || {}), built.spec);
  }

  // ------------------------------------------------------------ 1 simulator

  var SIM_DEFAULT = {
    fuel_shock_pct: 0, fuel_shock_months: 6, capacity_change_pct: 0, demand_shock_pct: 0,
    delivery_delay: { aircraft: 0, months: 0 }, horizon_months: 12, seed: 1
  };
  var sim = {
    request: null, response: null, sequence: 0, today: null, preset: null, reference: null, ctx: {},
    view: { metric: "operating_income_musd", visible: { costs_only: true, management: true, measured: true } },
    tab: "whatif", threshold: { breaks: 0, target: 0 }, grid: {}, main: null, runs: {}, retry: 0, gridRetry: 0
  };

  // Presets are starting points, not findings: each sets the controls and runs.
  var PRESETS = [
    { id: "today", label: "Today", needsToday: true, what: "Fuel held at its latest level for a year.",
      build: function (t) { return { fuel_shock_pct: t.pct, fuel_shock_months: t.months || 12 }; } },
    { id: "fuel_back", label: "Fuel falls back", what: "Fuel returns to the simulator's reference price.",
      build: function () { return { fuel_shock_pct: 0 }; } },
    { id: "demand", label: "Demand weakens", what: "Today's fuel, with travel demand 5% lower.",
      build: function (t) { return { fuel_shock_pct: t ? t.pct : 0, fuel_shock_months: 12, demand_shock_pct: -5 }; } },
    { id: "deliveries", label: "Deliveries slip", what: "Today's fuel, with 20 aircraft arriving six months late.",
      build: function (t) { return { fuel_shock_pct: t ? t.pct : 0, fuel_shock_months: 12, delivery_delay: { aircraft: 20, months: 6 } }; } },
    { id: "capacity", label: "Capacity cut", what: "Today's fuel, with United flying 3% fewer seats.",
      build: function (t) { return { fuel_shock_pct: t ? t.pct : 0, fuel_shock_months: 12, capacity_change_pct: -3 }; } }
  ];

  function months(v) {
    return v + " mo";
  }

  function describeRequest(q) {
    var delay = q.delivery_delay || {};
    var parts = ["Fuel " + (q.fuel_shock_pct ? signedPct(q.fuel_shock_pct) + " for " + q.fuel_shock_months + " months" : "unchanged")];
    parts.push("demand " + (q.demand_shock_pct ? signedPct(q.demand_shock_pct) : "unchanged"));
    parts.push("capacity " + (q.capacity_change_pct ? signedPct(q.capacity_change_pct) : "unchanged"));
    parts.push(delay.aircraft ? delay.aircraft + " aircraft " + (delay.months || 0) + " months late" : "no delivery delay");
    return parts.join("; ") + ". Horizon " + (q.horizon_months || 12) + " months.";
  }

  function readSimForm() {
    var form = $("sim-form");
    var n = function (name) { return Number(form.elements[name].value); };
    return {
      fuel_shock_pct: n("fuel_shock_pct"), fuel_shock_months: n("fuel_shock_months"),
      capacity_change_pct: n("capacity_change_pct"), demand_shock_pct: n("demand_shock_pct"),
      delivery_delay: { aircraft: n("delay_aircraft"), months: n("delay_months") },
      horizon_months: n("horizon_months"), seed: SIM_DEFAULT.seed
    };
  }

  function writeSimOutputs() {
    var q = readSimForm();
    $("sim-fuel-out").textContent = signedPct(q.fuel_shock_pct) + (isNum(sim.reference) ? " · " + fmt.usd(sim.reference * (1 + q.fuel_shock_pct / 100)) : "");
    $("sim-months-out").textContent = months(q.fuel_shock_months);
    $("sim-demand-out").textContent = signedPct(q.demand_shock_pct);
    $("sim-capacity-out").textContent = signedPct(q.capacity_change_pct);
    $("sim-aircraft-out").textContent = String(q.delivery_delay.aircraft);
    $("sim-delay-out").textContent = months(q.delivery_delay.months);
    $("sim-horizon-out").textContent = months(q.horizon_months);
  }

  function setSimForm(q) {
    var form = $("sim-form");
    if (!form || !q) return;
    var delay = q.delivery_delay || {};
    var set = function (name, value) {
      var input = form.elements[name];
      if (!input || value == null || !isNum(Number(value))) return;
      input.value = Math.max(Number(input.min), Math.min(Number(input.max), Number(value)));
    };
    var pct = q.fuel_shock_pct;
    if (pct == null && isNum(q.fuel_usd_gal) && isNum(sim.reference)) pct = (q.fuel_usd_gal / sim.reference - 1) * 100;
    set("fuel_shock_pct", pct == null ? null : Math.round(pct));
    set("fuel_shock_months", q.fuel_shock_months);
    set("capacity_change_pct", q.capacity_change_pct);
    set("demand_shock_pct", q.demand_shock_pct);
    set("delay_aircraft", delay.aircraft);
    set("delay_months", delay.months);
    set("horizon_months", q.horizon_months);
    writeSimOutputs();
  }

  function looksLikeScenarioRequest(o) {
    return !!o && typeof o === "object" && ("fuel_shock_pct" in o || "fuel_usd_gal" in o || "capacity_change_pct" in o || "demand_shock_pct" in o || "delivery_delay" in o);
  }

  function scenarioOf(o) {
    if (!o || typeof o !== "object") return null;
    if (o.modes) return o;
    if (o.result && o.result.modes) return o.result;
    if (o.scenario && o.scenario.modes) return o.scenario;
    return null;
  }

  function modeReady(response, id) {
    var mode = response && response.modes && response.modes[id];
    return !!mode && Array.isArray(mode.quarters) && mode.quarters.length > 0;
  }

  /** The behavior the tiles and the grid follow: the first one switched on and computed. */
  function focusMode() {
    var order = ["measured", "management", "costs_only"];
    var pick = order.filter(function (id) { return sim.view.visible[id] !== false && modeReady(sim.response, id); })[0];
    return L.modes.filter(function (m) { return m.id === pick; })[0] || null;
  }

  function simStatus(text, title) {
    var status = $("sim-status");
    if (!status) return;
    status.textContent = text || "";
    status.title = title || text || "";
  }

  function setBusy(busy) {
    [$("outcomes"), $("main-body"), $("fig-bridge")].forEach(function (node) {
      if (node) node.classList.toggle("is-refreshing", !!busy && !!node.firstChild);
    });
  }

  // Outcome tiles -------------------------------------------------------------

  var OUTCOMES = [
    { metric: "revenue_musd", label: "Revenue", format: function (v) { return fmt.usdCompact(v); }, optional: true },
    { metric: "operating_income_musd", label: "Operating income", format: function (v) { return fmt.usdCompact(v); },
      delta: function (mode) {
        var change = ((mode.next_12m || {}).operating_income_change_musd || {}).p50;
        return isNum(change) ? fmt.usdCompactSigned(change) + " vs no shock" : null;
      } },
    { metric: "operating_margin", label: "Operating margin", quarter: true, format: function (v) { return fmt.pct(v, 1); },
      delta: function (mode, q) {
        var base = (((q || {}).no_shock || {}).operating_margin || {}).p50;
        var now = ((q || {}).operating_margin || {}).p50;
        if (!isNum(base) || !isNum(now)) return null;
        var points = (now - base) * 100;
        return (Math.abs(points) < 0.05 ? "0.0" : (points > 0 ? "+" : "\u2212") + fmt.num(Math.abs(points), 1)) + " pts vs no shock";
      } },
    { metric: "fcf_musd", label: "Free cash flow", format: function (v) { return fmt.usdCompact(v); },
      delta: function (mode) {
        var p = (mode.prob || {}).fcf_negative_12m;
        return isNum(p) ? fmt.pct(p) + " of runs negative" : null;
      } },
    { metric: "eps_change", label: "EPS change", format: function (v) { return fmt.usdSigned(v); },
      delta: function () { return "vs no shock"; } }
  ];

  function renderOutcomes() {
    var host = $("outcomes");
    var response = sim.response;
    var mode = focusMode();
    host.replaceChildren();
    if (!response || !mode) {
      host.appendChild(el("div", { class: "outcome is-empty" }, [
        el("span", { class: "outcome-label", text: "Outcomes" }),
        el("span", { class: "outcome-value", text: response ? "Switch a behavior on" : "Waiting for a run" })
      ]));
      return;
    }
    var data = response.modes[mode.id];
    var quarters = data.quarters;
    var year = data.next_12m || null;
    var fourth = quarters[Math.min(3, quarters.length - 1)];
    var has = function (metric) {
      return (year && year[metric] && isNum(year[metric].p50)) || quarters.some(function (q) { return q[metric] && isNum(q[metric].p50); });
    };
    var shown = OUTCOMES.filter(function (o) { return !o.optional || has(o.metric); });
    if (shown.length > 4) shown = shown.filter(function (o) { return o.metric !== "operating_income_musd"; });
    shown = shown.slice(0, 4);
    // The chart can only show a measure the tiles offer.
    if (!shown.some(function (o) { return o.metric === sim.view.metric; })) sim.view.metric = shown[0].metric;
    shown.forEach(function (o) {
      var useQuarter = o.quarter || !year || !year[o.metric];
      var value = useQuarter ? fourth[o.metric] : year[o.metric];
      var period = useQuarter ? fmt.period(fourth.period) : "next 12 months";
      var swatch = el("i");
      swatch.style.background = mode.color;
      var button = el("button", { type: "button", class: "outcome", "aria-pressed": String(sim.view.metric === o.metric),
        title: o.label + ", " + period + ", with fares responding \u201c" + mode.name.toLowerCase() + "\u201d. Click to chart it." });
      button.appendChild(el("span", { class: "outcome-label" }, [swatch, o.label]));
      button.appendChild(el("span", { class: "outcome-period", text: period }));
      if (!value || !isNum(value.p50)) {
        button.classList.add("is-empty");
        button.appendChild(el("span", { class: "outcome-value", text: "Not computed yet" }));
      } else {
        button.appendChild(el("span", { class: "outcome-value", text: o.format(value.p50) }));
        var range = value.p10 < 0 || value.p90 < 0 ? o.format(value.p10) + " to " + o.format(value.p90) : fmt.range(value.p10, value.p90, o.format);
        button.appendChild(el("span", { class: "outcome-range", text: range, title: "Middle 80% of simulated paths: " + range }));
        var delta = o.delta ? o.delta(data, fourth) : null;
        if (delta) button.appendChild(el("span", { class: "outcome-delta", text: delta, title: delta }));
      }
      button.addEventListener("click", function () {
        sim.view.metric = o.metric;
        Array.prototype.forEach.call(host.querySelectorAll(".outcome"), function (b) { b.setAttribute("aria-pressed", String(b === button)); });
        if (sim.tab !== "whatif") selectTab("whatif");
        else if (sim.main) sim.main.redraw();
      });
      host.appendChild(button);
    });
  }

  // The main chart: three questions ---------------------------------------------

  var GRID = {
    x: { driver: "fuel_shock_pct", values: [-20, -10, 0, 10, 20, 30, 40, 50, 60], label: "Fuel price" },
    y: { driver: "demand_shock_pct", values: [-9, -6, -3, 0, 3], label: "Travel demand" },
    metric: "fcf_12m_musd", metricLabel: "12-month free cash flow"
  };
  var DRIVERS = {
    fuel_shock_pct: { label: "Fuel price", format: signedPct },
    fuel_usd_gal: { label: "Fuel, $ a gallon", format: function (v) { return fmt.usd(v); } },
    demand_shock_pct: { label: "Travel demand", format: signedPct },
    capacity_change_pct: { label: "Capacity", format: signedPct },
    delivery_delay_months: { label: "Delivery delay, months", format: function (v) { return String(v); } },
    recovery_share: { label: "Share of fuel cost recovered", format: function (v) { return fmt.pct(v); } }
  };
  var GRID_METRICS = {
    fcf_12m_musd: { label: "12-month free cash flow", format: function (v) { return fmt.usdCompact(v); } },
    operating_income_12m_musd: { label: "12-month operating income", format: function (v) { return fmt.usdCompact(v); } },
    operating_margin: { label: "Operating margin", format: function (v) { return fmt.pct(v, 1); } },
    eps_change: { label: "Change in earnings per share", format: function (v) { return fmt.usdSigned(v); } }
  };

  /** A grid result as a heatmap spec, for the main chart and for the console. */
  function gridSpec(data, marker) {
    var x = DRIVERS[(data.x || {}).driver] || DRIVERS.fuel_shock_pct;
    var y = DRIVERS[(data.y || {}).driver] || DRIVERS.demand_shock_pct;
    var metric = GRID_METRICS[data.metric] || GRID_METRICS.fcf_12m_musd;
    return L.gridHeat(data, {
      xLabel: x.label, yLabel: y.label, xFormat: x.format, yFormat: y.format, valueFormat: metric.format, marker: marker,
      share: data.direction === "below" ? "Runs below the line" : "Runs that meet the target"
    });
  }

  function infoButton(label, open) {
    return el("button", { type: "button", class: "info-btn", "aria-label": label, title: label, text: "i", onclick: open });
  }

  function scenarioDetails(response, request) {
    var nodes = [];
    if (request) nodes.push(textBlock("This scenario", describeRequest(request)));
    var fuel = response.fuel || {};
    if (isNum(fuel.reference_usd_gal)) {
      nodes.push(textBlock("Fuel", "Reference " + fmt.usd(fuel.reference_usd_gal) + " a gallon; this run " + fmt.usd(fuel.level_usd_gal) + " for " + fuel.held_months + " months" + (fuel.after_hold ? ", then " + fuel.after_hold : "") + "."));
    }
    var used = response.response_used || {};
    var curves = el("ul", { class: "limits" });
    L.modes.forEach(function (m) {
      var entry = used[m.id] || (m.id === "measured" && Array.isArray(used.curve_p50) ? used : null);
      if (!entry || !Array.isArray(entry.curve_p50)) return;
      curves.appendChild(el("li", { text: m.name + ": " + entry.curve_p50.map(function (v) { return fmt.pct(v); }).join(" → ") + (entry.note ? ". " + entry.note : "") }));
    });
    if (curves.firstChild) nodes.push(labelled("Share of extra fuel cost won back, by quarter", curves));
    L.modes.forEach(function (m) {
      var mode = (response.modes || {})[m.id];
      if (mode && mode.status === "not_computed") nodes.push(textBlock(m.name + ": not computed yet", mode.reason || "This behavior has not been built on this server."));
    });
    if (response.synthetic_response) nodes.push(textBlock("Synthetic response", "At least one behavior in this run uses a stand-in curve, not a measured one. Treat its numbers as a wiring test."));
    if (sim.preset === "today" && response === sim.response && sim.today && Array.isArray(sim.today.basis) && sim.today.basis.length) {
      nodes.push(listBlock("Where \u201cToday\u201d comes from", sim.today.basis.map(function (b) { return b && b.how; }).filter(Boolean)));
    }
    nodes.push(listBlock("What this run assumes", response.assumptions));
    var run = [];
    if (response.run_id) run.push("Run " + response.run_id);
    if (isNum(response.paths)) run.push(fmt.int(response.paths) + " simulated paths");
    if (isNum(response.elapsed_ms)) run.push(duration(response.elapsed_ms));
    if (run.length) nodes.push(labelled("Run", el("p", { class: "hash", text: run.join(" · ") })));
    return nodes;
  }

  function downloadLink(object, label) {
    try {
      var href = URL.createObjectURL(new Blob([JSON.stringify(object, null, 1)], { type: "application/json" }));
      return el("a", { href: href, download: "scenario.json", text: label });
    } catch (error) {
      return null;
    }
  }

  function renderWhatIf() {
    var body = $("main-body");
    var tools = $("main-tools");
    tools.replaceChildren();
    var response = sim.response;
    if (!response) return;
    var spec = L.scenarioFan({ modes: response.modes || {} }, sim.view);
    sim.main = L.panel(body, { bare: true, info: false, legend: false }, spec);
    tools.appendChild(infoButton("About this chart: how to read it, assumptions and data", function () {
      L.openDrawer("What if", [
        el("h3", { text: response.headline || "One scenario under each way fares might respond" }),
        L.legendList(spec.legend),
        textBlock("How to read it", "Each line is the middle of the simulated paths under one behavior and its band holds the middle 80% of them. The shock, the fleet and the random draws are identical across the three, so any gap between lines is caused by how fares respond. The gray line is the same airline with no shock. Click an outcome tile to chart that outcome.")
      ].concat(scenarioDetails(response, sim.request)).concat([
        el("p", { class: "drawer-actions" }, [downloadLink(response, "Download this run (JSON)")]),
        labelled("The numbers", el("div", { class: "fig-table", tabindex: "0" }, [L.table(spec.table.columns, spec.table.rows)]))
      ]));
    }));
  }

  function gridRequest(kind) {
    var q = sim.request || readSimForm();
    var mode = focusMode();
    return {
      x: { driver: GRID.x.driver, values: GRID.x.values }, y: { driver: GRID.y.driver, values: GRID.y.values },
      metric: GRID.metric, threshold: sim.threshold[kind], direction: kind === "breaks" ? "below" : "at_or_above",
      mode: mode ? mode.id : "measured",
      base: { fuel_shock_months: q.fuel_shock_months, capacity_change_pct: q.capacity_change_pct, delivery_delay: q.delivery_delay,
        horizon_months: q.horizon_months, seed: q.seed }
    };
  }

  // Where the boundary crosses a given demand level, by straight-line interpolation.
  function boundaryAt(boundary, y) {
    var points = (boundary || []).filter(function (b) { return isNum(b.x) && isNum(b.y); }).sort(function (a, b) { return a.y - b.y; });
    for (var i = 0; i < points.length - 1; i++) {
      var a = points[i];
      var b = points[i + 1];
      if (y >= a.y && y <= b.y && b.y !== a.y) return a.x + (b.x - a.x) * (y - a.y) / (b.y - a.y);
    }
    return null;
  }

  function renderGrid(kind) {
    var body = $("main-body");
    var tools = $("main-tools");
    var request = gridRequest(kind);
    var key = JSON.stringify(request);
    var input = el("input", { type: "number", step: "250", value: String(sim.threshold[kind]), "aria-label": GRID.metricLabel + (kind === "breaks" ? " below" : " above") + ", in millions of dollars" });
    input.addEventListener("change", function () {
      var value = Number(input.value);
      if (!isNum(value)) return;
      sim.threshold[kind] = value;
      renderGrid(kind);
    });
    tools.replaceChildren(el("label", { class: "threshold", title: GRID.metricLabel + (kind === "breaks" ? " below" : " at or above") + " this many million dollars" },
      [kind === "breaks" ? "Cash below $m" : "Cash above $m", input]));
    sim.main = null;
    sim.gridTicket = (sim.gridTicket || 0) + 1;
    var ticket = sim.gridTicket;
    clearTimeout(sim.gridRetry);
    if (body.firstChild) body.classList.add("is-refreshing");
    else L.state(body, "loading", { compact: true });

    var source = FIXTURES ? load(kind === "breaks" ? "grid" : "grid_target").then(function (r) {
      return r.status === "ok" ? r.data : { __state: r.status === "missing" ? "missing" : "error", text: r.status === "missing" ? "The placeholder grid has not been written." : r.message };
    }) : (sim.grid[key] || (sim.grid[key] = call("/grid", request, "The grid")));

    source.then(function (data) {
      if (ticket !== sim.gridTicket || sim.tab !== kind) return;
      if (data.__state === "busy") {
        delete sim.grid[key];
        L.state(body, "loading", { compact: true, title: "Waiting " + data.retryAfter + " s", text: data.text + " This one starts by itself." });
        body.classList.remove("is-refreshing");
        sim.gridRetry = setTimeout(function () { if (ticket === sim.gridTicket && sim.tab === kind) renderGrid(kind); }, data.retryAfter * 1000 + 200);
        return;
      }
      body.classList.remove("is-refreshing");
      if (data.__state || !Array.isArray(data.cells)) {
        delete sim.grid[key];
        var notBuilt = data.status === "not_computed";
        L.state(body, data.__state === "error" && !notBuilt ? "error" : "missing", { compact: true,
          text: data.text || data.reason || (notBuilt ? "This grid has not been built on this server yet." : "The grid came back empty."), file: FIXTURES ? null : "POST " + API + "/grid" });
        return;
      }
      if (!FIXTURES && hasFixtureFlag(data)) {
        L.state(body, "error", { compact: true, text: "The server returned placeholder data, so it is not shown as a result." });
        return;
      }
      var q = sim.request || readSimForm();
      var mode = L.modes.filter(function (m) { return m.id === (data.mode || request.mode); })[0];
      var spec = gridSpec(Object.assign({ x: { driver: GRID.x.driver }, y: { driver: GRID.y.driver }, metric: GRID.metric, direction: request.direction }, data),
        { x: q.fuel_shock_pct, y: q.demand_shock_pct });
      var edge = boundaryAt(data.boundary, q.demand_shock_pct);
      var readout = el("p", { class: "readout" });
      if (edge != null) {
        readout.append(kind === "breaks" ? "Breaks past fuel " : "Holds up to fuel ", el("b", { text: signedPct(Math.round(edge)) }),
          ", demand " + signedPct(q.demand_shock_pct));
        readout.title = "Where half of the simulated runs fall on each side, with demand as set on the left" + (mode ? " and fares responding \u201c" + mode.name.toLowerCase() + "\u201d" : "") + ".";
      } else {
        readout.textContent = (kind === "breaks" ? "Share of runs below " : "Share of runs above ") + fmt.usdCompact(sim.threshold[kind]);
      }
      var plotHost = el("div", { class: "tab-body" });
      body.replaceChildren(readout, plotHost);
      sim.main = L.panel(plotHost, { bare: true, info: false, legend: false }, spec);
      tools.appendChild(infoButton("About this grid: how to read it and its limits", function () {
        L.openDrawer(kind === "breaks" ? "What breaks" : "What must be true", [
          el("h3", { text: kind === "breaks" ? "Where " + GRID.metricLabel + " falls below " + fmt.usdCompact(sim.threshold[kind]) : "Where " + GRID.metricLabel + " stays above " + fmt.usdCompact(sim.threshold[kind]) }),
          L.legendList(spec.legend),
          textBlock("How to read it", "Each cell is one tested combination of fuel price and travel demand, with every other control as set on the left. The number is the share of simulated runs " +
            (kind === "breaks" ? "that end below the line you set." : "that meet the target you set.") + " The dark line is where that share is one half. The ring is the scenario on the left."),
          textBlock("What the share is not", data.prob_definition || "It is the share of simulated runs at that tested point. It is not the probability of the event in the world."),
          data.boundary_method ? textBlock("The line", data.boundary_method) : null,
          listBlock("Notes on this grid", data.notes),
          data.synthetic_response ? textBlock("Synthetic response", "This grid uses a stand-in response curve, not a measured one. Treat it as a wiring test.") : null,
          isNum(data.paths) ? textBlock("Run", fmt.int(data.paths) + " simulated paths per cell" + (isNum(data.elapsed_ms) ? "; " + duration(data.elapsed_ms) : "") + ".") : null,
          el("p", { class: "drawer-actions" }, [downloadLink(data, "Download this grid (JSON)")]),
          labelled("The numbers", el("div", { class: "fig-table", tabindex: "0" }, [L.table(spec.table.columns, spec.table.rows)]))
        ]);
      }));
    });
  }

  function renderMain() {
    var body = $("main-body");
    body.setAttribute("aria-labelledby", "tab-" + sim.tab);
    if (!sim.response) return;
    if (sim.tab === "whatif") {
      body.classList.remove("is-refreshing");
      renderWhatIf();
    } else {
      renderGrid(sim.tab);
    }
  }

  function selectTab(name) {
    sim.tab = name;
    Array.prototype.forEach.call(document.querySelectorAll("#main-tabs [role=tab]"), function (tab) {
      var on = tab.getAttribute("data-tab") === name;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
    });
    $("main-body").replaceChildren();
    renderMain();
  }

  function initTabs() {
    var tabs = Array.prototype.slice.call(document.querySelectorAll("#main-tabs [role=tab]"));
    tabs.forEach(function (tab, i) {
      tab.addEventListener("click", function () { selectTab(tab.getAttribute("data-tab")); });
      tab.addEventListener("keydown", function (event) {
        var step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
        if (!step) return;
        var next = tabs[(i + step + tabs.length) % tabs.length];
        next.focus();
        selectTab(next.getAttribute("data-tab"));
      });
    });
  }

  function renderBridge() {
    var host = $("fig-bridge");
    var response = sim.response;
    var mode = focusMode();
    if (!response) return;
    var steps = (mode && Array.isArray((response.modes[mode.id] || {}).bridge) && response.modes[mode.id].bridge) ||
      (Array.isArray(response.bridge_per_share) ? response.bridge_per_share : []);
    var spec = L.bridge({ steps: steps });
    L.panel(host, {
      title: response.bridge_headline || "EPS change, step by step",
      kicker: "Per-share bridge",
      read: "Start at the top. Each bar begins where the one above ended, so the steps add up. The dark bar is the net effect over the next 12 months" + (mode ? ", under “" + mode.name.toLowerCase() + "”." : "."),
      details: function () {
        var per = response.per_share || {};
        return [
          spec.skipped && spec.skipped.length ? textBlock("Not modeled", spec.skipped.join(", ") + ". The simulator has no value for " + (spec.skipped.length === 1 ? "this step" : "these steps") + ", so " + (spec.skipped.length === 1 ? "it is" : "they are") + " left out, not set to zero.") : null,
          per.method ? textBlock("From dollars to per share", per.method) : null,
          textBlock("Why the net can differ from the tile", "Each step here is an average across the simulated paths, so the steps add up. The tile above shows the middle path, which need not equal the average.")
        ];
      },
      source: "The same simulator run."
    }, spec);
  }

  function renderModeKey() {
    var host = $("mode-key");
    host.replaceChildren();
    L.modes.forEach(function (m) {
      var input = el("input", { type: "checkbox", "aria-label": "Show " + m.name });
      input.checked = sim.view.visible[m.id] !== false;
      input.addEventListener("change", function () {
        sim.view.visible[m.id] = input.checked;
        renderOutcomes();
        renderBridge();
        if (sim.tab === "whatif" && sim.main) sim.main.redraw();
        else renderMain();
      });
      var swatch = el("span", { class: "mode-swatch" });
      swatch.style.setProperty("--c", m.color);
      var row = el("label", { class: "mode", title: m.note }, [input, swatch, el("span", { class: "mode-name", text: m.name })]);
      if (sim.response && !modeReady(sim.response, m.id)) row.appendChild(el("span", { class: "mode-note", text: "not computed" }));
      host.appendChild(row);
    });
  }

  function showScenario(response, request) {
    sim.response = response;
    if (response.fuel && isNum(response.fuel.reference_usd_gal)) sim.reference = response.fuel.reference_usd_gal;
    setBusy(false);
    writeSimOutputs();
    renderModeKey();
    renderOutcomes();
    renderMain();
    renderBridge();
  }

  function simUnavailable(kind, text, file) {
    sim.response = null;
    sim.main = null;
    setBusy(false);
    renderOutcomes();
    $("main-tools").replaceChildren();
    L.state($("main-body"), kind, { text: text, file: file });
    var bridge = $("fig-bridge");
    bridge.replaceChildren(L.state(null, kind, { compact: true, text: "Waiting for a simulator run." }));
  }

  function runSimulation() {
    var request = readSimForm();
    var key = JSON.stringify(request);
    var ticket = ++sim.sequence;
    sim.request = request;
    clearTimeout(sim.retry);
    setBusy(true);
    simStatus("Running");

    var source = FIXTURES ? load("scenario").then(function (r) {
      return r.status === "ok" ? r.data : { __state: r.status === "missing" ? "missing" : "error", text: r.status === "missing" ? "The placeholder scenario has not been written." : r.message, file: r.url };
    }) : (sim.runs[key] || (sim.runs[key] = call("/scenario", request, "The simulator")));

    return source.then(function (data) {
      if (ticket !== sim.sequence) return null;
      if (data.__state === "busy") {
        // Keep the last result on screen, dimmed, and run again when the server allows it.
        delete sim.runs[key];
        simStatus("Waiting " + data.retryAfter + " s", data.text + " This run starts by itself in " + data.retryAfter + " seconds.");
        sim.retry = setTimeout(function () { if (ticket === sim.sequence) runSimulation(); }, data.retryAfter * 1000 + 200);
        return null;
      }
      if (data.__state) {
        delete sim.runs[key];
        simUnavailable(data.__state, data.text, data.file || "POST " + API + "/scenario");
        simStatus(data.__state === "missing" ? "Simulator not available" : "Run failed", data.text);
        return null;
      }
      if (!FIXTURES && hasFixtureFlag(data)) {
        simUnavailable("error", "The simulator returned placeholder data, so it is not shown as a result.");
        simStatus("Run failed");
        return null;
      }
      if (!data.modes) {
        delete sim.runs[key];
        var notBuilt = data.status === "not_computed";
        simUnavailable(notBuilt ? "missing" : "error", data.reason || (notBuilt ? "The simulator has not been built on this server yet." : "The simulator's answer had no results in it."), "POST " + API + "/scenario");
        simStatus(notBuilt ? "Not computed yet" : "Run failed");
        return null;
      }
      showScenario(data, request);
      var parts = [];
      if (FIXTURES) parts.push("placeholder run");
      else if (isNum(data.paths)) parts.push(fmt.int(data.paths) + " paths");
      if (data.synthetic_response) parts.push("synthetic curve");
      simStatus(parts.join(" \u00b7 "), FIXTURES ? "Placeholder response: the controls do not change these numbers until the simulator is connected." :
        (data.run_id ? "Run " + data.run_id + ". " : "") + (isNum(data.elapsed_ms) ? "Took " + duration(data.elapsed_ms) + ". " : "") +
        (data.synthetic_response ? "A stand-in response curve is in use, not a measured one." : ""));
      return data;
    });
  }

  function markPreset(id) {
    sim.preset = id;
    Array.prototype.forEach.call(document.querySelectorAll("#preset-row button"), function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.preset === id));
    });
  }

  function applyPreset(preset) {
    setSimForm(Object.assign({}, SIM_DEFAULT, preset.build(sim.today)));
    markPreset(preset.id);
    return runSimulation();
  }

  /** What the server says today's scenario is (GET /api/lab/today), or null. */
  function loadToday() {
    var from = FIXTURES ? load("today").then(function (r) { return r.status === "ok" ? r.data : null; })
      : fetch(API + "/today", { cache: "no-cache", headers: { Accept: "application/json" } }).then(function (response) {
        return response.ok ? response.json() : null;
      });
    return from.then(function (data) {
      var fuel = data && data.status === "ok" && data.context && data.context.fuel;
      if (!fuel || !isNum(fuel.reference_usd_gal) || !isNum(fuel.shock_pct) || (!FIXTURES && hasFixtureFlag(data))) return null;
      return {
        date: fuel.date, spot: fuel.spot_usd_gal, reference: fuel.reference_usd_gal,
        level: isNum(fuel.level_usd_gal) ? fuel.level_usd_gal : fuel.reference_usd_gal * (1 + fuel.shock_pct / 100),
        pct: Math.round(fuel.shock_pct), months: (data.request || {}).fuel_shock_months, basis: data.basis, assumptions: data.assumptions
      };
    }, function () { return null; });
  }

  /**
   * Fallback when the server cannot say: the reference price moved by the change in the
   * market price since the simulator's last fuel observation.
   */
  function todayContext(ctx, response) {
    var series = ctx.cost_clock && Array.isArray(ctx.cost_clock.daily_fuel) ? ctx.cost_clock.daily_fuel
      : ctx.signal && Array.isArray(ctx.signal.daily) ? ctx.signal.daily.map(function (d) { return [d.date, d.fuel]; }) : [];
    series = series.filter(function (d) { return isNum(d[1]); });
    var reference = response && response.fuel && response.fuel.reference_usd_gal;
    var cutoff = response && response.as_of && (response.as_of.fuel_last_observation || response.as_of.information_cutoff);
    if (!series.length || !isNum(reference) || reference <= 0 || !cutoff) return null;
    var before = series.filter(function (d) { return d[0] <= cutoff; }).pop();
    var last = series[series.length - 1];
    if (!before) return null;
    var level = reference + last[1] - before[1];
    return { date: last[0], spot: last[1], reference: reference, level: level, pct: Math.round((level / reference - 1) * 100), months: 12 };
  }

  function initSimulation(r) {
    var form = $("sim-form");
    if (!form) return;
    var timer = 0;
    sim.ctx = r.ctx;

    var row = $("preset-row");
    PRESETS.forEach(function (preset) {
      row.appendChild(el("button", {
        type: "button", class: "chip", text: preset.label, title: preset.what, "aria-pressed": "false",
        disabled: !!preset.needsToday, dataset: { preset: preset.id }, onclick: function () { applyPreset(preset); }
      }));
    });
    renderModeKey();
    writeSimOutputs();
    renderOutcomes();
    L.state($("main-body"), "loading", { compact: true });

    form.addEventListener("input", function () {
      writeSimOutputs();
      markPreset(null);
      clearTimeout(timer);
      timer = setTimeout(runSimulation, 450);
    });
    form.addEventListener("submit", function (event) { event.preventDefault(); });
    form.addEventListener("reset", function () {
      setTimeout(function () {
        writeSimOutputs();
        markPreset(null);
        runSimulation();
      }, 0);
    });
    initTabs();

    // Land on today's scenario. The server knows today's fuel level; if it cannot say,
    // run the no-shock baseline (which reports the reference price) and work it out here.
    var todayChip = row.querySelector('[data-preset="today"]');
    var landOnToday = function (today) {
      sim.today = today;
      if (isNum(today.reference)) sim.reference = today.reference;
      todayChip.disabled = false;
      todayChip.title = "Fuel at " + fmt.usd(today.level) + " a gallon, " + signedPct(today.pct) + " against the " + fmt.usd(today.reference) +
        " reference" + (today.date ? " (market price of " + fmt.day(today.date) + ")" : "") + ", held for a year.";
      return applyPreset(PRESETS[0]);
    };
    loadToday().then(function (today) {
      if (today) return landOnToday(today);
      return runSimulation().then(function (baseline) {
        var worked = baseline ? todayContext(r.ctx, baseline) : null;
        if (worked) return landOnToday(worked);
        todayChip.title = "Needs the latest fuel price and a simulator run.";
        return null;
      });
    });
  }

  function sendToSimulator(request) {
    goToView("simulator");
    setSimForm(Object.assign({}, SIM_DEFAULT, request));
    markPreset(null);
    runSimulation();
    var first = $("sim-fuel");
    if (first) first.focus({ preventScroll: true });
  }

  // --------------------------------------------------------- the ask console

  var SUGGESTIONS = [
    "Fuel stays at today's price",
    "Fuel +30%, capacity \u22123%",
    "Demand \u22125%, 20 aircraft late",
    "Management against history"
  ];
  var STEP_KINDS = { plan: "Plan", tool_call: "Run", tool_result: "Result", check: "Check" };
  var ask = { history: [], busy: false };

  function wait(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, reducedMotion ? 0 : ms); });
  }

  function sendButton(request) {
    return el("button", { type: "button", class: "chip", text: "Send to the simulator", title: "Set the controls to these inputs and run", onclick: function () { sendToSimulator(request); } });
  }

  function finishStep(row, ms, resultSummary) {
    if (!row) return;
    row.dataset.state = "done";
    var time = row.querySelector(".step-time");
    if (time) time.textContent = duration(ms) || "done";
    if (resultSummary) {
      var outcome = row.querySelector(".step-outcome");
      if (outcome) outcome.textContent = resultSummary;
    }
  }

  function addStep(list, step, turn) {
    if (!step || typeof step !== "object") return;
    if (step.kind === "tool_result") {
      var running = Array.prototype.slice.call(list.querySelectorAll('.step[data-state="running"]'));
      var match = running.filter(function (row) { return row.dataset.name === String(step.name || ""); })[0] || running[0];
      if (match) {
        finishStep(match, step.elapsed_ms, step.summary);
        return;
      }
    }
    var isCall = step.kind === "tool_call";
    var hasArgs = step.args && typeof step.args === "object" && Object.keys(step.args).length;
    var head = [
      el("span", { class: "step-state", "aria-hidden": "true" }),
      el("span", { class: "step-text" }, [
        el("span", { class: "step-kind", text: STEP_KINDS[step.kind] || humanize(step.kind) }),
        el("span", { class: "step-summary", text: step.summary || step.name || "" }),
        el("span", { class: "step-outcome" })
      ]),
      el("span", { class: "step-time", text: isCall ? "running" : duration(step.elapsed_ms) })
    ];
    var body = el("div", { class: "step-body" });
    if (step.name) body.appendChild(el("p", { class: "step-name" }, [el("code", { text: step.name })]));
    if (hasArgs) body.appendChild(el("pre", { class: "step-args", text: JSON.stringify(step.args, null, 2) }));
    if (isCall && looksLikeScenarioRequest(step.args)) {
      turn.scenarioRequests.push(step.args);
      body.appendChild(sendButton(step.args));
    }
    list.appendChild(el("li", { class: "step is-" + step.kind, dataset: { state: isCall ? "running" : "done", name: String(step.name || "") } }, [
      step.name || hasArgs ? el("details", null, [el("summary", null, head), body]) : el("div", { class: "step-line" }, head)
    ]));
    var scroller = $("console-body");
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  }

  var REPORT_FIGURES = { cost_clock: "cost", fare_clock: "fare", market_clock: "market", track_record: "track", q3_call: "distribution",
    signal: "daily", demand_signal: "demand", daily_fares_2022: "fares", capacity_response: "capacity" };
  // A file can be named more than once (a chart and a table of it); these keys add nothing to the first chart.
  var REPORT_SKIP = { odds: true, scores: true, promises: true };

  /**
   * One chart of a report. The server sends references, not data:
   *   {kind: "scenario", run_id}            a run in report.scenarios
   *   {kind: "comparison", run_ids: [a, b]} two of them
   *   {kind: "grid", grid: n}               a grid in report.grids
   *   {file: "fare_clock.json", key, kind}  one of the site's own charts
   * A scenario given inline ({modes, request}) is drawn too.
   */
  function reportChart(chart, turn, index, report) {
    if (!chart || typeof chart !== "object") return null;
    var host = el("section", { class: "report-chart" });
    var runs = Array.isArray(report.scenarios) ? report.scenarios : [];
    var grids = Array.isArray(report.grids) ? report.grids : [];
    var runFor = function (id) {
      var found = runs.filter(function (run) { return run && run.run_id === id; })[0];
      var result = found && (scenarioOf(found.result) || scenarioOf(found));
      return result ? { result: result, request: found.request || result.request } : null;
    };

    var scenarios = [];
    if (scenarioOf(chart)) scenarios.push({ result: scenarioOf(chart), request: [chart.request, chart.inputs, chart.args, scenarioOf(chart).request].filter(looksLikeScenarioRequest)[0] });
    else if (chart.kind === "scenario") scenarios.push(runFor(chart.run_id) || (runs.length === 1 ? runFor(runs[0].run_id) : null));
    else if (chart.kind === "comparison") (Array.isArray(chart.run_ids) ? chart.run_ids : []).forEach(function (id) { scenarios.push(runFor(id)); });
    scenarios = scenarios.filter(Boolean);
    if (scenarios.length) {
      var wrap = el("div", { class: "report-scenario" });
      scenarios.forEach(function (scenario, i) {
        var box = i === 0 ? host : el("section", { class: "report-chart" });
        var request = looksLikeScenarioRequest(scenario.request) ? scenario.request
          : turn.scenarioRequests[Math.min(index + i, turn.scenarioRequests.length - 1)] || null;
        wrap.appendChild(box);
        if (request) wrap.appendChild(el("p", null, [sendButton(request)]));
        turn.afterAttach.push(function () {
          L.panel(box, {
            bare: true, legend: false, height: 210, headingLevel: "h4",
            title: request ? describeRequest(request) : "Scenario the system ran", kicker: "Scenario the system ran",
            read: "Each line is the middle of the simulated paths under one behavior; the band holds the middle 80%. Same colors as the simulator.",
            details: function () { return scenarioDetails(scenario.result, request); }
          }, L.scenarioFan({ modes: scenario.result.modes }, { metric: sim.view.metric, visible: { costs_only: true, management: true, measured: true } }));
        });
      });
      return wrap;
    }

    if (chart.kind === "grid") {
      var grid = grids.filter(function (g) { return g && g.grid === chart.grid; })[0] || (grids.length === 1 ? grids[0] : null);
      var data = grid && (grid.result && Array.isArray(grid.result.cells) ? grid.result : grid);
      if (!data || !Array.isArray(data.cells)) return null;
      turn.afterAttach.push(function () {
        var metric = GRID_METRICS[data.metric] || GRID_METRICS.fcf_12m_musd;
        L.panel(host, {
          bare: true, legend: false, height: 230, headingLevel: "h4", kicker: "Grid the system ran",
          title: metric.label + (data.direction === "below" ? " below " : " at or above ") + metric.format(data.threshold),
          read: "Each cell is one tested combination. The number is the share of simulated runs on the stated side of the line; the dark line is where that share is one half.",
          details: function () { return [textBlock("What the share is not", data.prob_definition), listBlock("Notes on this grid", data.notes)]; }
        }, gridSpec(data));
      });
      return host;
    }

    if (chart.file) {
      var name = String(chart.file).replace(/\.json$/, "");
      var figureName = REPORT_FIGURES[name];
      if (!figureName || REPORT_SKIP[chart.key] || turn.drawn[name]) return null;
      turn.drawn[name] = true;
      turn.afterAttach.push(function () {
        L.state(host, "loading", { compact: true });
        Promise.all([load(name), load("meta")]).then(function (results) {
          if (results[0].status !== "ok") {
            L.state(host, results[0].status === "missing" ? "missing" : "error", { compact: true, text: (FILE_NAMES[name] || name) + " is not available.", file: results[0].url });
            return;
          }
          var ctx = {};
          ctx[name] = results[0].data;
          ctx.meta = results[1].status === "ok" ? results[1].data : {};
          var built = figures[figureName](ctx, { compact: true });
          var spec = figureName === "track" ? L.trackRecord(results[0].data, { only: "test" }) : built.spec;
          L.panel(host, Object.assign({ dataHref: results[0].url }, built.frame, { bare: true, legend: false, height: figureName === "track" ? 260 : 220, headingLevel: "h4" }), spec);
        });
      });
      return host;
    }
    return null;
  }

  function unsupportedItem(item) {
    if (typeof item === "string") return el("li", { text: item });
    if (!item || typeof item !== "object") return null;
    var asked = item.asked || item.request || item.what || item.question || item.name;
    var why = item.reason || item.why;
    var instead = item.instead || item.alternative || item.alternatives || item.can_run || item.suggestion;
    var li = el("li");
    if (asked) li.appendChild(el("b", { text: String(asked) }));
    if (why) li.appendChild(el("span", { text: (asked ? ": " : "") + String(why) }));
    if (instead) li.appendChild(alternatives(Array.isArray(instead) ? instead : [instead]));
    return li.firstChild ? li : el("li", { text: JSON.stringify(item) });
  }

  function alternatives(options) {
    return el("p", { class: "unsupported-instead" }, options.map(function (option) {
      var text = typeof option === "string" ? option : option && (option.question || option.label || option.text);
      return text ? el("button", { type: "button", class: "chip", text: String(text), onclick: function () { submitQuestion(String(text)); } }) : null;
    }));
  }

  function icon(path) {
    var span = el("span", { class: "verdict-icon", "aria-hidden": "true" });
    span.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.75">' + path + "</svg>";
    return span;
  }

  /** Fold the server's result into the report it carries. */
  function reportOf(result) {
    if (!result || typeof result !== "object") return null;
    var report = result.report && typeof result.report === "object" ? Object.assign({}, result.report) : null;
    if (!report && (result.title || result.sections || result.unsupported)) report = Object.assign({}, result);
    if (!report && (result.answer || result.status === "not_supported")) report = {};
    if (!report) return null;
    if (report.verified == null && result.verified != null) report.verified = result.verified;
    if (!report.summary && result.answer) report.summary = result.answer;
    if (!report.charts && result.charts) report.charts = result.charts;
    if (!report.evidence_ids && result.evidence_ids) report.evidence_ids = result.evidence_ids;
    if (!report.note && result.note) report.note = result.note;
    if (!report.alternatives && result.alternatives) report.alternatives = result.alternatives;
    if (!report.scenarios && result.scenarios) report.scenarios = result.scenarios;
    if (!report.grids && result.grids) report.grids = result.grids;
    report.status = result.status || report.status;
    return report;
  }

  function renderReport(host, report, turn) {
    host.replaceChildren();
    if (!report || typeof report !== "object") {
      L.state(host, "error", { compact: true, text: "The run finished without a report." });
      return;
    }
    var unsupported = Array.isArray(report.unsupported) ? report.unsupported : [];
    var options = Array.isArray(report.alternatives) ? report.alternatives : [];
    var hasBody = report.summary || (Array.isArray(report.sections) && report.sections.length);

    if (report.verified === true) {
      host.appendChild(el("p", { class: "verdict is-ok" }, [icon('<circle cx="8" cy="8" r="6.25"/><path d="M5 8.2l2.1 2.1L11 6.2"/>'), el("span", { text: "Every number here came from a model run." })]));
    } else if (hasBody && report.status !== "not_supported") {
      host.appendChild(el("p", { class: "verdict is-warn", role: "alert" }, [icon('<path d="M8 2.2l6.2 11H1.8L8 2.2Z"/><path d="M8 6.5v3.2M8 11.6v.2"/>'),
        el("span", { text: "Not verified. " + (report.note ? String(report.note) : "At least one number could not be matched to a model run, so treat this as unchecked.") })]));
    }
    if (report.title) host.appendChild(el("h3", { class: "report-title", text: report.title }));
    if (report.summary) host.appendChild(el("p", { class: "report-summary", text: report.summary }));

    (Array.isArray(report.sections) ? report.sections : []).forEach(function (section) {
      if (!section) return;
      if (section.heading) host.appendChild(el("h4", { text: section.heading }));
      String(section.text || "").split(/\n{2,}/).forEach(function (paragraph) {
        if (paragraph.trim()) host.appendChild(el("p", { class: "report-text", text: paragraph.trim() }));
      });
    });

    (Array.isArray(report.charts) ? report.charts : []).forEach(function (chart, i) {
      var node = reportChart(chart, turn, i, report);
      if (node) host.appendChild(node);
    });

    if (unsupported.length) {
      host.appendChild(el("h4", { text: "What this system cannot run" }));
      host.appendChild(el("ul", { class: "unsupported" }, unsupported.map(unsupportedItem)));
    }
    if (options.length) {
      host.appendChild(el("h4", { text: "It can run instead" }));
      host.appendChild(alternatives(options));
    }
    if (Array.isArray(report.assumptions) && report.assumptions.length) {
      host.appendChild(el("h4", { text: "Assumptions" }));
      host.appendChild(el("ul", { class: "report-list" }, report.assumptions.map(function (a) { return el("li", { text: String(a) }); })));
    }
    var ids = knownIds(report.evidence_ids);
    var unknown = (Array.isArray(report.evidence_ids) ? report.evidence_ids : []).filter(function (id) { return !facts[id]; });
    if (ids.length || unknown.length) {
      host.appendChild(el("h4", { text: "Sources quoted" }));
      host.appendChild(el("p", { class: "report-sources" }, ids.map(function (id) {
        var fact = facts[id];
        return el("button", { type: "button", class: "chip", text: (CLAIMS[fact.claim] || humanize(fact.claim)) + ", " + fmt.period(fact.period), onclick: function () { openEvidence([id]); } });
      }).concat(unknown.map(function (id) { return el("span", { class: "hash", text: id + " (not in the loaded sources)" }); }))));
    }
    turn.afterAttach.splice(0).forEach(function (fn) { fn(); });
  }

  function readEventStream(response, onEvent) {
    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";
    function dispatch(raw) {
      var type = "message";
      var data = [];
      raw.split("\n").forEach(function (line) {
        if (line.indexOf("data:") === 0) data.push(line.slice(5).replace(/^ /, ""));
        else if (line.indexOf("event:") === 0) type = line.slice(6).trim();
      });
      if (!data.length) return;
      var text = data.join("\n");
      if (text === "[DONE]") return;
      var payload;
      try { payload = JSON.parse(text); } catch (error) { return; }
      onEvent(payload, type);
    }
    function pump() {
      return reader.read().then(function (chunk) {
        buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
        buffer = buffer.replace(/\r\n/g, "\n");
        var cut;
        while ((cut = buffer.indexOf("\n\n")) >= 0) {
          dispatch(buffer.slice(0, cut));
          buffer = buffer.slice(cut + 2);
        }
        if (chunk.done) {
          if (buffer.trim()) dispatch(buffer);
          return null;
        }
        return pump();
      });
    }
    return pump();
  }

  function runTurn(message) {
    var turns = $("ask-turns");
    var list = el("ol", { class: "steps" });
    var reportHost = el("section", { class: "report", "aria-label": "Report" });
    var node = el("article", { class: "turn" }, [el("p", { class: "turn-question", text: message }), list, reportHost]);
    var turn = { scenarioRequests: [], afterAttach: [], drawn: {}, done: false };
    $("ask-suggest").hidden = true;
    turns.appendChild(node);
    var scroller = $("console-body");
    if (scroller) scroller.scrollTop = scroller.scrollHeight;

    function finish(report) {
      if (turn.done) return;
      turn.done = true;
      Array.prototype.forEach.call(list.querySelectorAll('.step[data-state="running"]'), function (row) { finishStep(row, null, null); });
      renderReport(reportHost, report, turn);
      ask.history.push({ role: "user", content: message });
      ask.history.push({ role: "assistant", content: String((report && (report.summary || report.title)) || "") });
      node.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "nearest" });
    }

    function fail(kind, text, title) {
      if (turn.done) return;
      turn.done = true;
      Array.prototype.forEach.call(list.querySelectorAll('.step[data-state="running"]'), function (row) {
        row.dataset.state = "stopped";
        var time = row.querySelector(".step-time");
        if (time) time.textContent = "stopped";
      });
      if (!list.firstChild) list.hidden = true;
      L.state(reportHost, kind, { compact: true, title: title || (kind === "missing" ? "AI is not configured on this server" : "The run stopped"), text: text });
    }

    function notConfigured() {
      fail("missing", "No language-model key is set, so questions cannot be answered here. The simulator works without it.");
    }

    function settle(result) {
      if (!result || typeof result !== "object") return fail("error", "The answer was empty.");
      if (result.status === "not_configured") return notConfigured();
      if (!FIXTURES && hasFixtureFlag(result)) return fail("error", "The server returned placeholder data, so it is not shown as an answer.");
      var report = reportOf(result);
      if (report) return finish(report);
      return fail("error", typeof result.detail === "string" ? result.detail : "The run finished without a report.");
    }

    function onEvent(payload, type) {
      if (!payload || typeof payload !== "object" || turn.done) return;
      if (type === "error" || payload.status === "error") {
        fail("error", String(payload.detail || payload.message || "The server reported an error."));
      } else if (type === "step" || (STEP_KINDS[payload.kind] && type !== "final")) {
        addStep(list, payload.step || payload, turn);
      } else {
        settle(payload);
      }
    }

    function oneShot(result) {
      var steps = result && Array.isArray(result.steps) ? result.steps : [];
      var chain = Promise.resolve();
      if (!list.firstChild) {
        steps.forEach(function (step) {
          chain = chain.then(function () { addStep(list, step, turn); return wait(step.kind === "tool_call" ? 520 : 260); });
        });
      }
      return chain.then(function () { settle(result); });
    }

    if (FIXTURES) {
      return Promise.all([load("chat_stream"), load("chat_report")]).then(function (results) {
        var bad = results.filter(function (x) { return x.status !== "ok"; })[0];
        if (bad) return fail(bad.status === "missing" ? "missing" : "error", "The placeholder run has not been written (" + bad.url + ").", "Placeholder run missing");
        return oneShot(Object.assign({}, results[1].data, { steps: results[0].data.events || [] }));
      });
    }

    var body = { message: message, history: ask.history.slice(-12) };
    var fallback = function () {
      return post("/chat", body).then(function (response) {
        if (response.status === 404 || response.status === 405) return fail("missing", "The question service is not on this server yet. The simulator works without it.", "Not available here");
        return response.json().then(function (json) {
          if (!response.ok && !(json && json.status === "not_configured")) return fail("error", json && typeof json.detail === "string" ? json.detail : "The server answered " + response.status + ".");
          return oneShot(json);
        }, function () { return fail("error", "The server answered " + response.status + " with something that is not JSON."); });
      }, function () { return fail("error", "The server could not be reached."); });
    };
    return post("/chat/stream", body, "text/event-stream").then(function (response) {
      var type = response.headers.get("content-type") || "";
      if (response.ok && type.indexOf("text/event-stream") >= 0 && response.body && response.body.getReader) {
        // A stream that ends or breaks before its report (a buffering proxy, a dropped line):
        // clear the half-told log and ask again in one piece.
        var again = function () {
          if (turn.done) return null;
          list.replaceChildren();
          turn.scenarioRequests = [];
          return fallback();
        };
        return readEventStream(response, onEvent).then(again, again);
      }
      if (response.ok && type.indexOf("application/json") >= 0) return response.json().then(oneShot);
      if (response.status === 404 || response.status === 405) return fallback();
      return response.json().then(function (json) {
        fail("error", json && typeof json.detail === "string" ? json.detail : "The server answered " + response.status + ".");
      }, fallback);
    }, fallback);
  }

  function submitQuestion(message) {
    message = String(message || "").trim();
    if (!message || ask.busy) return;
    var input = $("ask-input");
    var button = $("ask-run");
    var simulatorView = $("view-simulator");
    if (simulatorView && simulatorView.classList.contains("is-console-closed")) toggleConsole(true);
    ask.busy = true;
    button.disabled = true;
    button.textContent = "Working";
    input.value = "";
    var done = function () {
      ask.busy = false;
      button.disabled = false;
      button.textContent = "Run";
    };
    Promise.resolve(runTurn(message)).then(done, function (error) {
      done();
      if (global.console) console.error(error);
    });
  }

  function toggleConsole(open) {
    var view = $("view-simulator");
    var button = $("console-toggle");
    var closed = open === undefined ? !view.classList.contains("is-console-closed") : !open;
    view.classList.toggle("is-console-closed", closed);
    button.setAttribute("aria-expanded", String(!closed));
    button.setAttribute("aria-label", closed ? "Open the ask panel" : "Collapse the ask panel");
  }

  function initAsk() {
    var form = $("ask-form");
    if (!form) return;
    var input = $("ask-input");
    var suggest = $("ask-suggest");
    SUGGESTIONS.forEach(function (q) {
      suggest.appendChild(el("button", { type: "button", class: "chip", text: q, onclick: function () { submitQuestion(q); } }));
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      submitQuestion(input.value);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        submitQuestion(input.value);
      }
    });
    $("console-toggle").addEventListener("click", function () { toggleConsole(); });
    serverStatus.then(function (status) {
      if (!status || status.ai_configured !== false) return;
      suggest.hidden = true;
      input.disabled = true;
      $("ask-run").disabled = true;
      $("ask-turns").appendChild(L.state(null, "missing", { compact: true, title: "No language model is connected",
        text: "Questions cannot be answered on this server. The simulator works without it." }));
    });
  }

  // ------------------------------------------------------------ 2 behaviour

  // On a desktop window the small tiles fill the right half in two columns: three, four or five of them.
  function placeBehaviour() {
    var small = Array.prototype.filter.call(document.querySelectorAll("#view-behaviour > .tile:not(.is-large)"), function (t) { return !t.hidden; });
    var leftCount = Math.ceil(small.length / 2);
    small.forEach(function (node, i) {
      var left = i < leftCount;
      var inColumn = left ? leftCount : small.length - leftCount;
      var at = left ? i : i - leftCount;
      var span = 6 / Math.max(1, inColumn);
      var whole = Math.floor(span);
      node.style.gridColumn = small.length === 3 && i === 2 ? "3 / 5" : left && small.length !== 3 ? "3" : small.length === 3 ? String(3 + i) : "4";
      node.style.gridRow = small.length === 3 ? (i === 2 ? "4 / 7" : "1 / 4") : (1 + at * whole) + " / " + (1 + (at + 1) * whole);
    });
  }

  function renderBehaviour(r) {
    var ctx = r.ctx;
    var fare = $("fig-fare");
    load("peer_recovery").then(function (peer) {
      r.peer_recovery = peer;
      if (peer.status === "ok") ctx.peer_recovery = peer.data;
      if (!ready(fare, [r.fare_clock], "Fuel cost to revenue")) return;
      tile(fare, "fare", ctx, r.fare_clock, { legend: false, tools: [el("span", { class: "source-flag", text: "Drives the simulator", title: "This curve is the \u201cas history shows\u201d behavior in the simulator." })] });
    });
    var cost = $("fig-cost");
    if (ready(cost, [r.cost_clock], "Fuel price to fuel bill")) tile(cost, "cost", ctx, r.cost_clock, { legend: false });
    var demand = $("fig-demand");
    if (ready(demand, [r.demand_signal], "Travel demand")) tile(demand, "demand", ctx, r.demand_signal, { legend: false });
    var market = $("fig-market");
    if (ready(market, [r.market_clock], "Stock reaction")) tile(market, "market", ctx, r.market_clock, { legend: false });
    placeBehaviour();

    // Optional tiles: each appears only when its file exists.
    [["capacity_response", "fig-capacity", "capacity"], ["daily_fares_2022", "fig-fares", "fares"]].forEach(function (item) {
      load(item[0]).then(function (result) {
        r[item[0]] = result;
        if (result.status !== "ok") return;
        var host = $(item[1]);
        host.hidden = false;
        ctx[item[0]] = result.data;
        placeBehaviour();
        try {
          tile(host, item[2], ctx, result, { legend: false });
        } catch (error) {
          L.state(host, "error", { compact: true, text: "This chart could not be drawn." });
          if (global.console) console.error(error);
        }
      });
    });
  }

  // -------------------------------------------------------- 3 reality check

  var MODELS = {
    fitted: "As history shows", guidance_mid: "Guidance midpoint", costs_only: "No response",
    management: "As management says", consensus: "Analyst consensus"
  };
  var MODEL_KEYS = {
    fitted: ["range", L.colors.ours], guidance_mid: ["tick", L.colors.context],
    costs_only: ["square", L.colors.costs], management: ["diamond", L.colors.management], consensus: ["thin", L.colors.ink]
  };

  function renderTrack(r) {
    var host = $("fig-track");
    var scores = $("scores");
    var delta = $("delta");
    if (!ready(host, [r.track_record], FILE_NAMES.track_record)) {
      ready(scores, [r.track_record], "Scores");
      ready(delta, [r.track_record], "Delta check");
      return;
    }
    var d = r.track_record.data;
    var built = figures.track(r.ctx);
    var present = ["test", "select", "fit"].filter(function (s) { return (d.quarters || []).some(function (q) { return q.split === s; }); });
    var names = { test: "Locked test", select: "Selection", fit: "Fitting" };
    var controls = [];
    if (present.length > 1) controls.push({ id: "split", label: "Period", value: present[0], options: present.map(function (s) { return { value: s, label: names[s] }; }) });
    controls.push({ id: "mode", label: "Scale", value: "level", options: [{ value: "level", label: "$ a share" }, { value: "miss", label: "Miss" }] });
    L.panel(host, Object.assign({ dataHref: r.track_record.url, scroll: true }, built.frame, {
      details: function () {
        return [textBlock("The three periods", "Fitting: used to estimate the method. Selection: used to choose between versions. Locked test: quarters the method never saw while it was being built; the only ones that count."), inputsBlock(d)];
      }
    }), {
      legend: built.spec.legend, table: built.spec.table, controls: controls,
      render: function (width, values, height) {
        return L.trackRecord(d, { only: present.length ? values.split || present[0] : null }).render(width, { mode: values.mode }, height);
      }
    });

    // Scores: management is one row among the rivals.
    var rows = Array.isArray(d.scores) ? d.scores : [];
    scores.classList.add("tile");
    scores.replaceChildren();
    var scoreInfo = infoButton("About the scores: what each column means", function () {
      var ft = d.fuel_price_test || {};
      var fuelStats = el("dl", { class: "stat-row" });
      if (isNum(ft.mae_guided)) fuelStats.appendChild(stat("United's assumed fuel price, average miss", [fmt.usd(ft.mae_guided), el("small", { text: " a gallon" })]));
      if (isNum(ft.mae_marked)) fuelStats.appendChild(stat("Market price on the guidance date, average miss", [fmt.usd(ft.mae_marked), el("small", { text: " a gallon" })]));
      if (isNum(ft.n)) fuelStats.appendChild(stat("Quarters compared", [fmt.int(ft.n)]));
      L.openDrawer("Scores", [
        el("h3", { text: "How far off each forecaster was" }),
        textBlock("Miss", "The typical gap between a forecast and the reported number, in dollars a share. Statisticians call it MAE, the mean absolute error."),
        textBlock("Range score", "Rewards a range that is both narrow and right, in the same dollars a share. For a single-number forecast it equals the miss. Lower is better. Its technical name is CRPS."),
        textBlock("In range", "How often the reported number fell inside the forecaster's 10–90% range. An honest range contains it about eight times in ten."),
        fuelStats.firstChild ? labelled("Whose fuel price was closer?", fuelStats) : null,
        el("p", { class: "drawer-actions" }, [el("a", { href: r.track_record.url, download: true, text: "Download the data (JSON)" })])
      ]);
    });
    scores.appendChild(el("header", { class: "tile-head" }, [el("h2", { class: "tile-title", text: "Scores" }), el("div", { class: "tile-tools" }, [scoreInfo])]));
    if (!rows.length) {
      scores.appendChild(L.state(null, "missing", { compact: true, text: "No scores are in this file yet." }));
    } else {
      var maxMae = Math.max.apply(null, rows.map(function (s) { return isNum(s.mae) ? s.mae : 0; })) || 1;
      var columns = [
        { label: "Forecaster", value: function (s) {
          var key = MODEL_KEYS[s.model] || ["dot", L.colors.context];
          return el("span", null, [L.keyMark(key[0], key[1]), " ", MODELS[s.model] || humanize(s.model)]);
        } },
        { label: "Miss", num: true, value: function (s) {
          var bar = el("span", { class: "cellbar", "aria-hidden": "true" }, [el("i")]);
          bar.firstChild.style.width = (isNum(s.mae) ? Math.min(100, s.mae / maxMae * 100) : 0) + "%";
          return el("span", null, [bar, fmt.usd(s.mae)]);
        } },
        { label: "Range score", num: true, value: function (s) { return fmt.num(s.crps, 2); } },
        { label: "In range", num: true, value: function (s) { return isNum(s.inside_p10_p90) ? fmt.pct(s.inside_p10_p90) : fmt.missing; } },
        { label: "n", num: true, value: function (s) { return fmt.int(s.n); } }
      ];
      columns.rowClass = function (s) { return s.model === "fitted" ? "is-ours" : null; };
      scores.appendChild(miniTable(columns, rows));
    }

    // Delta check: the same method on a second airline.
    var dc = d.delta_check;
    delta.classList.add("tile");
    delta.replaceChildren();
    if (!dc) {
      delta.appendChild(el("header", { class: "tile-head" }, [el("h2", { class: "tile-title", text: "Delta check" })]));
      delta.appendChild(L.state(null, "missing", { compact: true, text: "The Delta check is not in this file yet." }));
      return;
    }
    var single = L.trackRecord({ quarters: [{
      period: dc.period, guidance_low: dc.guidance_low, guidance_high: dc.guidance_high,
      actual_eps: dc.actual_eps, calls: dc.calls || {}, split: "test"
    }] }, { single: true });
    var line = el("p", { class: "inline-stats" });
    var add = function (key, name, value) {
      line.appendChild(el("span", null, [key ? L.keyMark(key[0], key[1]) : null, name + " ", el("b", null, [value])]));
    };
    add(null, "Guided", ev(fmt.range(dc.guidance_low, dc.guidance_high, fmt.usd), dc.evidence_ids));
    add(null, "Reported", ev(fmt.usd(dc.actual_eps), dc.evidence_ids));
    ["fitted", "management", "costs_only"].forEach(function (model) {
      var value = (dc.calls || {})[model];
      if (value == null) return;
      add(MODEL_KEYS[model], MODELS[model], el("span", { text: typeof value === "object" ? fmt.usd(value.p50) : fmt.usd(value) }));
    });
    L.panel(delta, {
      title: "Delta, " + fmt.period(dc.period), kicker: "A second airline", legend: false, foot: line,
      read: "The same method, with nothing refitted, applied to Delta's guidance and set beside what Delta reported. Marks as in the replay chart.",
      source: "Delta's guidance and earnings release.", dataHref: r.track_record.url,
      details: function () { return [L.legendList(single.legend)]; }
    }, { render: function (width, values, height) { return single.render(width, { mode: "level" }, height); } });
  }

  var LINKS = {
    fuel_price_paid_usd_gal: { name: "Fuel price paid", note: "Average price a gallon over the quarter.", f: function (v) { return fmt.usd(v); } },
    recovery_share: { name: "Share recovered", note: "Of the extra fuel cost, the part won back through revenue.", f: function (v) { return fmt.pct(v); } },
    trasm_yoy: { name: "Unit revenue growth", note: "Revenue per seat flown one mile against a year earlier. Airlines call it TRASM.", f: function (v) { return fmt.pctSigned(v, 1); } },
    adj_eps_diluted: { name: "Earnings per share", note: "Adjusted and diluted, the number United guides to.", f: function (v) { return fmt.usd(v); } }
  };

  function managementText(m, f) {
    if (isNum(m)) return f(m);
    if (Array.isArray(m)) return fmt.range(m[0], m[1], f);
    if (typeof m === "string") {
      var match = /^(above|below|over|under|at least|at most|about|around)\s+(-?\d+(?:\.\d+)?)$/i.exec(m.trim());
      return match ? match[1].toLowerCase() + " " + f(Number(match[2])) : m;
    }
    return fmt.missing;
  }

  function managementNumbers(m) {
    if (isNum(m)) return [m];
    if (Array.isArray(m)) return m.filter(isNum);
    if (typeof m === "string") {
      var match = /(-?\d+(?:\.\d+)?)/.exec(m);
      return match ? [Number(match[1])] : [];
    }
    return [];
  }

  function chainNode(c, meta, height) {
    var g = meta.guidance || {};
    var links = Array.isArray(c.chain) ? c.chain : [];
    if (!links.length) return L.state(null, "missing", { compact: true, text: "The estimate's links are not in this file yet." });
    var grid = el("div", { class: "chain", role: "table", "aria-label": "The estimate, link by link" });
    // In a fixed-height tile the rows share the room they have, so none is cut off.
    if (height) grid.style.height = Math.min(height, 24 + links.length * 36) + "px";
    grid.appendChild(el("div", { class: "chain-row chain-head", role: "row" }, [
      el("span", { role: "columnheader", text: "Link" }),
      el("span", { role: "columnheader" }, [L.keyMark("range", L.colors.ours), " 10–90% ", L.keyMark("tick", L.colors.management), " Mgmt"]),
      el("span", { class: "chain-ours", role: "columnheader", text: "Middle" }),
      el("span", { class: "chain-mgmt", role: "columnheader", text: "Management" })
    ]));
    links.forEach(function (link) {
      var info = LINKS[link.link] || { name: humanize(link.link), note: "", f: function (v) { return fmt.num(v, 2); } };
      var numbers = [link.p10, link.p50, link.p90].concat(managementNumbers(link.management)).filter(isNum);
      var lo = Math.min.apply(null, numbers);
      var hi = Math.max.apply(null, numbers);
      var pad = (hi - lo || Math.abs(hi) || 1) * 0.3;
      var mgmt = typeof link.management === "string" ? managementNumbers(link.management)[0] : link.management;
      var ids = link.link === "recovery_share" ? ((meta.promises || [])[0] || {}).evidence_ids
        : link.link === "fuel_price_paid_usd_gal" || link.link === "adj_eps_diluted" ? g.evidence_ids : null;
      grid.appendChild(el("div", { class: "chain-row", role: "row" }, [
        el("span", { class: "chain-name", role: "cell", text: info.name, title: info.name + ". " + info.note }),
        el("span", { role: "cell", title: "10–90% range " + fmt.range(link.p10, link.p90, info.f) }, [L.rangeStrip({ domain: [lo - pad, hi + pad], p10: link.p10, p50: link.p50, p90: link.p90, management: mgmt })]),
        el("span", { class: "chain-ours", role: "cell", text: info.f(link.p50) }),
        el("span", { class: "chain-mgmt", role: "cell" }, [ev(managementText(link.management, info.f), ids)])
      ]));
    });
    return grid;
  }

  function daysUntil(iso) {
    var day = L.parseDay(iso);
    if (!day) return null;
    var now = new Date();
    return Math.round((day.getTime() - Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())) / 86400000);
  }

  function renderForward(r) {
    var host = $("forward");
    if (!ready(host, [r.q3_call], FILE_NAMES.q3_call)) return;
    var c = r.q3_call.data;
    var meta = r.meta.status === "ok" ? r.meta.data : {};
    var g = meta.guidance || {};
    var comparators = meta.comparators || {};
    var o = c.odds || {};
    var hasSignal = r.signal.status === "ok";
    var distribution = figures.distribution(r.ctx);

    var oddsParts = [
      { label: "Below guidance", value: o.below_range, note: isNum(g.eps_low) ? "under " + fmt.usd(g.eps_low) : null },
      { label: "Inside", value: o.inside_range, note: isNum(g.eps_low) ? fmt.range(g.eps_low, g.eps_high, fmt.usd) : null },
      { label: "Above", value: o.above_range, note: isNum(g.eps_high) ? "over " + fmt.usd(g.eps_high) : null }
    ];

    var seal = el("p", { class: "seal-line" });
    var days = daysUntil(meta.report_date);
    if (meta.report_date) {
      seal.appendChild(el("span", null, [el("b", { text: days != null && days < 0 ? "Reported " : "Next check " }), fmt.day(meta.report_date) +
        (days == null ? "" : days > 1 ? ", in " + days + " days" : days === 1 ? ", tomorrow" : days === 0 ? ", today" : "")]));
    }
    seal.appendChild(el("span", null, [el("b", { text: "Frozen " }), fmt.stamp(c.frozen_at)]));
    if (c.sha256) seal.appendChild(el("span", { title: "The estimate's fingerprint, first 12 characters. Copy gives all 64: " + c.sha256 }, [el("b", { text: "SHA-256 " }), el("span", { class: "hash", text: fmt.hashShort(c.sha256) }), " ", copyButton(c.sha256)]));
    if (c.timestamp_proof && !FIXTURES) seal.appendChild(el("a", { href: DATA + c.timestamp_proof, download: true, text: "Proof (.ots)" }));

    var options = [{ value: "chain", label: "Chain" }, { value: "odds", label: "Odds" }];
    if (hasSignal) options.push({ value: "daily", label: "Daily" });

    L.panel(host, {
      title: "Forward test" + (g.period ? ", " + fmt.period(g.period) : ""),
      headline: c.headline,
      kicker: "A dated forward test", legend: false, foot: seal, dataHref: r.q3_call.url, dataLabel: "Download the estimate (JSON)",
      read: "The same behavior, applied to a quarter that has not been reported yet. The estimate was fixed and fingerprinted beforehand, so it can be scored afterwards and cannot be edited. Chain: four estimates in a row, each feeding the next, beside management's own number. Odds: the whole estimate against United's guidance range. Daily: the estimate recomputed each trading day.",
      source: "The frozen estimate; United's guidance; analyst consensus and prediction market as cited.",
      details: function () {
        var scoring = c.scoring || {};
        var rivals = { guidance_mid: "the middle of United's guidance", consensus: "the analyst consensus", costs_only: "no response (costs only)", management: "what management says" };
        var names = (Array.isArray(scoring.baselines) ? scoring.baselines : []).map(function (b) { return rivals[b] || humanize(b).toLowerCase(); });
        var against = names.length > 1 ? names.slice(0, -1).join(", ") + " and " + names[names.length - 1] : names[0];
        var consensus = comparators.consensus_eps || {};
        var market = comparators.market_odds_beat || {};
        var meters = el("div", { class: "meters" });
        meters.appendChild(L.meter({ label: "This estimate", value: o.above_consensus, ours: true }));
        if (isNum(market.value)) {
          var note = ["Prediction-market price" + (market.as_of ? " on " + fmt.day(market.as_of) : "") + (isNum(market.volume_usd) ? "; " + fmt.usd(market.volume_usd, 0) + " traded in total. " : ". ")];
          if (market.source_url) note.push(el("a", { href: market.source_url, target: "_blank", rel: "noopener noreferrer", text: "Source" }));
          meters.appendChild(L.meter({ label: "Prediction market", value: market.value, note: note }));
        }
        var b = hasSignal ? r.signal.data.backtest || {} : {};
        return [
          labelled("The fingerprint", el("p", null, [el("span", { class: "hash", text: c.sha256 || fmt.missing }), " ", c.sha256 ? copyButton(c.sha256) : null])),
          textBlock("What it proves", "The fingerprint changes if a single character of the estimate changes. The timestamp proof ties that fingerprint to a public record of the time."),
          textBlock("Frozen at", fmt.stamp(c.frozen_at) + ", using data up to " + fmt.day(c.data_cutoff) + "."),
          listBlock("Known before the freeze", c.known_before_freeze),
          textBlock("How it will be scored", (scoring.rule === "CRPS" ? "By the range score (CRPS)" : "By " + (scoring.rule || "a rule fixed in advance")) + (against ? ", against " + against + "." : ".")),
          textBlock("Result", c.outcome == null ? "Pending until " + fmt.day(meta.report_date) + "." : typeof c.outcome === "object" ? Object.keys(c.outcome).map(function (k) { return humanize(k) + ": " + metricValue(k, c.outcome[k]); }).join("; ") : String(c.outcome)),
          labelled("Against United's guidance range", L.oddsBar(oddsParts)),
          labelled("Chance of beating the analyst consensus" + (isNum(consensus.value) ? " of " + fmt.usd(consensus.value) : ""), meters),
          consensus.source_url ? el("p", { class: "drawer-actions" }, [el("a", { href: consensus.source_url, target: "_blank", rel: "noopener noreferrer", text: "Consensus source" + (consensus.as_of ? ", " + fmt.day(consensus.as_of) : "") })]) : null,
          hasSignal ? labelled("One backtest", el("div", null, [
            b.definition ? el("p", { class: "drawer-text", text: b.definition }) : null,
            splitTable(b.by_split) || L.state(null, "missing", { compact: true, text: "The backtest results are not in this file yet." }),
            el("p", { class: "drawer-text", text: "Fitted on the first period, chosen on the second and run once on the third, the only column that counts." +
              (isNum(b.costs_bps_per_side) ? " Trading costs of " + fmt.num(b.costs_bps_per_side, 0) + " basis points each way are taken off." : "") +
              " The table is printed whatever it shows: a result that cannot be told apart from zero is a finding, not a gap." })
          ])) : null,
          hasSignal ? listBlock("Every version tried", b.variants_tried) : null
        ];
      }
    }, {
      controls: [{ id: "tab", label: "Show", value: "chain", options: options }],
      table: distribution.spec.table,
      render: function (width, values, height) {
        if (values.tab === "odds") {
          // The shape, then the three shares as one line; the full bar is in the drawer.
          var box = el("div");
          box.appendChild(distribution.spec.render(width, null, height ? Math.max(96, height - 30) : undefined));
          box.appendChild(L.oddsLine(oddsParts));
          return box;
        }
        if (values.tab === "daily" && hasSignal) {
          return figures.daily(r.ctx, { compact: true }).spec.render(width, null, height);
        }
        return chainNode(c, meta, height);
      }
    });
  }

  // -------------------------------------------------------------------- boot

  function boot() {
    initTheme();
    if (document.body.getAttribute("data-page") !== "index") return;
    var pill = $("fixture-pill");
    if (pill) pill.hidden = !FIXTURES;
    initViews();
    Array.prototype.forEach.call(document.querySelectorAll(".view-behaviour > .tile:not([hidden]), .view-reality .tile"), function (host) {
      L.state(host, "loading", { compact: true });
    });

    Promise.all(FILES.map(load)).then(function (list) {
      var r = { ctx: {} };
      var latest = "";
      list.forEach(function (result) {
        r[result.name] = result;
        if (result.status === "ok") {
          r.ctx[result.name] = result.data;
          if (result.data && typeof result.data.as_of === "string" && result.data.as_of > latest) latest = result.data.as_of;
        }
      });
      indexEvidence(r.evidence);
      var date = $("data-date");
      if (date) date.textContent = latest ? "Data to " + fmt.day(latest) : "";
      var about = $("about-btn");
      if (about) about.addEventListener("click", function () { openAbout(r); });
      [renderBehaviour, renderTrack, renderForward].forEach(function (render) {
        try { render(r); } catch (error) {
          if (global.console) console.error(error);
        }
      });
      initSimulation(r);
      initAsk();
    });
  }

  global.Lab = {
    fixtures: FIXTURES,
    dataBase: DATA,
    load: load,
    ready: ready,
    ev: ev,
    openEvidence: openEvidence,
    figures: figures,
    splitTable: splitTable,
    words: words,
    view: goToView,
    sim: { set: sendToSimulator, show: showScenario, read: readSimForm, state: sim },
    ask: { run: submitQuestion, renderReport: renderReport, readEventStream: readEventStream }
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})(window);
