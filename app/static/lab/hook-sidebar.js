/*
 * Hook Sidebar for plain HTML, CSS and JavaScript.
 *
 * Adapted from Rare UI's "Hook Sidebar" component (https://rareui.com).
 * Copyright (c) 2026 Swami Malode.
 * License: MIT + Commons Clause + Attribution; the full text is in
 * LICENSE-hook-sidebar.txt beside this file. The credit and the copyright line
 * above must stay in this file, and the site shows a link to https://rareui.com.
 *
 * The original is a React component animated with the `motion` library. This is
 * a port, not the original source. It keeps the geometry and the motion: a rail
 * one pixel wide, two pixels in from the left edge, running from the top of the
 * list to six pixels above the middle of the active item, where a 12 by 7 corner
 * (the path below, radius 6) hooks into the label; dashes two pixels on and two
 * off; a second, fainter rail for the hovered or focused item, which draws only
 * its corner when that item is at or above the active one and otherwise runs
 * down from the active item; the spring (stiffness 420, damping 34, mass 0.7)
 * on the rail's top, height and corner; the 0.2 second fade; the item box
 * (6px 8px 6px 20px, radius 8, two pixels apart, 14px text); and no movement at
 * all under prefers-reduced-motion. The rails start at the top and travel to
 * their place on first measure, as the original does.
 *
 * What is approximated:
 *   - React and `motion` are replaced by the DOM and a small spring integrator
 *     (fixed one-millisecond steps), so paths agree with `motion` to well under
 *     a pixel but not bit for bit.
 *   - The dashed vertical rail is an SVG line with the same 2-on, 2-off pattern,
 *     anchored at the bottom as in the original, where the original paints a
 *     repeating gradient. This site's stylesheets carry no gradients.
 *   - The fade is a CSS opacity transition of 0.2 seconds with an ease-out
 *     curve; `motion` picks its own default easing for a timed animation.
 *   - Colors come from the host page through CSS variables (see
 *     hook-sidebar.css). The original defaults to #FC4C01 for the active rail
 *     and to half-strength text for the other items; here the resting text
 *     color is a variable so a host can keep its contrast.
 *   - There is no router. The original marks the link whose href equals the
 *     current path; here the host says which item is active (setActive), and
 *     `current` chooses the aria-current token.
 *   - The original measures its items only when a ResizeObserver reports; here
 *     the list is also measured once at mount, so the active item is marked
 *     before the first frame and in a tab that is not being shown.
 *
 * Use:
 *   <nav class="hook-sidebar" aria-label="Sections">
 *     <div class="hook-list">
 *       <a class="hook-item" href="#one">One</a>     (or <button class="hook-item">)
 *     </div>
 *   </nav>
 *   var bar = HookSidebar.mount(nav, {
 *     active: 0,                              // default: the item with aria-current, else the first
 *     current: "location",                    // aria-current token; default "page" for links, "true" for buttons
 *     onChange: function (index, element) {}  // a person chose an item
 *   });
 *   bar.setActive(2);                 // from outside; does not call onChange
 *   bar.setActive(0, {jump: true});   // no travel
 *   bar.active                        // current index, -1 for none
 */
