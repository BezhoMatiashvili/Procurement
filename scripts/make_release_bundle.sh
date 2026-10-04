#!/usr/bin/env bash
# Assemble an offline release bundle: what a client's air-gapped server needs to install one commit.
#
#   scripts/make_release_bundle.sh --image procurement-agent:0.1.0 [--image TAG ...]
#   scripts/make_release_bundle.sh --no-image
#
#     --image TAG    docker-save this image into the bundle (repeatable; one archive, gzipped).
#                    The compose file also needs the Ollama and Caddy images: pass them too,
#                    or INSTALL.md tells the deployer which ones to bring separately.
#     --no-image     no image archive; RELEASE.txt and INSTALL.md say so. One of the two is required.
#     --commit SHA   refuse unless HEAD is this commit (a full or abbreviated hash, or a tag).
#     --out-dir DIR  where the bundle directory goes (default: dist/ in the repository, git-ignored).
#     --list-allow   print the repository files a bundle may contain, and exit.
#
# Writes <out-dir>/procurement-agent-<version>-<shortsha>/ (<version> from pyproject.toml, the commit
# from git) holding: the image archive (with --image), docker-compose.yml, .env.example, README.md,
# docs/SECURITY.md, docs/PRODUCTION_READINESS.md, docs/THIRD_PARTY.md, docs/sbom.cdx.json,
# INSTALL.md (the offline steps in order), RELEASE.txt (version, commit, tag, UTC date, image id and
# size, the SHA-256 of uv.lock and of the SBOM) and SHA256SUMS over every other file.
#
# Only the files in ALLOWED_FILES below are ever taken, from the commit (git archive), never from the
# working tree: no samples, data, .env, eval_runs, .eval_cache, .claude, tests, scripts or history.
# It refuses a dirty tree, a HEAD that is not --commit, an SBOM or licence inventory made from a
# different uv.lock, an image docker does not have, and a bundle directory that already exists.
# The bundle is built in a hidden staging directory and moved into place when it is complete.
#
# SOURCE_DATE_EPOCH, when set, is the date in RELEASE.txt (so a rebuild can be compared).
# Exit status: 0 done, 1 refused or failed, 2 bad usage.
set -euo pipefail
caller_dir=$PWD
cd "$(dirname "$0")/.."

# The only files taken from the repository. Add to this list, and to ALLOWED_FROM_REPO in
# tests/unit/test_release_bundle.py, to ship more.
ALLOWED_FILES=(
  .env.example
  README.md
  docker-compose.yml
  docs/PRODUCTION_READINESS.md
  docs/SECURITY.md
  docs/THIRD_PARTY.md
  docs/sbom.cdx.json
)

die() { echo "error: $*" >&2; exit 1; }
usage_error() {
  echo "error: $*" >&2
  echo "usage: $0 (--image TAG [--image TAG ...] | --no-image) [--commit SHA] [--out-dir DIR]" >&2
  echo "       $0 --list-allow   (see the header of the script)" >&2
  exit 2
}

images=()
no_image=0
want_commit=""
out_parent=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) [[ -n "${2:-}" ]] || usage_error "--image needs a tag"; images+=("$2"); shift ;;
    --no-image) no_image=1 ;;
    --commit) [[ -n "${2:-}" ]] || usage_error "--commit needs a commit"; want_commit="$2"; shift ;;
    --out-dir) [[ -n "${2:-}" ]] || usage_error "--out-dir needs a directory"; out_parent="$2"; shift ;;
    --list-allow) printf '%s\n' "${ALLOWED_FILES[@]}"; exit 0 ;;
    -h|--help) sed -n '2,/^set -euo pipefail/{/^set -euo pipefail/!p}' "$0"; exit 0 ;;
    *) usage_error "unknown argument: $1" ;;
  esac
  shift
