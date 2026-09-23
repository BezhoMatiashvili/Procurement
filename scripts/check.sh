#!/usr/bin/env bash
# Local quality gate: the same steps as .github/workflows/ci.yml, runnable offline.
#
#   scripts/check.sh            full gate (pip-audit needs network access)
#   scripts/check.sh --offline  no network: uv resolves from its cache, pip-audit is SKIPPED
#   scripts/check.sh --e2e      additionally run the e2e tests against $PA_OLLAMA_BASE_URL
#
# A step that cannot run is reported as SKIPPED in the summary and never counts as a pass.
set -euo pipefail
cd "$(dirname "$0")/.."

offline=0
e2e=0
for arg in "$@"; do
  case "$arg" in
    --offline) offline=1 ;;
    --e2e) e2e=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

uv_flags=()
if [[ $offline -eq 1 ]]; then
  uv_flags+=(--offline)
fi

if [[ $e2e -eq 1 && -z "${PA_OLLAMA_BASE_URL:-}" ]]; then
  echo "--e2e needs PA_OLLAMA_BASE_URL (e.g. http://127.0.0.1:11434)" >&2
  exit 2
fi

skipped=()
step() {
  echo
  echo "==> $*"
}

step "lock file matches pyproject.toml"
uv lock --check "${uv_flags[@]}"

step "ruff check"
uv run "${uv_flags[@]}" ruff check .

step "ruff format --check"
uv run "${uv_flags[@]}" ruff format --check .

step "mypy (strict)"
uv run "${uv_flags[@]}" mypy

step "bandit"
uv run "${uv_flags[@]}" bandit -q -c pyproject.toml -r src

step "pytest (unit + integration, coverage floor from pyproject.toml)"
# Same selection as CI, so a local pass predicts a CI pass even where Docling models exist.
uv run "${uv_flags[@]}" pytest -q -p no:cacheprovider -m "not e2e and not docling" \
  --cov=procurement_agent --cov-report=term

if [[ $e2e -eq 1 ]]; then
  step "pytest e2e against $PA_OLLAMA_BASE_URL"
  uv run "${uv_flags[@]}" pytest -q -p no:cacheprovider -m e2e
else
  skipped+=("e2e tests (pass --e2e with PA_OLLAMA_BASE_URL set)")
fi

if [[ $offline -eq 1 ]]; then
  skipped+=("pip-audit (needs network; CI runs it)")
else
  step "pip-audit (exactly what uv.lock pins)"
  req="$(mktemp)"
  trap 'rm -f "$req"' EXIT
  uv export --locked --all-groups --no-hashes --no-emit-project \
    --format requirements-txt -o "$req" >/dev/null
  uv run pip-audit --strict --disable-pip --no-deps -r "$req"
fi

if command -v gitleaks >/dev/null 2>&1; then
  step "gitleaks (full history)"
  gitleaks git . --redact --exit-code 1
else
  skipped+=("gitleaks (binary not installed; CI and pre-commit run it)")
fi

echo
echo "==> all executed checks passed"
if [[ ${#skipped[@]} -gt 0 ]]; then
  echo "!!  SKIPPED — not verified by this run:"
  for s in "${skipped[@]}"; do
    echo "!!    - $s"
  done
fi
