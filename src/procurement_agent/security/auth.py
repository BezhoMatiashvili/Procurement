"""API-key authentication, and the server-side sessions the browser UI signs in with.

Keys look like ``pa_<43 url-safe chars>`` (256 bits of entropy). Only the SHA-256 of the key is
stored; lookup is by a short prefix and the final comparison is constant-time. Keys are created
with the CLI (``python -m procurement_agent.cli create-key <user>``) and printed exactly once.

The UI never holds the key after sign-in. ``POST /ui/login`` checks it once and mints a random
session token; the HttpOnly cookie carries only that token and the database only its SHA-256.
A session expires on the server after ``ui_session_hours``, is revoked by logout, and stops
working the moment its API key is revoked. (The cookie used to be the API key itself: a copied
cookie was a permanent credential and logout merely asked the browser to forget it.)
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from procurement_agent.config import Settings, get_settings
from procurement_agent.db import get_session, session_scope
from procurement_agent.models import ApiKey, UiSession
from procurement_agent.security.audit import audit_now, log

KEY_PREFIX = "pa_"
#: Session tokens get their own prefix so logs and the redactor can recognise them.
SESSION_PREFIX = "ps_"
_PREFIX_LEN = 11  # "pa_" + 8 chars: enough to index, too short to matter if leaked
#: ``last_used_at`` / ``last_seen_at`` are bookkeeping. Writing them on every request took
#: SQLite's single write lock once per request, which the worker then queued behind.
_TOUCH_EVERY = timedelta(minutes=1)
_MAX_TOKEN_LEN = 200

_header = APIKeyHeader(name="X-API-Key", auto_error=False)
COOKIE_NAME = "pa_session"


@dataclass(frozen=True, slots=True)
class Principal:
    user: str
    key_id: str | None  # None for the dev key
    #: The UI session this request rode in on; None for header (API client) authentication.
    session_id: str | None = None


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def _equal(a: str, b: str) -> bool:
    """Constant-time comparison that accepts any text.

    ``hmac.compare_digest`` on two ``str`` raises TypeError for non-ASCII input, so a Georgian
    character typed into the key field used to be a 500 instead of a refusal.
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _aware(dt: datetime) -> datetime:
    """SQLite hands ``DateTime(timezone=True)`` back naive; every value we store is UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def is_dev_key(settings: Settings, key: str) -> bool:
    """Development convenience only; ``validate_for_prod()`` guarantees it is unset in prod."""
    return (
        settings.env == "dev"
        and bool(settings.dev_api_key)
        and _equal(key, settings.dev_api_key or "")
    )


def create_api_key(session: Session, user: str) -> tuple[str, ApiKey]:
    """Create and persist a new key for ``user``. Returns (plaintext key, row)."""
    key = generate_key()
    row = ApiKey(user=user, key_hash=hash_key(key), prefix=key[:_PREFIX_LEN])
    session.add(row)
    session.flush()
    return key, row


def revoke_api_key(session: Session, key_id: str) -> bool:
    """Revoke a key and every browser session signed in with it."""
    row = session.get(ApiKey, key_id)
    if row is None or row.revoked_at is not None:
        return False
    now = datetime.now(UTC)
    row.revoked_at = now
    session.execute(
        update(UiSession)
        .where(UiSession.api_key_id == key_id, UiSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    return True


def lookup_api_key(session: Session, key: str) -> ApiKey | None:
    """The live key row for ``key``, or None. Lookup by prefix, comparison constant-time."""
    if not key.startswith(KEY_PREFIX) or not 20 <= len(key) <= _MAX_TOKEN_LEN:
        return None
    digest = hash_key(key)
    rows = session.scalars(
        select(ApiKey).where(ApiKey.prefix == key[:_PREFIX_LEN], ApiKey.revoked_at.is_(None))
    ).all()
    for row in rows:
        if hmac.compare_digest(row.key_hash, digest):
            return row
    return None


# --- UI sessions ---------------------------------------------------------------------------


def create_ui_session(
    session: Session, settings: Settings, *, user: str, api_key_id: str | None
) -> str:
    """Persist a new sign-in and return its token - the only time the token exists in clear."""
    token = SESSION_PREFIX + secrets.token_urlsafe(32)
    session.add(
        UiSession(
            token_hash=hash_key(token),
            api_key_id=api_key_id,
            user=user,
            expires_at=datetime.now(UTC) + timedelta(hours=settings.ui_session_hours),
        )
    )
    session.flush()
    return token


def lookup_ui_session(session: Session, settings: Settings, token: str) -> UiSession | None:
    """The session a cookie names, if it is still good: not expired, not revoked, and its API
    key not revoked. A dev-key session is good only while the dev key is."""
    if not token.startswith(SESSION_PREFIX) or len(token) > _MAX_TOKEN_LEN:
        return None
    digest = hash_key(token)
    row = session.scalar(select(UiSession).where(UiSession.token_hash == digest))
    if row is None or not hmac.compare_digest(row.token_hash, digest):
        return None
    if row.revoked_at is not None or _aware(row.expires_at) <= datetime.now(UTC):
        return None
    if row.api_key_id is None:
        if not (settings.env == "dev" and settings.dev_api_key and row.user == "dev"):
            return None
    else:
        key = session.get(ApiKey, row.api_key_id)
        if key is None or key.revoked_at is not None:
            return None
    return row


def revoke_ui_session(session: Session, session_id: str) -> None:
    session.execute(
        update(UiSession)
        .where(UiSession.id == session_id, UiSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


def _touch(model: type[ApiKey] | type[UiSession], row_id: str, last: datetime | None) -> None:
    """Record use at most once a minute, in a transaction of its own.

    Its own transaction so the request's session stays read-only: a flushed write there holds
    SQLite's write lock until the request ends, and a second session in the same request (an
    audit of a refusal) would then wait out the busy timeout on its own caller.
    """
    now = datetime.now(UTC)
    if last is not None and now - _aware(last) < _TOUCH_EVERY:
        return
    column = "last_used_at" if model is ApiKey else "last_seen_at"
    try:
        with session_scope() as s:
            s.execute(update(model).where(model.id == row_id).values({column: now}))
    except SQLAlchemyError as e:  # bookkeeping must never fail an authenticated request
        log.warning("auth_touch_failed", error=type(e).__name__)


# --- the dependency --------------------------------------------------------------------------


def _refuse(request: Request, code: int, detail: str, action: str) -> HTTPException:
    """Audit a refused credential (never the credential itself) and build the exception."""
    audit_now(
        actor="anonymous",
        action=action,
        outcome="denied",
        ip=request.client.host if request.client else None,
        detail=f"{request.method} {request.url.path}: {detail}",
    )
    return HTTPException(code, detail)


def authenticate(
    request: Request,
    key: str | None = Depends(_header),
    cookie_token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Principal:
    """Header auth for API clients; server-side session cookie for the built-in UI.

    Cookie-authenticated unsafe requests must come from our own origin (CSRF guard). A request
    with no credential at all is not audited - that is every browser's first visit - but a
    wrong key, a dead session and a cross-site post are.
    """
    if key:
        if is_dev_key(settings, key):
            return Principal(user="dev", key_id=None)
        row = lookup_api_key(session, key)
        if row is None:
            raise _refuse(request, status.HTTP_401_UNAUTHORIZED, "invalid API key", "auth.denied")
        _touch(ApiKey, row.id, row.last_used_at)
        return Principal(user=row.user, key_id=row.id)

    if cookie_token:
        if request.method not in ("GET", "HEAD", "OPTIONS") and not is_same_origin(request):
            raise _refuse(
                request, status.HTTP_403_FORBIDDEN, "cross-site request refused", "auth.csrf"
            )
        ui = lookup_ui_session(session, settings, cookie_token)
        if ui is None:
            raise _refuse(
                request, status.HTTP_401_UNAUTHORIZED, "session expired or invalid", "auth.denied"
            )
        _touch(UiSession, ui.id, ui.last_seen_at)
        return Principal(user=ui.user, key_id=ui.api_key_id, session_id=ui.id)

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing API key")


def is_same_origin(request: Request) -> bool:
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site in ("same-origin", "none")
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return False
    return urlparse(origin).netloc == request.headers.get("host", "")
