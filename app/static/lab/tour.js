/* The guided tour. It uses driver.js (vendor/driver.iife.js) and drives the page as it goes: it loads a
   scenario, opens each chart, has the visitor put one question to the assistant, and shows the other two
   views. It starts by itself on a first visit and from the Tour button after that. ?tour=1 forces it,
   ?tour=0 keeps it away. */
(function (global) {
  "use strict";

  var SEEN = "lab-tour-seen";
  var QUESTION = 1;   // the second suggested question: fuel up 30%, capacity down 3%

  function view(name) {
    if (global.Lab && global.Lab.view) global.Lab.view(name);
  }

  function tab(name) {
    view("simulator");
    var button = document.getElementById("tab-" + name);
    if (button) button.click();
  }

  function preset(id) {
    view("simulator");
    if (global.Lab && global.Lab.sim && global.Lab.sim.preset) global.Lab.sim.preset(id);
  }

  function onScreen(node) {
    return !!node && node.getClientRects().length > 0;
  }

  function question() {
    var list = document.querySelectorAll("#ask-suggest .followup");
    return onScreen(list[QUESTION]) ? list[QUESTION] : null;
  }

  function openConsole() {
    view("simulator");
    var toggle = document.getElementById("console-toggle");
    if (toggle && toggle.getAttribute("aria-expanded") === "false") toggle.click();
  }

  /* before: what the page must show before the step is drawn. ask: the step waits on the suggested question. */
  var STEPS = [
    { title: "United Airlines, simulated", text: "A model of the airline as a business, run month by month across 10,000 possible futures. This tour drives the page for you." },
    { target: "#scenario", side: "right", title: "Pick a scenario", text: "Ready-made starting points. Next loads “Demand weakens”.", before: function () { view("simulator"); } },
    { target: "#sim-form .controls", side: "right", title: "The levers moved", text: "Demand is now 5% lower. Move fuel, demand, capacity or aircraft deliveries yourself at any time.", before: function () { preset("demand"); } },
    { target: "#mode-key", side: "right", title: "How United reacts", text: "The same futures, three ways: no fare response, what management says, and what its history shows.", before: function () { view("simulator"); } },
    { target: "#outcomes", side: "bottom", title: "The result", text: "The middle outcome for each measure, with the range that holds 80% of the futures.", before: function () { view("simulator"); } },
    { target: "#main-chart", side: "left", title: "What if", text: "The path of the outcome under each reaction. The gap between the lines is what the fare response is worth.", before: function () { tab("whatif"); } },
    { target: "#main-chart", side: "left", title: "What breaks", text: "Every mix of fuel price and demand, and the share of futures where cash falls below the line you set.", before: function () { tab("breaks"); } },
    { target: "#main-chart", side: "left", title: "What must be true", text: "The same map read the other way: the conditions under which a target is still met.", before: function () { tab("target"); } },
    { ask: true, side: "left", title: "Now ask it something", text: "Click the highlighted question, or press Ask it.", next: "Ask it", before: function () { tab("whatif"); openConsole(); } },
    { target: "#console", side: "left", title: "Watch it work", text: "It plans the steps, runs the simulator and writes up the answer with its numbers.", before: openConsole },
    { target: "#view-behaviour", side: "over", title: "Behavior", text: "The evidence. How United has actually responded in the past: what it pays for fuel, how fast fares make it back, how demand moves. These measurements drive the simulator.", before: function () { view("behaviour"); } },
    { target: "#view-reality", side: "over", title: "Reality check", text: "The test. The same method replayed on past quarters against what United reported, and a forecast frozen before the next report.", before: function () { view("reality"); } },
    { target: ".toplink-box", side: "bottom", title: "The method", text: "The Technical Report covers how it works and where it falls short." }
  ];

  function seen() {
    try { return global.localStorage.getItem(SEEN) === "1"; } catch (error) { return true; }
  }

  function markSeen() {
    try { global.localStorage.setItem(SEEN, "1"); } catch (error) { /* without storage the tour is offered by the button only */ }
  }

  function start() {
    var api = global.driver && global.driver.js && global.driver.js.driver;
    if (!api) return;
    view("simulator");
    var still = global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // The question step is left out once something has been asked: the suggestions are gone by then.
    var plan = STEPS.filter(function (step) { return !step.ask || question(); });
    var tour = null, moving = false;

    function go(index) {
      if (moving || index < 0) return;
      if (index >= plan.length) return tour.destroy();
      var step = plan[index];
      if (step.before) step.before();
      moving = true;
      // A view change lands on the next tick; wait for it so the step is measured on the right page.
      global.setTimeout(function () {
        moving = false;
        if (!tour.isActive()) return;
        tour.moveTo(index);
        if (step.ask) waitForQuestion(index);
      }, step.before ? 120 : 0);
    }

    /** The visitor's own click on the suggested question sends it; the tour then moves on to the answer. */
    function waitForQuestion(index) {
      var button = question();
      if (!button || button.getAttribute("data-tour") === "1") return;
      button.setAttribute("data-tour", "1");
      button.addEventListener("click", function () {
        global.setTimeout(function () { if (tour.isActive()) go(index + 1); }, 450);
      }, { once: true });
    }

    tour = api({
      steps: plan.map(function (step) {
        var popover = { title: step.title, description: step.text };
        if (step.next) popover.nextBtnText = step.next;
        if (!step.target && !step.ask) return { popover: popover };
        popover.side = step.side;
        popover.align = step.side === "over" ? "center" : "start";
        return {
          element: step.ask ? function () { return question() || document.getElementById("console"); } : step.target,
          popover: popover
        };
      }),
      animate: !still,
      smoothScroll: !still,
      showProgress: true,
      progressText: "{{current}} of {{total}}",
      nextBtnText: "Next",
      prevBtnText: "Back",
      doneBtnText: "Done",
      popoverClass: "lab-tour",
      overlayOpacity: 0.5,
      stagePadding: 6,
      stageRadius: 12,
      popoverOffset: 12,
      onNextClick: function () {
        var index = tour.getActiveIndex();
        var button = plan[index].ask ? question() : null;
        if (button) button.click(); else go(index + 1);
      },
      onPrevClick: function () { go(tour.getActiveIndex() - 1); },
      onDestroyed: function () {
        markSeen();
        tab("whatif");
      }
    });
    tour.drive();
  }

  /** Start once the first run is on the page, or give up quietly after eight seconds. */
  function startWhenDrawn() {
    var tries = 0;
    var timer = global.setInterval(function () {
      var outcomes = document.getElementById("outcomes");
      tries += 1;
      if (outcomes && outcomes.children.length) {
        global.clearInterval(timer);
        global.setTimeout(start, 400);
      } else if (tries > 40) {
        global.clearInterval(timer);
      }
    }, 200);
  }

  function boot() {
    if (document.body.getAttribute("data-page") !== "index") return;
    var button = document.getElementById("tour-btn");
    if (button) {
      button.hidden = false;
      button.addEventListener("click", start);
    }
    var asked = /[?&]tour=([01])\b/.exec(global.location.search);
    if (asked ? asked[1] === "1" : !seen() && !global.navigator.webdriver) startWhenDrawn();
  }

  global.LabTour = { start: start };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})(window);
