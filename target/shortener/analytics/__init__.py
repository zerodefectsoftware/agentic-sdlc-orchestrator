"""analytics — recording clicks off the redirect path, and answering questions about them.

Recording is asynchronous (A6). `record_click` hands the event to a bounded
in-process queue and returns; a background drain batches it into the datastore
within `FRESHNESS_SECONDS`. That is what buys AC2.3's p95 and AC5.2's promise
that a failing analytics store cannot fail a redirect — and the price, stated
rather than hidden, is that counts are eventually consistent and a crash loses
whatever had not drained.

A click is one successful redirect (A10 — crawlers included, visible through the
user-agent breakdown). Referrer and user-agent are the only visitor data kept:
no IP, no geography, no unique-visitor deduplication (A8).

Queries are bounded by construction (A11): breakdowns return the top
`BREAKDOWN_LIMIT` values plus an `OTHER_BUCKET` row, and a time series must name
its window and may not exceed `MAX_INTERVALS` buckets. An unbounded breakdown
over a multi-year window is a load source the service points at itself.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, Connection, and_, func, select

from shortener.config import get_settings
from shortener.errors import AnalyticsUnavailableError, InvalidQueryError
from shortener.storage import ClickRow, clicks_table, connection, insert_clicks

logger = logging.getLogger(__name__)

# A6: a click must be visible to the analytics API within this many seconds.
FRESHNESS_SECONDS: float = 60.0

# A11: top-N breakdown entries, with everything else summed into one row.
BREAKDOWN_LIMIT: int = 100
OTHER_BUCKET: str = "other"

# A11: the largest number of buckets one time-series query may return.
MAX_INTERVALS: int = 366

# The bucket widths a caller may ask for.
VALID_INTERVALS: tuple[str, ...] = ("hour", "day")

# What each of those names is worth, and the only place the two agree.
_INTERVAL_WIDTHS: dict[str, timedelta] = {
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
}

# A click with no Referer is not an unknown referrer, it is a direct visit, and
# the breakdown is easier to read if it says so.
_DIRECT = "direct"

# The label for a user-agent that was absent or that no rule below recognised.
_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BreakdownEntry:
    """One row of a grouped count — a referrer, a device class, a browser class."""

    value: str
    count: int


@dataclass(frozen=True, slots=True)
class IntervalCount:
    """Clicks in one time bucket, labelled by the bucket's start."""

    start: datetime
    count: int


@dataclass(frozen=True, slots=True)
class ClickSummary:
    """Everything R4 asks about one link: how many, when, and from what."""

    code: str
    total_clicks: int
    start: datetime
    end: datetime
    interval: str
    series: list[IntervalCount] = field(default_factory=list)
    referrers: list[BreakdownEntry] = field(default_factory=list)
    devices: list[BreakdownEntry] = field(default_factory=list)
    browsers: list[BreakdownEntry] = field(default_factory=list)


# The queue between the redirect path and the datastore, the thread draining it,
# and the lock that keeps starting and stopping that thread a single decision.
_queue: queue.Queue | None = None
_worker: threading.Thread | None = None
_recorder_lock = threading.Lock()

# Put on the queue by `stop_recorder` to end the drain. It travels behind every
# click already queued, so the worker persists all of them before it returns.
_SENTINEL = object()


def _get_queue() -> queue.Queue:
    """The click queue, created on first use.

    Built here rather than at import so `config` is only read when something
    actually records, and so a click that arrives before the drain has started
    is queued rather than lost.
    """
    global _queue
    if _queue is not None:  # the redirect path's case — no lock to contend for
        return _queue
    with _recorder_lock:
        if _queue is None:
            _queue = queue.Queue(maxsize=get_settings().analytics_queue_maxsize)
        return _queue


