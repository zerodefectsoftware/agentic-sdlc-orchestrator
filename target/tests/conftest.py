"""Make the target importable to its own tests, and give the suite its fixtures.

The target is treated as an arbitrary codebase, not as part of the orchestrator
package (D3) — so it is not installed. Its tests put it on the path themselves,
exactly as they would in the repository it came from.

## What this suite is

Acceptance tests, written from the requirement register before the
implementation exists (D5). Every test names the acceptance criterion it covers
in its docstring, as ``Covers: ACx.y``; the traceability gate reads those ids,
and ``acceptance_suite.json`` beside this file is the machine-readable form of
the same mapping.

## The three things behaviour alone cannot tell us

Almost everything here is asserted through HTTP. Three things cannot be, so they
are stated as contract — part of what the implementation has to satisfy:

1. **Entry point.** ``shortener.main`` exposes ``create_app()``, which returns
   the ASGI application and reads its configuration from the environment *at
   call time* (so a test can vary deployment parameters), plus a module-level
   ``app = create_app()`` for ``uvicorn shortener.main:app`` — the command in
   ``config/target.shortener.yaml``. ``shortener.api`` and ``shortener`` are
   accepted as alternative homes for the factory.

2. **Configuration.** These environment variables are honoured:

   | variable | meaning |
   | --- | --- |
   | ``SHORTENER_DATABASE_URL`` | datastore URL, e.g. ``sqlite:///…/shortener.db`` |
   | ``SHORTENER_RATE_LIMIT_MAX`` | creates allowed per client per window (E21) |
   | ``SHORTENER_RATE_LIMIT_WINDOW_SECONDS`` | that window, in seconds |

   A second name for each is set as well (see the ``*_ENV`` tuples below), so a
   near-miss on naming is tolerated. The design already requires the rate limit
   to be a deployment parameter; this only fixes where the deployment says it.

3. **Failure seams.** AC5.2 and AC5.3 are about a dependency *failing*, and a
   dependency cannot be made to fail over HTTP. ``break_seam`` replaces a
   named callable on the imported ``shortener`` modules with one that raises. It
   tries several plausible names and fails loudly if none exists, rather than
   passing quietly — a test that cannot inject the fault has not tested the
   fault.

``raise_server_exceptions=False`` is set on every client on purpose: AC5.4 is
about what the *client* sees when the service breaks, which is unobservable if
the test client re-raises the exception instead.
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# --------------------------------------------------------------------------- #
# contract
# --------------------------------------------------------------------------- #

APP_FACTORIES = (
    ("shortener.main", "create_app"),
    ("shortener.api", "create_app"),
    ("shortener", "create_app"),
)

DATABASE_URL_ENV = ("SHORTENER_DATABASE_URL", "SHORTENER_DB_URL")
RATE_LIMIT_ENV = ("SHORTENER_RATE_LIMIT_MAX", "SHORTENER_RATE_LIMIT_REQUESTS")
RATE_WINDOW_ENV = ("SHORTENER_RATE_LIMIT_WINDOW_SECONDS", "SHORTENER_RATE_LIMIT_WINDOW")

# High enough that the limiter never fires except in the test that asks it to.
UNLIMITED = 1_000_000

DEFAULT_URL = "https://example.com/a/deep/path?q=1&r=2#section"

# Names the redirect path might use to hand a click to analytics (E16), and
# names the health probe might use to reach the datastore (E19, E26).
ANALYTICS_SEAM_NAMES = ("record_click", "record", "enqueue_click", "enqueue", "publish")
STORAGE_SEAM_NAMES = (
    "check_dependencies",
    "check_health",
    "healthcheck",
    "is_reachable",
    "reachable",
    "probe",
    "ping",
)


def _app_factory():
    """Locate ``create_app`` at one of the contracted homes."""
    problems = []
    for module_name, attribute in APP_FACTORIES:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            problems.append(f"{module_name}: {type(exc).__name__}: {exc}")
            continue
        factory = getattr(module, attribute, None)
        if callable(factory):
            return factory
        problems.append(f"{module_name}: imported, but has no callable '{attribute}'")
    pytest.fail(
        "no application factory found. This suite needs create_app() at one of "
        + ", ".join(f"{m}.{a}" for m, a in APP_FACTORIES)
        + " — see the contract in target/tests/conftest.py. Tried:\n  "
        + "\n  ".join(problems)
    )


# --------------------------------------------------------------------------- #
# application and client
# --------------------------------------------------------------------------- #


@pytest.fixture
def database_url(tmp_path):
    """One datastore per test, so no test can see another's links."""
    return f"sqlite:///{tmp_path / 'shortener.db'}"


