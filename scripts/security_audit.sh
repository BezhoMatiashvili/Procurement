#!/usr/bin/env bash
# Re-measure every security claim this project makes, offline, and print one scorecard.
#
#   scripts/security_audit.sh               every offline step (about 20 minutes)
#   scripts/security_audit.sh --quick       skip the slow ones (mutation harness, resource ceilings)
#   scripts/security_audit.sh --only NAME   the steps whose name starts with NAME
#   scripts/security_audit.sh --online      also run pip-audit (sends package NAMES and VERSIONS
#                                           from uv.lock to PyPI/OSV - nothing else leaves)
#   scripts/security_audit.sh --strict      a SKIPPED step counts as a failure (for CI)
#   scripts/security_audit.sh --out FILE    also write the scorecard to FILE (owner-only)
#
# Each step ends PASS, FAIL or SKIPPED, with the number it measured. SKIPPED is never a pass: the
# step could not run here and says why, and the scorecard lists it so a green run cannot hide it.
# Exit status: 0 nothing failed, 1 something failed (or was skipped under --strict), 2 bad usage.
#
# Nothing here reads a client document or sends anything off this machine (--online excepted). The
# steps that start the application use temporary directories and loopback ports the kernel picks;
# they never touch the data directory or the port of a running instance.
#
# PA_VERIFY_PYTHON=/path/to/python runs that interpreter instead of `uv run --offline python`
# (it needs the project's dependencies and its dev tools; PYTHONPATH is set to src).
set -uo pipefail
cd "$(dirname "$0")/.."

quick=0
online=0
strict=0
only=""
out=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --quick) quick=1 ;;
    --online) online=1 ;;
    --strict) strict=1 ;;
    --only) only="${2:-}"; shift ;;
    --out) out="${2:-}"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ -n "${PA_VERIFY_PYTHON:-}" ]]; then
  py=("$PA_VERIFY_PYTHON")
  export PYTHONPATH="$PWD/src:$PWD${PYTHONPATH:+:$PYTHONPATH}"
else
  py=(uv run --offline python)
fi
export PYTHONDONTWRITEBYTECODE=1
umask 077
work="$(mktemp -d "${TMPDIR:-/tmp}/pa-audit.XXXXXX")"
trap 'rm -rf "$work"' EXIT
log="$work/step.log"

pytest_args=(-q -p no:cacheprovider -m "not e2e and not docling" --no-header)

# What each group of tests stands guard over (see docs/SECURITY.md section 11).
EGRESS_TESTS=(
  tests/unit/test_offline_pinning.py tests/unit/test_egress_guard.py tests/unit/test_egress_report.py
  tests/unit/test_egress_surface.py tests/unit/test_llm_policy.py tests/unit/test_network_settings.py
  tests/unit/test_hosts.py tests/integration/test_network_policy.py
)
SECURITY_TESTS=(
  tests/integration/test_security_matrix.py tests/integration/test_preauth.py
  tests/integration/test_headers_and_cookies.py tests/integration/test_audit_trail.py
  tests/integration/test_export_formula_safety.py tests/integration/test_availability_web.py
  tests/integration/test_hardening_web.py tests/unit/test_hardening.py tests/unit/test_hardening_checks.py
  tests/unit/test_retention.py tests/unit/test_migrations.py tests/unit/test_upload_guard.py
  tests/unit/test_upload_guard_hostile.py
  tests/unit/test_xml_safety.py tests/unit/test_docx_sanitize.py tests/unit/test_models_manifest.py
  tests/unit/test_capacity_and_pdfium.py tests/unit/test_text_limits.py tests/unit/test_repo_guards.py
)
LOG_TESTS=(tests/integration/test_log_canary.py tests/unit/test_logscrub.py tests/unit/test_safe_errors.py)
DEPLOY_TESTS=(
  tests/unit/test_deployment_policy.py tests/unit/test_supervisor.py
  tests/integration/test_supervisor_real_app.py
)

