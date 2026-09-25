// Polls job / document status and reloads when it changes. No secrets are handled here:
// the API is called with the HttpOnly session cookie.
(function () {
  "use strict";
  function poll(url, isDone) {
    var tick = function () {
      fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) {
          if (!j) return;
          var p = document.getElementById("job-progress"); if (p && typeof j.progress === "number") p.value = j.progress;
          var pct = document.getElementById("job-pct"); if (pct && typeof j.progress === "number") pct.textContent = j.progress + "%";
          var m = document.getElementById("job-message"); if (m && j.message) m.textContent = j.message;
          if (isDone(j)) { window.location.reload(); } else { setTimeout(tick, 3000); }
        })
        .catch(function () { setTimeout(tick, 5000); });
    };
    setTimeout(tick, 3000);
  }
  var jobEl = document.querySelector("[data-poll-job]");
  if (jobEl) poll("/api/v1/jobs/" + jobEl.dataset.pollJob, function (j) { return j.status !== "queued" && j.status !== "running"; });
  var docEl = document.querySelector("[data-poll-doc]");
  if (docEl) poll("/api/v1/documents/" + docEl.dataset.pollDoc, function (d) { return d.status !== "uploaded" && d.status !== "parsing"; });
  document.querySelectorAll("form[data-confirm]").forEach(function (f) {
    f.addEventListener("submit", function (e) { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); });
  });

  // A link cannot open the print dialog, and the dialog is the only thing standing between a
  // report and a PDF, so this is a button whose whole job is one call. Every report has one.
  document.querySelectorAll("[data-print]").forEach(function (b) {
    b.addEventListener("click", function () { window.print(); });
  });
  var closed = [];
  window.addEventListener("beforeprint", function () {
    closed = Array.prototype.filter.call(document.querySelectorAll("details"), function (d) { return !d.open; });
    closed.forEach(function (d) { d.open = true; });
  });
  window.addEventListener("afterprint", function () {
    closed.forEach(function (d) { d.open = false; });
    closed = [];
  });

  // Show / hide a password field. A pasted key is long and one wrong character is the whole
  // difference between signing in and "invalid key".
  document.querySelectorAll("[data-reveal]").forEach(function (b) {
    var input = document.getElementById(b.dataset.reveal);
    if (!input) return;
    var use = b.querySelector("use");
    b.addEventListener("click", function () {
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      b.setAttribute("aria-label", show ? b.dataset.hideLabel : b.dataset.showLabel);
      if (use) use.setAttribute("href", use.getAttribute("href").replace(/#.*$/, show ? "#eye-off" : "#eye"));
      input.focus();
    });
  });

  // The documents page: choosing or dropping a file uploads it at once. The form is a plain
  // post, so without this it still works - with a separate button.
  document.querySelectorAll("form[data-autosubmit]").forEach(function (form) {
    var input = form.querySelector("input[type=file]");
    var drop = form.querySelector(".drop");
    var hint = form.querySelector(".drop-hint");
    var go = function () {
      if (!input.files.length) return;
      hint.textContent = hint.dataset.busy + " · " + input.files[0].name;
      drop.classList.add("is-filled");
      drop.setAttribute("aria-busy", "true");
      form.submit();
    };
    input.addEventListener("change", go);
    ["dragenter", "dragover"].forEach(function (name) {
      drop.addEventListener(name, function (e) { e.preventDefault(); drop.classList.add("is-over"); });
    });
    ["dragleave", "drop"].forEach(function (name) {
      drop.addEventListener(name, function () { drop.classList.remove("is-over"); });
    });
    drop.addEventListener("drop", function (e) {
      e.preventDefault();
      if (!e.dataTransfer.files.length) return;
      // the input takes one file; keep the first rather than post several and show one
      try {
        var one = new DataTransfer();
        one.items.add(e.dataTransfer.files[0]);
        input.files = one.files;
      } catch (err) { input.files = e.dataTransfer.files; }
      go();
    });
  });

  // Extraction fields: all / none, so asking for one field is two clicks, not twelve.
  document.querySelectorAll("[data-check-all]").forEach(function (b) {
    b.addEventListener("click", function () {
      var on = b.dataset.checkAll === "on";
      document.querySelectorAll("input[name=targets]").forEach(function (c) { c.checked = on; });
    });
  });
})();
