"""Make the target importable to its own tests, and hold the suite's fixtures.

The target is treated as an arbitrary codebase, not as part of the orchestrator
package (D3) — so it is not installed. Its tests put it on the path themselves,
exactly as they would in the repository it came from.

--------------------------------------------------------------------------
Why this file is strict
--------------------------------------------------------------------------
An earlier draft of this suite carried "tolerant readers" that accepted a list
of plausible JSON key spellings, because the design named the analytics payload
without fixing its field names. That is no longer true: the architect now ships
the interface contract as stub packages, and `shortener.main` declares
`AnalyticsResponse`, `LinkDetailResponse` and `HealthResponse` field by field.

So the readers below assert the contract's names exactly. A tolerant reader
against a fixed contract is not robustness — it is a test that would pass on a
payload the contract forbids, and it hides the one bug most likely to appear
when eight implementers work in parallel: two modules disagreeing about a key.

Everything asserted here is a commitment something made: the register (A1's 301,
A6's 60 s, A9's 99.9%, A11's 366 intervals), the design spec (the six routes),
or the stub contract (every field name and status code).
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

# --------------------------------------------------------------------------- #
# documented constants — these come from the register and the contract, not taste
# --------------------------------------------------------------------------- #

FRESHNESS_SECONDS = 60.0  # A6 / analytics.FRESHNESS_SECONDS
P95_BUDGET_SECONDS = 0.100  # AC2.3: p95 redirect latency under 100 ms
AVAILABILITY_TARGET = 0.999  # A9: 99.9% successful responses on the redirect path
MAX_INTERVALS = 366  # A11 / analytics.MAX_INTERVALS
CODE_LENGTH = 7  # codes.CODE_LENGTH
REDIRECT_STATUS = 301  # A1 / main.REDIRECT_STATUS

# The error envelope is exactly these keys and nothing else (A13, errors.ErrorEnvelope).
ENVELOPE_KEYS = {"code", "message", "details"}

# A well-formed URL that naive normalisation would mangle — trailing-slash
# stripping, query re-ordering or percent-decoding all break AC2.1's
# "byte-identical" requirement, and this URL exposes each of them.
TRICKY_URL = "https://example.com/a//b/?z=1&a=%20two&empty=#frag"

DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# --------------------------------------------------------------------------- #
# environment — set at import time, before anything imports the target
# --------------------------------------------------------------------------- #

_TMP = tempfile.mkdtemp(prefix="shortener-acceptance-")
_DB_FILE = str(Path(_TMP) / "acceptance.sqlite3")

# `SHORTENER_` is the prefix config.Settings reads (config.Settings.model_config).
# One stable datastore for the whole session: every test is scoped to the codes
# it created, so isolation comes from the codes, not from a fresh file. AC5.1
# needs the path to survive a re-import, which a per-test file would defeat.
os.environ.setdefault("SHORTENER_DATABASE_URL", f"sqlite:///{_DB_FILE}")

# The origin short URLs are built from, and the host creation must refuse to
# point at (A12). TestClient sends requests to http://testserver.
SERVICE_BASE_URL = "http://testserver"
SERVICE_HOST = "testserver"
os.environ.setdefault("SHORTENER_BASE_URL", SERVICE_BASE_URL)


def _load_app():
    """Return the target's ASGI application.

    The contract fixes the entry point: `shortener.main` exports `app`, assigned
    by the implementer as `app = create_app()`. There is no candidate list,
    because there is no longer a choice to be tolerant about.
    """
    module = importlib.import_module("shortener.main")
    app = getattr(module, "app", None)
    if app is None:
        raise RuntimeError(
            "shortener.main imported but exports no `app`. The contract declares "
            "`app: FastAPI`, assigned by the implementer as `app = create_app()`."
        )
    return app


def _purge_target_modules() -> None:
    for name in [n for n in sys.modules if n == "shortener" or n.startswith("shortener.")]:
        del sys.modules[name]


def _ip_for(nodeid: str) -> str:
    """A deterministic, per-test client IP.

    Rate limiting is keyed on client IP (ratelimit module docstring), so tests
    that shared an IP would contend for one token bucket and fail each other. A
    distinct IP per test makes that impossible — and is exactly the isolation
    AC5.4 promises.
    """
    digest = hashlib.md5(nodeid.encode()).digest()
    return f"10.{digest[0]}.{digest[1]}.{digest[2] or 1}"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def app():
    return _load_app()


@pytest.fixture
def fresh_app():
    """Re-import the target from scratch — the closest in-process analogue of a
    process restart, used by AC5.1. The datastore path is unchanged, so
    anything that did not reach durable storage is gone."""

    def _fresh():
        _purge_target_modules()
        return _load_app()

    return _fresh


@pytest.fixture
def make_client(request):
    """Build a TestClient bound to a chosen source IP, without lifespan."""
    from fastapi.testclient import TestClient

    created = []

    def _make(suffix: int = 0):
        ip = _ip_for(f"{request.node.nodeid}#{suffix}")
        client = TestClient(
            _load_app(), client=(ip, 40000 + suffix), follow_redirects=False
        )
        client.source_ip = ip
        created.append(client)
        return client

    yield _make
    for client in created:
        with contextlib.suppress(Exception):
            client.close()


@pytest.fixture
def client(app, request):
    """The primary client: lifespan entered, so storage is open and the
    analytics drain (analytics.start_recorder) is running."""
    from fastapi.testclient import TestClient

    ip = _ip_for(request.node.nodeid)
    with TestClient(app, client=(ip, 40000), follow_redirects=False) as test_client:
        test_client.source_ip = ip
        yield test_client


@pytest.fixture
def client_pool(client, make_client):
    """Twenty clients on distinct IPs.

    The load-shaped criteria (AC2.3's 50 rps, AC5.5's soak) describe traffic
    from many visitors. Driving them through one IP would measure the rate
    limiter instead of the redirect path, so the pool spreads the load the way
    real redirect traffic is spread.
    """
    return [client] + [make_client(i) for i in range(1, 20)]


# --------------------------------------------------------------------------- #
# request helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def create_link(client):
    """POST a URL and return the parsed 201 body, asserting it was created."""

    def _create(url: str = "https://example.com/target", **extra):
        payload = {"url": url, **extra}
        response = client.post("/api/links", json=payload)
        assert response.status_code == 201, (
            f"creation of {url!r} failed: {response.status_code} {response.text}"
        )
        return response.json()

    return _create


@pytest.fixture
def click():
    """Issue N redirects for a code and return the responses."""

    def _click(client, code, times=1, referer=None, user_agent=None):
        headers = {}
        if referer is not None:
            headers["Referer"] = referer
        if user_agent is not None:
            headers["User-Agent"] = user_agent
        responses = []
        for _ in range(times):
            responses.append(client.get(f"/{code}", headers=headers))
        return responses

    return _click


# --------------------------------------------------------------------------- #
# readers for the analytics response — main.AnalyticsResponse, field for field
# --------------------------------------------------------------------------- #


def assert_error_envelope(response, what):
    """Every non-2xx body is exactly {code, message, details} (A13)."""
    try:
        body = response.json()
    except ValueError:  # pragma: no cover - only reached on a malformed failure
        raise AssertionError(
            f"{what} returned {response.status_code} with a non-JSON body: {response.text!r}"
        ) from None
    assert isinstance(body, dict), f"{what} should return an object, got {body!r}"
    assert set(body) <= ENVELOPE_KEYS, (
        f"{what} must return only the {{code, message, details}} envelope (A13), "
        f"got keys {sorted(body)}"
    )
    assert isinstance(body.get("code"), str) and body["code"], (
        f"{what} needs a machine-readable error code (A13), got {body!r}"
    )
    assert isinstance(body.get("message"), str) and body["message"], (
        f"{what} needs a human-readable message (A13), got {body!r}"
    )
    return body


def read_total(body) -> int:
    """`AnalyticsResponse.total_clicks`."""
    assert "total_clicks" in body, (
        f"analytics response has no 'total_clicks' (main.AnalyticsResponse), "
        f"keys were {sorted(body)}"
    )
    total = body["total_clicks"]
    assert isinstance(total, int) and not isinstance(total, bool), (
        f"total_clicks should be an integer, got {total!r}"
    )
    return total


def read_series(body) -> list[tuple[str, int]]:
    """`AnalyticsResponse.series` as [(IntervalPoint.start, IntervalPoint.count)]."""
    assert "series" in body, (
        f"analytics response has no 'series' (main.AnalyticsResponse), "
        f"keys were {sorted(body)}"
    )
    raw = body["series"]
    assert isinstance(raw, list), f"series should be a list, got {type(raw).__name__}"
    series = []
    for entry in raw:
        assert isinstance(entry, dict), f"series entry should be an object, got {entry!r}"
        assert "start" in entry and "count" in entry, (
            f"series entry must be an IntervalPoint {{start, count}}, got {entry!r}"
        )
        count = entry["count"]
        assert isinstance(count, int) and not isinstance(count, bool), (
            f"series entry count should be an integer, got {entry!r}"
        )
        series.append((str(entry["start"]), count))
    return series


def read_breakdown(body, key) -> dict[str, int]:
    """A breakdown list of `BreakdownItem {value, count}`, as {value: count}."""
    assert key in body, (
        f"analytics response has no {key!r} breakdown (main.AnalyticsResponse), "
        f"keys were {sorted(body)}"
    )
    raw = body[key]
    assert isinstance(raw, list), (
        f"{key} should be a list of BreakdownItem, got {type(raw).__name__}"
    )
    result: dict[str, int] = {}
    for entry in raw:
        assert isinstance(entry, dict), f"{key} entry should be an object, got {entry!r}"
        assert "value" in entry and "count" in entry, (
            f"{key} entry must be a BreakdownItem {{value, count}}, got {entry!r}"
        )
        count = entry["count"]
        assert isinstance(count, int) and not isinstance(count, bool), (
            f"{key} entry count should be an integer, got {entry!r}"
        )
        label = "" if entry["value"] is None else str(entry["value"])
        assert label not in result, (
            f"{key} repeats the value {label!r}; a breakdown is a grouped count, "
            f"so each value appears once"
        )
        result[label] = count
    return result


# --------------------------------------------------------------------------- #
# windows — a time-series query must name its window (A11)
# --------------------------------------------------------------------------- #


def default_window(days_back=30, days_forward=1, interval="day"):
    """A wide window that comfortably contains 'now'.

    A11 requires every analytics query to name its window, so there is no
    "omit it and see" path here: tests that only care about the total still
    pass a window wide enough that widening it cannot change the answer.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    return {
        "start": (now - timedelta(days=days_back)).isoformat(),
        "end": (now + timedelta(days=days_forward)).isoformat(),
        "interval": interval,
    }