done
if [[ $no_image -eq 1 && ${#images[@]} -gt 0 ]]; then usage_error "--image and --no-image exclude each other"; fi
if [[ $no_image -eq 0 && ${#images[@]} -eq 0 ]]; then usage_error "say --image TAG or --no-image"; fi
if [[ -n "$out_parent" && "$out_parent" != /* ]]; then out_parent="$caller_dir/$out_parent"; fi
out_parent="${out_parent:-dist}"

# --- the commit ----------------------------------------------------------------------------------
git rev-parse --git-dir >/dev/null 2>&1 || die "not inside a git repository"
if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  echo "error: the working tree is not clean; a bundle is built from a commit, so commit or stash first:" >&2
  git status --short --untracked-files=all | head -20 >&2
  exit 1
fi
head_sha="$(git rev-parse --verify 'HEAD^{commit}')"
if [[ -n "$want_commit" ]]; then
  want_sha="$(git rev-parse --verify --quiet "${want_commit}^{commit}")" \
    || die "--commit $want_commit is not a commit in this repository"
  [[ "$want_sha" == "$head_sha" ]] \
    || die "HEAD is not $want_commit (HEAD is $head_sha); check that commit out first"
fi
short_sha="${head_sha:0:12}"

version="$(git show HEAD:pyproject.toml | awk '
  /^\[project\]/ { in_project = 1; next }
  /^\[/ { in_project = 0 }
  in_project && /^version[[:space:]]*=/ { gsub(/^version[[:space:]]*=[[:space:]]*"|"[[:space:]]*$/, ""); print; exit }')"
[[ -n "$version" ]] || die "pyproject.toml has no [project] version"

for f in "${ALLOWED_FILES[@]}"; do
  git cat-file -e "HEAD:$f" 2>/dev/null || die "$f is not in commit $short_sha"
done

lock_sha="$(git show HEAD:uv.lock | sha256sum | cut -d' ' -f1)"
sbom_lock="$(git show HEAD:docs/sbom.cdx.json | grep -A1 '"pa:uv-lock-sha256"' | grep -Eo '[0-9a-f]{64}' | head -n1 || true)"
[[ "$sbom_lock" == "$lock_sha" ]] \
  || die "docs/sbom.cdx.json was made from a different uv.lock; regenerate it (uv run python scripts/sbom.py --out docs/sbom.cdx.json), commit, and run this again"
git show HEAD:docs/THIRD_PARTY.md | grep -q "SHA-256 \`$lock_sha\`" \
  || die "docs/THIRD_PARTY.md was made from a different uv.lock; regenerate it (uv run python scripts/third_party.py --out docs/THIRD_PARTY.md), commit, and run this again"

bundle_name="procurement-agent-${version}-${short_sha}"
final="$out_parent/$bundle_name"
[[ ! -e "$final" ]] || die "$final already exists; not overwriting it"

if [[ ${#images[@]} -gt 0 ]]; then
  command -v docker >/dev/null 2>&1 || die "docker is not on PATH, and --image needs it"
  for tag in "${images[@]}"; do
    docker image inspect --format '{{.Id}}' "$tag" >/dev/null 2>&1 \
      || die "docker has no image $tag; build or load it first"
  done
fi

# --- assemble in a staging directory ---------------------------------------------------------------
mkdir -p "$out_parent"
stage="$(mktemp -d "$out_parent/.${bundle_name}.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
chmod 755 "$stage"

echo ">> files from commit $short_sha"
git archive --format=tar HEAD -- "${ALLOWED_FILES[@]}" | tar -x -C "$stage"

image_file="procurement-agent-${version}-image.tar.gz"
if [[ ${#images[@]} -gt 0 ]]; then
  echo ">> docker save ${images[*]} | gzip"
  docker save "${images[@]}" | gzip -n > "$stage/$image_file"
fi

compose_images=()
while IFS= read -r ref; do compose_images+=("$ref"); done \
  < <(grep -E '^[[:space:]]+image:' "$stage/docker-compose.yml" | awk '!seen[$2]++ {print $2}')
in_archive=()
not_in_archive=()
for ref in "${compose_images[@]}"; do
  found=0
  for tag in "${images[@]+"${images[@]}"}"; do
    if [[ "$tag" == "$ref" || "$tag" == "${ref%%@*}" ]]; then found=1; fi
  done
  if [[ $found -eq 1 ]]; then in_archive+=("$ref"); else not_in_archive+=("$ref"); fi
done

tag_name="$(git describe --exact-match --tags HEAD 2>/dev/null || true)"
if [[ -n "${SOURCE_DATE_EPOCH:-}" ]]; then
  date_utc="$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%dT%H:%M:%SZ)"
else
  date_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi
sha_of() { sha256sum "$1" | cut -d' ' -f1; }

# --- RELEASE.txt -------------------------------------------------------------------------------------
{
  echo "# procurement-agent release bundle"
  echo "product: procurement-agent"
  echo "version: $version"
  echo "commit: $head_sha"
  echo "tag: ${tag_name:-none}"
  echo "date_utc: $date_utc"
  echo "uv_lock_sha256: $lock_sha"
  echo "sbom_sha256: $(sha_of "$stage/docs/sbom.cdx.json")"
  echo "third_party_sha256: $(sha_of "$stage/docs/THIRD_PARTY.md")"
  if [[ ${#images[@]} -gt 0 ]]; then
    echo "image_archive: $image_file"
    for tag in "${images[@]}"; do
      id="$(docker image inspect --format '{{.Id}}' "$tag")"
      size="$(docker image inspect --format '{{.Size}}' "$tag")"
      digests="$(docker image inspect --format '{{range .RepoDigests}}{{.}} {{end}}' "$tag")"
      digests="${digests% }"
      echo "  image: $tag"
      echo "    id: $id"
      echo "    size_bytes: $size"
      echo "    repo_digests: ${digests:-none (built or loaded locally)}"
    done
  else
    echo "image_archive: not included (built with --no-image; no application image is part of this bundle)"
  fi
  echo "compose_images:"
  for ref in "${compose_images[@]}"; do echo "  $ref"; done
} > "$stage/RELEASE.txt"

# --- INSTALL.md ----------------------------------------------------------------------------------------
backup_cmd="$(grep -m1 '^docker compose exec -T app python -c' "$stage/docs/PRODUCTION_READINESS.md" || true)"
[[ -n "$backup_cmd" ]] || die "docs/PRODUCTION_READINESS.md no longer has the backup command INSTALL.md quotes"

# Settings the deployer must look at, quoted from the example file (a key the example lacks is left
# out, so this cannot name a setting that does not exist).
env_settings=()
for key in PA_ALLOWED_HOSTS PA_PARSER; do
  line="$(grep -m1 "^${key}=" "$stage/.env.example" || true)"
  if [[ -n "$line" ]]; then env_settings+=("$line"); fi
done

{
  printf '# Installing procurement-agent %s on an air-gapped server\n\n' "$version"
  printf 'Version %s, commit `%s` (RELEASE.txt). Every step works without a network.\n\n' "$version" "$head_sha"
  cat <<'EOF'
In this directory: `docker-compose.yml`, `.env.example`, `README.md`, `docs/` (SECURITY.md,
PRODUCTION_READINESS.md, THIRD_PARTY.md, sbom.cdx.json), RELEASE.txt, SHA256SUMS and, if the bundle
was made with images, the image archive. The README links to documents that are not in the bundle
(ACCURACY.md and others); they are in the repository.

Not in it, because they come from other places: the models (section 3), the reference data
(section 7) and a TLS reverse proxy (section 8). `docker-compose.yml` says `build: .` for the app;
there is no build context here, so never run `docker compose build`: the image is loaded (section 2).

Run every command from this directory.

## 1. Verify the files

```bash
sha256sum -c SHA256SUMS
```

Every line must say OK. RELEASE.txt records the SHA-256 of `uv.lock` and of `docs/sbom.cdx.json`
(the packages in the image, with the hashes of their files); compare the bundle's own hash with the
one you were given separately when it was handed over. `docs/THIRD_PARTY.md` lists the licences.

## 2. Load the images

EOF
  if [[ ${#images[@]} -gt 0 ]]; then
    printf 'Images in the archive: '
    sep=""
    for ref in "${in_archive[@]+"${in_archive[@]}"}"; do printf '%s`%s`' "$sep" "$ref"; sep=", "; done
    [[ ${#in_archive[@]} -gt 0 ]] || printf '(none of the images docker-compose.yml names)'
    printf '\n\n```bash\ngunzip -c %s | docker load\n```\n\n' "$image_file"
  else
    printf 'This bundle has no image archive (it was made with `--no-image`). Bring every image below to\nthe server yourself: `docker save` on a connected machine, `docker load` here.\n\n'
  fi
  if [[ ${#not_in_archive[@]} -gt 0 ]]; then
    if [[ ${#images[@]} -gt 0 ]]; then
      printf 'Images docker-compose.yml needs that are NOT in the archive (bring them separately,\n`docker save` there and `docker load` here):\n\n'
    else
      printf 'Images docker-compose.yml needs:\n\n'
    fi
    for ref in "${not_in_archive[@]}"; do printf -- '* `%s`\n' "$ref"; done
    printf '\n'
  fi
  cat <<'EOF'
Check that docker has them under the names compose uses:

```bash
docker compose config --images
docker image ls
```

`ollama` and `caddy` are pinned in `docker-compose.yml` by tag **and digest**. Whether a loaded
image satisfies a digest reference depends on the Docker version and image store of the target
server: verify on the target Docker (`docker image inspect <reference>` for each reference above).
If compose later reports it would pull, the reference was not satisfied; do not work around it by
editing the digest without telling whoever delivered the bundle.

## 3. Models and the Ollama volume

The models are made on a connected machine, from a checkout of this commit, with
`scripts/prefetch_models.sh`: Docling's layout and table models in `./models/docling`, the
checksum manifest `models/docling/MANIFEST.sha256` written there by
`python -m procurement_agent.cli models-manifest write`, and the Ollama models (`PA_LLM_MODEL` and
`PA_LLM_FAST_MODEL` in `.env.example`) in the Docker volume `ollama`. Copy both here
(docs/PRODUCTION_READINESS.md 5.3).

1. Put `models/` next to `docker-compose.yml` and check it:

   ```bash
   (cd models/docling && sha256sum -c MANIFEST.sha256)
   ```

   The manifest has to be the one written on the connected machine: one written here would only
   checksum whatever arrived. Production verifies it at every start and refuses to run on a changed,
   missing or extra model file. The spelling dictionary is baked into the image: nothing to copy.
2. Put the Ollama models into this server's `ollama` volume. One way, not exercised by this
   bundle: on the connected machine, `docker run --rm -v ollama:/from:ro -v "$PWD":/to --entrypoint tar <ollama image> czf /to/ollama-volume.tgz -C /from .`
   (use the volume's real name from `docker volume ls` there); here, after section 4,
   `docker volume create procurement-agent_ollama` and
   `docker run --rm -v procurement-agent_ollama:/to -v "$PWD":/from:ro --entrypoint tar <ollama image> xzf /from/ollama-volume.tgz -C /to`.
   After section 5, `docker compose exec ollama ollama list` shows what the server has.

## 4. Configuration (.env)

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

* Nothing to add for the project name: `docker-compose.yml` fixes it (`name: procurement-agent`),
  so the volumes are `procurement-agent_app-data` and `procurement-agent_ollama` whichever
  directory this release is unpacked in, and each release finds the keys, the audit trail and the
  models of the one before. If an earlier release was started from a directory with another name,
  its volumes carry that name instead: keep it once or copy them (docs/PRODUCTION_READINESS.md 5.6;
  `docker compose ls`, `docker volume ls`).
EOF
  for line in "${env_settings[@]+"${env_settings[@]}"}"; do
    key="${line%%=*}"
    case "$key" in
      PA_ALLOWED_HOSTS)
        printf '* `%s` (from .env.example): set it to the host names users type in the browser, comma\n  separated. Compose refuses to start without it, and the app answers 400 to any other name.\n' "$line" ;;
      *)
        printf '* `%s` (from .env.example): a setting the example file carries; read the comment above it\n  there and set it for this site.\n' "$line" ;;
    esac
  done
  cat <<'EOF'
* Keep `PA_DEV_API_KEY` empty: production refuses to start with it set. `PA_ENV`, the data and model
  paths and the Ollama address are set by `docker-compose.yml` itself, whatever `.env` says.
* `PA_APP_CPUS` and `PA_OLLAMA_CPUS`: below the host's core count, so that one runaway parse cannot
  starve the host (no default: Docker refuses a number above the cores).
* `PA_INTERNAL_SUBNET`, `PA_PROXY_IP`, `PA_EDGE_SUBNET`, `PA_EDGE_GATEWAY`: only if `172.30.231.0/24`
  or `172.30.232.0/24` collides with a network on the host; change them together.

Retention, upload limits and the rest are described in `.env.example`.

## 5. Start the stack

```bash
docker compose up -d --no-build --pull never
docker compose ps                     # app, parser, ollama and proxy should all turn "healthy"
```

`--no-build --pull never` keep compose from reaching for a Dockerfile or a registry: an image that
is missing is reported by name. The relay listens on `127.0.0.1:8000` only; the reverse proxy
(section 8) is what users reach.

## 6. Create the first API key

```bash
docker compose exec app python -m procurement_agent.cli create-key alice
```

The key is shown once. One key per person, so revocation and the audit trail stay per person.
`list-keys` and `revoke-key <key-id>` are the other two commands (docs/PRODUCTION_READINESS.md 5.4).

## 7. Reference data (exchange rates, business registry)

Both are prepared on a connected machine, copied here, imported, and the copy removed
(docs/PRODUCTION_READINESS.md 5.3). The preparing script is in a checkout of this commit, not in
this bundle.

Exchange rates for foreign-currency acts (`scripts/fetch_nbg_rates.py` in the checkout):

```bash
python3 scripts/fetch_nbg_rates.py --from <first day needed> --out nbg.json    # connected machine
docker compose cp nbg.json app:/data/nbg.json
docker compose exec app python -m procurement_agent.cli load-fx-rates /data/nbg.json
docker compose exec app rm /data/nbg.json
docker compose exec app python -m procurement_agent.cli fx-rates-status
```

Repeat as acts need newer days (a day's rate can also be typed on the report).

Business registry: the client's export as CSV (`id_code,name,address,directors,status,as_of[,legal_form]`),
imported the same way with `load-registry <csv> --as-of <export date>`, and `PA_REGISTRY_DB` set in
`.env`. The container's only writable place is `/data`, so give it a path there
(`PA_REGISTRY_DB=/data/registry.db`; the example in `.env.example` is for a development checkout:
verify).

## 8. Reverse proxy (the deployer's duties)

docs/PRODUCTION_READINESS.md 5.2 has the detail. In short, the company's proxy in front of
`127.0.0.1:8000` must:

* terminate TLS with the company certificate and redirect HTTP to HTTPS;
* set `X-Forwarded-For` to the client address (nginx: `proxy_set_header X-Forwarded-For $remote_addr;`),
  or every user shares one rate-limit bucket and the audit log records the Docker gateway;
* pass the original `Host` header (nginx: `proxy_set_header Host $host;`) and list the names users
  type in `PA_ALLOWED_HOSTS`;
* allow request bodies of at least 2 x `PA_MAX_FILE_MB` + 1 MB (101 MB by default), with read
  timeouts long enough for the slowest client link;
* not cache `/api/` responses.

## 9. Backups and restore

Put the `app-data` volume (`/data`) on an encrypted disk, and keep `.env` readable by the operator
only. `app.db` holds the keys (hashed), the sessions and the audit trail; SQLite runs in WAL mode,
so copy it with the backup API, never with `cp` of a live file. The image has no `sqlite3` binary;
use Python (docs/PRODUCTION_READINESS.md 5.5):

```bash
EOF
  printf '%s\n' "$backup_cmd"
  cat <<'EOF'
```

Check a copy with `sqlite3 ./backup/app-<date>.db 'pragma integrity_check'`. Uploaded documents are
deliberately short-lived (`PA_RETENTION_DAYS`); backing them up extends their life past that
promise, so decide it with the client. Store backups encrypted.

Restore (the stale `-wal` and `-shm` files must go, or SQLite replays them onto the restored file):

```bash
docker compose stop proxy app
docker compose run --rm --no-deps -T --entrypoint sh app -c \
  'cat > /data/app.db.restore && rm -f /data/app.db-wal /data/app.db-shm && mv /data/app.db.restore /data/app.db && chmod 600 /data/app.db' \
  < ./backup/app-<date>.db
docker compose up -d --no-build --pull never
```

## 10. Upgrade to a newer release

1. Back up `app.db` (section 9).
2. Unpack the new bundle beside this one, then do sections 1 and 2 for it.
3. In the OLD directory, stop and remove the containers:

   ```bash
   docker compose down
   ```

   Do not add the `--volumes` option: it deletes the volumes, and with them the keys, the audit
   trail and the models.
4. Bring `.env` and `models/` into the new directory: copy or move them. The volumes keep their
   names because the compose file fixes the project name.
5. In the new directory: `docker compose up -d --no-build --pull never`, then
   `docker compose ps` until all four services are healthy, and sign in once.

The worker stops within its timeout, the new process recovers interrupted jobs and brings the
database to the new release's schema version, after a copy of it (docs/PRODUCTION_READINESS.md 5.6
says how to look first with `db-status`, and how to go back). If Ollama, Docling or the models
changed, run `scripts/evaluate.py` from a checkout.
EOF
} > "$stage/INSTALL.md"

# --- SHA256SUMS, the allow-list check, and the move into place ----------------------------------------------
(
  cd "$stage"
  find . -type f ! -name SHA256SUMS | sed 's#^\./##' | LC_ALL=C sort | xargs -d '\n' sha256sum > SHA256SUMS
)

expected=("${ALLOWED_FILES[@]}" INSTALL.md RELEASE.txt SHA256SUMS)
if [[ ${#images[@]} -gt 0 ]]; then expected+=("$image_file"); fi
actual="$(cd "$stage" && find . -type f | sed 's#^\./##' | LC_ALL=C sort)"
wanted="$(printf '%s\n' "${expected[@]}" | LC_ALL=C sort)"
[[ "$actual" == "$wanted" ]] \
  || die "the bundle's files are not the allow-list's: $(diff <(echo "$wanted") <(echo "$actual") | tr '\n' ' ')"
[[ -z "$(find "$stage" -mindepth 1 ! -type f ! -type d)" ]] \
  || die "the bundle holds something that is neither a file nor a directory"

mv "$stage" "$final"
trap - EXIT
(cd "$final" && sha256sum -c --quiet SHA256SUMS) || die "SHA256SUMS does not verify the bundle just written"

echo ">> $final"
(cd "$final" && find . -type f | sed 's#^\./##' | LC_ALL=C sort | while IFS= read -r f; do
  printf '   %9s  %s\n' "$(stat -c %s "$f")" "$f"
done)
if [[ ${#images[@]} -eq 0 ]]; then
  echo ">> no image archive: RELEASE.txt and INSTALL.md say so (--no-image)"
fi
