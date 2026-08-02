"""config — every tunable the service reads, in one settings object.

Read from the environment with the `SHORTENER_` prefix, so a deployment changes
behaviour without a code change and the acceptance suite can point the service
at its own datastore. Defaults are sized for the scale the register settled on
(A3): ~10k links/day, peak 100 redirects/second, one region, one datastore.

Nothing here reaches for a network or a file at import time — `get_settings()`
is the only way in, so a caller that never asks never pays.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """The service's configuration, read once per process from the environment."""

    model_config = SettingsConfigDict(env_prefix="SHORTENER_", extra="ignore")

    # Persistence (AC5.1): a single indexed datastore, no cache tier, no shards.
    database_url: str = "sqlite:///./shortener.sqlite3"

    # The origin short URLs are built from, and the host creation refuses to
    # point at (A12 — a self-referential short URL is a redirect loop).
    base_url: str = "http://localhost:8000"

    # Rate limiting (AC5.4), per client key, as a token bucket.
    rate_limit_requests: int = 120
    rate_limit_window_seconds: float = 60.0

    # Asynchronous click recording (A6): bounded queue, batched drain. The
    # queue is bounded on purpose — an unbounded one trades the redirect
    # path's latency budget for memory it can never give back.
    analytics_queue_maxsize: int = 10_000
    analytics_flush_interval_seconds: float = 1.0
    analytics_batch_size: int = 200

    # The denylist hook A12 leaves open; empty means no host is refused beyond
    # the service's own.
    blocked_hosts: tuple[str, ...] = ()


def get_settings() -> Settings:
    """Return the process-wide settings, constructed once and reused."""
    raise NotImplementedError