@pytest.fixture
def analytics():
    """GET the analytics endpoint with a window, asserting 200, returning the body."""

    def _analytics(client, code, **params):
        query = params or default_window()
        response = client.get(f"/api/links/{code}/analytics", params=query)
        assert response.status_code == 200, (
            f"analytics for {code!r} with {query!r} failed: "
            f"{response.status_code} {response.text}"
        )
        return response.json()

    return _analytics


@pytest.fixture
def wait_for_total(analytics):
    """Poll analytics until the total reaches `expected`, within A6's bound.

    Returns the settled body. Fails with the last value seen — a count that
    never arrives and a count that arrives wrong are different bugs, and the
    message distinguishes them.
    """

    def _wait(client, code, expected, timeout=FRESHNESS_SECONDS, **params):
        deadline = time.monotonic() + timeout
        last = None
        while True:
            body = analytics(client, code, **params)
            last = read_total(body)
            if last == expected:
                return body
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        raise AssertionError(
            f"total clicks for {code!r} settled at {last}, expected {expected}, "
            f"after waiting {timeout}s (A6 freshness bound)"
        )

    return _wait


# --------------------------------------------------------------------------- #
# seams used by the failure-injection criteria (AC5.2, AC5.3)
# --------------------------------------------------------------------------- #


def _patch_everywhere(monkeypatch, name, replacement, what):
    """Replace `name` on every loaded target module that defines it.

    A module that did `from shortener.analytics import record_click` holds its
    own reference, so patching only the defining module would leave the real
    function on the request path. Patching every binding closes that hole.

    The names are contract exports — `analytics.record_click`, `storage.ping` —
    not guesses. If no binding is found the fixture fails loudly: a criterion
    that silently injects nothing is a criterion that silently passes.
    """
    patched = []
    for module_name, module in list(sys.modules.items()):
        if module_name != "shortener" and not module_name.startswith("shortener."):
            continue
        if callable(getattr(module, name, None)):
            monkeypatch.setattr(module, name, replacement, raising=False)
            patched.append(f"{module_name}.{name}")
    if not patched:
        raise AssertionError(
            f"found no {what} to disable: looked for {name!r} on the target's loaded "
            f"modules, so this criterion cannot be exercised"
        )
    return patched


@pytest.fixture
def break_analytics_recorder(monkeypatch):
    """Make click recording fail on the request path, as AC5.2 requires.

    Defaults to the failure the contract documents — `record_click` raises
    `AnalyticsUnavailableError`, which the redirect path must log and swallow —
    and accepts an arbitrary exception so the test can also prove an
    *undocumented* failure is absorbed rather than only the expected one.
    """

    def _break(exc_factory=None):
        if exc_factory is None:

            def exc_factory():
                from shortener.errors import AnalyticsUnavailableError

                return AnalyticsUnavailableError("recorder unavailable (injected by AC5.2)")

        def _raise(*args, **kwargs):
            raise exc_factory()

        return _patch_everywhere(
            monkeypatch, "record_click", _raise, "analytics click recorder"
        )

    return _break


@pytest.fixture
def break_datastore_ping(monkeypatch):
    """Make the datastore reachability check fail, as AC5.3 requires.

    `storage.ping()` is the contract's reachability probe and the documented
    evidence behind GET /health.
    """

    def _break():
        def _false(*args, **kwargs):
            return False

        return _patch_everywhere(
            monkeypatch, "ping", _false, "datastore reachability check"
        )

    return _break
