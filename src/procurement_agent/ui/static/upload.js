// Drives the three task upload screens: N sequential uploads, then poll each document until it
// is parsed, then open the report. app.js is left alone - its poll() serves the extraction pages
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
  var steps = document.querySelectorAll("[data-steps] li");
  // Translated strings ride on data-* attributes rather than an inline JSON block: a
  // <script type="application/json"> is never executed and so is not a CSP violation, but
  // dataset needs no such argument.
  var strings = rows.dataset;
  var running = false;

  // The slots now validate themselves, beside the slot, in the page's language - rather than
  // in a browser bubble pointing at an input the stylesheet has hidden.
  form.noValidate = true;

  // --- small helpers -----------------------------------------------------------------------
  var SPRITE = (document.querySelector("use[href*='icons.svg']") || { getAttribute: function () { return "/static/icons.svg#"; } })
    .getAttribute("href").split("#")[0];

  function icon(name) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "i");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    var use = document.createElementNS(ns, "use");
    use.setAttribute("href", SPRITE + "#" + name);
    svg.appendChild(use);
    return svg;
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function size(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function lock(on) {
    form.querySelectorAll("[data-slot] input[type=file], .file-chip button").forEach(function (b) { b.disabled = on; });
    form.querySelectorAll(".drop").forEach(function (d) { d.classList.toggle("is-locked", on); });
  }

  function setStep(n) {
    for (var i = 0; i < steps.length; i++) {
      steps[i].classList.toggle("is-done", i < n);
      if (i === n) steps[i].setAttribute("aria-current", "step");
      else steps[i].removeAttribute("aria-current");
    }
  }

  // --- slots: drop zone, chips, validation -------------------------------------------------
  // The <input type=file> stays the real control (keyboard, screen reader, and the no-JS form
  // post); the zone and the chips are an enhancement layered on top of it.
  var canRebuild = (function () {
    try { return !!new DataTransfer().items; } catch (err) { return false; }
  })();

  function accepted(input, file) {
    var list = (input.getAttribute("accept") || "").split(",");
    var name = file.name.toLowerCase();
    return !list[0] || list.some(function (ext) { return name.endsWith(ext.trim()); });
  }

  function setError(slot, message) {
    var box = slot.querySelector("[data-error]");
    slot.classList.toggle("is-invalid", !!message);
    box.textContent = "";
    if (message) {
      box.appendChild(icon("alert"));
      box.appendChild(el("span", null, message));
    }
  }

  function render(slot) {
    var input = slot.querySelector("input[type=file]");
    var list = slot.querySelector("[data-files]");
    var hint = slot.querySelector(".drop-hint");
    var files = Array.prototype.slice.call(input.files);
    list.replaceChildren();
    files.forEach(function (file, index) {
      var li = el("li", "file-chip");
      li.appendChild(icon("file"));
      li.appendChild(el("span", "name", file.name));
      li.appendChild(el("span", "size", size(file.size)));
      if (canRebuild) {
        var remove = el("button", "ghost");
        remove.type = "button";
        remove.setAttribute("aria-label", strings.sRemove + ": " + file.name);
        remove.title = strings.sRemove;
        remove.appendChild(icon("x"));
        remove.addEventListener("click", function () {
          if (running) return;
          setFiles(input, files.filter(function (_, i) { return i !== index; }));
          render(slot);
          input.focus();
        });
        li.appendChild(remove);
      }
      list.appendChild(li);
    });
    slot.querySelector(".drop").classList.toggle("is-filled", files.length > 0);
    hint.textContent = files.length
      ? (slot.hasAttribute("data-multiple") ? strings.sAdd : strings.sReplace)
      : hint.dataset.idle;
    if (files.length) setError(slot, "");
    // What the slot holds now - after a pick, a drop or a remove. The next pick merges into
    // this, so a file removed here can never come back with it.
    slot.kept = files;
  }

  function setFiles(input, files) {
    var dt = new DataTransfer();
    files.forEach(function (f) { dt.items.add(f); });
    input.files = dt.files;
  }

  // Choosing bids a second time adds to the first choice instead of silently replacing it -
  // bidders' workbooks usually live in more than one folder.
  function take(slot, input, incoming, previous) {
    var multiple = slot.hasAttribute("data-multiple");
    var fresh = Array.prototype.slice.call(incoming);
    var bad = fresh.filter(function (f) { return !accepted(input, f); });
    var good = fresh.filter(function (f) { return accepted(input, f); });
    if (!multiple) good = good.slice(0, 1);
    if (canRebuild) {
      var merged = multiple ? previous.concat(good) : (good.length ? good : previous);
      var seen = {};
      merged = merged.filter(function (f) {
        var key = f.name + "|" + f.size;
        if (seen[key]) return false;
        seen[key] = true;
        return true;
      });
      setFiles(input, merged);
    } else if (incoming !== input.files) {
      input.files = incoming;
    }
    render(slot);
    if (bad.length) {
      setError(slot, strings.sType.replace("{name}", bad.map(function (f) { return f.name; }).join(", "))
        .replace("{types}", slot.querySelector(".drop-types").textContent));
    }
  }

  form.querySelectorAll("[data-slot]").forEach(function (slot) {
    var input = slot.querySelector("input[type=file]");
    var drop = slot.querySelector(".drop");
    var hint = slot.querySelector(".drop-hint");
    hint.dataset.idle = hint.textContent;
    slot.kept = [];

    ["dragenter", "dragover"].forEach(function (name) {
      drop.addEventListener(name, function (e) {
        e.preventDefault();
        if (!running) drop.classList.add("is-over");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      drop.addEventListener(name, function () { drop.classList.remove("is-over"); });
    });
    drop.addEventListener("drop", function (e) {
      e.preventDefault();
      if (running) return;
      take(slot, input, e.dataTransfer.files, slot.kept);
    });
    // The picker has already replaced input.files by the time "change" fires, so the earlier
    // choice is remembered here rather than read back.
    input.addEventListener("change", function () {
      take(slot, input, input.files, slot.kept);
    });
    // a reload restores the input's files in some browsers; show them
    if (input.files.length) render(slot);
  });

  // --- progress rows --------------------------------------------------------------------
  function addRow(name) {
    var li = el("li", "upload-row");
    li.appendChild(icon("file"));
    li.appendChild(el("span", "upload-name", name));
    var state = el("span", "upload-state", strings.sWaiting);
    li.appendChild(state);
    var bar = document.createElement("progress");
    bar.max = 100;
    bar.hidden = true;
    li.appendChild(bar);
    rows.appendChild(li);
    return { li: li, state: state, bar: bar };
  }

  function stage(row, text, pct) {
    row.state.textContent = text;
    row.bar.hidden = false;
    if (pct == null) row.bar.removeAttribute("value");  // indeterminate
    else row.bar.value = pct;
  }

  function done(row) {
    row.li.classList.add("is-ready");
    row.li.replaceChild(icon("check-circle"), row.li.firstChild);
    row.state.textContent = strings.sReady;
  }

  function fail(row, message) {
    row.li.classList.add("is-failed");
    row.li.replaceChild(icon("alert"), row.li.firstChild);
    row.state.textContent = message;
  }

  // --- transport ------------------------------------------------------------------------
  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
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

  // XMLHttpRequest rather than fetch for the upload itself: it is the only one of the two that
  // reports upload progress, and a 40 MB scan on a slow link otherwise looks frozen.
  function post(file, row) {
    return new Promise(function (resolve, reject) {
      var body = new FormData();
      body.append("file", file);
      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/v1/documents");
      xhr.withCredentials = true;
      xhr.upload.addEventListener("progress", function (e) {
        if (e.lengthComputable) stage(row, strings.sUploading, Math.round((e.loaded / e.total) * 100));
      });
      xhr.addEventListener("load", function () { resolve(xhr); });
      xhr.addEventListener("error", function () { reject(new Error(strings.sFailed)); });
      xhr.send(body);
    });
  }

  function upload(file, row) {
    stage(row, strings.sUploading, 0);
    var attempt = function () {
      return post(file, row).then(function (xhr) {
        if (xhr.status === 429) {
          var wait = parseInt(xhr.getResponseHeader("Retry-After") || "5", 10);
          row.state.textContent = strings.sBusy.replace("{n}", String(wait));
          return sleep(wait * 1000).then(attempt);
        }
        var j = {};
        try { j = JSON.parse(xhr.responseText); } catch (err) { /* an HTML error page */ }
        if (xhr.status < 200 || xhr.status >= 300) throw new Error(j.detail || strings.sFailed);
        return j.document.id;
      });
    };
    return attempt();
  }

  // An xlsx parse takes about a tenth of a second, so app.js's flat 3s first tick would leave a
  // finished five-file flow sitting still. Start fast, back off for the slow PDF case.
  function waitUntilReady(id, row) {
    stage(row, strings.sParsing, null);
    var delay = 400;
    var blips = 0;
    var tick = function () {
      return send("/api/v1/documents/" + id, {
        credentials: "same-origin", headers: { "Accept": "application/json" }
      }, row).then(function (r) {
        // 404 (deleted, or not yours) and 401 (session over) are answers, not "still reading"
        if (!r.ok) {
          return r.json().catch(function () { return {}; }).then(function (j) {
            throw new Error(j.detail || strings.sFailed);
          });
        }
        blips = 0;
        return r.json();
      }, function () {
        // the network, not the server: a Wi-Fi blip or a restart mid-parse. Try again a few
        // times before giving up, and never show the browser's English error text.
        if (++blips > 5) throw new Error(strings.sFailed);
        return null;
      }).then(function (doc) {
        if (doc === null) return sleep(3000).then(tick);
        if (doc.status === "ready") {
          done(row);
          return id;
        }
        if (doc.status === "failed") {
          throw new Error(doc.error || strings.sFailed);
        }
        row.state.textContent = strings.sParsing;
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

  function validate() {
    var first = null;
    form.querySelectorAll("[data-slot][data-required]").forEach(function (slot) {
      var input = slot.querySelector("input[type=file]");
      if (input.files.length) return;
      setError(slot, strings.sRequired);
      if (!first) first = input;
    });
    if (first) {
      first.closest("[data-slot]").scrollIntoView({ block: "center", behavior: "smooth" });
      first.focus({ preventScroll: true });
      return false;
    }
    return true;
  }

  function reportUrl(ids) {
    if (flow === "aoa") {
      var params = new URLSearchParams({ act: ids.act[0] });
      if (ids.contract) params.set("contract", ids.contract[0]);
      // the parties' registry extracts (rule 1), each uploaded on its own
      if (ids.registry) params.set("registry", ids.registry.slice(0, 6).join(","));
      var type = form.querySelector("[name=contractor_type]");
      if (type && type.value) params.set("contractor_type", type.value);
      // the advance for a foreign-currency act (rule 9); left blank, the report reads the act's
      ["advance_amount", "advance_date"].forEach(function (name) {
        var field = form.querySelector("[name=" + name + "]");
        if (field && field.value.trim()) params.set(name, field.value.trim());
      });
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

  // Leaving mid-upload throws the half-finished run away; ask first.
  window.addEventListener("beforeunload", function (e) {
    if (!running) return;
    e.preventDefault();
    e.returnValue = "";
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (running || !validate()) return;
    var queue = collect();
    if (!queue.length) return;

    running = true;
    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");
    lock(true);
    rows.replaceChildren();
    setStep(1);
    var ids = {};

    // Sequential, not parallel: the parse worker is a single thread, so concurrency buys
    // nothing here and spends the rate-limit budget faster.
    var planned = queue.map(function (item) { return addRow(item.file.name); });
    rows.scrollIntoView({ block: "nearest", behavior: "smooth" });
    var chain = queue.reduce(function (previous, item, i) {
      return previous.then(function () {
        var row = planned[i];
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
      setStep(2);
      var note = el("li", "upload-row is-note");
      note.appendChild(icon("arrow-right"));
      note.appendChild(el("span", "upload-name", strings.sOpening));
      rows.appendChild(note);
      running = false;
      window.location.assign(reportUrl(ids));
    }).catch(function () {
      // The failing row already says why; let the user swap that one file and try again.
      // Files queued behind it never started, and "waiting" would be a promise nobody keeps.
      planned.forEach(function (row) {
        if (!row.li.classList.contains("is-ready") && !row.li.classList.contains("is-failed")) row.li.remove();
      });
      running = false;
      setStep(0);
      submit.disabled = false;
      submit.removeAttribute("aria-busy");
      lock(false);
      var retry = el("li", "upload-row is-note");
      retry.appendChild(icon("refresh"));
      retry.appendChild(el("span", "upload-name", strings.sRetry));
      rows.appendChild(retry);
    });
  });
})();