def _drain(pending: queue.Queue) -> None:
    """Move queued clicks into the datastore in batches until told to stop."""
    settings = get_settings()
    while True:
        batch: list[ClickRow] = []
        finished = False

        # One blocking wait, so an idle service is not a spin loop, then take
        # whatever else has arrived without waiting again.
        try:
            item = pending.get(timeout=settings.analytics_flush_interval_seconds)
        except queue.Empty:
            item = None
        while item is not None:
            if item is _SENTINEL:
                finished = True
                break
            batch.append(item)
            if len(batch) >= settings.analytics_batch_size:
                break
            try:
                item = pending.get_nowait()
            except queue.Empty:
                break

        if batch:
            try:
                insert_clicks(batch)
            except Exception as exc:  # noqa: BLE001
                # A6 already says a crash loses what had not drained; a datastore
                # that refuses the batch loses it the same way. Logged rather
                # than retried, because a retry loop here is unbounded memory on
                # the path that exists to keep memory bounded.
                logger.warning("could not persist %d clicks: %s", len(batch), exc)

        if finished:
            return


def start_recorder() -> None:
    """Start the background drain that moves queued clicks into the datastore."""
    global _worker
    pending = _get_queue()
    with _recorder_lock:
        if _worker is not None:
            return
        _worker = threading.Thread(
            target=_drain,
            args=(pending,),
            name="analytics-recorder",
            daemon=True,
        )
        _worker.start()


def stop_recorder() -> None:
    """Drain what is queued and stop the background worker."""
    global _worker
    with _recorder_lock:
        worker, _worker = _worker, None
        pending = _queue
    if worker is None:
        return
    pending.put(_SENTINEL)
    worker.join(timeout=FRESHNESS_SECONDS)


