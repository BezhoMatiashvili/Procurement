"""API-key authentication.

Keys look like ``pa_<43 url-safe chars>`` (256 bits of entropy). Only the SHA-256 of the key is
stored; lookup is by a short prefix and the final comparison is constant-time. Keys are created
with the CLI (``python -m procurement_agent.cli create-key <user>``) and printed exactly once.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.orm import Session

from procurement_agent.config import Settings, get_settings
from procurement_agent.db import get_session
from procurement_agent.models import ApiKey

KEY_PREFIX = "pa_"
_PREFIX_LEN = 11  # "pa_" + 8 chars: enough to index, too short to matter if leaked

_header = APIKeyHeader(name="X-API-Key", auto_error=False)
COOKIE_NAME = "pa_session"


@dataclass(frozen=True, slots=True)
class Principal:
    user: str
    key_id: str | None  # None for the dev key


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def create_api_key(session: Session, user: str) -> tuple[str, ApiKey]:
    """Create and persist a new key for ``user``. Returns (plaintext key, row)."""
    key = generate_key()
    row = ApiKey(user=user, key_hash=hash_key(key), prefix=key[:_PREFIX_LEN])
    session.add(row)
    session.flush()
    return key, row


def revoke_api_key(session: Session, key_id: str) -> bool:
    row = session.get(ApiKey, key_id)
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(UTC)
    return True


def _lookup(session: Session, key: str) -> ApiKey | None:
    if not key.startswith(KEY_PREFIX) or len(key) < 20:
        return None
    digest = hash_key(key)
    rows = session.scalars(
        select(ApiKey).where(ApiKey.prefix == key[:_PREFIX_LEN], ApiKey.revoked_at.is_(None))
    ).all()
    for row in rows:
        if hmac.compare_digest(row.key_hash, digest):
            return row
    return None


def authenticate(
    request: Request,
    key: str | None = Depends(_header),
    cookie_key: str | None = Cookie(default=None, alias=COOKIE_NAME),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """Header auth for API clients; HttpOnly cookie auth for the built-in UI.

    Cookie-authenticated unsafe requests must come from our own origin (CSRF guard).
    """
    if not key and cookie_key:
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-site request refused")
        key = cookie_key
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing API key")

    # Development convenience only; validate_for_prod() guarantees this is unset in prod.
    if (
        settings.env == "dev"
        and settings.dev_api_key
        and hmac.compare_digest(key, settings.dev_api_key)
    ):
        return Principal(user="dev", key_id=None)

    row = _lookup(session, key)
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
    row.last_used_at = datetime.now(UTC)
    principal = Principal(user=row.user, key_id=row.id)
    request.state.principal = principal
    return principal


def _same_origin(request: Request) -> bool:
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site in ("same-origin", "none")
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return False
    from urllib.parse import urlparse

    return urlparse(origin).netloc == request.headers.get("host", "")
