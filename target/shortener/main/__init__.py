"""main — the HTTP surface: the app, its routes, its schemas, its error handler.

This is the only module that knows about HTTP. It maps `AppError` subclasses
onto statuses and the `{code, message, details}` envelope (A13), charges every
request against the rate limiter (AC5.4), and composes the two services that
know nothing about each other — `links` for the link, `analytics` for its
counts.

    POST   /api/links                    create                     201
    GET    /{code}                       redirect                   301
    GET    /api/links/{code}             metadata + total clicks    200
    DELETE /api/links/{code}             retire the code            204
    GET    /api/links/{code}/analytics   counts, series, breakdowns 200
    GET    /health                       datastore reachability     200/503

301 and not 302, because the register settled it (A1): a short link is a
permanent identifier and caching it is the point. The accepted cost is written
into the docs rather than absorbed — a cached redirect never reaches the
service, so click counts under-report and a deleted link stays followable for
clients that already cached it.

The redirect path is deliberately thin: resolve, record the click through the
queue, redirect. A failure anywhere in recording is logged and swallowed, never
turned into a non-2xx (AC5.2).
"""

from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from shortener import analytics, links, storage
from shortener.errors import (
    AppError,
    ErrorEnvelope,
    InvalidQueryError,
    RateLimitedError,
    StorageError,
)
from shortener.ratelimit import enforce_rate_limit
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

# A1. A single named constant so the decision is one edit, not six handlers.
REDIRECT_STATUS: int = 301

# Statuses Starlette raises on its own — an unrouted path, a wrong method —
# still leave through the one envelope A13 fixed.
_HTTP_ERROR_CODES: dict[int, str] = {404: "not_found", 405: "method_not_allowed"}


class CreateLinkRequest(BaseModel):
    """The creation payload: a URL, and optionally when it stops working (A4)."""

    url: str
    expires_at: datetime | None = None


class LinkResponse(BaseModel):
    """The 201 body — short URL, code and the long URL exactly as submitted."""

    code: str
    short_url: str
    long_url: str
    created_at: datetime
    expires_at: datetime | None = None


class LinkDetailResponse(LinkResponse):
    """The metadata body: the link, plus the click total AC3.1 asks for."""

    total_clicks: int
    deleted_at: datetime | None = None


class BreakdownItem(BaseModel):
    """One grouped count in an analytics breakdown."""

    value: str
    count: int


class IntervalPoint(BaseModel):
    """Clicks in one time bucket of the analytics series."""

    start: datetime
    count: int


class AnalyticsResponse(BaseModel):
    """The analytics body: totals, per-interval counts, and the breakdowns."""

    code: str
    total_clicks: int
    start: datetime
    end: datetime
    interval: str
    series: list[IntervalPoint]
    referrers: list[BreakdownItem]
    devices: list[BreakdownItem]
    browsers: list[BreakdownItem]


class HealthResponse(BaseModel):
    """What `/health` reports: the verdict and the dependency it rests on."""

    status: str
    datastore: bool


def _charge_rate_limit(request: Request) -> None:
    """Charge one request against the caller's budget (AC5.4).

    A dependency rather than middleware so `RateLimitedError` reaches the
    handler below and leaves as the same envelope every other failure does.
    `/health` is deliberately not charged: a load balancer polling it must not
    be told to go away by a burst from someone else behind the same address.
    """
    enforce_rate_limit(request.client.host if request.client else "unknown")


