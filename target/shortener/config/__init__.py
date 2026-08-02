"""config — every tunable the service reads, in one settings object.

Read from the environment with the `SHORTENER_` prefix, so a deployment changes
behaviour without a code change and the acceptance suite can point the service
at its own datastore. Defaults are sized for the scale the register settled on
(A3): ~10k links/day, peak 100 redirects/second, one region, one datastore.

Nothing here reaches for a network or a file at import time — `get_settings()`
is the only way in, so a caller that never asks never pays.

The numeric bounds below are refusals, not decoration. Every one of them names a
value the rest of the service cannot honour but would accept in silence: a queue
size of zero is an *unbounded* queue, which is the state A6 traded away on
purpose, and a rate-limit window of zero is a division by zero on the redirect
path. Both look like a working configuration until the failure arrives, so they
are refused here, at the one boundary where the environment gets a say.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """The service's configuration, read once per process from the environment."""

    model_config = SettingsConfigDict(env_prefix="SHORTENER_", extra="ignore")

    # Persistence (AC5.1): a single indexed datastore, no cache tier, no shards.
    database_url: str = "sqlite:///./shortener.sqlite3"

    # The origin short URLs are built from, and the host creation refuses to
    # point at (A12 — a self-referential short URL is a redirect loop).
    base_url: str = "http://localhost:8000"

    # Rate limiting (AC5.4), per client key, as a token bucket. A budget of no
    # requests would refuse every caller, and a window of no seconds is the
    # bucket's refill rate divided by zero.
    rate_limit_requests: int = Field(default=120, gt=0)
    rate_limit_window_seconds: float = Field(default=60.0, gt=0)

    # Asynchronous click recording (A6): bounded queue, batched drain. The
    # queue is bounded on purpose — an unbounded one trades the redirect
    # path's latency budget for memory it can never give back, and a maxsize
    # of zero is exactly that unbounded queue wearing this setting's name.
    analytics_queue_maxsize: int = Field(default=10_000, gt=0)
    analytics_flush_interval_seconds: float = Field(default=1.0, gt=0)
    analytics_batch_size: int = Field(default=200, gt=0)

    # The denylist hook A12 leaves open; empty means no host is refused beyond
    # the service's own.
    blocked_hosts: tuple[str, ...] = ()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, constructed once and reused."""
    return Settings()
