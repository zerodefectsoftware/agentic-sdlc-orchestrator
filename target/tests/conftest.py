"""Make the target importable to its own tests, and hold the suite's fixtures.

The target is treated as an arbitrary codebase, not as part of the orchestrator
package (D3) — so it is not installed. Its tests put it on the path themselves,
exactly as they would in the repository it came from.

--------------------------------------------------------------------------
Why this file contains tolerant readers
--------------------------------------------------------------------------
The acceptance criteria fix *observable outcomes* (status codes, header values,
counts) but the design spec does not fix every JSON field name — e.g. E8 says
the analytics response carries "total clicks plus per-interval counts and
breakdowns by referrer and by device/browser class" without naming the keys.

So the readers below accept a small, explicit list of conventional key names
and fail loudly when none is present. That keeps each test asserting the
criterion's outcome rather than a key spelling the design never committed to.
The candidate lists are deliberately short: an unrecognised shape is a failure,
never a silent pass.

Anything the design *did* commit to (301, 7-char base62, 410 on expiry, the
{code, message, details} error envelope) is asserted exactly.
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
# documented constants — these come from the register and the design, not taste
# --------------------------------------------------------------------------- #

FRESHNESS_SECONDS = 60.0  # A6: clicks visible in the analytics API within 60s
P95_BUDGET_SECONDS = 0.100  # AC2.3: p95 redirect latency under 100 ms
AVAILABILITY_TARGET = 0.999  # A9: 99.9% successful responses on the redirect path
MAX_INTERVALS = 366  # A11/E19: time-series capped at 366 intervals

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

# One stable datastore for the whole session: every test is scoped to the codes
# it created, so isolation comes from the codes, not from a fresh file. AC5.1
# needs the path to survive a re-import, which a per-test file would defeat.
os.environ.setdefault("SHORTENER_DATABASE_URL", f"sqlite:///{_DB_FILE}")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB_FILE}")
os.environ.setdefault("SHORTENER_DB_PATH", _DB_FILE)
os.environ.setdefault("SHORTENER_BASE_URL", "http://testserver")

_APP_CANDIDATES = (
    # CLAUDE.md documents the entry point as `shortener.main:app`.
    ("shortener.main", "app"),
    ("shortener.api", "app"),
    ("shortener.api.app", "app"),
    ("shortener.app", "app"),
)


def _load_app():
    """Return the target's ASGI application, or fail with what was tried."""
    tried = []
    for module_name, attr in _APP_CANDIDATES:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - the reason is the message
            tried.append(f"{module_name}: {type(exc).__name__}: {exc}")
            continue
        app = getattr(module, attr, None)
        if app is not None:
            return app
        tried.append(f"{module_name}: imported but has no attribute {attr!r}")
    raise RuntimeError(
        "No ASGI application found in the target. Tried:\n  " + "\n  ".join(tried)
    )


def _purge_target_modules() -> None:
    for name in [n for n in sys.modules if n == "shortener" or n.startswith("shortener.")]:
        del sys.modules[name]


