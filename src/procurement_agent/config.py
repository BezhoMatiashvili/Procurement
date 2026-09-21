"""Application settings.

All configuration comes from environment variables (prefix ``PA_``) or a local ``.env`` file.
The module also pins the process into offline mode for every ML library we use, so that no
component can reach the internet even if misconfigured.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

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
    rate_limit: str = Field(default="120/minute", description="slowapi limit string")

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
    llm_chunk_token_budget: int = Field(default=6000, ge=500)

    # --- Docling / OCR ---
    docling_artifacts_path: Path | None = None
    docling_num_threads: int = Field(default=12, ge=1, le=256)
    #: Docling had been sharing the LLM's timeout, so tuning one silently moved the other.
    docling_timeout_s: int = Field(default=900, ge=10)
    ocr_langs: Annotated[tuple[str, ...], NoDecode] = ("kat", "eng")

    # --- business registry snapshot (optional; absent means registry rules are skipped) ---
    registry_db: Path | None = None

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
        # The LLM endpoint must be a private/internal address; refuse anything else loudly.
        from urllib.parse import urlparse

        host = urlparse(v).hostname or ""
        private = (
            host in {"localhost", "ollama", "host.docker.internal"}
            or host.startswith(("127.", "10.", "192.168.", "172."))
            or host.endswith((".local", ".internal", ".lan"))
        )
        if not private:
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
