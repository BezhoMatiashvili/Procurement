"""Application settings.

All configuration comes from environment variables (prefix ``PA_``) or a local ``.env`` file.
The module also pins the process into offline mode for every ML library we use, so that no
component can reach the internet even if misconfigured.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Hard offline mode: HuggingFace hub / transformers / docling must never phone home.
# Set before any of those libraries are imported anywhere in the process.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

DocumentKind = Literal["pdf", "docx", "xlsx"]
#: The most files any single form posts (the act check: act + contract; compare: two versions).
MAX_FILES_PER_REQUEST = 2

#: Names that are internal by construction: the compose service, the host alias Docker adds,
#: and the loopback name.
_INTERNAL_NAMES = frozenset({"localhost", "ollama", "host.docker.internal"})
#: Suffixes that only a private resolver answers for (RFC 6762 ``.local``, the de-facto
#: ``.lan`` and ``.internal``).
_INTERNAL_SUFFIXES = (".local", ".internal", ".lan")
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


def _is_internal_host(host: str) -> bool:
    """True for a private IP literal, a known internal name, or an internal-suffix hostname.

    ``ipaddress`` calls ``0.0.0.0`` and ``169.254.169.254`` (the cloud metadata service)
    private, so link-local, unspecified, multicast and reserved addresses are excluded
    explicitly. An IPv4-mapped IPv6 literal is judged by the IPv4 address it carries.
    """
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.is_loopback:
            return True  # checked first: ``::1`` also sits in a reserved block
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            return False
        return ip.is_private
    if host in _INTERNAL_NAMES:
        return True
    if not _HOSTNAME_RE.match(host) or not host.endswith(_INTERNAL_SUFFIXES):
        return False
    # "127.0.0.1.lan" is a name, not an address; an all-numeric first label is the shape of the
    # wildcard-DNS trick above, so refuse it rather than guess what a resolver does with it.
    return not host.split(".", 1)[0].isdigit()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PA_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["dev", "prod"] = "dev"
    data_dir: Path = Path("./data")

    # --- upload limits ---
    max_file_mb: int = Field(default=50, ge=1, le=500)
    max_pages: int = Field(default=300, ge=1, le=5000)
    allowed_kinds: Annotated[tuple[DocumentKind, ...], NoDecode] = ("pdf", "docx", "xlsx")
    retention_days: int = Field(default=7, ge=0, le=365)
    rate_limit: str = Field(default="120/minute", description="requests per client address")
    #: Queued plus running jobs one user may have at once. The worker is a single thread, so
    #: an unbounded queue is one user's script starving everybody else for hours.
    max_active_jobs_per_user: int = Field(default=20, ge=1, le=10_000)

    # --- worker ---
    #: How long shutdown waits for the running job before giving up on it. The job is then
    #: failed by the next start's recovery, like any other interrupted job.
    worker_stop_timeout_s: float = Field(default=30.0, ge=1.0, le=3600.0)

    # --- browser sessions ---
    #: Lifetime of a UI sign-in, enforced on the server (the cookie's max-age is only a hint).
    ui_session_hours: int = Field(default=8, ge=1, le=168)

    # --- LLM ---
    ollama_base_url: str = "http://ollama:11434"
    llm_model: str = "gemma3:12b"
    llm_fast_model: str = "gemma3:4b"
    llm_num_ctx: int = Field(default=8192, ge=2048, le=131072)
    llm_num_thread: int = Field(default=16, ge=1, le=256)
    llm_timeout_s: int = Field(default=900, ge=10)
    #: Cap on generated tokens. Output length dominates CPU latency, so this is the single
    #: most effective latency knob; grammar-constrained output that hits it is truncated JSON.
    llm_max_output_tokens: int = Field(default=1024, ge=64, le=8192)
    #: Aggregate estimated tokens of the chunks one job may send to the LLM. Counted with
    #: ``tokens.estimate_tokens``; when that estimate was corrected upwards (it under-counted
    #: Georgian by up to a third) this default moved from 6000 to 7800 so the same real amount
    #: of text is still selected. Change the two together.
    llm_chunk_token_budget: int = Field(default=7800, ge=500)
    #: Ask closed-answer-space questions (VAT treatment, and a check on each generated fact)
    #: alongside the generation pass. **Leave this off.** Measured on the client set: it lifts
    #: the LLM path's precision from 66.2% to 71.0% at no cost in recall — the verifier really
    #: does remove values the model cannot stand behind — but the LLM path scores F1 0.755
    #: against 0.790 for rules and anchors alone, so the whole path is a regression here and
    #: this flag only makes the regression smaller. It also costs 60% more wall clock (28 -> 45
    #: minutes over 7 documents), because the hoped-for prefix reuse does NOT materialise at
    #: real chunk sizes: the median choice call still spent 8.7 s on prefill, i.e. roughly one
    #: prefill per question rather than per chunk. Worth revisiting only on a corpus where the
    #: anchors do not fire. See docs/ACCURACY.md.
    llm_choices: bool = False

    # --- document comparison ---
    #: Most segments (paragraphs, list items, table cells, spreadsheet cells) one side may have.
    #: A 13-sheet BOQ measures about 2600, a Word acceptance act about 60.
    compare_max_segments: int = Field(default=20_000, ge=100, le=200_000)
    #: Below this overlap the two files are not two versions of one document and the
    #: line-by-line diff is noise. Measured on the client's set: revisions of one act align at
    #: 0.81-0.98, an act against its own contract draft at 0.08.
    compare_min_similarity: float = Field(default=0.35, ge=0.0, le=1.0)
    #: Whether the "explain with the model" action is offered at all. This is the operator's
    #: switch, not the default behaviour: no comparison ever calls the model unless a user asks
    #: for it, because one batched call costs tens of seconds of CPU.
    compare_llm_enabled: bool = True
    #: Changes sent to the model in one batch. One call, one prefill - see docs/ACCURACY.md on
    #: why serial calls would not be cheaper.
    compare_llm_max_hunks: int = Field(default=8, ge=1, le=40)

    # --- acceptance act: the model review of the subject and the services ---
    #: Whether the act report offers "review with the model" for the notes the rules could not
    #: decide (``checks/aoa_llm.py``). The operator's switch: the check itself never calls the
    #: model, and the review only ever marks a note - it cannot clear one or make it an error.
    aoa_llm_enabled: bool = True
    #: Queue the review by itself when a report has notes to review. Off: the worker is a
    #: single thread, and one review is about a minute and a half of CPU that every report view
    #: would otherwise put ahead of other users' uploads.
    aoa_llm_auto: bool = False
    #: Notes sent to the model in one review. Each answer carries two quotes and two sentences,
    #: so the output grows with this; eight fit the default context window with room to spare.
    aoa_llm_max_items: int = Field(default=8, ge=1, le=40)
    #: Characters of document text one review prompt may hold; notes past it are not sent, and
    #: a note in the report says how many.
    aoa_llm_max_chars: int = Field(default=6000, ge=500, le=60_000)

    # --- Docling / OCR ---
    docling_artifacts_path: Path | None = None
    docling_num_threads: int = Field(default=12, ge=1, le=256)
    #: Docling had been sharing the LLM's timeout, so tuning one silently moved the other.
    docling_timeout_s: int = Field(default=900, ge=10)
    ocr_langs: Annotated[tuple[str, ...], NoDecode] = ("kat", "eng")

    # --- business registry snapshot (optional; absent means registry rules are skipped) ---
    registry_db: Path | None = None

    # --- exchange rates for the foreign-currency invoice (client rule 9) ---
    #: Where the NBG rates come from (``fx/provider.py``). Only "none" exists: with no source the
    #: check writes out the invoice formula and the days whose rates to look up, and says the
    #: total in lari was not calculated. A connector adds its name here.
    fx_rate_source: Literal["none"] = "none"

    # --- auth ---
    dev_api_key: str | None = None

    # NoDecode on the fields above keeps pydantic-settings from json-decoding them first, so
    # these validators receive the raw "kat,eng" string that .env.example documents.
    @field_validator("ocr_langs", "allowed_kinds", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """Accept ``kat,eng`` (as documented) and ``["kat","eng"]`` (what people wrote to work
        around the json-decoding this field used to be subject to)."""
        if not isinstance(v, str):
            return v
        text = v.strip()
        if text.startswith("["):
            try:
                loaded = json.loads(text)
            except ValueError:
                pass
            else:
                if isinstance(loaded, list):
                    return tuple(str(x).strip() for x in loaded if str(x).strip())
        return tuple(x.strip() for x in text.split(",") if x.strip())

    @field_validator("ollama_base_url")
    @classmethod
    def _local_only(cls, v: str) -> str:
        """The LLM endpoint must be a private/internal address; refuse anything else loudly.

        This used to be a string-prefix test, which accepted ``10.evil.example``,
        ``192.168.attacker.net``, ``127.0.0.1.nip.io`` (a public wildcard-DNS name that
        resolves wherever its label says) and every public ``172.x`` address. Document text is
        sent to this URL, so a typo here would ship client data off the box. Now an IP literal
        must really be private, and a name must be one of ours or carry an internal suffix.
        """
        parsed = urlparse(v)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not _is_internal_host(host):
            msg = f"PA_OLLAMA_BASE_URL must point to an internal host, got {host!r}"
            raise ValueError(msg)
        return v.rstrip("/")

    # --- derived paths ---
    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def max_request_bytes(self) -> int:
        """Largest request body the server will read, counted as it arrives.

        The no-JavaScript act and comparison forms post two files in one request, so the cap
        is two files plus a megabyte for multipart framing and the small text fields. One global
        cap is used rather than a per-path table: every other endpoint takes at most one file or
        a small JSON body, and a single number cannot drift out of step with the routes.
        """
        return MAX_FILES_PER_REQUEST * self.max_file_bytes + 1024 * 1024

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.upload_dir, self.results_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)

    def validate_for_prod(self) -> None:
        """Fail fast on insecure production configuration."""
        if self.env != "prod":
            return
        problems: list[str] = []
        if self.dev_api_key:
            problems.append("PA_DEV_API_KEY must be empty in prod")
        if self.docling_artifacts_path is None:
            problems.append("PA_DOCLING_ARTIFACTS_PATH must point to pre-downloaded models in prod")
        if problems:
            raise RuntimeError("Refusing to start: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
