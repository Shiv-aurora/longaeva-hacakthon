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
        if (!parent || parent.closest("svg, [hidden], .sr-only, .expand:not(.is-open), script, style, noscript")) continue;
        var box = parent.getBoundingClientRect();
        if (!box.width || !box.height) continue;
        var n = node.nodeValue.split(/\s+/).filter(isWord).length;
        var clipped = parent.closest(".tile-title, .outcome-range, .outcome-delta, .chain-name");
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
    tab: "whatif", threshold: { breaks: 0, target: 0 }, grid: {}, main: null, runs: {}, retry: 0, gridRetry: 0,
    // What the controls changed by themselves on the last move, and the most aircraft a scenario
    // may delay for each hold length (the server says so when it is exceeded).
    note: "", aircraftLimit: {}
  };

  // Presets are starting points, not findings: each sets the controls and runs.
  var PRESETS = [
    { id: "today", label: "Today", needsToday: true, what: "Latest fuel price for a year",
      build: function (t) { return { fuel_shock_pct: t.pct, fuel_shock_months: t.months || 12 }; } },
    { id: "fuel_back", label: "Fuel falls back", what: "Fuel at the reference price",
      build: function () { return { fuel_shock_pct: 0 }; } },
    { id: "demand", label: "Demand weakens", what: "Today's fuel, demand 5% lower",
      build: function (t) { return { fuel_shock_pct: t ? t.pct : 0, fuel_shock_months: 12, demand_shock_pct: -5 }; } },
    { id: "deliveries", label: "Deliveries slip", what: "20 aircraft arrive 6 months late",
      build: function (t) { return { fuel_shock_pct: t ? t.pct : 0, fuel_shock_months: 12, delivery_delay: { aircraft: 20, months: 6 } }; } },
    { id: "capacity", label: "Capacity cut", what: "Today's fuel, 3% fewer seats",
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

  /**
   * The delivery delay is a pair the simulator takes only whole: both zero, or both above
   * zero. `moved` is the control just touched ("aircraft" or "months"), if any: moving one
   * above zero brings the other to its first step, and moving one to zero clears the other.
   * A pair that arrives half given (a preset, a request from the agent) is completed.
   */
  function pairedDelay(aircraft, months, moved, firstStep) {
    aircraft = aircraft > 0 ? aircraft : 0;
    months = months > 0 ? months : 0;
    if (moved === "aircraft" && !aircraft) return { aircraft: 0, months: 0 };
    if (moved === "months" && !months) return { aircraft: 0, months: 0 };
    if (aircraft && !months) return { aircraft: aircraft, months: 1 };
    if (months && !aircraft) return { aircraft: firstStep > 0 ? firstStep : 1, months: months };
    return { aircraft: aircraft, months: months };
  }

  /**
   * Put the two delay sliders in a state the simulator accepts and say what was changed.
   * Besides the pairing, a scenario cannot delay more aircraft than the fleet plan has
   * arriving while the fuel shock is held; that number is learned from the server.
   */
  function settleDelay(moved) {
    var form = $("sim-form");
    var aircraft = form.elements.delay_aircraft;
    var months = form.elements.delay_months;
    var step = Number(aircraft.step) || 1;
    var held = Number(form.elements.fuel_shock_months.value);
    var pair = pairedDelay(Number(aircraft.value), Number(months.value), moved, step);
    // A longer hold takes in every delivery a shorter one does, so a limit learned for a
    // longer hold is a ceiling for this one too.
    var limit = null;
    Object.keys(sim.aircraftLimit).forEach(function (known) {
      if (Number(known) >= held && (limit == null || sim.aircraftLimit[known] < limit)) limit = sim.aircraftLimit[known];
    });
    var note = "";
    if (isNum(limit) && pair.aircraft > limit) {
      var most = Math.floor(limit / step) * step;
      note = most > 0 ? "No more than " + limit + " aircraft are due in this window, so " + most + " are delayed."
        : "No aircraft are due while fuel is held for " + held + (held === 1 ? " month" : " months") + ". Hold it longer to delay deliveries.";
      pair = most > 0 ? { aircraft: most, months: pair.months } : { aircraft: 0, months: 0 };
    }
    aircraft.value = pair.aircraft;
    months.value = pair.months;
    return note;
  }

  function readSimForm() {
    var form = $("sim-form");
    var n = function (name) { return Number(form.elements[name].value); };
    return {
      fuel_shock_pct: n("fuel_shock_pct"), fuel_shock_months: n("fuel_shock_months"),
      capacity_change_pct: n("capacity_change_pct"), demand_shock_pct: n("demand_shock_pct"),
      delivery_delay: pairedDelay(n("delay_aircraft"), n("delay_months"), null, Number(form.elements.delay_aircraft.step) || 1),
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
    sim.note = settleDelay(null);
    writeSimOutputs();
  }

  // The simulator names its inputs; a visitor knows them by the labels on the controls.
  var CONTROL_NAMES = [["delivery_delay.aircraft", "Aircraft delayed"], ["delivery_delay.months", "Delay"], ["delivery_delay", "The delivery delay"],
    ["fuel_shock_months", "Held for"], ["fuel_shock_pct", "Fuel price"], ["fuel_usd_gal", "Fuel price"], ["demand_shock_pct", "Travel demand"],
    ["capacity_change_pct", "Capacity cut"], ["horizon_months", "Horizon"]];

  /** Why a run was refused, in the words on the controls, and what to do about it. */
  function plainSimError(text, what) {
    var said = String(text || "").trim();
    var named = false;
    CONTROL_NAMES.forEach(function (pair) {
      if (said.indexOf(pair[0]) < 0) return;
      said = said.split(pair[0]).join(pair[1]);
      named = true;
    });
    if (named) return said.replace(/\.$/, "") + ". Change that control and " + (what || "the simulator") + " runs again.";
    return (what === "the grid" ? "The grid" : "The simulator") + " could not run this scenario. Change a control, or press Reset to start from the defaults.";
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

  /** The one line under the delay sliders: what the controls changed by themselves, or a short wait. Empty otherwise. */
  function simNote(text) {
    var note = $("sim-note");
    if (note) note.textContent = text || "";
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
    var hadFocus = host.contains(document.activeElement);
    host.replaceChildren();
    host.removeAttribute("role");
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
    // The tiles are one radio group: Tab reaches the chosen one, arrow keys move the choice.
    host.setAttribute("role", "radiogroup");
    host.setAttribute("aria-label", "Outcome shown in the chart");
    var cards = [];
    var choose = function (card, metric, focus) {
      sim.view.metric = metric;
      cards.forEach(function (b) {
        b.setAttribute("aria-checked", String(b === card));
        b.tabIndex = b === card ? 0 : -1;
      });
      if (focus) card.focus();
      if (sim.tab !== "whatif") selectTab("whatif");
      else if (sim.main) sim.main.redraw();
    };
    shown.forEach(function (o, index) {
      var useQuarter = o.quarter || !year || !year[o.metric];
      var value = useQuarter ? fourth[o.metric] : year[o.metric];
      var period = useQuarter ? fmt.period(fourth.period) : "next 12 months";
      var swatch = el("i");
      swatch.style.background = mode.color;
      var chosen = sim.view.metric === o.metric;
      var button = el("button", { type: "button", class: "outcome", role: "radio", "aria-checked": String(chosen), tabindex: chosen ? "0" : "-1",
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
      button.addEventListener("click", function () { choose(button, o.metric, false); });
      button.addEventListener("keydown", function (event) {
        var last = shown.length - 1;
        var to = event.key === "ArrowRight" || event.key === "ArrowDown" ? (index === last ? 0 : index + 1)
          : event.key === "ArrowLeft" || event.key === "ArrowUp" ? (index === 0 ? last : index - 1)
          : event.key === "Home" ? 0 : event.key === "End" ? last : -1;
        if (to < 0 || to === index) return;
        event.preventDefault();
        choose(cards[to], shown[to].metric, true);
      });
      cards.push(button);
      host.appendChild(button);
    });
    // A redraw while a tile had the keyboard keeps the keyboard on the chosen tile.
    if (hadFocus) cards.forEach(function (b) { if (b.tabIndex === 0) b.focus(); });
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
        var refused = data.__state === "error" && !notBuilt;
        L.state(body, refused ? "error" : "missing", { compact: true, title: refused ? "This grid cannot be run" : null,
          text: refused ? plainSimError(data.text, "the grid") : data.text || data.reason || (notBuilt ? "This grid has not been built on this server yet." : "The grid came back empty.") });
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

  function simUnavailable(kind, text, file, title) {
    sim.response = null;
    sim.main = null;
    setBusy(false);
    renderOutcomes();
    $("main-tools").replaceChildren();
    L.state($("main-body"), kind, { text: text, file: file, title: title });
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

    var source = FIXTURES ? load("scenario").then(function (r) {
      return r.status === "ok" ? r.data : { __state: r.status === "missing" ? "missing" : "error", text: r.status === "missing" ? "The placeholder scenario has not been written." : r.message, file: r.url };
    }) : (sim.runs[key] || (sim.runs[key] = call("/scenario", request, "The simulator")));

    return source.then(function (data) {
      if (ticket !== sim.sequence) return null;
      if (data.__state === "busy") {
        // Keep the last result on screen, dimmed, and run again when the server allows it.
        delete sim.runs[key];
        simNote(data.text + " This run starts by itself in " + data.retryAfter + " seconds.");
        sim.retry = setTimeout(function () { if (ticket === sim.sequence) runSimulation(); }, data.retryAfter * 1000 + 200);
        return null;
      }
      if (data.__state) {
        delete sim.runs[key];
        // More aircraft than the fleet plan has arriving in this window: remember the number,
        // bring the control within it and run again, so the visitor never lands on a refusal.
        var most = /delivery_delay\.aircraft must be at most (\d+)/.exec(String(data.text || ""));
        if (most && sim.aircraftLimit[request.fuel_shock_months] !== Number(most[1])) {
          sim.aircraftLimit[request.fuel_shock_months] = Number(most[1]);
          sim.note = settleDelay(null);
          writeSimOutputs();
          return runSimulation();
        }
        var refused = data.__state === "error";
        simUnavailable(data.__state, refused ? plainSimError(data.text) : data.text, refused ? null : data.file, refused ? "This scenario cannot be run" : null);
        simNote(sim.note);
        return null;
      }
      if (!FIXTURES && hasFixtureFlag(data)) {
        simUnavailable("error", "The simulator returned placeholder data, so it is not shown as a result.");
        simNote(sim.note);
        return null;
      }
      if (!data.modes) {
        delete sim.runs[key];
        var notBuilt = data.status === "not_computed";
        simUnavailable(notBuilt ? "missing" : "error", data.reason || (notBuilt ? "The simulator has not been built on this server yet." : "The simulator's answer had no results in it."));
        simNote(sim.note);
        return null;
      }
      showScenario(data, request);
      simNote(sim.note);
      return data;
    });
  }

  /** Name the scenario on the selector: a preset, or "Custom" once the controls have moved away from one. */
  function markPreset(id) {
    sim.preset = id;
    var preset = PRESETS.filter(function (p) { return p.id === id; })[0];
    var name = $("scenario-name");
    if (name) name.textContent = preset ? preset.label : "Custom";
  }

  /**
   * The scenario selector: the presets as one list under a trigger that names the current
   * one. The list has a field to find by, one highlight that glides between rows, and a
   * check on the scenario in force. It is the same popup as the composer's command menu.
   */
  function initScenarioSelector() {
    var host = $("scenario");
    var trigger = $("scenario-btn");
    if (!host || !trigger) return;
    var find = el("input", { type: "text", class: "menu-find", placeholder: "Find...", "aria-label": "Find a scenario", autocomplete: "off", spellcheck: "false",
      role: "combobox", "aria-autocomplete": "list", "aria-expanded": "false", "aria-controls": "scenario-menu-list" });
    var close = function (refocus) {
      menu.close();
      trigger.setAttribute("aria-expanded", "false");
      if (refocus) trigger.focus();
    };
    var menu = popup($("scenario-menu"), find, {
      label: "Scenarios", empty: "No scenario matches",
      head: el("div", { class: "menu-head" }, [find, el("button", { type: "button", class: "menu-key", text: "Esc", "aria-label": "Close the list", tabindex: "-1", onclick: function () { close(true); } })]),
      onPick: function (item) {
        close(true);
        applyPreset(item.preset);
      }
    });
    var items = function () {
      var typed = find.value.trim().toLowerCase();
      return PRESETS.filter(function (p) { return !typed || (p.label + " " + p.what).toLowerCase().indexOf(typed) >= 0; }).map(function (p) {
        var waiting = !!p.needsToday && !sim.today;
        return { preset: p, name: p.label, checked: p.id === sim.preset, disabled: waiting,
          note: waiting ? "Needs the latest fuel price" : p.needsToday ? fmt.usd(sim.today.level) + " a gallon, held for a year" : p.what };
      });
    };
    var open = function () {
      find.value = "";
      var list = items();
      menu.open(list, list.map(function (item) { return item.checked; }).indexOf(true));
      trigger.setAttribute("aria-expanded", "true");
      find.focus();
    };
    trigger.addEventListener("click", function () {
      if (menu.isOpen()) close(false);
      else open();
    });
    trigger.addEventListener("keydown", function (event) {
      if ((event.key === "ArrowDown" || event.key === "ArrowUp") && !menu.isOpen()) {
        event.preventDefault();
        open();
      }
    });
    find.addEventListener("input", function (event) {
      event.stopPropagation();
      menu.open(items());
    });
    find.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        menu.move(event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Enter") {
        event.preventDefault();
        menu.pick();
      } else if (event.key === "Escape") {
        event.preventDefault();
        close(true);
      } else if (event.key === "Tab") {
        close(false);
      }
    });
    document.addEventListener("pointerdown", function (event) {
      if (menu.isOpen() && !host.contains(event.target)) close(false);
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
        pct: Math.round(fuel.shock_pct), months: (data.request || {}).fuel_shock_months, basis: data.basis, assumptions: data.assumptions,
        // How many aircraft the fleet plan delivers in each hold length, so the slider is capped before a request is sent.
        aircraftLimit: (data.limits || {}).delivery_delay_max_aircraft
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

    initScenarioSelector();
    renderModeKey();
    writeSimOutputs();
    renderOutcomes();
    L.state($("main-body"), "loading", { compact: true });

    form.addEventListener("input", function (event) {
      var name = event.target && event.target.name;
      if (!name) return;
      sim.note = settleDelay(name === "delay_aircraft" ? "aircraft" : name === "delay_months" ? "months" : null);
      writeSimOutputs();
      markPreset(null);
      clearTimeout(timer);
      timer = setTimeout(runSimulation, 450);
    });
    form.addEventListener("submit", function (event) { event.preventDefault(); });
    form.addEventListener("reset", function () {
      setTimeout(function () {
        sim.note = settleDelay(null);
        writeSimOutputs();
        markPreset(null);
        runSimulation();
      }, 0);
    });
    initTabs();

    // Land on today's scenario. The server knows today's fuel level; if it cannot say,
    // run the no-shock baseline (which reports the reference price) and work it out here.
    var landOnToday = function (today) {
      sim.today = today;
      if (isNum(today.reference)) sim.reference = today.reference;
      Object.keys(today.aircraftLimit || {}).forEach(function (months) {
        if (isNum(today.aircraftLimit[months])) sim.aircraftLimit[Number(months)] = today.aircraftLimit[months];
      });
      return applyPreset(PRESETS[0]);
    };
    loadToday().then(function (today) {
      if (today) return landOnToday(today);
      return runSimulation().then(function (baseline) {
        var worked = baseline ? todayContext(r.ctx, baseline) : null;
        if (worked) return landOnToday(worked);
        markPreset(null);
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
  //
  // One column. Before anything is asked it lists a few questions; after that, a thread. A turn is
  // the question, the agent's steps (a status line over a list, folded to one line once the
  // answer is in), the answer, and what to ask next. The composer stays at the foot.
  // Everything shown comes from the server: a field that is missing leaves its part out.

  var SUGGESTIONS = [
    "Fuel stays at today's price",
    "Fuel +30%, capacity −3%",
    "Demand −5%, 20 aircraft late",
    "Management against history"
  ];
  // "retried" is a check that sent the draft back: the rewrite and its check follow as their own steps.
  var STATUS_WORDS = { running: "running", done: "done", failed: "failed", retried: "sent back for a rewrite" };
  var ask = { history: [], busy: false, model: null, turns: 0, composer: null };

  // Icons are drawn here, on a 16 by 16 grid, one stroke weight.
  var ICONS = {
    check: '<path d="M3.8 8.3l2.7 2.7 5.7-5.9"/>',
    x: '<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>',
    chevron: '<path d="M6 3.5 10.5 8 6 12.5"/>',
    back: '<path d="M10 3.5 5.5 8 10 12.5"/>',
    down: '<path d="M3.5 6 8 10.5 12.5 6"/>',
    copy: '<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5V4A1.5 1.5 0 0 0 9 2.5H4A1.5 1.5 0 0 0 2.5 4v5A1.5 1.5 0 0 0 4 10.5h1.5"/>',
    again: '<path d="M13 8a5 5 0 1 1-1.6-3.7"/><path d="M13 2.5v3h-3"/>',
    open: '<path d="M6.5 3.5h-3v9h9v-3"/><path d="M9.5 3.5h3v3M12.5 3.5 7.5 8.5"/>',
    sources: '<path d="M4 2.5h5.5L12 5v8.5H4z"/><path d="M9.5 2.5V5H12M6 8.5h4M6 11h4"/>',
    reply: '<path d="M3.5 3.5v3.5a2 2 0 0 0 2 2h7"/><path d="M10 6.5 12.5 9 10 11.5"/>',
    send: '<path d="M8 12.5v-9M4.5 7 8 3.5 11.5 7"/>',
    slash: '<path d="M10 3 6 13"/>'
  };

  function icon(name, className) {
    var span = el("span", { class: "icon" + (className ? " " + className : ""), "aria-hidden": "true" });
    span.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' + ICONS[name] + "</svg>";
    return span;
  }

  /** A region whose height opens and closes by grid rows (.expand in lab.css). */
  function expander(inner, open) {
    return el("div", { class: "expand" + (open ? " is-open" : "") }, [el("div", { class: "expand-inner" }, [inner])]);
  }

  function setOpen(region, button, open) {
    region.classList.toggle("is-open", !!open);
    if (button) button.setAttribute("aria-expanded", String(!!open));
  }

  /** Elapsed time as the status line shows it: 3.4s, then 1m 2.3s. */
  function clock(ms) {
    var s = Math.max(0, ms) / 1000;
    return s < 60 ? s.toFixed(1) + "s" : Math.floor(s / 60) + "m " + (s % 60).toFixed(1) + "s";
  }

  function announce(text) {
    var live = $("ask-live");
    if (live) live.textContent = text;
  }

  // 1. The status line ----------------------------------------------------------

  /** Nine cells that pulse as a chevron driving right, the running step's label, a live timer. */
  function thinking() {
    var grid = el("span", { class: "thinking-grid", "aria-hidden": "true" });
    for (var row = 0; row < 3; row++) {
      for (var col = 0; col < 3; col++) {
        var cell = el("i");
        cell.style.animationDelay = (col + Math.abs(row - 1)) * 90 + "ms";
        grid.appendChild(cell);
      }
    }
    var label = el("span", { class: "thinking-label", text: "Planning" });
    // The timer is for the eye only: a status region that changed ten times a second would be read out each time.
    var time = el("span", { class: "thinking-time", "aria-hidden": "true" });
    var began = performance.now();
    var tick = function () { time.textContent = clock(performance.now() - began); };
    var timer = setInterval(tick, 100);
    tick();
    return {
      node: el("div", { class: "thinking", role: "status" }, [grid, label, time]),
      set: function (text) { if (text && label.textContent !== text) label.textContent = text; },
      stop: function () { clearInterval(timer); return performance.now() - began; }
    };
  }

  // 2. The steps ----------------------------------------------------------------

  /**
   * A step as the list draws it. The stream sends {id, kind, status, label, meta, details}
   * (docs/lab_contract.md, "Agent shapes"); a step that takes time comes as running and
   * again under the same id when it resolves. A label such as "Ran the simulator: fuel at
   * $5.24 a gallon" is set as a head and the line beneath it.
   */
  function normalizeStep(raw, turn) {
    if (!raw || typeof raw !== "object" || raw.id == null || !STATUS_WORDS[raw.status]) return null;
    var label = String(raw.label || raw.summary || humanize(raw.kind || "step")).trim().replace(/\.$/, "");
    var cut = label.indexOf(": ");
    var args = raw.args && typeof raw.args === "object" ? raw.args : null;
    if (raw.kind === "tool" && looksLikeScenarioRequest(args) && turn.scenarioRequests.indexOf(args) < 0 && raw.status === "running") turn.scenarioRequests.push(args);
    return {
      id: String(raw.id), kind: String(raw.kind || "tool"), status: raw.status,
      label: cut > 0 ? label.slice(0, cut) : label,
      sub: cut > 0 ? label.charAt(cut + 2).toUpperCase() + label.slice(cut + 3) : "",
      meta: raw.meta == null ? "" : String(raw.meta),
      details: (Array.isArray(raw.details) ? raw.details : []).map(function (d) {
        return d && typeof d === "object" && d.label != null ? { label: humanize(d.label), meta: d.meta == null ? "" : String(d.meta) } : null;
      }).filter(Boolean)
    };
  }

  /** The status line over one list card: a row per step, each opening onto its details. */
  function taskList() {
    var uid = "steps-" + (++ask.turns);
    var list = el("ol", { class: "tasks-list", hidden: true });
    var region = expander(list, true);
    region.id = uid;
    var status = thinking();
    var head = el("div", { class: "tasks-head" }, [status.node]);
    var node = el("section", { class: "tasks", "aria-label": "Steps" }, [head, region]);
    var rows = {};
    var order = [];
    var lastEnter = 0;

    function mark(row) {
      var s = row.step;
      if (s.status === "running") {
        var ring = el("span", { class: "task-ring" });
        ring.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.5"><circle class="task-ring-track" cx="12" cy="12" r="10"/>' +
          '<circle class="task-ring-arc" cx="12" cy="12" r="10" pathLength="100" stroke-linecap="round"/></svg>';
        return el("span", { class: "task-mark", "aria-hidden": "true" }, [ring, el("span", { class: "task-num", text: String(row.number) })]);
      }
      var kind = s.status === "failed" ? "failed" : s.status === "retried" ? "retried" : "done";
      // The badge pops in when the step resolves, not each time its details are redrawn.
      var badge = el("span", { class: "task-badge is-" + kind + (row.drawn !== s.status ? " pop-in" : "") }, [icon(kind === "failed" ? "x" : kind === "retried" ? "again" : "check")]);
      return el("span", { class: "task-mark", "aria-hidden": "true" }, [badge]);
    }

    function draw(row) {
      var s = row.step;
      var kids = [
        mark(row),
        el("span", { class: "task-text" }, [
          el("span", { class: "task-label" }, [s.label, row.failed && s.status === "done" ? el("span", { class: "task-tag", text: "after a retry" }) : null, el("span", { class: "sr-only", text: ", " + STATUS_WORDS[s.status] })]),
          s.sub ? el("span", { class: "task-sub", text: s.sub }) : null
        ]),
        el("span", { class: "task-meta", text: s.meta || "" })
      ];
      if (s.details.length) {
        var detailsId = uid + "-" + row.number;
        var button = el("button", { type: "button", class: "task-row", "aria-expanded": String(row.open), "aria-controls": detailsId }, kids.concat([icon("chevron", "task-chevron")]));
        var details = expander(el("dl", { class: "task-details" }, s.details.map(function (d, i) {
          var line = el("div", { class: "task-detail" }, [el("dt", { text: d.label }), d.meta ? el("dd", { text: d.meta }) : null]);
          line.style.setProperty("--i", String(i));
          return line;
        })), row.open);
        details.id = detailsId;
        button.addEventListener("click", function () {
          row.open = !row.open;
          setOpen(details, button, row.open);
        });
        row.li.replaceChildren(button, details);
      } else {
        row.li.replaceChildren(el("div", { class: "task-row" }, kids));
      }
      row.li.dataset.status = s.status;
      row.drawn = s.status;
    }

    function upsert(step) {
      if (!step) return;
      var row = rows[step.id];
      if (!row) {
        row = rows[step.id] = { li: el("li", { class: "task fade-up" }), number: order.length + 1, open: false, failed: false, drawn: null, step: step };
        order.push(step.id);
        // Rows that arrive together still enter one after another.
        var now = performance.now();
        var delay = Math.max(0, lastEnter + 80 - now);
        lastEnter = now + delay;
        row.li.style.animationDelay = Math.round(delay) + "ms";
        list.hidden = false;
        list.appendChild(row.li);
      } else {
        // The same step, resolved: a later event may carry fewer details than an earlier one did.
        row.step = Object.assign({}, row.step, step, { details: step.details.length ? step.details : row.step.details });
      }
      if (step.status === "failed") row.failed = true;
      draw(row);
      var running = order.map(function (id) { return rows[id].step; }).filter(function (s) { return s.status === "running"; }).pop();
      if (running) status.set(running.label);
    }

    /** Fold the list to one line that says how much was done and can be reopened. */
    function close(word) {
      var ms = status.stop();
      var failed = 0;
      order.forEach(function (id) {
        var row = rows[id];
        if (row.step.status === "running") {
          row.step = Object.assign({}, row.step, word ? { status: "failed", meta: word } : { status: "done" });
          draw(row);
        }
        if (row.step.status === "failed") failed += 1;
      });
      if (!order.length) {
        node.hidden = true;
        return ms;
      }
      var text = order.length + (order.length === 1 ? " step" : " steps") + ", " + (word || duration(ms)) + (failed && !word ? ", " + failed + " failed" : "");
      var button = el("button", { type: "button", class: "tasks-summary", "aria-expanded": "true", "aria-controls": uid }, [el("span", { text: text }), icon("chevron")]);
      button.addEventListener("click", function () { setOpen(region, button, !region.classList.contains("is-open")); });
      head.replaceChildren(button);
      setOpen(region, button, !!word);
      return ms;
    }

    return {
      node: node,
      upsert: upsert,
      close: close,
      count: function () { return order.length; },
      clear: function () {
        rows = {};
        order = [];
        list.replaceChildren();
        list.hidden = true;
      }
    };
  }

  // 3. The answer ---------------------------------------------------------------

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
    var host = el("section", { class: "answer-chart" });
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
      var wrap = el("div", { class: "answer-charts" });
      scenarios.forEach(function (scenario, i) {
        var box = i === 0 ? host : el("section", { class: "answer-chart" });
        var request = looksLikeScenarioRequest(scenario.request) ? scenario.request
          : turn.scenarioRequests[Math.min(index + i, turn.scenarioRequests.length - 1)] || null;
        if (request && turn.requests.indexOf(request) < 0) turn.requests.push(request);
        wrap.appendChild(box);
        turn.afterAttach.push(function () {
          var charted = OUTCOMES.filter(function (o) { return o.metric === sim.view.metric; })[0];
          L.panel(box, {
            bare: true, legend: false, height: 190, headingLevel: "h4",
            // One run: the answer's own title says which. Two runs: each chart says which it is.
            title: scenarios.length > 1 && request ? describeRequest(request) : (charted ? charted.label : "The run") + ", by quarter", kicker: "Scenario the system ran",
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
          bare: true, legend: false, height: 220, headingLevel: "h4", kicker: "Grid the system ran",
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
          L.panel(host, Object.assign({ dataHref: results[0].url }, built.frame, { bare: true, legend: false, key: null, height: figureName === "track" ? 250 : 200, headingLevel: "h4" }), spec);
        });
      });
      return host;
    }
    return null;
  }

  /** Fold the server's result into the report it carries. */
  function reportOf(result) {
    if (!result || typeof result !== "object") return null;
    var report = result.report && typeof result.report === "object" ? Object.assign({}, result.report) : null;
    if (!report && (result.title || result.sections || result.unsupported)) report = Object.assign({}, result);
    if (!report && (result.answer || result.status === "not_supported")) report = {};
    if (!report) return null;
    ["verified", "charts", "evidence_ids", "note", "alternatives", "scenarios", "grids", "facts", "sources", "follow_ups"].forEach(function (key) {
      if (report[key] == null && result[key] != null) report[key] = result[key];
    });
    if (!report.summary && result.answer) report.summary = result.answer;
    report.status = result.status || report.status;
    return report;
  }

  function sourceIndex(report) {
    var index = {};
    (Array.isArray(report.sources) ? report.sources : []).forEach(function (source) {
      if (!source || typeof source !== "object" || !source.name) return;
      if (source.id != null) index[String(source.id)] = source;
      if (!index[String(source.name)]) index[String(source.name)] = source;
    });
    return index;
  }

  /** A source's name as a chip can carry it: the name when short, else its file or site. Nothing is cut. */
  function sourceShort(source) {
    var name = String(source.name || "");
    var detail = String(source.detail || "");
    if (name && name.length <= 18) return name;
    return detail && detail.length <= 24 && !/\s/.test(detail) ? detail : "";
  }

  /**
   * The summary as text and source chips. A chip follows the first figure that came from
   * each source, where the figure can be found in the sentence: three at most.
   */
  function summaryParts(report, onChip) {
    var text = String(report.summary || "");
    var sources = sourceIndex(report);
    var facts = (Array.isArray(report.facts) ? report.facts : []).filter(function (fact) { return fact && fact.id != null && fact.source != null; });
    // The summary's template names the facts it was filled from, in order ({f15}, {f17.p50}).
    // Without one, only figures that read as figures are looked for.
    var named = [];
    String((report.template && report.template.summary) || "").replace(/\{(f\d+)(?:\.[a-z0-9]+)?\}/gi, function (all, id) { named.push(id); return all; });
    var ordered = named.length
      ? named.map(function (id) { return facts.filter(function (fact) { return String(fact.id) === id; })[0]; }).filter(Boolean)
      : facts.filter(function (fact) { return /[$%]/.test(String(fact.text || "")); });
    var marks = [];
    var used = {};
    var cursor = 0;
    ordered.forEach(function (fact) {
      var whole = String(fact.text || "");
      var found = [whole, whole.replace(/\s*\(P10.*$/, "")].map(function (figure) {
        var at = figure ? text.indexOf(figure, cursor) : -1;
        return at < 0 ? null : { at: at, end: at + figure.length };
      }).filter(Boolean)[0];
      if (!found) return;
      cursor = found.end;
      var source = sources[String(fact.source)];
      var short = source ? sourceShort(source) : "";
      if (marks.length >= 3 || !short || used[source.name]) return;
      var end = found.end;
      while (/[.,;:)]/.test(text.charAt(end))) end += 1;
      marks.push({ at: end, source: source, short: short });
      used[source.name] = true;
    });
    var parts = [];
    var from = 0;
    marks.forEach(function (m) {
      parts.push({ text: text.slice(from, m.at) });
      parts.push({ node: el("button", { type: "button", class: "src-chip", text: m.short, title: "Source: " + m.source.name + (m.source.detail ? ", " + m.source.detail : ""),
        onclick: function () { onChip(m.source); } }) });
      from = m.at;
    });
    parts.push({ text: text.slice(from) });
    return parts;
  }

  /**
   * Show a paragraph word by word. The text is all there from the start (a screen reader has
   * it at once); only the eye waits, about 30 ms a word and never more than 1.5 s in all.
   */
  function reveal(p, parts, done) {
    var units = [];
    parts.forEach(function (part) {
      if (part.node) {
        part.node.classList.add("w");
        units.push(part.node);
        p.appendChild(part.node);
        return;
      }
      String(part.text || "").split(/(\s+)/).forEach(function (piece) {
        if (!piece) return;
        if (/^\s+$/.test(piece)) {
          p.appendChild(document.createTextNode(" "));
          return;
        }
        var word = el("span", { class: "w", text: piece });
        units.push(word);
        p.appendChild(word);
      });
    });
    if (reducedMotion || !units.length) {
      units.forEach(function (unit) { unit.classList.add("is-in"); });
      done();
      return;
    }
    var step = Math.min(32, 1500 / units.length);
    var caret = el("span", { class: "caret", "aria-hidden": "true" });
    var began = performance.now();
    var shown = 0;
    (function tick() {
      var due = Math.min(units.length, Math.floor((performance.now() - began) / step) + 1);
      while (shown < due) units[shown++].classList.add("is-in");
      if (shown < units.length) {
        units[shown - 1].after(caret);
        setTimeout(tick, step);
      } else {
        caret.remove();
        done();
      }
    })();
  }

  function figure(cell) {
    if (cell == null) return [el("b", { class: "num", text: fmt.missing })];
    if (typeof cell !== "object") return [el("b", { class: "num", text: String(cell) })];
    return [el("b", { class: "num", text: String(cell.text == null ? fmt.missing : cell.text), title: cell.label || null }), cell.range ? el("small", { class: "num", text: String(cell.range) }) : null];
  }

  /**
   * The table code built for the report: {columns, rows: [{label, cells: [{text, range}]}], note}.
   * One or two figures a row stay a table. More than that will not sit side by side in this
   * column with their ranges, so each row becomes a group with its figures on a two-column grid.
   */
  function answerTable(table) {
    if (!table || typeof table !== "object" || !Array.isArray(table.rows) || !table.rows.length) return null;
    var columns = (Array.isArray(table.columns) ? table.columns : []).map(String);
    var rows = table.rows.filter(function (row) { return row && Array.isArray(row.cells); });
    if (!rows.length) return null;
    var widest = Math.max.apply(null, rows.map(function (row) { return row.cells.length; }));
    var wrap = el("div", { class: "answer-table" });
    if (widest <= 2) {
      var head = el("tr", null, columns.slice(0, widest + 1).map(function (name, i) { return el("th", { scope: "col", class: i ? "num" : null, text: name }); }));
      var body = rows.map(function (row) {
        return el("tr", null, [el("th", { scope: "row", text: String(row.label == null ? "" : row.label) })].concat(row.cells.map(function (cell) { return el("td", { class: "num" }, figure(cell)); })));
      });
      wrap.appendChild(el("table", { class: "fact-table" }, [columns.length ? el("thead", null, [head]) : null, el("tbody", null, body)]));
    } else {
      rows.forEach(function (row) {
        var name = String(row.label == null ? "" : row.label);
        var mode = L.modes.filter(function (m) { return m.name === name; })[0];
        var dot = mode ? el("i", { class: "fact-dot", "aria-hidden": "true" }) : null;
        if (dot) dot.style.background = mode.color;
        wrap.appendChild(el("section", { class: "fact-group" }, [
          el("h4", { class: "fact-group-name" }, [dot, name]),
          el("dl", { class: "fact-grid" }, row.cells.map(function (cell, i) {
            return el("div", { class: "fact-cell" }, [el("dt", { text: columns[i + 1] || (cell && cell.label) || "" }), el("dd", null, figure(cell))]);
          }))
        ]));
      });
    }
    if (table.note) wrap.appendChild(el("p", { class: "answer-note", text: String(table.note) }));
    return wrap;
  }

  /** The answer as plain text, for the copy button. */
  function answerText(report) {
    var lines = [report.title, report.summary];
    var table = report.table && Array.isArray(report.table.rows) ? report.table : null;
    if (table) {
      var columns = (Array.isArray(table.columns) ? table.columns : []).map(String);
      table.rows.forEach(function (row) {
        if (!row || !Array.isArray(row.cells)) return;
        lines.push(String(row.label || "") + ": " + row.cells.map(function (cell, i) {
          var value = cell && typeof cell === "object" ? String(cell.text == null ? "" : cell.text) + (cell.range ? " (" + cell.range + ")" : "") : String(cell == null ? "" : cell);
          return (columns[i + 1] ? columns[i + 1] + " " : "") + value;
        }).join("; "));
      });
    }
    (Array.isArray(report.sections) ? report.sections : []).forEach(function (section) {
      if (section) lines.push(section.heading, section.text);
    });
    return lines.filter(Boolean).map(String).join("\n\n");
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var box = el("textarea", { class: "sr-only", "aria-hidden": "true", tabindex: "-1" });
      box.value = text;
      document.body.appendChild(box);
      box.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (error) { ok = false; }
      box.remove();
      if (ok) resolve(); else reject(new Error("copy is not available"));
    });
  }

  /** A link the server named is followed only to this site or over http(s). */
  function safeHref(href) {
    if (typeof href !== "string" || !href) return null;
    if (href.charAt(0) === "/" && href.charAt(1) !== "/") return { href: href, external: false };
    return /^https?:\/\//i.test(href) ? { href: href, external: true } : null;
  }

  /** What the answer rests on: the server's sources, or the quoted evidence when it sends none. */
  function sourcesList(report) {
    var rows = [];
    (Array.isArray(report.sources) ? report.sources : []).forEach(function (source) {
      if (!source || typeof source !== "object" || !source.name) return;
      var link = safeHref(source.href);
      var detail = source.detail || source.kind || "";
      var right = !detail ? null : link
        ? el("a", link.external ? { class: "source-detail", href: link.href, target: "_blank", rel: "noopener noreferrer", text: String(detail) } : { class: "source-detail", href: link.href, text: String(detail) })
        : el("span", { class: "source-detail", text: String(detail) });
      rows.push({ source: source, node: el("li", { class: "source" }, [el("span", { class: "source-name", text: String(source.name) }), right]) });
    });
    if (!rows.length) {
      knownIds(report.evidence_ids).forEach(function (id) {
        var fact = facts[id];
        rows.push({ node: el("li", { class: "source" }, [
          el("span", { class: "source-name", text: CLAIMS[fact.claim] || humanize(fact.claim) }),
          el("button", { type: "button", class: "source-detail", text: fmt.period(fact.period), onclick: function () { openEvidence([id]); } })
        ]) });
      });
    }
    return rows;
  }

  function unsupportedItem(item) {
    if (typeof item === "string") return el("li", { text: item });
    if (!item || typeof item !== "object") return null;
    var asked = item.asked || item.request || item.what || item.question || item.name;
    var why = item.reason || item.why;
    var li = el("li");
    if (asked) li.appendChild(el("b", { text: String(asked) }));
    if (why) li.appendChild(el("span", { text: (asked ? ": " : "") + String(why) }));
    return li.firstChild ? li : null;
  }

  function renderAnswer(host, report, turn, message) {
    host.replaceChildren();
    if (!report || typeof report !== "object") {
      L.state(host, "error", { compact: true, text: "The run finished without an answer." });
      return;
    }
    var after = [];
    var sources = sourcesList(report);
    var assumptions = (Array.isArray(report.assumptions) ? report.assumptions : []).filter(Boolean).map(String);
    var sourcesButton = null;
    var sourcesRegion = null;
    var showSource = function (source) {
      if (!sourcesRegion) return;
      setOpen(sourcesRegion, sourcesButton, true);
      var row = sources.filter(function (r) { return r.source === source; })[0];
      if (!row) return;
      row.node.classList.remove("is-marked");
      void row.node.offsetWidth;
      row.node.classList.add("is-marked");
      row.node.scrollIntoView({ block: "nearest", behavior: reducedMotion ? "auto" : "smooth" });
    };

    if (report.verified === false && (report.summary || report.title)) {
      host.appendChild(el("p", { class: "answer-warning", role: "alert", text: "Not verified. " + (report.note ? String(report.note) : "At least one number could not be matched to a model run, so treat this as unchecked.") }));
    }
    if (report.title) host.appendChild(el("h3", { class: "answer-title", text: String(report.title) }));
    var summary = report.summary ? el("p", { class: "answer-summary" }) : null;
    if (summary) host.appendChild(summary);

    var table = answerTable(report.table);
    if (table) after.push(table);
    (Array.isArray(report.sections) ? report.sections : []).forEach(function (section) {
      if (!section) return;
      var block = el("div", { class: "answer-section" });
      if (section.heading) block.appendChild(el("h4", { text: String(section.heading) }));
      String(section.text || "").split(/\n{2,}/).forEach(function (paragraph) {
        if (paragraph.trim()) block.appendChild(el("p", { class: "answer-text", text: paragraph.trim() }));
      });
      if (block.firstChild) after.push(block);
    });
    (Array.isArray(report.charts) ? report.charts : []).forEach(function (chart, i) {
      var node = reportChart(chart, turn, i, report);
      if (node) after.push(node);
    });
    var unsupported = (Array.isArray(report.unsupported) ? report.unsupported : []).map(unsupportedItem).filter(Boolean);
    if (unsupported.length) after.push(el("div", { class: "answer-section" }, [el("h4", { text: "What this system cannot run" }), el("ul", { class: "answer-list" }, unsupported)]));
    if (report.note && report.verified !== false) after.push(el("p", { class: "answer-note", text: String(report.note) }));
    after.forEach(function (node, i) {
      node.classList.add("fade-up");
      node.style.animationDelay = 60 + i * 60 + "ms";
      host.appendChild(node);
    });

    // Actions, sources and follow-ups wait for the text to finish.
    var ran = (Array.isArray(report.scenarios) ? report.scenarios : []).map(function (run) { return run && run.request; }).filter(looksLikeScenarioRequest);
    var request = turn.requests[0] || turn.scenarioRequests[0] || ran[0] || null;
    var copy = el("button", { type: "button", class: "act", "aria-label": "Copy the answer", title: "Copy the answer" }, [icon("copy")]);
    copy.addEventListener("click", function () {
      copyText(answerText(report)).then(function () {
        copy.replaceChildren(icon("check"));
        announce("Copied");
        setTimeout(function () { copy.replaceChildren(icon("copy")); }, 1600);
      }, function () { announce("Copying is not available in this browser"); });
    });
    var actions = el("div", { class: "answer-actions" }, [
      copy,
      el("button", { type: "button", class: "act", "aria-label": "Ask again", title: "Ask again", onclick: function () { submitQuestion(message); } }, [icon("again")]),
      request ? el("button", { type: "button", class: "act", title: "Set the controls to these inputs and run", onclick: function () { sendToSimulator(request); } }, [icon("open"), el("span", { text: "Send to the simulator" })]) : null
    ]);
    var foot = el("div", { class: "answer-foot" }, [actions]);
    var more = el("div", { class: "answer-actions" });
    var tail = [foot];
    /** A count under the actions that opens a list beneath it. */
    var disclosure = function (name, count, word, list) {
      var region = expander(list, false);
      region.id = name + "-" + ask.turns;
      var button = el("button", { type: "button", class: "act", "aria-expanded": "false", "aria-controls": region.id },
        [el("span", { text: count + " " + word + (count === 1 ? "" : "s") }), icon("chevron", "act-chevron")]);
      button.addEventListener("click", function () { setOpen(region, button, !region.classList.contains("is-open")); });
      more.appendChild(button);
      if (!more.parentNode) foot.appendChild(more);
      foot.appendChild(region);
      return { button: button, region: region };
    };
    if (sources.length) {
      var opened = disclosure("sources", sources.length, "source", el("ul", { class: "sources-list" }, sources.map(function (row) { return row.node; })));
      sourcesButton = opened.button;
      sourcesRegion = opened.region;
    }
    if (assumptions.length) disclosure("assumptions", assumptions.length, "assumption", el("ul", { class: "answer-list assumptions" }, assumptions.map(function (text) { return el("li", { text: text }); })));
    var instead = report.status === "not_supported" || !(Array.isArray(report.follow_ups) && report.follow_ups.length);
    var next = (instead ? report.alternatives : report.follow_ups) || [];
    next = (Array.isArray(next) ? next : []).map(function (option) {
      return typeof option === "string" ? option : option && (option.question || option.label || option.text);
    }).filter(Boolean).slice(0, 3);
    if (next.length) {
      tail.push(el("div", { class: "followups" }, [el("p", { class: "followups-label", text: instead ? "It can answer instead" : "Follow-ups" })].concat(next.map(function (question, i) {
        var row = el("button", { type: "button", class: "followup", onclick: function () { submitQuestion(String(question)); } }, [icon("reply"), el("span", { text: String(question) })]);
        row.style.animationDelay = i * 90 + "ms";
        return row;
      }))));
    }
    var finish = function () {
      tail.forEach(function (node) {
        node.classList.add("is-in");
        host.appendChild(node);
      });
    };
    if (summary) reveal(summary, summaryParts(report, showSource), finish);
    else finish();
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

  /** Put a new turn at the top of the panel, with room beneath it for the answer to arrive into. */
  function pin(node) {
    var scroller = $("console-body");
    if (!scroller) return;
    if (/(auto|scroll)/.test(getComputedStyle(scroller).overflowY)) {
      Array.prototype.forEach.call(scroller.querySelectorAll(".turn"), function (turn) { turn.style.minHeight = ""; });
      node.style.minHeight = Math.max(0, scroller.clientHeight - parseFloat(getComputedStyle(scroller).paddingBottom)) + "px";
      var top = node.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
      scroller.scrollTo({ top: top, behavior: reducedMotion ? "auto" : "smooth" });
    } else {
      node.scrollIntoView({ block: "nearest", behavior: reducedMotion ? "auto" : "smooth" });
    }
  }

  function runTurn(message) {
    var turns = $("ask-turns");
    var tasks = taskList();
    var answerHost = el("section", { class: "answer", "aria-label": "Answer" });
    var node = el("article", { class: "turn" }, [el("p", { class: "turn-question", text: message }), tasks.node, answerHost]);
    var turn = { scenarioRequests: [], requests: [], afterAttach: [], drawn: {}, done: false };
    turns.appendChild(node);
    pin(node);

    function finish(report) {
      if (turn.done) return;
      turn.done = true;
      tasks.close();
      renderAnswer(answerHost, report, turn, message);
      announce("Answer ready");
      ask.history.push({ role: "user", content: message });
      ask.history.push({ role: "assistant", content: String((report && (report.summary || report.title)) || "") });
    }

    function fail(kind, text, title) {
      if (turn.done) return;
      turn.done = true;
      tasks.close("stopped");
      L.state(answerHost, kind, { compact: true, title: title || "The run stopped", text: text });
    }

    function settle(result) {
      if (!result || typeof result !== "object") return fail("error", "The answer was empty.");
      if (result.status === "not_configured") return fail("missing", "No language model is connected, so questions cannot be answered here. The simulator works without it.", "Not available here");
      if (!FIXTURES && hasFixtureFlag(result)) return fail("error", "The server returned placeholder data, so it is not shown as an answer.");
      var report = reportOf(result);
      if (report) return finish(report);
      return fail("error", typeof result.detail === "string" ? result.detail : "The run finished without an answer.");
    }

    function onEvent(payload, type) {
      if (!payload || typeof payload !== "object" || turn.done) return;
      if (type === "error" || payload.status === "error") fail("error", String(payload.detail || payload.message || "The server reported an error."));
      else if (type === "step") tasks.upsert(normalizeStep(payload.step || payload, turn));
      else settle(payload);
    }

    /** A whole result at once (no stream): its steps are already done, so they are listed and the answer follows. */
    function oneShot(result) {
      if (!tasks.count()) (result && Array.isArray(result.steps) ? result.steps : []).forEach(function (step) { tasks.upsert(normalizeStep(step, turn)); });
      settle(result);
    }

    if (FIXTURES) {
      return Promise.all([load("chat_stream"), load("chat_report")]).then(function (results) {
        var bad = results.filter(function (x) { return x.status !== "ok"; })[0];
        if (bad) return fail(bad.status === "missing" ? "missing" : "error", "The placeholder run has not been written (" + bad.url + ").", "Placeholder run missing");
        // The placeholder stream is played at its own recorded pace, so the steps can be watched resolving.
        var chain = Promise.resolve();
        (results[0].data.events || []).forEach(function (event) {
          var pause = event.status === "running" ? 150 : Math.max(150, Math.min(1500, Number(event.elapsed_ms) || 0));
          chain = chain.then(function () {
            return new Promise(function (resolve) { setTimeout(resolve, pause); });
          }).then(function () { onEvent(event, "step"); });
        });
        return chain.then(function () { settle(results[1].data); });
      });
    }

    var body = { message: message, history: ask.history.slice(-12) };
    if (ask.model != null) body.model = ask.model;
    var refused = function (response, json) {
      if (response.status === 429) {
        var seconds = Number(response.headers.get("Retry-After"));
        return fail("error", "Questions are limited to a few at a time." + (isNum(seconds) && seconds > 0 ? " Try again in " + Math.ceil(seconds) + " s." : " Try again shortly."), "One moment");
      }
      return fail("error", json && typeof json.detail === "string" ? json.detail : "The server answered " + response.status + ".");
    };
    var fallback = function () {
      return post("/chat", body).then(function (response) {
        if (response.status === 404 || response.status === 405) return fail("missing", "The question service is not on this server yet. The simulator works without it.", "Not available here");
        return response.json().then(function (json) {
          if (!response.ok && !(json && json.status === "not_configured")) return refused(response, json);
          return oneShot(json);
        }, function () { return response.status === 429 ? refused(response, null) : fail("error", "The server answered " + response.status + " with something that is not JSON."); });
      }, function () { return fail("error", "The server could not be reached."); });
    };
    return post("/chat/stream", body, "text/event-stream").then(function (response) {
      var type = response.headers.get("content-type") || "";
      if (response.ok && type.indexOf("text/event-stream") >= 0 && response.body && response.body.getReader) {
        // A stream that ends or breaks before its answer (a buffering proxy, a dropped line):
        // clear the half-told list and ask again in one piece.
        var again = function () {
          if (turn.done) return null;
          tasks.clear();
          turn.scenarioRequests = [];
          return fallback();
        };
        return readEventStream(response, onEvent).then(again, again);
      }
      if (response.ok && type.indexOf("application/json") >= 0) return response.json().then(oneShot);
      if (response.status === 404 || response.status === 405) return fallback();
      if (response.status === 429) return response.json().then(function (json) { return refused(response, json); }, function () { return refused(response, null); });
      return response.json().then(function (json) { return refused(response, json); }, fallback);
    }, fallback);
  }

  function submitQuestion(message) {
    message = String(message || "").trim();
    if (!message || ask.busy) return;
    var simulatorView = $("view-simulator");
    if (simulatorView && simulatorView.classList.contains("is-console-closed")) toggleConsole(true);
    ask.busy = true;
    if (ask.composer) ask.composer.clear();
    $("ask-suggest").hidden = true;
    var done = function () {
      ask.busy = false;
      if (ask.composer) ask.composer.refresh();
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

  // 4. The composer -------------------------------------------------------------

  /**
   * A list that opens above the composer, with one highlight that glides between its rows.
   * The keyboard stays on the control that opened it, which names the active row to a
   * screen reader through aria-activedescendant.
   */
  function popup(host, owner, options) {
    var highlight = el("span", { class: "menu-highlight", "aria-hidden": "true" });
    var list = el("div", { class: "menu-list", role: "listbox", id: host.id + "-list", "aria-label": options.label }, [highlight]);
    host.replaceChildren.apply(host, [options.head || null, list, options.foot ? el("p", { class: "menu-foot", text: options.foot }) : null].filter(Boolean));
    var state = { items: [], nodes: [], active: -1 };

    function setActive(index, instant) {
      state.active = index;
      state.nodes.forEach(function (node, i) { node.setAttribute("aria-selected", String(i === index)); });
      var node = state.nodes[index];
      if (!node) {
        highlight.style.opacity = "0";
        owner.removeAttribute("aria-activedescendant");
        return;
      }
      owner.setAttribute("aria-activedescendant", node.id);
      // The first placement is not a glide.
      if (instant) highlight.classList.add("is-instant");
      highlight.style.opacity = "1";
      highlight.style.height = node.offsetHeight + "px";
      highlight.style.transform = "translateY(" + node.offsetTop + "px)";
      if (instant) {
        void highlight.offsetWidth;
        highlight.classList.remove("is-instant");
      }
    }

    function open(items, active) {
      state.items = items;
      state.nodes = items.map(function (item, i) {
        var node = el("div", { class: "menu-item" + (item.mono ? " is-command" : ""), role: "option", id: host.id + "-" + i, "aria-selected": "false", "aria-disabled": item.disabled ? "true" : null }, [
          el("span", { class: "menu-name", text: item.name }),
          item.note ? el("span", { class: "menu-note", text: item.note }) : null,
          item.checked ? icon("check", "menu-check") : null,
          item.checked ? el("span", { class: "sr-only", text: ", current" }) : null
        ]);
        node.addEventListener("mousemove", function () { if (!item.disabled && state.active !== i) setActive(i); });
        // The press is taken on mousedown so the control that opened the list keeps the keyboard.
        node.addEventListener("mousedown", function (event) { event.preventDefault(); });
        node.addEventListener("click", function () { if (!item.disabled) options.onPick(item); });
        return node;
      });
      list.replaceChildren.apply(list, [highlight].concat(state.nodes.length || !options.empty ? state.nodes : [el("p", { class: "menu-empty", text: options.empty })]));
      host.hidden = false;
      owner.setAttribute("aria-expanded", "true");
      var first = isNum(active) && active >= 0 ? active : items.map(function (item) { return !item.disabled; }).indexOf(true);
      setActive(first, true);
    }

    function close() {
      if (host.hidden) return;
      host.hidden = true;
      owner.setAttribute("aria-expanded", "false");
      owner.removeAttribute("aria-activedescendant");
      state.active = -1;
    }

    function move(delta) {
      var n = state.items.length;
      if (!n) return;
      var i = state.active;
      for (var tries = 0; tries < n; tries++) {
        i = (i + delta + n) % n;
        if (!state.items[i].disabled) return setActive(i);
      }
    }

    return {
      open: open, close: close, move: move,
      isOpen: function () { return !host.hidden; },
      pick: function () {
        var item = state.items[state.active];
        if (item && !item.disabled) options.onPick(item);
        return !!item;
      }
    };
  }

  function initComposer(status) {
    var form = $("ask-form");
    var input = $("ask-input");
    var send = $("ask-run");
    var commandButton = $("command-btn");
    var modelButton = $("model-btn");
    var commands = (status && Array.isArray(status.commands) ? status.commands : []).filter(function (c) { return c && typeof c === "object" && c.name; }).map(function (c) {
      return { name: "/" + String(c.name).replace(/^\//, ""), note: c.description ? String(c.description) : "", template: typeof c.template === "string" ? c.template : "", mono: true };
    });
    var models = (status && Array.isArray(status.models) ? status.models : []).filter(function (m) { return m && typeof m === "object" && m.id != null && m.label; });
    var measure = document.createElement("canvas").getContext("2d");

    /** One line shares its row with the controls; text that needs more takes the full width and they drop below. */
    function layout() {
      var style = getComputedStyle(input);
      var value = input.value;
      var controls = [commandButton, modelButton, send].reduce(function (width, node) { return width + (node.hidden ? 0 : node.offsetWidth + 6); }, 0);
      var room = form.clientWidth - parseFloat(getComputedStyle(form).paddingLeft) * 2 - controls - parseFloat(style.paddingLeft) * 2 - 2;
      measure.font = style.fontWeight + " " + style.fontSize + " " + style.fontFamily;
      if (form.clientWidth) form.classList.toggle("is-stacked", value.indexOf("\n") >= 0 || measure.measureText(value).width > room);
      input.style.height = "auto";
      input.style.height = Math.min(100, input.scrollHeight) + "px";
      send.disabled = ask.busy || !value.trim();
    }

    function useCommand(item) {
      input.value = item.template;
      commandMenu.close();
      layout();
      input.focus();
      // The first blank in the template is selected, ready to be typed over.
      var blank = /\{[^{}]*\}|\[[^\[\]]*\]|<[^<>]*>|_{2,}/.exec(input.value);
      if (blank) input.setSelectionRange(blank.index, blank.index + blank[0].length);
      else input.setSelectionRange(input.value.length, input.value.length);
    }

    var commandMenu = popup($("command-menu"), input, { label: "Commands", foot: "Up and down to move, Enter or Tab to pick, Esc to close", onPick: useCommand });
    var modelMenu = popup($("model-menu"), modelButton, { label: "Model", onPick: function (item) {
      ask.model = item.id;
      $("model-label").textContent = item.name;
      modelMenu.close();
      layout();
    } });

    function openCommands(typed) {
      var filter = String(typed || "").toLowerCase();
      var found = commands.filter(function (c) { return c.name.toLowerCase().indexOf(filter) === 0; });
      modelMenu.close();
      if (found.length) commandMenu.open(found);
      else commandMenu.close();
    }

    function openModels() {
      commandMenu.close();
      var items = models.map(function (m) {
        return { id: m.id, name: String(m.label), note: m.note ? String(m.note) : "", disabled: m.available === false, checked: m.id === ask.model };
      });
      modelMenu.open(items, items.map(function (item) { return item.checked; }).indexOf(true));
    }

    commandButton.hidden = !commands.length;
    if (commands.length) {
      commandButton.addEventListener("click", function () {
        if (commandMenu.isOpen()) commandMenu.close();
        else openCommands("/");
        input.focus();
      });
    }
    modelButton.hidden = !models.length;
    if (models.length) {
      var current = models.filter(function (m) { return m.available !== false && m.id === status.model; })[0] || models.filter(function (m) { return m.available !== false; })[0];
      if (current) {
        ask.model = current.id;
        $("model-label").textContent = String(current.label);
      } else {
        modelButton.hidden = true;
      }
      modelButton.addEventListener("click", function () {
        if (modelMenu.isOpen()) modelMenu.close();
        else openModels();
      });
      modelButton.addEventListener("keydown", function (event) {
        if (!modelMenu.isOpen()) {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            openModels();
          }
          return;
        }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          modelMenu.move(event.key === "ArrowDown" ? 1 : -1);
        } else if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          modelMenu.pick();
        } else if (event.key === "Escape" || event.key === "Tab") {
          if (event.key === "Escape") event.preventDefault();
          modelMenu.close();
        }
      });
    }

    input.addEventListener("input", function () {
      layout();
      var typed = /^\/\S*$/.exec(input.value);
      if (typed && commands.length) openCommands(typed[0]);
      else commandMenu.close();
    });
    input.addEventListener("keydown", function (event) {
      if (event.isComposing) return;
      if (commandMenu.isOpen()) {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          commandMenu.move(event.key === "ArrowDown" ? 1 : -1);
          return;
        }
        if (event.key === "Enter" || event.key === "Tab") {
          event.preventDefault();
          commandMenu.pick();
          return;
        }
        if (event.key === "Escape") {
          event.preventDefault();
          commandMenu.close();
          return;
        }
      }
      if (event.key === "Escape" && modelMenu.isOpen()) modelMenu.close();
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        submitQuestion(input.value);
      }
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      submitQuestion(input.value);
    });
    document.addEventListener("pointerdown", function (event) {
      var target = event.target && event.target.closest ? event.target : form;
      if (!target.closest("#model-menu, #model-btn")) modelMenu.close();
      if (!target.closest("#command-menu, #command-btn, #ask-input")) commandMenu.close();
    });
    form.addEventListener("focusout", function (event) {
      if (event.relatedTarget && form.contains(event.relatedTarget)) return;
      if (event.relatedTarget) {
        commandMenu.close();
        modelMenu.close();
      }
    });
    if (global.ResizeObserver) new ResizeObserver(layout).observe(form);
    layout();

    ask.composer = {
      refresh: layout,
      clear: function () {
        input.value = "";
        commandMenu.close();
        modelMenu.close();
        layout();
      }
    };
  }

  function initAsk() {
    var form = $("ask-form");
    if (!form) return;
    $("console-toggle").addEventListener("click", function () { toggleConsole(); });
    // Before anything is asked: a short list of questions to start from.
    var suggest = $("ask-suggest");
    SUGGESTIONS.forEach(function (question) {
      suggest.appendChild(el("button", { type: "button", class: "followup", onclick: function () { submitQuestion(question); } }, [icon("reply"), el("span", { text: question })]));
    });
    // On placeholder data the console's own status (models, commands) is a placeholder file too.
    var status = FIXTURES ? load("status").then(function (result) { return result.status === "ok" ? result.data : null; }) : serverStatus;
    status.then(function (data) { initComposer(data || {}); });
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
      if (isNum(ft.mae_clock_on_statement_date)) fuelStats.appendChild(stat("Fuel model on the same day, average miss", [fmt.usd(ft.mae_clock_on_statement_date), el("small", { text: " a gallon" })]));
      if (isNum(ft.mae_marked)) fuelStats.appendChild(stat("Fuel model at quarter end, average miss", [fmt.usd(ft.mae_marked), el("small", { text: " a gallon" })]));
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
    seal.appendChild(el("span", null, [el("b", { text: "Frozen " }), "before the report"]));   // the exact time is in the drawer
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
          textBlock("What it proves", "The fingerprint changes if a single character of the estimate changes." + (c.timestamp_proof ? " The timestamp proof ties that fingerprint to a public record of the time." : " Published before the report, it shows the estimate was not changed afterwards.")),
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
      list.forEach(function (result) {
        r[result.name] = result;
        if (result.status === "ok") r.ctx[result.name] = result.data;
      });
      indexEvidence(r.evidence);
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
    ask: { run: submitQuestion, renderAnswer: renderAnswer, readEventStream: readEventStream, state: ask }
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})(window);
