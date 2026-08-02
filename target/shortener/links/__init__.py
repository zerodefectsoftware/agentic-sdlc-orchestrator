"""links — the lifecycle of a short link: create, resolve, inspect, retire.

This is where the register's rules about links live, and the only module that
writes to `links_table`:

* creation validates the URL before minting anything (AC1.2) — absolute,
  http/https, and not pointing at this service's own host (A12) — then mints a
  candidate code and retries on collision until the budget is spent (AC1.4);
* a long URL submitted twice gets a new code every time (A5), so two campaigns
  never share a click count;
* resolution is the redirect path's only lookup, and distinguishes "never
  issued" (404) from "issued and now gone" — deleted (A7) or expired (A4),
  both 410;
* deletion is soft and idempotent: the code is retired permanently, never
  reissued, and its clicks stay readable through `analytics`.

`Link` carries `short_url` because the caller of `create_link` needs it in the
201 body (AC1.1) and only this module knows the configured origin. It carries
no click count: that number belongs to `analytics`, and importing it here would
put the analytics store on the creation path AC5.2 works to keep it off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from shortener.codes import generate_code
from shortener.config import get_settings
from shortener.errors import (
    CodeCollisionError,
    InvalidURLError,
    LinkGoneError,
    NotFoundError,
)
from shortener.storage import LinkRow, fetch_link, insert_link, mark_link_deleted

# A12: the only schemes a short link may point at.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# How many codes creation will mint before giving up. The space is 62**7, so at
# the register's 10k links/day a single retry is already surplus; five attempts
# turn a collision into a certainty of success rather than a 500 (AC1.4).
_MINT_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class Link:
    """A short link as the rest of the service sees it.

    `long_url` is stored and returned byte-for-byte as submitted: AC2.1 compares
    the `Location` header against it exactly, so nothing here normalises a
    trailing slash, re-orders a query string or percent-decodes anything.
    """

    code: str
    long_url: str
    short_url: str
    created_at: datetime
    expires_at: datetime | None = None
    deleted_at: datetime | None = None


def create_link(long_url: str, expires_at: datetime | None = None) -> Link:
    """Validate, mint a code, persist, and return the new link."""
    _validate_url(long_url)

    created_at = datetime.now(UTC)
    expiry = None if expires_at is None else _as_utc(expires_at)

    for _ in range(_MINT_ATTEMPTS):
        # The primary key is what rejects a code already in use, so this is a
        # mint-and-try rather than a read-then-write two creations could race
        # through together.
        row = LinkRow(
            code=generate_code(),
            long_url=long_url,
            created_at=created_at,
            expires_at=expiry,
        )
        try:
            insert_link(row)
        except CodeCollisionError:
            continue
        return _to_link(row)

    raise CodeCollisionError(
        "could not mint an unused short code",
        details={"attempts": _MINT_ATTEMPTS},
    )


def resolve_link(code: str) -> Link:
    """Return the live link for a redirect, or say why it cannot be followed."""
    link = get_link(code)

    if link.deleted_at is not None:
        raise LinkGoneError(
            "this short link has been deleted",
            details={"code": code},
        )
    if link.expires_at is not None and link.expires_at <= datetime.now(UTC):
        raise LinkGoneError(
            "this short link has expired",
            details={"code": code, "expires_at": link.expires_at.isoformat()},
        )
    return link


def get_link(code: str) -> Link:
    """Return a link's metadata, deleted or expired ones included.

    The state is on the returned `Link` — `deleted_at`, `expires_at` — so the
    metadata endpoint can report a retired link (AC3.1) while the redirect path
    still refuses it (AC3.2).
    """
    row = fetch_link(code)
    if row is None:
        raise NotFoundError("no such short code", details={"code": code})
    return _to_link(row)


def delete_link(code: str) -> None:
    """Retire a code permanently.

    Idempotent: deleting an already-retired code is a no-op, so a retried
    DELETE still answers 204. A code that was never issued is `NotFoundError`.
    """
    if fetch_link(code) is None:
        raise NotFoundError("no such short code", details={"code": code})

    # Whether this call was the one that retired the code is not interesting to
    # the caller: never-issued is the only 404, and a retried DELETE is a 204.
    mark_link_deleted(code, datetime.now(UTC))


def _validate_url(long_url: str) -> None:
    """Refuse anything creation is not allowed to shorten (AC1.2, A12)."""
    if not long_url.strip():
        raise InvalidURLError("url is required")

    parsed = urlsplit(long_url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise InvalidURLError(
            "url must be an absolute http or https URL",
            details={"scheme": parsed.scheme or None},
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise InvalidURLError("url must name a host", details={"url": long_url})

    if host in _refused_hosts():
        raise InvalidURLError(
            "url must not point at this service",
            details={"host": host},
        )


def _refused_hosts() -> set[str]:
    """The service's own host, plus whatever the denylist hook was given (A12)."""
    settings = get_settings()
    hosts = {host.lower() for host in settings.blocked_hosts}
    own = urlsplit(settings.base_url).hostname
    if own:
        hosts.add(own.lower())
    return hosts


def _to_link(row: LinkRow) -> Link:
    """Turn a stored row into the link the rest of the service sees."""
    base_url = get_settings().base_url.rstrip("/")
    return Link(
        code=row.code,
        long_url=row.long_url,
        short_url=f"{base_url}/{row.code}",
        created_at=_as_utc(row.created_at),
        expires_at=None if row.expires_at is None else _as_utc(row.expires_at),
        deleted_at=None if row.deleted_at is None else _as_utc(row.deleted_at),
    )


def _as_utc(moment: datetime) -> datetime:
    """Read a timestamp as UTC, whether or not it arrived carrying an offset.

    Timestamps are UTC throughout, but SQLite has no timestamp type and hands
    them back without one. Expiry is compared against `now` on the redirect
    path, where a naive-versus-aware mismatch would be a 500 rather than a 301.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