def create_app() -> FastAPI:
    """Build the application: routes, error handler, and the lifespan.

    The lifespan opens the datastore and starts the analytics drain, and stops
    the drain on shutdown so queued clicks are not silently lost.
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        storage.init_storage()
        analytics.start_recorder()
        yield
        analytics.stop_recorder()

    app = FastAPI(
        title="shortener",
        description="A URL shortener with click analytics.",
        lifespan=lifespan,
    )

    rate_limited = [Depends(_charge_rate_limit)]

    # ---------------------------------------------------------------- errors

    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        """Every failure the service raises, rendered as its own envelope (A13)."""
        response = JSONResponse(
            status_code=exc.status_code,
            content=exc.to_envelope().model_dump(mode="json"),
        )
        if isinstance(exc, RateLimitedError):
            # Whole seconds, rounded up and never zero: a client told to retry
            # in 0 s has been told nothing (AC5.4).
            response.headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after)))
        return response

    async def handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """A malformed payload or query is a 400 in the same envelope (AC1.2).

        FastAPI's own 422 body is a different shape, and A13 fixed one shape.
        """
        return JSONResponse(
            status_code=400,
            content=ErrorEnvelope(
                code="invalid_request",
                message="the request is not valid",
                details={
                    "errors": [
                        {
                            "field": ".".join(str(part) for part in error["loc"]),
                            "message": error["msg"],
                        }
                        for error in exc.errors()
                    ]
                },
            ).model_dump(mode="json"),
        )

    async def handle_http_error(
        _: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """An unrouted path or a wrong method leaves through the envelope too."""
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorEnvelope(
                code=_HTTP_ERROR_CODES.get(exc.status_code, "http_error"),
                message=str(exc.detail),
            ).model_dump(mode="json"),
        )

    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_error)

    # ---------------------------------------------------------------- routes
    # The redirect is registered last: `/{code}` matches anything, so every
    # named route has to be declared before it.

    @app.post(
        "/api/links",
        response_model=LinkResponse,
        status_code=201,
        dependencies=rate_limited,
    )
    def create_link(payload: CreateLinkRequest) -> LinkResponse:
        link = links.create_link(payload.url, payload.expires_at)
        return LinkResponse(
            code=link.code,
            short_url=link.short_url,
            long_url=link.long_url,
            created_at=link.created_at,
            expires_at=link.expires_at,
        )

    @app.get(
        "/api/links/{code}",
        response_model=LinkDetailResponse,
        dependencies=rate_limited,
    )
    def read_link(code: str) -> LinkDetailResponse:
        link = links.get_link(code)
        return LinkDetailResponse(
            code=link.code,
            short_url=link.short_url,
            long_url=link.long_url,
            created_at=link.created_at,
            expires_at=link.expires_at,
            deleted_at=link.deleted_at,
            total_clicks=analytics.total_clicks(link.code),
        )

    @app.delete("/api/links/{code}", status_code=204, dependencies=rate_limited)
    def remove_link(code: str) -> Response:
        links.delete_link(code)
        return Response(status_code=204)

    @app.get(
        "/api/links/{code}/analytics",
        response_model=AnalyticsResponse,
        dependencies=rate_limited,
    )
    def read_analytics(
        code: str,
        start: datetime | None = None,
        end: datetime | None = None,
        interval: str = "day",
    ) -> AnalyticsResponse:
        # Existence first, so a code that was never issued is refused before the
        # window is even looked at and discloses nothing either way (AC4.5).
        links.get_link(code)
        if start is None or end is None:
            raise InvalidQueryError(
                "an analytics query must name both ends of its window",
                details={"start": start is not None, "end": end is not None},
            )
        summary = analytics.summarize_clicks(code, start, end, interval)
        return AnalyticsResponse(
            code=summary.code,
            total_clicks=summary.total_clicks,
            start=summary.start,
            end=summary.end,
            interval=summary.interval,
            series=[
                IntervalPoint(start=point.start, count=point.count)
                for point in summary.series
            ],
            referrers=[
                BreakdownItem(value=entry.value, count=entry.count)
                for entry in summary.referrers
            ],
            devices=[
                BreakdownItem(value=entry.value, count=entry.count)
                for entry in summary.devices
            ],
            browsers=[
                BreakdownItem(value=entry.value, count=entry.count)
                for entry in summary.browsers
            ],
        )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        # Only what redirects need. The analytics recorder is deliberately not
        # consulted: AC5.2 makes a failing recorder a healthy state, and a
        # health check that went red over it would pull a still-serving
        # instance out of the load balancer.
        if not storage.ping():
            raise StorageError("the datastore is not reachable")
        return HealthResponse(status="ok", datastore=True)

    @app.get(
        "/{code}",
        status_code=REDIRECT_STATUS,
        response_class=Response,
        dependencies=rate_limited,
    )
    def redirect(code: str, request: Request) -> Response:
        link = links.resolve_link(code)
        try:
            analytics.record_click(
                link.code,
                datetime.now(UTC),
                referrer=request.headers.get("referer"),
                user_agent=request.headers.get("user-agent"),
            )
        except Exception as exc:  # noqa: BLE001
            # AC5.2: the redirect fails open on every recorder failure,
            # documented or not — a surprise in the recorder must not become a
            # 500 on the path with the tightest budget. Logged at WARNING,
            # because a service silently dropping clicks looks healthy.
            logger.warning("could not record a click for %r: %s", link.code, exc)
        return Response(
            status_code=REDIRECT_STATUS,
            # Set as a raw header rather than through RedirectResponse, which
            # re-quotes the URL. AC2.1 compares Location byte-for-byte.
            headers={"location": link.long_url},
        )

    return app


# The ASGI application, and the entry point everything else names:
# `shortener.main:app` for uvicorn, and the same attribute the acceptance suite
# imports. Constructing it here is safe because `create_app()` only wires routes
# and handlers — the datastore and the analytics drain are opened by the
# lifespan, not at import.
app: FastAPI = create_app()