(function (global) {
  "use strict";

  // The hook: down the rail, round a corner of radius 6, out to the label.
  var CORNER = 6;
  var CORNER_PATH = "M0.5 0a6 6 0 0 0 6 6H12";
  var CORNER_W = 12;
  var CORNER_H = 7;
  // Two pixels on, two pixels off.
  var DASH = "2 2";
  // The original's spring.
  var SPRING = { stiffness: 420, damping: 34, mass: 0.7 };

  var SVG_NS = "http://www.w3.org/2000/svg";
  var STEP = 0.001;        // integrator step, seconds
  var MAX_FRAME = 0.064;   // a stalled tab does not get one huge step
  var REST_DELTA = 0.01;   // pixels
  var REST_SPEED = 0.1;    // pixels per second

  function reducedMotion() {
    return !!global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function svg(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    return node;
  }

  function spring(value) {
    return { x: value, v: 0, target: value };
  }

  // One rail: a vertical line from `top` for `height` pixels, and the corner below it.
  function createRail(kind, dashed) {
    var el = document.createElement("span");
    el.className = "hook-rail hook-rail-" + kind;
    el.setAttribute("aria-hidden", "true");

    var line = svg("svg", { class: "hook-rail-line", width: 1, height: 0, focusable: "false" });
    var stroke = { x1: 0.5, y1: "100%", x2: 0.5, y2: 0, stroke: "currentColor" };
    if (dashed) {
      // Drawn upward from the bottom and started two pixels into the pattern, so the
      // first two pixels above the corner are a gap, as with the original's gradient.
      stroke["stroke-dasharray"] = DASH;
      stroke["stroke-dashoffset"] = 2;
    }
    line.appendChild(svg("line", stroke));

    var corner = svg("svg", { class: "hook-rail-corner", width: CORNER_W, height: CORNER_H, viewBox: "0 0 " + CORNER_W + " " + CORNER_H, fill: "none", focusable: "false" });
    var path = { d: CORNER_PATH, stroke: "currentColor" };
    if (dashed) path["stroke-dasharray"] = DASH;
    corner.appendChild(svg("path", path));

    el.appendChild(line);
    el.appendChild(corner);
    return { el: el, line: line, corner: corner, top: spring(0), height: spring(0), cornerTop: spring(-CORNER) };
  }

  // Where a rail should sit for a start point and the middle of its item (null for none).
  function aim(rail, from, y) {
    var at = y === null ? 0 : y;
    rail.top.target = from;
    rail.height.target = Math.max(0, at - CORNER - from);
    rail.cornerTop.target = at - CORNER;
  }

  function paint(rail) {
    rail.line.style.top = rail.top.x + "px";
    rail.line.style.height = Math.max(0, rail.height.x) + "px";
    rail.corner.style.top = rail.cornerTop.x + "px";
  }

  function mount(nav, options) {
    options = options || {};
    var list = nav.querySelector(".hook-list");
    if (!list) throw new Error("HookSidebar: the element has no .hook-list");
    var items = Array.prototype.slice.call(list.querySelectorAll(".hook-item"));
    var dashed = options.dashed !== false;

    // The hover rail sits under the active one.
    var hoverRail = createRail("hover", dashed);
    var activeRail = createRail("active", dashed);
    list.insertBefore(activeRail.el, list.firstChild);
    list.insertBefore(hoverRail.el, activeRail.el);
    var springs = [hoverRail.top, hoverRail.height, hoverRail.cornerTop, activeRail.top, activeRail.height, activeRail.cornerTop];

    var marked = items.map(function (el) { return el.hasAttribute("aria-current"); }).indexOf(true);
    var state = {
      active: typeof options.active === "number" ? options.active : marked >= 0 ? marked : 0,
      hover: null,
      pointerInside: false,
      focusInside: false,
      centers: []
    };

    var frame = 0;
    var last = 0;

    function settle() {
      springs.forEach(function (s) {
        s.x = s.target;
        s.v = 0;
      });
      paint(hoverRail);
      paint(activeRail);
    }

    function tick(now) {
      var elapsed = Math.min(MAX_FRAME, (now - last) / 1000);
      last = now;
      var moving = false;
      springs.forEach(function (s) {
        for (var t = 0; t < elapsed; t += STEP) {
          var force = -SPRING.stiffness * (s.x - s.target) - SPRING.damping * s.v;
          s.v += (force / SPRING.mass) * STEP;
          s.x += s.v * STEP;
        }
        if (Math.abs(s.x - s.target) < REST_DELTA && Math.abs(s.v) < REST_SPEED) {
          s.x = s.target;
          s.v = 0;
        } else {
          moving = true;
        }
      });
      paint(hoverRail);
      paint(activeRail);
      frame = moving ? global.requestAnimationFrame(tick) : 0;
    }

    function travel(jump) {
      if (jump || reducedMotion() || document.hidden || !global.requestAnimationFrame) {
        if (frame) global.cancelAnimationFrame(frame);
        frame = 0;
        settle();
        return;
      }
      if (!frame) {
        last = global.performance && global.performance.now ? global.performance.now() : Date.now();
        frame = global.requestAnimationFrame(tick);
      }
    }

    function center(index) {
      if (index === null || index < 0) return null;
      var y = state.centers[index];
      return typeof y === "number" ? y : null;
    }

    function update(jump) {
      var activeY = center(state.active);
      var hoverY = center(state.hover);
      // At or above the active row the accent line already covers the span, so only the corner is drawn.
      var hoverFrom = activeY !== null && hoverY !== null && hoverY <= activeY ? Math.max(0, hoverY - CORNER) : (activeY === null ? 0 : activeY);

      aim(hoverRail, hoverFrom, hoverY);
      aim(activeRail, 0, activeY);
      hoverRail.el.classList.toggle("is-visible", (state.pointerInside || state.focusInside) && state.hover !== state.active && hoverY !== null);
      activeRail.el.classList.toggle("is-visible", activeY !== null);

      items.forEach(function (el, index) {
        var isActive = index === state.active;
        el.setAttribute("data-active", String(isActive));
        if (isActive) el.setAttribute("aria-current", options.current || (el.tagName === "A" ? "page" : "true"));
        else el.removeAttribute("aria-current");
      });
      travel(jump);
    }

    function measure() {
      state.centers = items.map(function (el) { return el.offsetTop + el.offsetHeight / 2; });
      update(false);
    }

    function setActive(index, how) {
      index = typeof index === "number" && index >= 0 && index < items.length ? index : -1;
      if (index === state.active) return;
      state.active = index;
      update(!!(how && how.jump));
    }

    var cleanups = [];
    function on(target, type, handler) {
      target.addEventListener(type, handler);
      cleanups.push(function () { target.removeEventListener(type, handler); });
    }

    on(list, "mouseleave", function () {
      state.pointerInside = false;
      update(false);
    });
    items.forEach(function (el, index) {
      on(el, "mouseenter", function () {
        state.hover = index;
        state.pointerInside = true;
        update(false);
      });
      on(el, "focus", function () {
        state.hover = index;
        state.focusInside = true;
        update(false);
      });
      on(el, "blur", function () {
        state.focusInside = false;
        update(false);
      });
      on(el, "click", function () {
        if (state.active !== index) {
          state.active = index;
          update(false);
        }
        if (options.onChange) options.onChange(index, el);
      });
    });

    // Measured once now, so the active item is marked before the first frame, and again
    // whenever the list changes size (a hidden list has no size until it is shown).
    measure();
    var observer = null;
    if (global.ResizeObserver) {
      observer = new global.ResizeObserver(measure);
      observer.observe(list);
    } else {
      on(global, "resize", measure);
    }
    nav.setAttribute("data-hook", "ready");

    return {
      el: nav,
      items: items,
      get active() { return state.active; },
      setActive: setActive,
      measure: measure,
      destroy: function () {
        if (frame) global.cancelAnimationFrame(frame);
        if (observer) observer.disconnect();
        cleanups.forEach(function (off) { off(); });
        [hoverRail.el, activeRail.el].forEach(function (el) { if (el.parentNode) el.parentNode.removeChild(el); });
        nav.removeAttribute("data-hook");
      }
    };
  }

  global.HookSidebar = { mount: mount, spring: SPRING, corner: CORNER };
})(window);
