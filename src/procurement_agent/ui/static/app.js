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
})();
