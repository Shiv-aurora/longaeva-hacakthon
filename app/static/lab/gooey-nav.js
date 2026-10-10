/*
 * Gooey Nav for plain HTML, CSS and JavaScript.
 *
 * Adapted from Rare UI's "Gooey Nav" component (https://rareui.com).
 * Copyright (c) 2026 Swami Malode.
 * Licence: MIT + Commons Clause + Attribution; the full text is in
 * LICENSE-gooey-nav.txt beside this file. The credit and the copyright line
 * above must stay in this file, and the site shows a link to https://rareui.com.
 *
 * The original is a React component animated with the `motion` library. This is
 * a port, not the original source: it keeps the geometry and the timing (the
 * neck path, the break point, the nominal height, the per-size separation and
 * corner radius, the one-pixel overlap on closed seams, the spring constants,
 * the 400 ms colour fade on entering and none on leaving) and replaces React
 * and `motion` with the DOM and a small spring integrator. Styles are in
 * gooey-nav.css.
 *
 * Use:
 *   var nav = GooeyNav.mount(document.getElementById("view-nav"), {
 *     items: [{label: "Simulator", value: "simulator"}, ...],   // or leave out to adopt the <button>s already in the element
 *     active: 0, size: "sm",
 *     compact: {query: "(max-width: 359px)", size: "xs"},        // optional smaller size on a narrow screen
 *     onChange: function (index, item) {}                        // a person chose an item
 *   });
 *   nav.setActive(2);                 // controlled from outside; does not call onChange
 *   nav.setActive(0, {jump: true});   // no animation
 *   nav.active                        // current index
 */
