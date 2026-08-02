"""ratelimit — per-caller request budgets, as an in-process token bucket.

AC5.4 asks for two things: a caller over the limit gets 429 with `Retry-After`,
and other callers are unaffected. That makes the bucket per key, and the key is
the client IP — the register removed every other identity when it made the API
anonymous (A2).

In-process because the design has one instance and one datastore (A3). A shared
limiter would need a second dependency on the redirect path, which is exactly
what AC5.2 and the p95 budget push against; the cost, written down here rather
than discovered later, is that two instances would each grant a full budget.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from shortener.config import get_settings
from shortener.errors import RateLimitedError

# One bucket per caller: how many tokens were left, and the monotonic reading
# they were left at. Refill is computed from that reading when the caller next
# appears, so an idle bucket costs nothing between requests.
_buckets: dict[str, tuple[float, float]] = {}

# Requests reach this from the server's thread pool, so read-modify-write of a
# bucket has to be one step or two simultaneous callers on the same key each
# spend the same token.
_lock = threading.Lock()

# A bucket that has refilled to capacity is indistinguishable from one that was
# never opened, so it can be forgotten. Without that, a dict keyed on client IP
# only ever grows — a slow leak on an endpoint anyone can reach. The sweep is
# gated on size so the common path never pays for it.
_SWEEP_ABOVE = 10_000


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """The verdict for one request, and what the caller may tell the client."""

    allowed: bool
    remaining: int
    retry_after: float


def enforce_rate_limit(client_key: str) -> RateLimitDecision:
    """Charge one request against `client_key`'s bucket.

    Returns the decision when the request is allowed, and raises
    `RateLimitedError` — which carries `retry_after` — when it is not, so no
    caller can forget to check a boolean.
    """
    settings = get_settings()
    capacity = float(settings.rate_limit_requests)
    window = settings.rate_limit_window_seconds
    # Tokens per second. A budget that refills over exactly one window reads as
    # "N requests per window" but spends as a trickle, so a caller that backs
    # off is served again a token at a time instead of waiting for the next
    # fixed window to open — which is what makes `Retry-After` a real answer.
    refill = capacity / window

    # `monotonic`, not `time()`: a clock adjustment must not hand a caller a
    # free budget or freeze one out.
    now = time.monotonic()

    with _lock:
        if len(_buckets) > _SWEEP_ABOVE:
            _forget_refilled_buckets(now, window)

        tokens, charged_at = _buckets.get(client_key, (capacity, now))
        tokens = min(capacity, tokens + (now - charged_at) * refill)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        _buckets[client_key] = (tokens, now)

    if allowed:
        return RateLimitDecision(
            allowed=True, remaining=int(tokens), retry_after=0.0
        )

    # The wait until the bucket holds one whole token — the only number a
    # refused client can actually act on (AC5.4).
    raise RateLimitedError(
        "too many requests from this client",
        retry_after=(1.0 - tokens) / refill,
        details={"limit": settings.rate_limit_requests, "window_seconds": window},
    )


def _forget_refilled_buckets(now: float, window: float) -> None:
    """Drop the buckets that have sat idle long enough to be full again.

    Called with `_lock` held. A caller silent for a whole window is back at
    capacity, so keeping its bucket would only reserve memory for a state the
    default already describes.
    """
    idle = [
        key
        for key, (_, charged_at) in _buckets.items()
        if now - charged_at >= window
    ]
    for key in idle:
        del _buckets[key]
