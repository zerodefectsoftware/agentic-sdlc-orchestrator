"""storage — the one datastore, its schema, and the rows that cross its boundary.

A single indexed relational store, reached through SQLAlchemy Core (A3): no
sharding, no replication, no cache tier. SQLAlchemy keeps a Postgres swap open
while SQLite makes the prototype runnable with no setup, and Core rather than
the ORM because every row that leaves this module leaves as a frozen dataclass
— a detached ORM instance that lazy-loads after its connection closed is the
one failure mode a parallel implementer cannot debug from inside another module.

What lives here is persistence only. Aggregation over `clicks` belongs to
`analytics`, which reads `clicks_table` through `connection()`; URL rules and
code minting belong to `links`. This module never decides an HTTP status: it
raises `StorageError` or `CodeCollisionError` and lets the caller do that.

`ping()` is the reachability probe the health endpoint reads (AC5.3), and it is
the only "is the system up" question this module answers.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    Column,
    Connection,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

metadata = MetaData()

# The code is the primary key, so uniqueness is the datastore's invariant rather
# than a check that can race two concurrent creations (AC1.4). `deleted_at`
# makes deletion soft: the row survives, the code is retired forever, and its
# clicks stay readable (A7).
links_table = Table(
    "links",
    metadata,
    Column("code", String(16), primary_key=True),
    Column("long_url", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
)

# One row per successful redirect (A8/A10: no IP, no geography, no visitor
# deduplication, no bot filtering). Indexed on the query every analytics call
# makes — one code over one time window.
clicks_table = Table(
    "clicks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("code", String(16), nullable=False, index=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False, index=True),
    Column("referrer", Text, nullable=True),
    Column("user_agent", Text, nullable=True),
    Column("device_class", String(32), nullable=False),
    Column("browser_class", String(32), nullable=False),
)


@dataclass(frozen=True, slots=True)
class LinkRow:
    """One row of `links`, detached from the connection that read it."""

    code: str
    long_url: str
    created_at: datetime
    expires_at: datetime | None = None
    deleted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ClickRow:
    """One recorded redirect, as `analytics` hands it over for durable storage."""

    code: str
    occurred_at: datetime
    referrer: str | None = None
    user_agent: str | None = None
    device_class: str = "unknown"
    browser_class: str = "unknown"


def init_storage() -> None:
    """Open the engine and create the schema if it is absent.

    Idempotent, and called once from the application lifespan. Creating the
    schema on start is what makes AC5.1 hold across a restart without a
    migration step the README would have to explain.
    """
    raise NotImplementedError


@contextmanager
def connection() -> Iterator[Connection]:
    """Yield a connection with a transaction, committing on exit, rolling back on error."""
    raise NotImplementedError


def ping() -> bool:
    """Whether the datastore is reachable right now — the health endpoint's evidence."""
    raise NotImplementedError


def insert_link(row: LinkRow) -> None:
    """Persist a new link, or reject the code as already taken."""
    raise NotImplementedError


def fetch_link(code: str) -> LinkRow | None:
    """Return the row for `code`, including deleted and expired ones, or None."""
    raise NotImplementedError


def mark_link_deleted(code: str, deleted_at: datetime) -> bool:
    """Retire a live code, returning whether this call was the one that retired it."""
    raise NotImplementedError


def insert_clicks(rows: Sequence[ClickRow]) -> int:
    """Append a batch of recorded clicks, returning how many were written."""
    raise NotImplementedError
