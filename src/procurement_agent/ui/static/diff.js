// The comparison page. Everything here is an upgrade on markup that already works: the
// side-by-side layout is CSS, the filter is a GET form, the folds are <details>, and the
// print stylesheet needs no script at all. Without this file the page still filters, still
// folds and still prints — it just costs a round trip.
//
// No inline handlers and no inline style: the CSP is script-src 'self' with no 'unsafe-inline'.
(function () {
  "use strict";

  var diff = document.querySelector(".diff");

  if (!diff) return;

  // --- filtering --------------------------------------------------------------------------
  var form = document.querySelector("[data-diff-filter]");

  function apply(show) {
    var rows = diff.querySelectorAll(".diff-row");
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var m = row.dataset.m;
      var keep = show === "all" || (show === "changes" && m) || (show === "critical" && m === "critical");
      row.hidden = !keep;
    }
    diff.querySelectorAll(".diff-fold").forEach(function (fold) {
      // A fold is a run of unchanged lines: under any filter but "all" it has nothing to show.
      fold.hidden = show !== "all";
    });
    var empty = diff.querySelector(".empty-state");
    var visible = diff.querySelector(".diff-row:not([hidden])");
    if (empty) empty.hidden = !!visible;
  }

  if (form) {
    form.querySelectorAll(".chip").forEach(function (chip) {
      chip.addEventListener("click", function (e) {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;  // let it open a tab
        e.preventDefault();
        var show = chip.dataset.show;
        form.querySelectorAll(".chip").forEach(function (other) {
          other.setAttribute("aria-current", other === chip ? "true" : "false");
        });
        apply(show);
        // Keep the URL honest: a reload, a bookmark or a share must show what is on screen.
        try { window.history.replaceState(null, "", chip.getAttribute("href")); } catch (err) { /* file:// */ }
      });
    });
  }

  // --- keyboard: j / k between changes, the way a diff is normally read --------------------
  function changed() {
    return Array.prototype.filter.call(
      diff.querySelectorAll(".diff-row[data-m]"),
      function (r) { return !r.hidden && r.offsetParent !== null; }
    );
  }

  var at = -1;
  function go(step) {
    var rows = changed();
    if (!rows.length) return;
    at = Math.max(0, Math.min(rows.length - 1, at + step));
    var row = rows[at];
    row.scrollIntoView({ block: "center", behavior: "smooth" });
    // Focus, so the next Tab continues from here and a screen reader announces the row.
    row.setAttribute("tabindex", "-1");
    row.focus({ preventScroll: true });
  }

  // Scoped to the diff itself, never the whole page: single-letter shortcuts that fire
  // anywhere collide with speech input and type-ahead find (WCAG 2.1.4). The diff is
  // focusable, so a click or a Tab into it is all it takes.
  diff.addEventListener("keydown", function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var el = document.activeElement;
    if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT")) return;
    if (e.key === "j" || e.key === "n") { e.preventDefault(); go(1); }
    else if (e.key === "k" || e.key === "p") { e.preventDefault(); go(-1); }
  });
})();
