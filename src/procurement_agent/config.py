"""Application settings.

All configuration comes from environment variables (prefix ``PA_``) or a local ``.env`` file.
The offline pins for every ML library live in ``procurement_agent/__init__.py``, which runs before
any library is imported, whichever module an entry point imports first.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DocumentKind = Literal["pdf", "docx", "xlsx"]
#: The most files any single form posts (the act check: act + contract; compare: two versions).
MAX_FILES_PER_REQUEST = 2
#: The most a request body may carry on a route that takes no file: sign-in, a check's JSON, a
#: form of ids and short text. Generous - the largest real one is a few hundred bytes.
SMALL_BODY_BYTES = 1024 * 1024

#: Names that are internal by construction: the compose service, the host alias Docker adds,
#: and the loopback name.
_INTERNAL_NAMES = frozenset({"localhost", "ollama", "host.docker.internal"})
#: Suffixes that only a private resolver answers for (RFC 6762 ``.local``, the de-facto
#: ``.lan`` and ``.internal``).
_INTERNAL_SUFFIXES = (".local", ".internal", ".lan")
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
#: The longest path a Unix socket address holds on Linux (``sun_path`` is 108 bytes with its NUL).
_UNIX_PATH_MAX = 107


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


def _is_local_host(host: str) -> bool:
    """True for this machine and the compose service: a loopback address, or one of the names
    that mean them (``localhost``, the ``ollama`` service, Docker's ``host.docker.internal``)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in _INTERNAL_NAMES
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def check_llm_url(
    url: str,
    *,
    allow_lan: bool = False,
    name: str = "PA_OLLAMA_BASE_URL",
    lan_hint: str = "set PA_LLM_ALLOW_LAN=true to use another host on the company network",
) -> str:
    """The URL without a trailing slash, or ``ValueError`` when it may not receive document text.

    Document text is sent to this URL, so it has to name a private host - and unless
    ``allow_lan`` says otherwise, this machine or the compose service: then "nothing leaves this
    computer" is a fact about one address, not about whatever the company network holds. Shared
    by the settings, the client (a ``model_copy`` bypasses validators) and the scripts that take
    a ``--base`` (an API key goes there), which say what they were given in ``name``.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not _is_internal_host(host):
        msg = f"{name} must point to an internal host, got {host!r}"
        raise ValueError(msg)
    if not allow_lan and not _is_local_host(host):
        msg = (
            f"{name} must be this machine or the compose service (localhost, 127.0.0.1, ollama, "
            f"host.docker.internal), got {host!r}; {lan_hint}"
        )
        raise ValueError(msg)
    return url.rstrip("/")


#: A name in ``PA_ALLOWED_HOSTS``: labels of letters, digits and hyphens, optionally ``*.`` first.
_HOST_ENTRY = re.compile(r"(?:\*\.)?[a-z0-9](?:[a-z0-9.\-]*[a-z0-9])?")
#: What Ollama accepts as a model name: ``name[:tag]``, the name possibly a path (``hf.co/org/m``).
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,80}(?::[A-Za-z0-9][A-Za-z0-9._-]{0,40})?")
#: A model Ollama forwards to its hosted service (``gpt-oss:120b-cloud``): the prompt, which is
#: the document's text, would leave the machine through the local server.
_CLOUD_MODEL = re.compile(r"(?:^|[:/._-])cloud(?:$|[:/._-])", re.IGNORECASE)


def check_model_name(name: str) -> str:
    """The model name as Ollama will see it, or ``ValueError`` if this server cannot run it here.

    ``gpt-oss:120b-cloud`` and its like are forwarded by Ollama to a hosted service, so the
    document would leave the machine through the local server. The client checks every name it is
    handed, not only the configured ones: the evaluation scripts take ``--model``.
    """
    name = name.strip()
    if not _MODEL_NAME.fullmatch(name):
        msg = f"{name!r} is not an Ollama model name"
        raise ValueError(msg)
    if _CLOUD_MODEL.search(name):
        msg = f"{name!r} is a hosted model; only models that run on this server are allowed"
        raise ValueError(msg)
    return name


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PA_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["dev", "prod"] = "dev"
    data_dir: Path = Path("./data")

    # --- upload limits ---
    max_file_mb: int = Field(default=50, ge=1, le=500)
    max_pages: int = Field(default=300, ge=1, le=5000)
    #: Characters of text a parsed document may hold. The upload limit bounds the file, and a
    #: compressed one holds hundreds of megabytes of text, every character of which is read by
    #: hundreds of patterns and, for a comparison, aligned against another document. The largest
    #: real document measured has well under a million; this is six times that.
    max_text_chars: int = Field(default=6_000_000, ge=10_000, le=200_000_000)
    allowed_kinds: Annotated[tuple[DocumentKind, ...], NoDecode] = ("pdf", "docx", "xlsx")
    retention_days: int = Field(default=7, ge=0, le=365)
    rate_limit: str = Field(default="120/minute", description="requests per client address")
    #: Queued plus running jobs one user may have at once. The worker is a single thread, so
    #: an unbounded queue is one user's script starving everybody else for hours.
    max_active_jobs_per_user: int = Field(default=20, ge=1, le=10_000)
    #: Reports (the act check, a comparison, a bill of quantities - and each download of one) being
    #: computed at the same time. They run on the web server's shared thread pool; past this many
    #: the next request is answered with 503 and ``Retry-After`` instead of waiting without limit.
    max_concurrent_reports: int = Field(default=4, ge=1, le=64)
    #: How long a report waits for a free slot before it is refused with 503.
    report_wait_s: float = Field(default=2.0, ge=0.0, le=60.0)

    # --- worker ---
    #: How long shutdown waits for the running job before giving up on it. The job is then
    #: failed by the next start's recovery, like any other interrupted job.
    worker_stop_timeout_s: float = Field(default=30.0, ge=1.0, le=3600.0)

    # --- browser sessions ---
    #: Lifetime of a UI sign-in, enforced on the server (the cookie's max-age is only a hint).
    ui_session_hours: int = Field(default=8, ge=1, le=168)
    #: A sign-in that has not been used for this long is over, however much of its lifetime is
    #: left: a browser left open on a shared screen is not a credential for the rest of the shift.
    ui_session_idle_minutes: int = Field(default=60, ge=5, le=1440)

    # --- LLM ---
    ollama_base_url: str = "http://ollama:11434"
    #: May the model server be a host elsewhere on the company network? Off: it has to be this
    #: machine or the compose service, so document text provably stays on one computer.
    llm_allow_lan: bool = False
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

    # --- BOQ check ---
    #: Most cells a BOQ workbook may have. The check reads every workbook twice with every cell
    #: built in memory (about 420 bytes each), and a few megabytes of archive can inflate to
    #: hundreds of millions of cells; a workbook over this is refused, a bid as unreadable. The
    #: largest real BOQ workbook measured has about 10 000 cells.
    boq_max_cells: int = Field(default=300_000, ge=1_000, le=20_000_000)

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

    # --- acceptance act: prose spelling (AOA.SPELLING) ---
    #: The hunspell ka_GE dictionary (``ka_GE.aff`` + ``ka_GE.dic``), fetched once with
    #: ``scripts/fetch_spelling_dictionary.py`` and read from disk: nothing is downloaded at
    #: runtime. Missing files skip the prose check with a note; they never fail a check.
    spelling_dictionary_path: Path = Path("./models/hunspell")
    #: Check the act's prose words against the dictionary, beyond the months, the numbers in
    #: words and the stray letters the other spelling rules read. Measured in docs/ACCURACY.md.
    prose_spelling_enabled: bool = True

    # --- Docling / OCR ---
    docling_artifacts_path: Path | None = None
    #: In production, refuse to start unless the model files match ``MANIFEST.sha256`` beside them
    #: (``procurement-agent models-manifest write`` after the download). Not a defence against code
    #: in a model - the formats cannot carry any - but the check that the weights are the ones that
    #: were evaluated.
    models_manifest_required: bool = True
    docling_num_threads: int = Field(default=12, ge=1, le=256)
    #: Docling had been sharing the LLM's timeout, so tuning one silently moved the other.
    docling_timeout_s: int = Field(default=900, ge=10)
    ocr_langs: Annotated[tuple[str, ...], NoDecode] = ("kat", "eng")

    # --- the parser sandbox (docs/SECURITY.md section 2) ---
    #: Where Docling runs. ``socket``: in the parser container, over ``parser_socket``
    #: (``procurement_agent/parser_service``); ``inline``: in this process, which is what a
    #: developer's machine and the tests do. Unset is ``inline``, and production refuses it unset:
    #: running the parser in the web process is a choice somebody makes, not a default.
    parser_mode: Literal["inline", "socket"] | None = None
    #: The parser's Unix socket: on a volume only the app and the parser containers share.
    parser_socket: Path = Path("/run/parser/parser.sock")
    #: Most a converted document (``docling.json``) may weigh on the wire, checked by both sides
    #: before they allocate for it. The largest real parse measured is a few megabytes.
    parser_max_result_mb: int = Field(default=64, ge=1, le=1024)
    #: How long a parse job waits for the parser to accept a connection. The parser is killed on a
    #: wedge and restarted by Docker, and loads its models before it listens (about a minute).
    parser_connect_wait_s: float = Field(default=90.0, ge=0.0, le=600.0)
    #: How long past ``docling_timeout_s`` a conversion may run before the kernel kills the parser
    #: (Docling's own timeout is cooperative: it stops between steps, and saves what it has).
    parser_kill_grace_s: int = Field(default=30, ge=1, le=600)

    # --- business registry snapshot (optional; absent means registry rules are skipped) ---
    registry_db: Path | None = None

    # --- exchange rates for the foreign-currency invoice (client rule 9) ---
    #: Where the NBG rates come from (``fx/provider.py``): "table" reads the rates an operator
    #: imported (``procurement-agent load-fx-rates``, from a file ``scripts/fetch_nbg_rates.py``
    #: wrote on a connected machine); "none" turns the lookup off. A day with no rate leaves the
    #: invoice as a formula with the days to look up, unless the user types the rate in.
    fx_rate_source: Literal["none", "table"] = "table"
    #: The imported rates table; default ``<data_dir>/fx_rates.db``.
    fx_rates_db: Path | None = None

    # --- network ---
    #: Refuse, inside the process, every outbound connection that is not loopback, a Unix socket
    #: or the model server (``security/egress.py``): the second line behind the container's
    #: network, and the only one on a developer's machine. Prod refuses to start with it off.
    egress_guard: bool = True
    #: The host names this server is reached by (``tasks.company.example``). A request that names
    #: any other host - a tunnel, a DNS-rebinding page - is refused. Loopback is always allowed
    #: (the health checks); prod refuses to start without a list.
    allowed_hosts: Annotated[tuple[str, ...], NoDecode] = ()

    # --- auth ---
    dev_api_key: str | None = None

    # NoDecode on the fields above keeps pydantic-settings from json-decoding them first, so
    # these validators receive the raw "kat,eng" string that .env.example documents.
    @field_validator("ocr_langs", "allowed_kinds", "allowed_hosts", mode="before")
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

    @field_validator("allowed_hosts")
    @classmethod
    def _host_names(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        """Plain names (``tasks.company.example``, ``*.company.example``) or bracketed IPv6
        literals, lower case. No ports (the header's port is not compared), schemes or paths."""
        out: list[str] = []
        for raw in v:
            host = raw.strip().lower().rstrip(".")
            ok = bool(_HOST_ENTRY.fullmatch(host))
            if not ok and host.startswith("[") and host.endswith("]"):
                try:
                    ipaddress.IPv6Address(host[1:-1])
                except ValueError:
                    ok = False
                else:
                    ok = True
            if not ok:
                msg = (
                    f"PA_ALLOWED_HOSTS entry {raw!r} is not a host name (no ports, schemes, paths)"
                )
                raise ValueError(msg)
            out.append(host)
        return tuple(out)

    @field_validator("parser_mode", mode="before")
    @classmethod
    def _blank_mode_is_unset(cls, v: object) -> object:
        """``PA_PARSER_MODE=`` in a .env file is the mode left unset, not an invalid one."""
        if isinstance(v, str):
            return v.strip().lower() or None
        return v

    @field_validator("parser_socket")
    @classmethod
    def _socket_path(cls, v: Path) -> Path:
        """A Unix socket address holds 107 bytes; a longer path fails at bind time, deep in a
        container start. Absolute, because the two containers must name the same file."""
        if not v.is_absolute():
            msg = f"PA_PARSER_SOCKET must be an absolute path, got {str(v)!r}"
            raise ValueError(msg)
        if len(os.fsencode(v)) > _UNIX_PATH_MAX:
            msg = f"PA_PARSER_SOCKET is too long for a Unix socket ({_UNIX_PATH_MAX} bytes at most)"
            raise ValueError(msg)
        return v

    @field_validator("ollama_base_url")
    @classmethod
    def _internal_only(cls, v: str) -> str:
        """The LLM endpoint must be a private/internal address; refuse anything else loudly.

        This used to be a string-prefix test, which accepted ``10.evil.example``,
        ``192.168.attacker.net``, ``127.0.0.1.nip.io`` (a public wildcard-DNS name that
        resolves wherever its label says) and every public ``172.x`` address. Document text is
        sent to this URL, so a typo here would ship client data off the box. Now an IP literal
        must really be private, and a name must be one of ours or carry an internal suffix.
        Whether a private host *other than this machine* is acceptable is ``llm_allow_lan``,
        checked once every field is known.
        """
        return check_llm_url(v, allow_lan=True)

    @field_validator("llm_model", "llm_fast_model")
    @classmethod
    def _local_model(cls, v: str) -> str:
        return check_model_name(v)

    @model_validator(mode="after")
    def _llm_host_policy(self) -> Settings:
        check_llm_url(self.ollama_base_url, allow_lan=self.llm_allow_lan)
        return self

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
    def fx_rates_path(self) -> Path:
        """The imported NBG rates (``fx/rates_table.py``)."""
        return self.fx_rates_db or self.data_dir / "fx_rates.db"

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def max_request_bytes(self) -> int:
        """Largest body of a request that carries a file, counted as it arrives.

        The no-JavaScript act and comparison forms post two files in one request, so the cap
        is two files plus a megabyte for multipart framing and the small text fields. It applies
        only to the routes that take a file (``security.preauth.upload_routes`` finds them);
        every other route is held to ``SMALL_BODY_BYTES``.
        """
        return MAX_FILES_PER_REQUEST * self.max_file_bytes + 1024 * 1024

    @property
    def use_parser_socket(self) -> bool:
        """Docling runs in the parser container (unset, like ``inline``, means in this process)."""
        return self.parser_mode == "socket"

    @property
    def parser_max_result_bytes(self) -> int:
        return self.parser_max_result_mb * 1024 * 1024

    @property
    def parser_max_request_bytes(self) -> int:
        """Largest request the parser reads: one file, and a megabyte for the frame around it."""
        return self.max_file_bytes + 1024 * 1024

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.upload_dir, self.results_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            with contextlib.suppress(OSError):
                d.chmod(0o700)  # ``mkdir`` leaves a directory that already existed as it was

    def validate_for_prod(self) -> None:
        """Fail fast on insecure production configuration."""
        if self.env != "prod":
            return
        problems: list[str] = []
        if self.dev_api_key:
            problems.append("PA_DEV_API_KEY must be empty in prod")
        problems += self._model_problems()
        if not self.egress_guard:
            problems.append("PA_EGRESS_GUARD must stay on in prod")
        if not self.allowed_hosts:
            problems.append("PA_ALLOWED_HOSTS must list the host names users reach this server by")
        if os.environ.get("FORWARDED_ALLOW_IPS", "").strip() == "*":
            problems.append(
                "FORWARDED_ALLOW_IPS must not be '*': any caller could then choose the client "
                "address the audit log and the rate limiter record"
            )
        if self.parser_mode is None:
            problems.append(
                "PA_PARSER_MODE is not set: set PA_PARSER_MODE=socket (compose) or inline "
                "(degraded: parsing runs in the web process)"
            )
        if problems:
            raise RuntimeError("Refusing to start: " + "; ".join(problems))

    def validate_for_parser(self) -> None:
        """The parser container's start-up check: what ``validate_for_prod`` asks that a process
        with no web server, no host list and no database can be held to - the models are the ones
        that were evaluated, and the egress guard is on."""
        if self.env != "prod":
            return
        problems = self._model_problems()
        if not self.egress_guard:
            problems.append("PA_EGRESS_GUARD must stay on in prod")
        if problems:
            raise RuntimeError("Refusing to start the parser: " + "; ".join(problems))

    def _model_problems(self) -> list[str]:
        problems: list[str] = []
        if self.docling_artifacts_path is None:
            problems.append("PA_DOCLING_ARTIFACTS_PATH must point to pre-downloaded models in prod")
        elif self.models_manifest_required:
            from procurement_agent.security import models_manifest

            checked = models_manifest.verify(self.docling_artifacts_path)
            if not checked.ok:
                shown = "; ".join(checked.problems[:5])
                more = len(checked.problems) - 5
                problems.append(
                    f"the model files do not match their manifest ({shown}"
                    + (f"; and {more} more" if more > 0 else "")
                    + "); restore them, or rewrite the manifest after a deliberate change "
                    "(procurement-agent models-manifest write)"
                )
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
