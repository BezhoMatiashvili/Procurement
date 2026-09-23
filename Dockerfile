# syntax=docker/dockerfile:1.7
# Multi-stage build: dependencies are resolved from uv.lock; the runtime image is minimal,
# runs as non-root and contains Tesseract with Georgian + English language data.
#
# Base images are pinned to exact patch tags (Python 3.12.14 on Debian 12, uv 0.12.8 — the uv
# that wrote uv.lock). Tags can still be re-pushed upstream, so pin digests at release time:
#   docker buildx imagetools inspect python:3.12.14-slim-bookworm --format '{{.Manifest.Digest}}'
#   docker buildx imagetools inspect ghcr.io/astral-sh/uv:0.12.8   --format '{{.Manifest.Digest}}'
# and append @sha256:<digest> to both references below (keep the tag for readability).
ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.8

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
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
COPY --chown=app:app src ./src
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
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    XDG_CACHE_HOME=/tmp/.cache \
    HF_HOME=/tmp/.cache/huggingface \
    TORCH_HOME=/tmp/.cache/torch \
    MPLCONFIGDIR=/tmp/.cache/matplotlib \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    PA_ENV=prod \
    PA_DATA_DIR=/data \
    PA_DOCLING_ARTIFACTS_PATH=/models/docling \
    OMP_NUM_THREADS=12
USER app
VOLUME /data
EXPOSE 8000
# Readiness, not liveness: /ready fails when the database or the job worker is not healthy, so
# `docker ps` shows "unhealthy" for an instance that accepts uploads it will never process.
# /health stays a pure liveness probe for anything that only needs to know the process is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/ready', timeout=3).status == 200 else 1)"]
CMD ["uvicorn", "procurement_agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
