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

from dataclasses import dataclass, field
from datetime import datetime

# A6: a click must be visible to the analytics API within this many seconds.
FRESHNESS_SECONDS: float = 60.0

# A11: top-N breakdown entries, with everything else summed into one row.
BREAKDOWN_LIMIT: int = 100
OTHER_BUCKET: str = "other"

# A11: the largest number of buckets one time-series query may return.
MAX_INTERVALS: int = 366

# The bucket widths a caller may ask for.
VALID_INTERVALS: tuple[str, ...] = ("hour", "day")


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


def start_recorder() -> None:
    """Start the background drain that moves queued clicks into the datastore."""
    raise NotImplementedError


def stop_recorder() -> None:
    """Drain what is queued and stop the background worker."""
    raise NotImplementedError


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
    raise NotImplementedError


def total_clicks(code: str) -> int:
    """Total recorded clicks for a code — the count the metadata endpoint reports."""
    raise NotImplementedError


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
    raise NotImplementedError
