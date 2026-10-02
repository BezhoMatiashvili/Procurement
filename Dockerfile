# syntax=docker/dockerfile:1.7
# Multi-stage build: dependencies are resolved from uv.lock; the runtime image is minimal,
# runs as non-root and contains Tesseract with Georgian + English language data.
#
# Base images are pinned to exact patch tags (Python 3.12.14 on Debian 12, uv 0.12.8 — the uv
# that wrote uv.lock) AND to the digest those tags had on 2026-10-02, because a tag can be
# re-pushed upstream. To move to a newer base on purpose:
#   docker buildx imagetools inspect python:3.12.14-slim-bookworm --format '{{.Manifest.Digest}}'
#   docker buildx imagetools inspect ghcr.io/astral-sh/uv:0.12.8   --format '{{.Manifest.Digest}}'
# and replace the @sha256:<digest> on both references below (keep the tag for readability).
ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.8@sha256:d1cbaeadc234fe19c0d93daabcf5e98738cd93c6d1dd4918ef6aa30735feb23a

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# The hunspell ka_GE dictionary for the act's prose spelling check (AOA.SPELLING): one upstream
# commit, checked against its SHA-256 by the build itself, so the running image never fetches it.
# MIT; generated from word lists under MIT and CC-BY 4.0 (docs/ACCURACY.md). It goes under /app,
# not /models, which docker-compose.yml mounts over. scripts/fetch_spelling_dictionary.py pins the
# same URLs and hashes, and a unit test fails when the two differ.
ADD --checksum=sha256:80d04afc4319209882a6e9be3bcb8d41543e79d94a8057c1e08b07d5e805352d https://raw.githubusercontent.com/gamag/ka_GE.spell/26d33614360c3784c199e3a6766eb5b095a134b8/dictionaries/ka_GE.aff /app/dictionaries/hunspell/ka_GE.aff
ADD --checksum=sha256:5963f268e996f1e27e17163873b4d32f41fae2a1e5bef0117126caefe62f5857 https://raw.githubusercontent.com/gamag/ka_GE.spell/26d33614360c3784c199e3a6766eb5b095a134b8/dictionaries/ka_GE.dic /app/dictionaries/hunspell/ka_GE.dic
# A downloaded file arrives mode 600 and ADD --chmod would set the directory's mode too, leaving
# nobody able to enter it: readable by the unprivileged app user, explicitly.
RUN chmod 0755 /app/dictionaries /app/dictionaries/hunspell \
 && chmod 0644 /app/dictionaries/hunspell/ka_GE.aff /app/dictionaries/hunspell/ka_GE.dic
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM ${PYTHON_IMAGE} AS runtime
ARG PYTHON_IMAGE
LABEL org.opencontainers.image.title="procurement-agent" \
      org.opencontainers.image.description="Local, air-gapped document intelligence for procurement (PDF/DOCX/XLSX, Georgian + English)" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.licenses="Proprietary" \
      org.opencontainers.image.base.name="${PYTHON_IMAGE}"
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-kat tesseract-ocr-eng libgl1 libglib2.0-0 \
 && apt-get clean && rm -r /var/lib/apt/lists \
 && groupadd -r app && useradd -r -g app -d /app -s /usr/sbin/nologin app \
 && mkdir -p /data && chown app:app /data && chmod 700 /data
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder /app/dictionaries /app/dictionaries
COPY --chown=app:app src ./src
# The process that restarts the server when /ready stops answering (docker-compose.yml runs it): the
# one file of scripts/ in the image. Standard library only, so it runs when the app's imports break.
COPY --chown=app:app scripts/supervise.py ./scripts/supervise.py
# The root filesystem is read-only in docker-compose.yml and HOME (/app) is not writable, so
# every library that keeps a cache (HuggingFace, torch, matplotlib, generic XDG users) is
# pointed at /tmp, which compose mounts as tmpfs. Nothing there needs to survive a restart:
# models are read from /models, never downloaded.
#
# FORWARDED_ALLOW_IPS is read by uvicorn itself (no PA_ prefix). It lists the peers allowed to
# set X-Forwarded-For; client addresses feed the audit log and the rate limiter, so trusting
# "*" would let any caller pick its own. The default trusts nobody but loopback; the deployer
# sets it to the reverse proxy's address (docker-compose.yml sets it to its relay).
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_IMPLICIT_TOKEN=1 \
    HF_HUB_DISABLE_UPDATE_CHECK=1 \
    DO_NOT_TRACK=1 \
    XDG_CACHE_HOME=/tmp/.cache \
    HF_HOME=/tmp/.cache/huggingface \
    TORCH_HOME=/tmp/.cache/torch \
    MPLCONFIGDIR=/tmp/.cache/matplotlib \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    PA_ENV=prod \
    PA_DATA_DIR=/data \
    PA_DOCLING_ARTIFACTS_PATH=/models/docling \
    PA_SPELLING_DICTIONARY_PATH=/app/dictionaries/hunspell \
    OMP_NUM_THREADS=12
USER app
VOLUME /data
EXPOSE 8000
# Readiness, not liveness: /ready fails when the database or the job worker is not healthy, so
# `docker ps` shows "unhealthy" for an instance that accepts uploads it will never process.
# /health stays a pure liveness probe for anything that only needs to know the process is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/ready', timeout=3).status == 200 else 1)"]
CMD ["uvicorn", "procurement_agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--no-server-header"]
