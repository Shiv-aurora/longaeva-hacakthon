/* The guided tour of the simulator view. It uses driver.js (vendor/driver.iife.js) and only points at
   what is already on the page: it changes no control and runs nothing. It starts by itself on a first
   visit and from the Tour button after that. ?tour=1 forces it, ?tour=0 keeps it away. */
(function (global) {
  "use strict";

  var SEEN = "lab-tour-seen";

  var STEPS = [
    { title: "United Airlines, simulated", text: "A model of the airline as a business, run month by month across 10,000 possible futures. This tour takes under a minute." },
    { target: "#scenario", side: "right", title: "Pick a scenario", text: "Start from a ready-made one." },
    { target: "#sim-form .controls", side: "right", title: "Or build your own", text: "Move fuel, demand, capacity or aircraft deliveries. The simulation reruns as you go." },
    { target: "#mode-key", side: "right", title: "How United reacts", text: "The same futures, three ways: no fare response, what management says, and what its history shows." },
    { target: "#outcomes", side: "bottom", title: "The result", text: "The middle outcome for each measure, with the range that holds 80% of the futures." },
    { target: "#main-chart", side: "top", title: "Three questions", text: "What if this happens, what breaks the plan, and what must be true to hit a target." },
    { target: "#console", side: "left", title: "Ask in plain words", text: "The assistant plans the work, runs the simulator and reports back with its numbers." },
    { target: "#view-nav", side: "bottom", title: "Check the model", text: "Behavior shows the evidence behind it. Reality check replays it on past quarters and holds a forecast frozen before the next report." },
    { target: ".toplink-box", side: "bottom", title: "The method", text: "The Technical Report covers how it works and where it falls short." }
  ];

  function seen() {
    try { return global.localStorage.getItem(SEEN) === "1"; } catch (error) { return true; }
  }

  function markSeen() {
    try { global.localStorage.setItem(SEEN, "1"); } catch (error) { /* without storage the tour is offered by the button only */ }
  }

  function onScreen(node) {
    return !!node && node.getClientRects().length > 0;
  }

  /** Steps whose target is on screen now; the first one has no target and sits in the middle. */
  function steps() {
    return STEPS.filter(function (step) {
      return !step.target || onScreen(document.querySelector(step.target));
    }).map(function (step) {
      var popover = { title: step.title, description: step.text };
      if (!step.target) return { popover: popover };
      popover.side = step.side;
      popover.align = "start";
      return { element: step.target, popover: popover };
    });
  }

  function start() {
    var api = global.driver && global.driver.js && global.driver.js.driver;
    if (!api) return;
    if (global.Lab && global.Lab.view) global.Lab.view("simulator");
    var still = global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // One frame so the simulator view is laid out before the steps are measured.
    global.requestAnimationFrame(function () {
      var list = steps();
      if (list.length < 2) return;
      api({
        steps: list,
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
        onDestroyed: markSeen
      }).drive();
    });
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
