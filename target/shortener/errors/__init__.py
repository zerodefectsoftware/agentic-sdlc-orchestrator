"""errors — the shared exception hierarchy and the one error envelope.

Every non-2xx response the service emits is one of these exceptions rendered as
an `ErrorEnvelope`: `{"code", "message", "details"}` and nothing else (A13). The
HTTP status is a class attribute of the exception, so a module raising
`NotFoundError` fixes the status without importing anything HTTP-shaped — which
is what keeps `links`, `storage` and `analytics` free of FastAPI.

The status codes are the design's, not incidental:

    400 invalid_url / invalid_query   AC1.2, A11
    404 not_found                     AC2.2, AC3.3, AC4.5
    410 gone                          AC3.2 (soft delete, A7) and expiry (A4)
    429 rate_limited                  AC5.4 — carries `retry_after`
    500 code_collision                AC1.4, after the mint budget is exhausted
    503 storage_unavailable           AC5.3
    503 analytics_unavailable         AC5.2 — never fatal on the redirect path
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ErrorEnvelope(BaseModel):
    """The body of every non-2xx response: machine-readable code, message, details."""

    code: str
    message: str
    details: dict[str, Any] | None = None


class AppError(Exception):
    """Base application error carrying a machine-readable code and an HTTP status."""

    code: str = "internal_error"
    status_code: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        if code is not None:
            # Shadows the class attribute for this instance only, so a caller
            # can narrow the code without needing a subclass for every variant.
            self.code = code

    def to_envelope(self) -> ErrorEnvelope:
        """Render this error as the response body clients parse."""
        return ErrorEnvelope(code=self.code, message=self.message, details=self.details)


class InvalidURLError(AppError):
    """The submitted URL is missing, not absolute, not http/https, or self-referential."""

    code = "invalid_url"
    status_code = 400


class InvalidQueryError(AppError):
    """An analytics window or interval the API refuses to serve; maps to HTTP 400."""

    code = "invalid_query"
    status_code = 400


class NotFoundError(AppError):
    """The short code was never issued; maps to HTTP 404."""

    code = "not_found"
    status_code = 404


class LinkGoneError(AppError):
    """The code was issued but is deleted or expired; maps to HTTP 410."""

    code = "gone"
    status_code = 410


class CodeCollisionError(AppError):
    """Code minting exhausted its attempts against live codes; maps to HTTP 500."""

    code = "code_collision"
    status_code = 500


class RateLimitedError(AppError):
    """The caller exceeded its request budget; maps to HTTP 429 with Retry-After."""

    code = "rate_limited"
    status_code = 429

    def __init__(
        self,
        message: str,
        *,
        retry_after: float,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.retry_after = retry_after


class StorageError(AppError):
    """The datastore is unreachable or rejected the operation; maps to HTTP 503."""

    code = "storage_unavailable"
    status_code = 503


class AnalyticsUnavailableError(AppError):
    """Click recording or querying failed.

    On the redirect path this is caught and logged, never surfaced: AC5.2 makes
    the redirect independent of the analytics store.
    """

    code = "analytics_unavailable"
    status_code = 503
