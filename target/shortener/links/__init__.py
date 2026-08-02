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
from datetime import datetime


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
    raise NotImplementedError


def resolve_link(code: str) -> Link:
    """Return the live link for a redirect, or say why it cannot be followed."""
    raise NotImplementedError


def get_link(code: str) -> Link:
    """Return a link's metadata, deleted or expired ones included.

    The state is on the returned `Link` — `deleted_at`, `expires_at` — so the
    metadata endpoint can report a retired link (AC3.1) while the redirect path
    still refuses it (AC3.2).
    """
    raise NotImplementedError


def delete_link(code: str) -> None:
    """Retire a code permanently.

    Idempotent: deleting an already-retired code is a no-op, so a retried
    DELETE still answers 204. A code that was never issued is `NotFoundError`.
    """
    raise NotImplementedError
