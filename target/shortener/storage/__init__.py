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

import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    Connection,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from shortener.config import get_settings
from shortener.errors import CodeCollisionError, StorageError

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


_engine: Engine | None = None
_engine_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# timestamps
# --------------------------------------------------------------------------- #
# SQLite keeps no timezone: SQLAlchemy formats a datetime's components and drops
# its tzinfo, so an aware non-UTC value would be stored as its local wall clock
# and read back as UTC. Every timestamp is therefore converted to UTC on the way
# in and marked UTC on the way out — which also makes an aware UTC datetime bound
# directly into a WHERE clause (as `analytics` does over `clicks_table`) compare
# against exactly what was written.


def _to_storage(moment: datetime | None) -> datetime | None:
    """The stored form of an instant: UTC, with the tzinfo dropped."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(UTC).replace(tzinfo=None)


def _from_storage(moment: datetime | None) -> datetime | None:
    """The stored form read back as an aware UTC instant."""
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #


def _create_engine() -> Engine:
    """Build the engine for the configured datastore."""
    url = get_settings().database_url
    if not url.startswith("sqlite"):
        return create_engine(url)

    # The click drain writes from a background thread while the redirect path
    # reads from the request threads, so connections cross threads and SQLite's
    # default journal would make readers and the writer take turns. WAL lets
    # them run at once — the redirect's latency budget (AC2.3) is not something
    # to spend on the side path the design worked to move off it.
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


def _get_engine() -> Engine:
    """The process-wide engine, opened on first use."""
    global _engine
    with _engine_lock:
        if _engine is None:
            try:
                _engine = _create_engine()
            except SQLAlchemyError as exc:
                raise StorageError("the datastore could not be opened") from exc
        return _engine


def init_storage() -> None:
    """Open the engine and create the schema if it is absent.

    Idempotent, and called once from the application lifespan. Creating the
    schema on start is what makes AC5.1 hold across a restart without a
    migration step the README would have to explain.
    """
    engine = _get_engine()
    try:
        metadata.create_all(engine)
    except SQLAlchemyError as exc:
        raise StorageError("the datastore schema could not be created") from exc


@contextmanager
def connection() -> Iterator[Connection]:
    """Yield a connection with a transaction, committing on exit, rolling back on error."""
    try:
        with _get_engine().begin() as conn:
            yield conn
    except SQLAlchemyError as exc:
        # Only the datastore's own failures are translated. An `AppError` a
        # caller raised inside the block — `CodeCollisionError` from
        # `insert_link` — passes through with the transaction rolled back.
        raise StorageError("the datastore rejected the operation") from exc


def ping() -> bool:
    """Whether the datastore is reachable right now — the health endpoint's evidence."""
    try:
        with _get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except (SQLAlchemyError, StorageError):
        return False
    return True


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #


def insert_link(row: LinkRow) -> None:
    """Persist a new link, or reject the code as already taken."""
    with connection() as conn:
        try:
            conn.execute(
                links_table.insert().values(
                    code=row.code,
                    long_url=row.long_url,
                    created_at=_to_storage(row.created_at),
                    expires_at=_to_storage(row.expires_at),
                    deleted_at=_to_storage(row.deleted_at),
                )
            )
        except IntegrityError as exc:
            # The primary key is the collision check (AC1.4): a code already
            # issued is refused here rather than by a read-then-write that two
            # concurrent creations could both win.
            raise CodeCollisionError(
                f"the code {row.code!r} is already in use",
                details={"code": row.code},
            ) from exc


def fetch_link(code: str) -> LinkRow | None:
    """Return the row for `code`, including deleted and expired ones, or None."""
    with connection() as conn:
        row = conn.execute(
            select(links_table).where(links_table.c.code == code)
        ).first()

    if row is None:
        return None
    return LinkRow(
        code=row.code,
        long_url=row.long_url,
        created_at=_from_storage(row.created_at),
        expires_at=_from_storage(row.expires_at),
        deleted_at=_from_storage(row.deleted_at),
    )


def mark_link_deleted(code: str, deleted_at: datetime) -> bool:
    """Retire a live code, returning whether this call was the one that retired it."""
    with connection() as conn:
        result = conn.execute(
            links_table.update()
            .where(links_table.c.code == code, links_table.c.deleted_at.is_(None))
            .values(deleted_at=_to_storage(deleted_at))
        )
        # False for a code already retired, so a retried DELETE is a no-op here
        # rather than an error the caller has to recognise (AC3.2).
        return result.rowcount == 1


# --------------------------------------------------------------------------- #
# clicks
# --------------------------------------------------------------------------- #


def insert_clicks(rows: Sequence[ClickRow]) -> int:
    """Append a batch of recorded clicks, returning how many were written."""
    if not rows:
        return 0

    with connection() as conn:
        conn.execute(
            clicks_table.insert(),
            [
                {
                    "code": row.code,
                    "occurred_at": _to_storage(row.occurred_at),
                    "referrer": row.referrer,
                    "user_agent": row.user_agent,
                    "device_class": row.device_class,
                    "browser_class": row.browser_class,
                }
                for row in rows
            ],
        )
    # The insert is one transaction, so reaching here means every row landed.
    return len(rows)