@pytest.fixture
def make_app(monkeypatch, database_url):
    """Build an application with the deployment parameters a test needs.

    Returned as a callable rather than an app so that AC5.5 can stand two
    instances over the same datastore, and the rate-limit test can stand one
    with a limit low enough to reach.
    """

    def _make(*, rate_limit=None, window_seconds=60, url=None):
        for name in DATABASE_URL_ENV:
            monkeypatch.setenv(name, url or database_url)
        for name in RATE_LIMIT_ENV:
            monkeypatch.setenv(name, str(UNLIMITED if rate_limit is None else rate_limit))
        for name in RATE_WINDOW_ENV:
            monkeypatch.setenv(name, str(window_seconds))
        return _app_factory()()

    return _make


@pytest.fixture
def make_client():
    """Wrap an app in a started test client — lifespan runs, redirects are not followed."""
    from fastapi.testclient import TestClient

    def _make(app):
        return TestClient(app, follow_redirects=False, raise_server_exceptions=False)

    return _make


@pytest.fixture
def client(make_app, make_client):
    with make_client(make_app()) as started:
        yield started


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def create(client):
    """Create a link and return its body, asserting the precondition succeeded."""

    def _create(url=DEFAULT_URL, expires_at=None):
        payload = {"url": url}
        if expires_at is not None:
            payload["expires_at"] = expires_at
        response = client.post("/links", json=payload)
        assert response.status_code == 201, (
            f"precondition failed: POST /links {payload} returned "
            f"{response.status_code} {response.text}"
        )
        return response.json()

    return _create


@pytest.fixture
def wait_for_clicks(client):
    """Poll analytics until the count settles, because counting is async (E16).

    Returns the last payload seen — the assertion belongs in the test, so a
    timeout fails on the count rather than on a helper's own opinion.
    """

    def _wait(code, expected, timeout=15.0):
        deadline = time.monotonic() + timeout
        last = None
        while True:
            response = client.get(f"/links/{code}/analytics")
            if response.status_code == 200:
                last = response.json()
                if last.get("total_clicks") == expected:
                    return last
            if time.monotonic() >= deadline:
                return last
            time.sleep(0.05)

    return _wait


@pytest.fixture
def break_seam(monkeypatch):
    """Make a named dependency raise, wherever the target imported it.

    Patches every imported ``shortener`` module that carries the attribute, so
    it works whether a caller does ``analytics.record_click(...)`` or
    ``from ...analytics import record_click``.
    """

    def _break(candidates, *, label):
        def unavailable(*args, **kwargs):
            raise RuntimeError(f"injected failure: {label} is unavailable")

        patched = []
        for name, module in list(sys.modules.items()):
            if module is None or not (name == "shortener" or name.startswith("shortener.")):
                continue
            for attribute in candidates:
                if callable(getattr(module, attribute, None)):
                    monkeypatch.setattr(module, attribute, unavailable, raising=False)
                    patched.append(f"{name}.{attribute}")
        if not patched:
            pytest.fail(
                f"cannot make {label} fail: none of {list(candidates)} is a callable "
                f"attribute of any imported shortener module, so this criterion's "
                f"fault cannot be injected. See the seam contract in "
                f"target/tests/conftest.py."
            )
        return patched

    return _break


@pytest.fixture
def break_analytics(break_seam):
    """Take the click-recording path out of service (AC5.2, E25)."""

    def _break():
        return break_seam(ANALYTICS_SEAM_NAMES, label="the analytics recording path")

    return _break


@pytest.fixture
def break_datastore_probe(break_seam):
    """Make the health endpoint's dependency check fail (AC5.3, E19)."""

    def _break():
        return break_seam(STORAGE_SEAM_NAMES, label="the datastore")

    return _break


@pytest.fixture
def failing_route():
    """Register a route that raises, to observe the unhandled-failure path (AC5.4).

    Added at the front of the routing table: the ``/{code}`` redirect route
    would otherwise swallow the path and answer 404.
    """

    def _add(app, path="/__acceptance_internal_failure__"):
        def boom():
            raise RuntimeError("injected unhandled failure")

        if hasattr(app, "add_api_route"):
            app.add_api_route(path, boom, methods=["GET"])
        elif hasattr(app, "add_route"):
            app.add_route(path, lambda request: boom(), methods=["GET"])
        else:
            pytest.fail(
                "the application object exposes neither add_api_route nor add_route, "
                "so no failing route can be registered to observe AC5.4"
            )
        routes = getattr(getattr(app, "router", None), "routes", None)
        if routes:
            routes.insert(0, routes.pop())
        return path

    return _add
