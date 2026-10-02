#!/usr/bin/env bash
# Prove the product needs no network: run it inside a network namespace that has loopback and
# nothing else, where an attempt to leave the machine fails in the kernel - from Python, from a
# native library, from uvloop; the in-process egress guard (security/egress.py) cannot see the
# last two.
#
#   scripts/verify_offline.sh              smoke flow, then the unit + integration tests
#   scripts/verify_offline.sh --quick      smoke flow only
#
# The smoke flow (scripts/offline_smoke.py) starts the real application and a stub model server on
# loopback and drives upload -> parse -> extract -> compare -> BOQ -> reports -> UI -> delete.
# The namespace keeps your uid, so permission tests behave as they do outside it.
#
# PA_VERIFY_PYTHON=/path/to/python runs that interpreter instead of `uv run --offline python`
# (it needs the project's dependencies; PYTHONPATH is set to src).
# Exit 0 = everything passed inside the namespace; 2 = this host cannot create one (not a pass).
set -euo pipefail
cd "$(dirname "$0")/.."

mode=full
inside=0
for arg in "$@"; do
  case "$arg" in
    --quick) mode=quick ;;
    --inside) inside=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [[ -n "${PA_VERIFY_PYTHON:-}" ]]; then
  py=("$PA_VERIFY_PYTHON")
  export PYTHONPATH="$PWD/src:$PWD${PYTHONPATH:+:$PYTHONPATH}"
else
  py=(uv run --offline python)
fi

if [[ $inside -eq 0 ]]; then
  if ! unshare --user --map-user="$(id -u)" --map-group="$(id -g)" --net --keep-caps true 2>/dev/null; then
    echo "cannot create a user + network namespace here (unshare failed): NOT VERIFIED" >&2
    echo "  Fedora/RHEL: check user.max_user_namespaces; Debian/Ubuntu: kernel.unprivileged_userns_clone" >&2
    exit 2
  fi
  # `ip link set lo up` needs the namespace's capabilities, which --keep-caps preserves across
  # this one exec; the interpreter it then execs has none, and does not need them.
  exec unshare --user --map-user="$(id -u)" --map-group="$(id -g)" --net --keep-caps \
    sh -c 'ip link set lo up && exec "$0" --inside "$@"' "$0" "$@"
fi

echo "==> inside the namespace: interfaces = $(awk -F: 'NR>2{gsub(/ /,"",$1); printf "%s ", $1}' /proc/net/dev)"
"${py[@]}" - <<'PY'
import errno, socket, _socket
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
socket.create_connection(("127.0.0.1", srv.getsockname()[1]), timeout=2).close()
print("==> loopback works")
raw = _socket.socket(); raw.settimeout(2)
try:
    raw.connect(("1.1.1.1", 53))
except OSError as e:
    assert e.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH), e
    print(f"==> the internet is unreachable ({errno.errorcode[e.errno]})")
else:
    raise SystemExit("the namespace has a route out: refusing to call this verified")
PY

echo "==> smoke flow"
"${py[@]}" scripts/offline_smoke.py --expect-no-network

if [[ "$mode" == "full" ]]; then
  echo "==> unit + integration tests (the e2e tests need a real model server and are not part of this)"
  if [[ -n "${PA_VERIFY_PYTHON:-}" ]]; then
    "${py[@]}" -m pytest -q -p no:cacheprovider -m "not e2e and not docling" --no-header -q
  else
    uv run --offline pytest -q -p no:cacheprovider -m "not e2e and not docling" --no-header -q
  fi
fi
echo "==> VERIFIED: ran with no network at all"
