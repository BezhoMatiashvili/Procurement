#!/usr/bin/env bash
# Refuse documents, archives, databases, keys and environment files in the repository.
#
#   scripts/check_no_documents.sh              every file git tracks (what CI runs)
#   scripts/check_no_documents.sh FILE...      only these (what the pre-commit hook passes)
#
# The repository is the one place client material could leave this computer by accident: a push
# publishes it. The only documents allowed are the synthetic ones under tests/fixtures/. This is
# the generic rule - it knows nothing about any client. scripts/client_canary.py is the specific
# one (it knows the client's identifiers by hash, and runs on this machine only).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

if [[ $# -gt 0 ]]; then
  files=("$@")
else
  mapfile -t files < <(git ls-files)
fi

bad=()
for f in "${files[@]}"; do
  lower="${f,,}"
  base="${lower##*/}"
  case "$lower" in
    # databases, keys, certificates: never, not even as a fixture
    *.db|*.db-wal|*.db-shm|*.sqlite|*.sqlite3|*.pem|*.key|*.p12|*.pfx|*.jks|*.kdbx|*.keystore) bad+=("$f  (database or key)"); continue ;;
  esac
  case "$base" in
    .env|.env.*) [[ "$base" == ".env.example" ]] || { bad+=("$f  (environment file)"); continue; } ;;
    id_rsa*|id_ed25519*|id_ecdsa*) bad+=("$f  (ssh key)"); continue ;;
  esac
  case "$lower" in
    # what the working tree keeps client material in. The bare names are there for a checkout that
    # links these directories in from another one (a git worktree): a symlink is one tracked path,
    # `samples`, which `samples/*` does not match - and `git add -A` stages it.
    data|data/*|.eval_cache|.eval_cache/*|eval_runs|eval_runs/*|samples|samples/*|"ai test"|"ai test"/*|"ai test.zip"|uploads/*|results/*)
      bad+=("$f  (client-derived directory)"); continue ;;
    models|models/*)
      bad+=("$f  (model files are downloaded, never committed)"); continue ;;
  esac
  case "$lower" in
    *.pdf|*.doc|*.docx|*.docm|*.dot|*.dotx|*.dotm|*.xls|*.xlsx|*.xlsm|*.xlsb|*.ppt|*.pptx|*.odt|*.ods|*.rtf|*.zip|*.rar|*.7z|*.tar|*.tar.gz|*.tgz|*.gz|*.bz2|*.xz)
      [[ "$lower" == tests/fixtures/* ]] || bad+=("$f  (document or archive)") ;;
  esac
done

if [[ ${#bad[@]} -gt 0 ]]; then
  echo "refusing: these must not be in the repository (only synthetic fixtures under tests/fixtures/ may be documents):" >&2
  printf '  %s\n' "${bad[@]}" >&2
  exit 1
fi
echo "ok: ${#files[@]} file(s) checked, no documents, databases, keys or environment files outside tests/fixtures/"