(function (global) {
  "use strict";

  // The original's spring, damped so nothing overshoots.
  var SPRING = { stiffness: 200, damping: 28, mass: 1 };
  // The neck has thinned to nothing by the time the gap is this far open.
  var NECK_BREAK = 0.22;
  // Nominal viewBox height; the svg stretches to whatever the tile actually is.
  var NECK_H = 100;
  var SIZES = {
    xs: { radius: 8, separation: 14 },
    sm: { radius: 10, separation: 16 },
    md: { radius: 12, separation: 20 },
    lg: { radius: 14, separation: 24 }
  };
  // Closed seams pull in a pixel so no hairline shows through.
  var CLOSED = -1;

  var SVG_NS = "http://www.w3.org/2000/svg";
  var STEP = 0.001;        // integrator step, seconds
  var MAX_FRAME = 0.064;   // a stalled tab does not get one huge step
  var REST_DELTA = 0.01;   // pixels
  var REST_SPEED = 0.1;    // pixels per second
  var counter = 0;

  // Two concave curves pinching toward the middle, drawn in the gap the tiles leave.
  function neckPath(gap, span) {
    // A NaN or negative span would otherwise emit a path full of NaN coordinates.
    if (!isFinite(gap) || !isFinite(span) || gap <= 0 || span <= 0) return "";
    var waist = NECK_H * (1 - gap / (span * NECK_BREAK));
    if (waist <= 0) return "";
    var start = span - gap;
    var mid = start + gap / 2;
    return "M" + start + " 0 Q" + mid + " " + (NECK_H - waist) + " " + span + " 0 L" + span + " " + NECK_H +
      " Q" + mid + " " + waist + " " + start + " " + NECK_H + " Z";
  }

  function svg(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    return node;
  }

  function reducedMotion() {
    return !!global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function mount(root, options) {
    options = options || {};
    if (!root) return null;
    var id = "gooey-neck-" + (++counter);
    var list = root.querySelector("ul") || root.appendChild(document.createElement("ul"));
    list.classList.add("gooey-list");
    root.classList.add("gooey");

    // Items: given in options, or adopted from the markup so the bar is there before the script runs.
    var given = (options.items || []).map(function (item) { return typeof item === "string" ? { label: item } : item; });
    if (given.length) {
      list.replaceChildren();
      given.forEach(function (item) {
        var control = document.createElement(item.href ? "a" : "button");
        if (item.href) control.href = item.href;
        else control.type = "button";
        control.textContent = item.label;
        var li = document.createElement("li");
        li.appendChild(control);
        list.appendChild(li);
      });
    }
    var segments = Array.prototype.slice.call(list.children);
    var controls = segments.map(function (li) { return li.querySelector("button, a"); });
    var items = given.length ? given : controls.map(function (c) { return { label: c.textContent.trim(), value: c.value || c.getAttribute("data-value") || null }; });
    var count = segments.length;
    if (!count) return null;

    var active = Math.max(0, Math.min(count - 1, options.active == null ? 0 : options.active));
    var span = 0;
    var corner = 0;
    // One spring per seam between tile i - 1 and tile i (i >= 1): the gap in pixels.
    var seams = [];
    var frame = 0;
    var last = 0;
    var settle = 0;

    segments.forEach(function (li, i) {
      li.classList.add("gooey-seg");
      controls[i].classList.add("gooey-item");
      if (i === 0) return;
      var neck = svg("svg", { "aria-hidden": "true", class: "gooey-neck", preserveAspectRatio: "none", focusable: "false" });
      var defs = svg("defs");
      var gradient = svg("linearGradient", { id: id + "-" + i, x1: "0", x2: "1" });
      var left = svg("stop", { offset: "0" });
      var right = svg("stop", { offset: "1" });
      gradient.appendChild(left);
      gradient.appendChild(right);
      defs.appendChild(gradient);
      var path = svg("path", { fill: "url(#" + id + "-" + i + ")" });
      neck.appendChild(defs);
      neck.appendChild(path);
      li.insertBefore(neck, li.firstChild);
      seams[i] = { x: CLOSED, v: 0, target: CLOSED, neck: neck, path: path, left: left, right: right };
    });

    function open(seam) {
      return seam === 0 || seam === count || seam - 1 === active || seam === active;
    }

    // Draw the current spring positions: margins, necks and corner radii.
    function render() {
      var grown = function (seam) {
        if (seam === 0 || seam === count) return corner;
        // The radius rides the same spring as the gap, so it is the same fraction of the way there.
        return corner * Math.max(0, Math.min(1, (seams[seam].x - CLOSED) / (span - CLOSED)));
      };
      segments.forEach(function (li, i) {
        if (i > 0) {
          li.style.marginLeft = seams[i].x.toFixed(3) + "px";
          seams[i].path.setAttribute("d", neckPath(seams[i].x, span));
        }
        var l = grown(i).toFixed(3) + "px";
        var r = grown(i + 1).toFixed(3) + "px";
        li.style.borderRadius = l + " " + r + " " + r + " " + l;
      });
    }

    function finish() {
      cancelAnimationFrame(frame);
      clearTimeout(settle);
      frame = 0;
      seams.forEach(function (s) { s.x = s.target; s.v = 0; });
      render();
    }

    function tick(now) {
      var dt = Math.min(MAX_FRAME, Math.max(0, (now - last) / 1000));
      last = now;
      var moving = false;
      for (var t = 0; t < dt; t += STEP) {
        var h = Math.min(STEP, dt - t);
        for (var i = 1; i < count; i++) {
          var s = seams[i];
          // Semi-implicit Euler on m x'' = -k (x - target) - c x'.
          s.v += (-SPRING.stiffness * (s.x - s.target) - SPRING.damping * s.v) / SPRING.mass * h;
          s.x += s.v * h;
        }
      }
      for (var j = 1; j < count; j++) {
        var seam = seams[j];
        if (Math.abs(seam.x - seam.target) < REST_DELTA && Math.abs(seam.v) < REST_SPEED) {
          seam.x = seam.target;
          seam.v = 0;
        } else {
          moving = true;
        }
      }
      render();
      if (moving) {
        frame = requestAnimationFrame(tick);
      } else {
        frame = 0;
        clearTimeout(settle);
      }
    }

    // Point every spring and every colour at the current selection.
    function layout(jump) {
      segments.forEach(function (li, i) {
        var on = i === active;
        li.classList.toggle("is-active", on);
        if (on) controls[i].setAttribute("aria-current", controls[i].tagName === "A" ? "page" : "true");
        else controls[i].removeAttribute("aria-current");
        if (i > 0) {
          seams[i].target = open(i) ? span : CLOSED;
          // The neck is filled left to right between the two neighbouring tile colours.
          seams[i].left.setAttribute("class", i - 1 === active ? "is-active" : "");
          seams[i].right.setAttribute("class", on ? "is-active" : "");
        }
      });
      if (jump || reducedMotion() || document.hidden || !global.requestAnimationFrame) {
        finish();
        return;
      }
      if (!frame) {
        last = global.performance.now();
        frame = requestAnimationFrame(tick);
      }
      // If frames stop arriving (a throttled or covered window), land on the end state anyway.
      clearTimeout(settle);
      settle = setTimeout(finish, 1500);
    }

    function applySize() {
      var name = options.size || "md";
      if (options.compact && global.matchMedia && global.matchMedia(options.compact.query).matches) name = options.compact.size;
      if (!SIZES[name]) name = "md";
      root.setAttribute("data-size", name);
      span = options.separation == null ? SIZES[name].separation : options.separation;
      corner = options.radius == null ? SIZES[name].radius : options.radius;
      for (var i = 1; i < count; i++) {
        seams[i].neck.setAttribute("width", span);
        seams[i].neck.setAttribute("viewBox", "0 0 " + span + " " + NECK_H);
      }
      layout(true);
    }

    function setActive(index, how) {
      index = Math.max(0, Math.min(count - 1, Number(index) || 0));
      if (index === active) return;
      active = index;
      layout(how && how.jump);
    }

    controls.forEach(function (control, i) {
      control.addEventListener("click", function (event) {
        if (control.tagName !== "A") event.preventDefault();
        if (!options.controlled) setActive(i);
        if (options.onChange) options.onChange(i, items[i]);
      });
      control.addEventListener("keydown", function (event) {
        var to = event.key === "ArrowRight" || event.key === "ArrowDown" ? (i + 1) % count
          : event.key === "ArrowLeft" || event.key === "ArrowUp" ? (i - 1 + count) % count
            : event.key === "Home" ? 0 : event.key === "End" ? count - 1 : -1;
        if (to < 0) return;
        event.preventDefault();
        controls[to].focus();
      });
    });

    applySize();
    if (options.compact && global.matchMedia) {
      var media = global.matchMedia(options.compact.query);
      if (media.addEventListener) media.addEventListener("change", applySize);
      else if (media.addListener) media.addListener(applySize);
    }
    root.setAttribute("data-ready", "");

    return {
      el: root,
      items: items,
      setActive: setActive,
      get active() { return active; }
    };
  }

  global.GooeyNav = { mount: mount, neckPath: neckPath, SPRING: SPRING, NECK_BREAK: NECK_BREAK, NECK_H: NECK_H, SIZES: SIZES };
})(window);
