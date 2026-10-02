#!/usr/bin/env bash
# Move the development Ollama onto an internal network (docker-compose.dev.yml).
#
#   scripts/switch_dev_ollama.sh             dry run: show what would happen, change nothing
#   scripts/switch_dev_ollama.sh --yes       stop the old `ollama` container, start the isolated stack
#   scripts/switch_dev_ollama.sh --rollback  back to a plain `docker run` container on the default bridge
#   scripts/switch_dev_ollama.sh --pull gemma3:12b
#                                            download a model through a temporary CONNECTED container
#                                            (the isolated stack has no route out, by design)
#
# Switching cuts the model out from under the running dev server for about a minute. The `ollama`
# volume - the pulled models - is never touched. Requires Docker.
set -euo pipefail
cd "$(dirname "$0")/.."

FILE=docker-compose.dev.yml
IMAGE='ollama/ollama:0.34.2@sha256:da6e0dc5651df159e45686fd663c4dbe1624a52c44d7280eeac1551d8f865532'
mode=dry; model=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) mode=switch ;;
    --rollback) mode=rollback ;;
    --pull) mode=pull; model="${2:?--pull needs a model name}"; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '==> %s\n' "$*"; }
current() { docker inspect ollama --format 'network={{.HostConfig.NetworkMode}} image={{.Config.Image}} capdrop={{.HostConfig.CapDrop}}' 2>/dev/null || echo "no container named ollama"; }

docker compose -f "$FILE" config -q   # the file must be valid before anything is touched

case "$mode" in
  dry)
    say "now: $(current)"
    say "would: docker stop ollama && docker rm ollama   (the volume 'ollama' stays)"
    say "would: docker compose -f $FILE up -d            (ollama on an internal network + loopback relay)"
    say "then check: 127.0.0.1:11434 answers, the network is internal, the model is still there"
    say "dry run only; pass --yes to do it"
    ;;
  switch)
    say "now: $(current)"
    docker volume inspect ollama >/dev/null   # models live here; refuse to go on without it
    docker stop ollama >/dev/null 2>&1 || true
    docker rm ollama >/dev/null 2>&1 || true
    docker compose -f "$FILE" up -d
    say "waiting for the relay..."
    for _ in $(seq 1 60); do
      if curl -fsS --max-time 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then break; fi
      sleep 2
    done
    curl -fsS http://127.0.0.1:11434/api/version && echo
    internal=$(docker network inspect procurement-dev_llm --format '{{.Internal}}')
    say "network procurement-dev_llm internal: $internal"
    [[ "$internal" == "true" ]] || { echo "the model network is not internal" >&2; exit 1; }
    say "models: $(curl -fsS http://127.0.0.1:11434/api/tags | python3 -c 'import json,sys; print([m["name"] for m in json.load(sys.stdin)["models"]])')"
    say "the relay cannot reach the internet either (expect a failure below):"
    if docker compose -f "$FILE" exec -T relay wget -q -T 3 -O /dev/null https://example.com; then
      echo "the relay reached the internet" >&2; exit 1
    else
      say "no route out: good"
    fi
    ;;
  rollback)
    docker compose -f "$FILE" down   # no -v: the models stay
    docker run -d --name ollama --restart unless-stopped -p 127.0.0.1:11434:11434 \
      -v ollama:/root/.ollama "$IMAGE"
    say "back on the default bridge (this one CAN reach the internet): $(current)"
    ;;
  pull)
    if [[ -n "$(docker compose -f "$FILE" ps -q 2>/dev/null)" ]]; then
      echo "stop the isolated stack first: docker compose -f $FILE down" >&2; exit 1
    fi
    say "pulling $model through a temporary CONNECTED container; this is the one step that uses the network"
    docker run --rm -v ollama:/root/.ollama --entrypoint sh "$IMAGE" \
      -c "ollama serve >/dev/null 2>&1 & sleep 4; ollama pull '$model'"
    say "done; start the stack again: docker compose -f $FILE up -d"
    ;;
esac
