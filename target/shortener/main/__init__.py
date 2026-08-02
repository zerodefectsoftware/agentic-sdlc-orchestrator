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

from datetime import datetime

from fastapi import FastAPI
from pydantic import BaseModel

# A1. A single named constant so the decision is one edit, not six handlers.
REDIRECT_STATUS: int = 301


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


def create_app() -> FastAPI:
    """Build the application: routes, error handler, and the lifespan.

    The lifespan opens the datastore and starts the analytics drain, and stops
    the drain on shutdown so queued clicks are not silently lost.
    """
    raise NotImplementedError


# Assigned by the implementer as `app = create_app()`. Declared without a value
# here on purpose: a stub that constructed the app at import time would call a
# function that raises, and every module in the package would stop importing.
app: FastAPI