status=""
measure=""
rc=0
capture() { "$@" >"$log" 2>&1; rc=$?; }
last_line() { grep -v '^[[:space:]]*$' "$log" | tail -n 1; }
passed_line() { grep -E '[0-9]+ passed' "$log" | tail -n 1 | sed -E 's/^=+ *//; s/ *=+$//'; }
verdict() { if [[ $rc -eq 0 ]]; then status=PASS; else status=FAIL; fi; }

# --- static analysis ----------------------------------------------------------------------------

step_static_ruff() {
  capture "${py[@]}" -m ruff check --no-cache .
  verdict
  measure="$([[ $rc -eq 0 ]] && echo "0 findings, flake8-bandit (S) rules on" || last_line)"
}

step_static_mypy() {
  capture "${py[@]}" -m mypy --cache-dir=/dev/null
  verdict
  measure="$(last_line | sed 's/^Success: //')"
}

step_static_bandit() {
  "${py[@]}" -m bandit -q -c pyproject.toml -r src -f json >"$work/bandit.json" 2>"$log"
  rc=$?
  local nosec issues loc
  nosec="$(grep -rn 'nosec' src | wc -l | tr -d ' ')"
  read -r issues loc < <("${py[@]}" -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(len(d["results"]), d["metrics"]["_totals"]["loc"])' "$work/bandit.json" 2>/dev/null || echo "? ?")
  measure="$issues issue(s) over $loc lines, $nosec nosec marker(s)"
  if [[ "$issues" == "0" && "$nosec" == "0" ]]; then status=PASS; else status=FAIL; fi
}

# --- the repository and this machine ------------------------------------------------------------

step_repo_documents() {
  capture bash scripts/check_no_documents.sh
  verdict
  measure="$(last_line | sed 's/^ok: //')"
}

step_repo_secrets() {
  # A small offline stand-in for gitleaks (which CI runs with the real rule set): credential-shaped
  # strings - private keys, cloud/forge/chat tokens, JWTs - in every commit of every ref and in the
  # tree, tracked or not. Counts only; a hit is looked up by hand.
  local pattern='-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|hf_[A-Za-z0-9]{30,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|sk-[A-Za-z0-9]{32,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}'
  local history tree commits
  commits="$(git rev-list --all --count 2>/dev/null || echo '?')"
  history="$(git log --all -p --no-color 2>/dev/null | grep -cE -e "$pattern")"
  tree="$(git grep -I --untracked -hE -e "$pattern" -- . ':!uv.lock' 2>/dev/null | wc -l | tr -d ' ')"
  measure="$history line(s) in the history of $commits commit(s), $tree in the tree"
  if [[ "$history" == "0" && "$tree" == "0" ]]; then status=PASS; else status=FAIL; fi
}

step_repo_identifiers() {
  local store="${PA_CANARY_DIR:-$HOME/.config/procurement-canary}/tokens.json"
  if [[ ! -f "$store" ]]; then
    status=SKIPPED
    measure="no token file at $store (scripts/client_canary.py harvest builds it from the client's documents)"
    return
  fi
  capture "${py[@]}" scripts/client_canary.py scan-tree
  verdict
  # a soft hit is a client name that is also an ordinary dictionary word: reported, not refused
  local soft hard tokens
  soft="$(grep -c '^warning' "$log")"
  hard="$(grep -vc '^warning\|^[[:space:]]*$' "$log")"
  tokens="$(grep -o '\[#[0-9a-f]*\]' "$log" | sort -u | wc -l | tr -d ' ')"
  measure="$hard hard hit(s); $soft soft warning(s) from $tokens distinct token(s)"
}

step_repo_hooks() {
  if [[ -n "${CI:-}" ]]; then status=SKIPPED; measure="a local guard; CI runs the generic rule instead"; return; fi
  local hooks missing=()
  hooks="$(git rev-parse --git-path hooks)"
  for h in pre-commit pre-push; do
    grep -qs 'client_canary' "$hooks/$h" || missing+=("$h")
  done
  grep -qs '_generic_refusals' scripts/client_canary.py || missing+=("(the tool lacks the generic rule)")
  if [[ ${#missing[@]} -eq 0 ]]; then
    status=PASS
    measure="pre-commit refuses staged documents, the generic rule (db, keys, .env, client dirs) and the client's identifiers; pre-push the identifiers"
  else
    status=FAIL; measure="not installed: ${missing[*]} (python scripts/client_canary.py install)"
  fi
}

step_machine_client_paths() {
  # Group/world access to client material is the cheapest way for it to leak. Metadata only.
  local root checked=0 open=() path mode
  root="$(cd "$(git rev-parse --git-common-dir)/.." && pwd)"
  for path in "AI Test" "AI Test.zip" samples .eval_cache eval_runs data; do
    [[ -e "$root/$path" ]] || continue
    checked=$((checked + 1))
    mode="$(stat -L -c '%a' "$root/$path")"
    (( (8#$mode & 8#077) == 0 )) || open+=("$path=$mode")
  done
  if [[ $checked -eq 0 ]]; then status=SKIPPED; measure="no client directories on this checkout"; return; fi
  if [[ ${#open[@]} -eq 0 ]]; then
    status=PASS; measure="$checked client path(s), none readable by group or others"
  else
    status=FAIL; measure="group/world access: ${open[*]}"
  fi
}

step_machine_ai_tools() {
  local settings=".claude/settings.json"
  if [[ ! -f "$settings" ]]; then
    status=SKIPPED; measure="no $settings (a local guardrail, not part of the repository)"; return
  fi
  capture "${py[@]}" - "$settings" <<'PY'
import json, sys

deny = json.load(open(sys.argv[1])).get("permissions", {}).get("deny", [])
need = ["AI Test", "samples/gold", ".eval_cache", "eval_runs", "data/", ".env"]
missing = [n for n in need if not any(n in rule for rule in deny)]
print(f"{len(deny)} deny rule(s)" + (f"; nothing covers: {', '.join(missing)}" if missing else ", all client paths covered"))
sys.exit(1 if missing else 0)
PY
  verdict
  measure="$(last_line)"
}

# --- nothing leaves this computer ---------------------------------------------------------------

step_egress_policy() {
  capture "${py[@]}" -m pytest "${pytest_args[@]}" "${EGRESS_TESTS[@]}"
  verdict
  measure="$(passed_line): one outbound-capable code path, pinned; guard, URL and Host policy"
}

step_offline_kernel() {
  capture bash scripts/verify_offline.sh --quick
  case $rc in
    0) status=PASS; measure="whole product flow with only loopback up (unshare --net): $(grep -c '^  ok\|^ok' "$log") step(s) ok" ;;
    2) status=SKIPPED; measure="this host cannot create a network namespace (not a pass)" ;;
    *) status=FAIL; measure="$(last_line)" ;;
  esac
}

step_deps_audit() {
  if [[ $online -eq 0 ]]; then
    status=SKIPPED
    measure="needs the network (package names and versions go to PyPI/OSV): run with --online"
    return
  fi
  capture uv export --locked --all-groups --no-hashes --no-emit-project --format requirements-txt -o "$work/requirements-audit.txt"
  if [[ $rc -ne 0 ]]; then status=FAIL; measure="could not export the lock: $(last_line)"; return; fi
  # torch exports as `2.14.1+cpu`, a local version PyPI does not list: audit the release it is built from
  sed -i -E 's/(==[0-9][^ ;+]*)\+[A-Za-z0-9.]+/\1/' "$work/requirements-audit.txt"
  capture "${py[@]}" -m pip_audit --strict --disable-pip --no-deps -r "$work/requirements-audit.txt"
  verdict
  measure="$(grep -c '==' "$work/requirements-audit.txt") pinned requirement(s) audited: $(last_line)"
}

# --- the application's own behaviour ------------------------------------------------------------

step_security_suite() {
  capture "${py[@]}" -m pytest "${pytest_args[@]}" "${SECURITY_TESTS[@]}"
  verdict
  measure="$(passed_line): authz/IDOR/CSRF matrix over every route, pre-auth, headers, hostile uploads, retention"
}

step_log_canary() {
  capture "${py[@]}" -m pytest "${pytest_args[@]}" "${LOG_TESTS[@]}"
  verdict
  measure="$(passed_line): a marker in every input reaches 0 log lines and 0 database columns"
}

step_deploy_policy() {
  capture "${py[@]}" -m pytest "${pytest_args[@]}" "${DEPLOY_TESTS[@]}"
  verdict
  measure="$(passed_line): compose, Dockerfile, build context; the supervisor restarts a real, hung server"
}

step_regex_budget() {
  capture "${py[@]}" tests/regex_budget.py --through-limits
  verdict
  measure="$(last_line) (strings as the application receives them, 0.25 s budget)"
}

step_regex_controls() {
  capture "${py[@]}" -m pytest "${pytest_args[@]}" tests/unit/test_regex_budget.py
  verdict
  measure="$(passed_line): the directed search still finds the pre-fix contract-number pattern"
}

step_resources() {
  capture "${py[@]}" scripts/audit_resources.py
  verdict
  measure="$(last_line)"
}

step_mutation() {
  capture "${py[@]}" scripts/mutate_security.py
  verdict
  measure="$(last_line | sed -E 's/^ *//; s/ mutations caught/ security mutations caught/')"
}

# name:speed - the order is the order of the scorecard
STEPS=(
  static_ruff:fast static_mypy:fast static_bandit:fast
  repo_documents:fast repo_secrets:fast repo_identifiers:fast repo_hooks:fast machine_client_paths:fast machine_ai_tools:fast
  egress_policy:fast offline_kernel:fast deps_audit:fast
  security_suite:fast log_canary:fast deploy_policy:fast
  regex_budget:slow regex_controls:fast resources:slow mutation:slow
)

names=()
results=()
details=()
times=()
failed=0
skipped=0
for entry in "${STEPS[@]}"; do
  name="${entry%%:*}"
  speed="${entry##*:}"
  [[ -z "$only" || "$name" == "$only"* ]] || continue
  status=""
  measure=""
  started=$SECONDS
  if [[ $quick -eq 1 && "$speed" == "slow" ]]; then
    status=SKIPPED
    measure="skipped by --quick"
  else
    echo "==> $name"
    : >"$log"
    "step_$name"
    if [[ "$status" == "FAIL" && -s "$log" ]]; then
      echo "    --- last lines of the output ---"
      tail -n 15 "$log" | sed 's/^/    /'
    fi
  fi
  names+=("$name")
  results+=("$status")
  details+=("$measure")
  times+=("$((SECONDS - started))")
  [[ "$status" == "FAIL" ]] && failed=$((failed + 1))
  [[ "$status" == "SKIPPED" ]] && skipped=$((skipped + 1))
done

if [[ ${#names[@]} -eq 0 ]]; then echo "no step matches '$only'" >&2; exit 2; fi

head_ref="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
dirty="$([[ -n "$(git status --porcelain 2>/dev/null)" ]] && echo ' + uncommitted changes' || true)"
report() {
  echo
  echo "SECURITY AUDIT - $(date '+%Y-%m-%d %H:%M') - commit ${head_ref}${dirty}"
  printf '%-22s %-8s %6s  %s\n' step result secs measured
  for i in "${!names[@]}"; do
    printf '%-22s %-8s %6s  %s\n' "${names[$i]}" "${results[$i]}" "${times[$i]}" "${details[$i]}"
  done
  echo
  echo "$((${#names[@]} - failed - skipped)) passed, $failed failed, $skipped skipped"
  if [[ $skipped -gt 0 ]]; then
    echo "SKIPPED steps were NOT verified by this run:"
    for i in "${!names[@]}"; do
      [[ "${results[$i]}" == "SKIPPED" ]] && echo "  - ${names[$i]}: ${details[$i]}"
    done
  fi
}
if [[ -n "$out" ]]; then report | tee "$out"; chmod 600 "$out"; else report; fi

[[ $failed -eq 0 ]] || exit 1
[[ $strict -eq 0 || $skipped -eq 0 ]] || exit 1
exit 0