def record_click(
    code: str,
    occurred_at: datetime,
    referrer: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Queue one successful redirect for recording, without touching the datastore.

    Classification of the user-agent into device and browser classes happens
    here rather than at query time, so a breakdown is a `GROUP BY` and not a
    scan (AC4.3).

    Raises `AnalyticsUnavailableError` when the event cannot even be queued.
    Callers on the redirect path must log that and still redirect (AC5.2).
    """
    row = ClickRow(
        code=code,
        occurred_at=_utc(occurred_at),
        referrer=referrer,
        user_agent=user_agent,
        device_class=_device_class(user_agent),
        browser_class=_browser_class(user_agent),
    )
    try:
        _get_queue().put_nowait(row)
    except queue.Full as exc:
        # The queue is bounded on purpose, so a full one is back-pressure, not a
        # bug. Blocking here would spend the redirect's latency budget on a side
        # path AC5.2 says the redirect must not depend on.
        raise AnalyticsUnavailableError(
            "the click queue is full; the event was dropped",
            details={"code": code},
        ) from exc


def total_clicks(code: str) -> int:
    """Total recorded clicks for a code — the count the metadata endpoint reports."""
    with connection() as conn:
        return conn.execute(
            select(func.count())
            .select_from(clicks_table)
            .where(clicks_table.c.code == code)
        ).scalar_one()


def summarize_clicks(
    code: str,
    start: datetime,
    end: datetime,
    interval: str = "day",
) -> ClickSummary:
    """Answer R4 for one link over the half-open window `[start, end)`.

    The total, the series and the breakdowns all cover that window and no more,
    so the series sums to the total (AC4.2).
    """
    if interval not in VALID_INTERVALS:
        raise InvalidQueryError(
            f"{interval!r} is not a bucket width this API serves",
            details={"interval": interval, "supported": list(VALID_INTERVALS)},
        )
    window_start, window_end = _utc(start), _utc(end)
    if window_end <= window_start:
        raise InvalidQueryError(
            "the end of an analytics window must follow its start",
            details={
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
            },
        )

    width = _INTERVAL_WIDTHS[interval]
    span = window_end - window_start
    # Rounded up, in whole microseconds rather than through a float: this is the
    # number the `counts` list is sized by, and a ratio that rounds the wrong way
    # is an index error on a query path.
    buckets = span // width + (1 if span % width else 0)
    if buckets > MAX_INTERVALS:
        raise InvalidQueryError(
            "that window asks for more intervals than this API will serve",
            details={"intervals": buckets, "max_intervals": MAX_INTERVALS},
        )

    # The buckets start at `start` rather than at a rounded boundary, so every
    # interval the caller gets back lies inside the window it asked for.
    counts = [0] * buckets
    in_window = and_(
        clicks_table.c.code == code,
        clicks_table.c.occurred_at >= window_start,
        clicks_table.c.occurred_at < window_end,
    )

    with connection() as conn:
        for (occurred_at,) in conn.execute(
            select(clicks_table.c.occurred_at).where(in_window)
        ):
            counts[(_utc(occurred_at) - window_start) // width] += 1
        referrers = _breakdown(conn, clicks_table.c.referrer, in_window, _DIRECT)
        devices = _breakdown(conn, clicks_table.c.device_class, in_window, _UNKNOWN)
        browsers = _breakdown(conn, clicks_table.c.browser_class, in_window, _UNKNOWN)

    return ClickSummary(
        code=code,
        # Summed from the series rather than counted separately, so the two
        # numbers in one response cannot disagree.
        total_clicks=sum(counts),
        start=window_start,
        end=window_end,
        interval=interval,
        series=[
            IntervalCount(start=window_start + index * width, count=count)
            for index, count in enumerate(counts)
        ],
        referrers=referrers,
        devices=devices,
        browsers=browsers,
    )


def _breakdown(
    conn: Connection,
    column: ColumnElement,
    in_window: ColumnElement[bool],
    null_label: str,
) -> list[BreakdownEntry]:
    """One grouped count, bounded to the top `BREAKDOWN_LIMIT` values (A11).

    The grouping is a `GROUP BY` over a column classified at write time, and the
    tail below the limit is summed into `OTHER_BUCKET` rather than dropped, so a
    breakdown still accounts for every click in the window.
    """
    entries = [
        BreakdownEntry(value=null_label if value is None else str(value), count=count)
        for value, count in conn.execute(
            select(column, func.count())
            .where(in_window)
            .group_by(column)
            .order_by(func.count().desc())
        )
    ]
    top = entries[:BREAKDOWN_LIMIT]
    tail = sum(entry.count for entry in entries[BREAKDOWN_LIMIT:])
    if tail:
        top.append(BreakdownEntry(value=OTHER_BUCKET, count=tail))
    return top


def _utc(moment: datetime) -> datetime:
    """Every timestamp this module reasons about, on one timeline.

    Timestamps arrive from three places — the redirect path, a query string, and
    a datastore that may or may not have kept the offset — and comparing an
    aware one with a naive one raises. A naive timestamp is UTC here, because
    UTC is what the recorder writes.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _device_class(user_agent: str | None) -> str:
    """The device a user-agent came from, inferred at write time (AC4.3)."""
    if not user_agent:
        return _UNKNOWN
    agent = user_agent.lower()
    handheld = any(
        marker in agent for marker in ("mobi", "iphone", "ipod", "android", "ipad")
    )
    if not handheld:
        return "desktop"
    # Android says "Mobile" on a phone and omits it on a tablet; iPadOS says iPad.
    if "ipad" in agent or "tablet" in agent or "mobi" not in agent:
        return "tablet"
    return "mobile"


def _browser_class(user_agent: str | None) -> str:
    """The browser family a user-agent claims, inferred at write time (AC4.3).

    Order matters and is the whole reason this is a list rather than a set:
    Chrome's user-agent also says Safari, and Edge's says both.
    """
    if not user_agent:
        return _UNKNOWN
    agent = user_agent.lower()
    for marker, family in (
        ("edg/", "edge"),
        ("opr/", "opera"),
        ("crios/", "chrome"),
        ("chrome/", "chrome"),
        ("fxios/", "firefox"),
        ("firefox/", "firefox"),
        ("safari/", "safari"),
    ):
        if marker in agent:
            return family
    return "other"
