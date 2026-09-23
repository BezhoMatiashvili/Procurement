// Drives the two task upload screens: N sequential uploads, then poll each document until it is
// parsed, then open the report. app.js is left alone - its poll() serves the extraction pages
// and has a different cadence and a different done-condition.
//
// No inline handlers and no inline style anywhere: the CSP is script-src 'self' with no
// 'unsafe-inline', so a stray onclick= would simply not run in a browser.
(function () {
  "use strict";

  var form = document.querySelector("[data-upload-flow]");
  if (!form) return;

  var flow = form.dataset.uploadFlow;
  var nextBase = form.dataset.next;
  var rows = form.querySelector("[data-upload-rows]");
  var submit = form.querySelector("[data-upload-submit]");
  // Translated strings ride on data-* attributes rather than an inline JSON block: a
  // <script type="application/json"> is never executed and so is not a CSP violation, but
  // dataset needs no such argument.
  var strings = rows.dataset;

  // --- drop zones -----------------------------------------------------------------------
  // The <input type=file> stays the real control (keyboard, screen reader); the drop zone is
  // only an enhancement layered on top of it.
  form.querySelectorAll("[data-slot]").forEach(function (slot) {
    var input = slot.querySelector("input[type=file]");
    var drop = slot.querySelector(".drop");
    ["dragenter", "dragover"].forEach(function (name) {
      drop.addEventListener(name, function (e) {
        e.preventDefault();
        drop.classList.add("is-over");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      drop.addEventListener(name, function () { drop.classList.remove("is-over"); });
    });
    drop.addEventListener("drop", function (e) {
      e.preventDefault();
      input.files = e.dataTransfer.files;
      describe(slot, input);
    });
    input.addEventListener("change", function () { describe(slot, input); });
  });

  function describe(slot, input) {
    var hint = slot.querySelector(".drop-hint");
    var names = Array.prototype.map.call(input.files, function (f) { return f.name; });
    slot.querySelector(".drop").classList.toggle("is-filled", names.length > 0);
    hint.textContent = names.length ? names.join(", ") : hint.dataset.idle;
  }

  form.querySelectorAll(".drop-hint").forEach(function (h) { h.dataset.idle = h.textContent; });

  // --- progress rows --------------------------------------------------------------------
  function addRow(name) {
    var li = document.createElement("li");
    li.className = "upload-row";
    var label = document.createElement("span");
    label.className = "upload-name";
    label.textContent = name;
    var state = document.createElement("span");
    state.className = "upload-state";
    state.textContent = strings.sWaiting;
    li.appendChild(label);
    li.appendChild(state);
    rows.appendChild(li);
    return { li: li, state: state };
  }

  function fail(row, message) {
    row.li.classList.add("is-failed");
    row.state.textContent = message;
  }

  // --- transport ------------------------------------------------------------------------
  function sleep(ms) {
    return new Promise(function (done) { setTimeout(done, ms); });
  }

  // 120 requests/minute is comfortable for one run and not for three. Say so rather than
  // failing silently on the fourth file.
  function send(url, options, row) {
    return fetch(url, options).then(function (r) {
      if (r.status !== 429) return r;
      var wait = parseInt(r.headers.get("Retry-After") || "5", 10);
      row.state.textContent = strings.sBusy.replace("{n}", String(wait));
      return sleep(wait * 1000).then(function () { return send(url, options, row); });
    });
  }

  function upload(file, row) {
    row.state.textContent = strings.sUploading;
    var body = new FormData();
    body.append("file", file);
    return send("/api/v1/documents", {
      method: "POST", credentials: "same-origin", body: body
    }, row).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.detail || strings.sFailed);
        return j.document.id;
      });
    });
  }

  // An xlsx parse takes about a tenth of a second, so app.js's flat 3s first tick would leave a
  // finished five-file flow sitting still. Start fast, back off for the slow PDF case.
  function waitUntilReady(id, row) {
    row.state.textContent = strings.sParsing;
    var delay = 400;
    var tick = function () {
      return fetch("/api/v1/documents/" + id, {
        credentials: "same-origin", headers: { "Accept": "application/json" }
      }).then(function (r) { return r.json(); }).then(function (doc) {
        if (doc.status === "ready") {
          row.state.textContent = strings.sReady;
          return id;
        }
        if (doc.status === "failed" || doc.status === "deleted") {
          throw new Error(doc.error || strings.sFailed);
        }
        delay = Math.min(Math.round(delay * 1.5), 3000);
        return sleep(delay).then(tick);
      });
    };
    return sleep(delay).then(tick);
  }

  // --- the flow -------------------------------------------------------------------------
  function collect() {
    var queue = [];
    form.querySelectorAll("[data-slot]").forEach(function (slot) {
      var input = slot.querySelector("input[type=file]");
      Array.prototype.forEach.call(input.files, function (file) {
        queue.push({ field: slot.dataset.field, file: file });
      });
    });
    return queue;
  }

  function reportUrl(ids) {
    if (flow === "aoa") {
      var params = new URLSearchParams({ act: ids.act[0] });
      if (ids.contract) params.set("contract", ids.contract[0]);
      var type = form.querySelector("[name=contractor_type]");
      if (type && type.value) params.set("contractor_type", type.value);
      return nextBase + "?" + params.toString();
    }
    if (flow === "compare") {
      return nextBase + "?" + new URLSearchParams({
        left: ids.left[0], right: ids.right[0]
      }).toString();
    }
    return nextBase + "?" + new URLSearchParams({
      template: ids.template[0], bids: ids.bids.join(",")
    }).toString();
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var queue = collect();
    if (!queue.length) return;

    submit.disabled = true;
    rows.replaceChildren();
    var ids = {};

    // Sequential, not parallel: the parse worker is a single thread, so concurrency buys
    // nothing here and spends the rate-limit budget faster.
    var chain = queue.reduce(function (previous, item) {
      return previous.then(function () {
        var row = addRow(item.file.name);
        return upload(item.file, row)
          .then(function (id) { return waitUntilReady(id, row); })
          .then(function (id) {
            (ids[item.field] = ids[item.field] || []).push(id);
          })
          .catch(function (err) {
            fail(row, err.message);
            throw err;
          });
      });
    }, Promise.resolve());

    chain.then(function () {
      rows.appendChild(document.createTextNode(""));
      var announce = addRow("");
      announce.state.textContent = strings.sOpening;
      window.location.assign(reportUrl(ids));
    }).catch(function () {
      // The failing row already says why; let the user swap that one file and try again.
      submit.disabled = false;
    });
  });
})();
