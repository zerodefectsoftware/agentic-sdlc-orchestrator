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

from dataclasses import dataclass


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
    raise NotImplementedError