def _ip_for(nodeid: str) -> str:
    """A deterministic, per-test client IP.

    Rate limiting is keyed on client IP (E13), so tests that share an IP would
    contend for one token bucket and fail each other. A distinct IP per test
    makes that impossible — and is exactly the isolation AC5.4 promises.
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
    """The primary client: lifespan entered, so background workers are running."""
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
# tolerant readers for the analytics response (see module docstring)
# --------------------------------------------------------------------------- #

_TOTAL_KEYS = ("total_clicks", "total", "clicks", "click_count")
_SERIES_KEYS = ("series", "time_series", "timeseries", "intervals", "buckets", "by_interval")
_REFERRER_KEYS = ("referrers", "by_referrer", "referrer", "referrer_breakdown")
_DEVICE_KEYS = ("devices", "by_device", "device", "device_breakdown")
_BROWSER_KEYS = ("browsers", "by_browser", "browser", "browser_breakdown")

_COUNT_KEYS = ("count", "clicks", "total")
_LABEL_KEYS = (
    "value", "key", "label", "name", "referrer", "device", "browser", "class", "group",
)
_START_KEYS = ("start", "bucket", "timestamp", "interval_start", "ts", "time", "at")


def _search(body, candidates):
    """Find the first candidate key, allowing one level of `breakdowns` nesting."""
    scopes = [body]
    for container in ("breakdowns", "breakdown", "by", "data"):
        nested = body.get(container)
        if isinstance(nested, dict):
            scopes.append(nested)
    for scope in scopes:
        for key in candidates:
            if key in scope:
                return scope[key]
    return None


def _require(body, candidates, what):
    found = _search(body, candidates)
    if found is None:
        raise AssertionError(
            f"analytics response has no {what}: tried keys {list(candidates)}, "
            f"body keys were {sorted(body)}"
        )
    return found


def read_total(body) -> int:
    """Total click count from an analytics response."""
    total = _require(body, _TOTAL_KEYS, "total click count")
    assert isinstance(total, int) and not isinstance(total, bool), (
        f"total click count should be an integer, got {total!r}"
    )
    return total


def _entry_count(entry):
    for key in _COUNT_KEYS:
        if key in entry and isinstance(entry[key], int) and not isinstance(entry[key], bool):
            return entry[key]
    raise AssertionError(f"breakdown entry has no integer count: {entry!r}")


def _entry_label(entry):
    for key in _LABEL_KEYS:
        if key in entry:
            return "" if entry[key] is None else str(entry[key])
    raise AssertionError(f"breakdown entry has no label: {entry!r}")


def read_breakdown(body, candidates, what) -> dict[str, int]:
    """Normalise a breakdown to {label: count}, from a list or a mapping."""
    raw = _require(body, candidates, what)
    if isinstance(raw, dict):
        result = {}
        for label, count in raw.items():
            assert isinstance(count, int) and not isinstance(count, bool), (
                f"{what} entry {label!r} should map to an integer, got {count!r}"
            )
            result[str(label)] = count
        return result
    assert isinstance(raw, list), f"{what} should be a list or mapping, got {type(raw).__name__}"
    return {_entry_label(entry): _entry_count(entry) for entry in raw}


def read_series(body) -> list[tuple[str, int]]:
    """Normalise the time series to an ordered [(interval_start, count)]."""
    raw = _require(body, _SERIES_KEYS, "per-interval click counts")
    assert isinstance(raw, list), (
        f"time series should be a list of intervals, got {type(raw).__name__}"
    )
    series = []
    for entry in raw:
        assert isinstance(entry, dict), f"time-series entry should be an object, got {entry!r}"
        start = None
        for key in _START_KEYS:
            if key in entry:
                start = entry[key]
                break
        assert start is not None, (
            f"time-series entry has no interval start: tried {list(_START_KEYS)}, got {entry!r}"
        )
        series.append((str(start), _entry_count(entry)))
    return series


# --------------------------------------------------------------------------- #
# settling — analytics are eventually consistent (A6)
# --------------------------------------------------------------------------- #


def default_window(days_back=30, days_forward=1, interval="day"):
    """A wide window that comfortably contains 'now'.

    E8 requires a [start, end) window and an interval for the *time series*;
    it does not settle whether a caller asking only for the total must supply
    one. Tests that only care about the total therefore fall back to this
    window if the endpoint insists on one — see `analytics`.
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
    """GET the analytics endpoint, asserting 200, and return the body."""

    def _analytics(client, code, **params):
        response = client.get(f"/api/links/{code}/analytics", params=params or None)
        if response.status_code == 400 and not params:
            # The endpoint requires an explicit window (a legitimate reading of
            # E8/E19). Retry with one; the totals asserted by the caller are
            # unaffected by widening the window around 'now'.
            response = client.get(
                f"/api/links/{code}/analytics", params=default_window()
            )
        assert response.status_code == 200, (
            f"analytics for {code!r} failed: {response.status_code} {response.text}"
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
        body = None
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


def _patch_everywhere(monkeypatch, names, replacement, what):
    """Replace `names` on every loaded target module that defines one.

    A module that did `from ... import record_click` holds its own reference,
    so patching only the defining module would leave the real function on the
    request path. Patching every binding closes that hole.

    The named callables are design commitments, not implementation trivia:
    E25 puts the click recorder in the analytics module, E24 puts the health
    reachability ping in the storage module. If none is found the test fails —
    it never silently passes with nothing injected.
    """
    patched = []
    for module_name, module in list(sys.modules.items()):
        if module_name != "shortener" and not module_name.startswith("shortener."):
            continue
        for name in names:
            if callable(getattr(module, name, None)):
                monkeypatch.setattr(module, name, replacement, raising=False)
                patched.append(f"{module_name}.{name}")
    if not patched:
        raise AssertionError(
            f"found no {what} to disable: looked for {list(names)} on the target's "
            f"modules, so this criterion cannot be exercised"
        )
    return patched


@pytest.fixture
def break_analytics_recorder(monkeypatch):
    """Make click recording fail on the request path, as AC5.2 requires."""

    def _break():
        def _raise(*args, **kwargs):
            raise RuntimeError("analytics recorder unavailable (injected by AC5.2)")

        return _patch_everywhere(
            monkeypatch,
            ("record_click", "record", "enqueue_click", "enqueue", "submit_click"),
            _raise,
            "analytics click recorder",
        )

    return _break


@pytest.fixture
def break_datastore_ping(monkeypatch):
    """Make the datastore reachability check fail, as AC5.3 requires."""

    def _break():
        def _raise(*args, **kwargs):
            raise RuntimeError("datastore unreachable (injected by AC5.3)")

        return _patch_everywhere(
            monkeypatch,
            ("ping", "check_health", "healthcheck", "health_check", "is_reachable",
             "check_reachable", "reachable"),
            _raise,
            "datastore reachability check",
        )

    return _break
