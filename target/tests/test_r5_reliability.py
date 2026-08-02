"""R5 — the service keeps serving redirects under failure and load.

Covers AC5.1, AC5.2, AC5.3, AC5.4, AC5.5.
"""

from __future__ import annotations

import logging
import time

import pytest

from conftest import (
    AVAILABILITY_TARGET,
    P95_BUDGET_SECONDS,
    REDIRECT_STATUS,
    TRICKY_URL,
    _ip_for,
)

# --------------------------------------------------------------------------- #
# AC5.1 — durability
# --------------------------------------------------------------------------- #


def test_created_link_survives_process_restart(client, create_link, fresh_app):
    """AC5.1 — a link that returned 201 still redirects after a restart.

    The target is re-imported from scratch against the same datastore, which is
    the closest in-process analogue of a restart: every module-level cache,
    in-memory dict and connection pool is rebuilt. A link held only in process
    memory disappears here, which is exactly what "creation persists before
    returning 201" promises it will not do.
    """
    from fastapi.testclient import TestClient

    created = create_link(TRICKY_URL)
    code = created["code"]

    restarted = fresh_app()

    with TestClient(
        restarted, client=(_ip_for("restart"), 41000), follow_redirects=False
    ) as after:
        response = after.get(f"/{code}")

        assert response.status_code == REDIRECT_STATUS, (
            f"the link did not survive a restart: {response.status_code} {response.text}"
        )
        assert response.headers["location"] == TRICKY_URL, (
            "the long URL must survive a restart byte-identically, not just in shape"
        )

        metadata = after.get(f"/api/links/{code}")
        assert metadata.status_code == 200
        assert metadata.json()["long_url"] == TRICKY_URL


# --------------------------------------------------------------------------- #
# AC5.2 — the analytics path fails open
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exc_factory",
    [
        pytest.param(None, id="documented-AnalyticsUnavailableError"),
        pytest.param(
            lambda: RuntimeError("recorder blew up in a way nobody documented"),
            id="undocumented-RuntimeError",
        ),
    ],
)
def test_redirect_succeeds_when_analytics_recording_fails(
    client, create_link, break_analytics_recorder, exc_factory
):
    """AC5.2 — a failing analytics path does not break or slow the redirect.

    The contract promises this twice over: `record_click` never touches the
    datastore inline, and `AnalyticsUnavailableError` "on the redirect path is
    logged and swallowed, never returned". With the recorder raising on every
    call, the redirect must still be a correct 301 inside the AC2.3 budget.

    Both the documented exception and an undocumented one are injected. An
    implementation that catches only `AnalyticsUnavailableError` still turns a
    surprise bug in the recorder into a 500 on the redirect path, which is the
    failure this criterion exists to prevent.
    """
    url = "https://example.com/fail-open"
    code = create_link(url)["code"]
    assert client.get(f"/{code}").status_code == REDIRECT_STATUS, (
        "sanity: the link redirects before any failure is injected"
    )

    break_analytics_recorder(exc_factory)

    samples = []
    for _ in range(20):
        start = time.perf_counter()
        response = client.get(f"/{code}")
        samples.append(time.perf_counter() - start)

        assert response.status_code == REDIRECT_STATUS, (
            f"a failing analytics recorder broke the redirect: "
            f"{response.status_code} {response.text}"
        )
        assert response.headers["location"] == url

    samples.sort()
    p95 = samples[int(len(samples) * 0.95)]
    assert p95 < P95_BUDGET_SECONDS, (
        f"redirects degraded to {p95 * 1000:.1f} ms p95 while analytics was failing, "
        f"outside the AC2.3 budget — the failure is not being absorbed, it is being "
        f"waited on"
    )


def test_analytics_failure_is_logged(client, create_link, break_analytics_recorder, caplog):
    """AC5.2 — "and the failure is logged".

    Failing open silently is the dangerous half of this criterion: the service
    looks perfectly healthy while dropping every click, and nobody finds out
    until an owner asks why their campaign shows zero traffic. The degradation
    must surface as a log record at WARNING or above.
    """
    code = create_link("https://example.com/logged")["code"]
    break_analytics_recorder()

    with caplog.at_level(logging.WARNING):
        response = client.get(f"/{code}")
        assert response.status_code == REDIRECT_STATUS

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, (
        "the analytics recorder failed on the redirect path and nothing was logged "
        "at WARNING or above — the service would drop clicks silently"
    )


# --------------------------------------------------------------------------- #
# AC5.3 — health
# --------------------------------------------------------------------------- #


def test_health_returns_200_while_the_datastore_is_reachable(client):
    """AC5.3 — /health is 200 while the datastore serving redirects is up.

    The body is main.HealthResponse: a verdict and the datastore reachability
    it rests on, which is `storage.ping()`.
    """
    response = client.get("/health")

    assert response.status_code == 200, (
        f"health should be 200 with a working datastore, got "
        f"{response.status_code}: {response.text}"
    )
    body = response.json()
    assert body["datastore"] is True, (
        f"health must report the dependency its verdict rests on, got {body!r}"
    )
    assert isinstance(body["status"], str) and body["status"], (
        f"health must report a status, got {body!r}"
    )


def test_health_stays_200_when_only_analytics_is_degraded(client, break_analytics_recorder):
    """AC5.3 — health tracks redirect-critical dependencies only.

    The design is explicit that health reports what redirects need and
    deliberately not the analytics recorder, because AC5.2 makes a failing
    recorder a *healthy* state for this service. A health endpoint that went
    red here would pull a still-serving instance out of the load balancer over
    a side path that is designed to fail open.
    """
    break_analytics_recorder()

    response = client.get("/health")

    assert response.status_code == 200, (
        f"a degraded analytics writer must not fail the health check, got "
        f"{response.status_code}: {response.text}"
    )


def test_health_returns_non_2xx_when_the_datastore_is_unreachable(
    client, break_datastore_ping
):
    """AC5.3 — "and a non-2xx status otherwise".

    The datastore is required to serve redirects, so when its reachability
    probe reports false the endpoint must report unhealthy. An endpoint
    hard-coded to `return {"status": "ok"}` passes the positive case and fails
    here — which is the only reason the positive case is worth anything.
    """
    break_datastore_ping()

    response = client.get("/health")

    assert not (200 <= response.status_code < 300), (
        f"health returned {response.status_code} while the datastore was "
        f"unreachable; it must be non-2xx"
    )


# --------------------------------------------------------------------------- #
# AC5.4 — rate limiting
# --------------------------------------------------------------------------- #

# Far above any limit sane for a service peaking at 100 rps (A3); the default
# in the contract's Settings is 120 requests per 60 s.
_BURST_CEILING = 1000


def test_client_exceeding_rate_limit_gets_429_with_retry_after(
    client, create_link, make_client
):
    """AC5.4 — an over-limit client gets 429 with a Retry-After header.

    The exact budget is configuration (`Settings.rate_limit_requests`), not a
    criterion, so this drives one IP hard enough that any sane limit must
    engage, and asserts the shape of the response when it does. `Retry-After`
    is the part a client actually needs: a 429 without it tells the caller to
    back off for an unknown length of time.
    """
    code = create_link("https://example.com/rate-limited")["code"]
    noisy = make_client(1)

    limited = None
    for _ in range(_BURST_CEILING):
        response = noisy.get(f"/{code}")
        if response.status_code == 429:
            limited = response
            break

    assert limited is not None, (
        f"{_BURST_CEILING} back-to-back requests from a single client IP were never "
        f"rate limited; AC5.4 requires a documented per-client limit"
    )

    retry_after = limited.headers.get("Retry-After")
    assert retry_after is not None, (
        f"a 429 must carry Retry-After, got headers {dict(limited.headers)!r}"
    )
    assert retry_after.strip(), "Retry-After must not be empty"
    assert float(retry_after) > 0, (
        f"Retry-After must be a positive number of seconds, got {retry_after!r}"
    )


def test_rate_limited_client_does_not_affect_other_clients(
    client, create_link, make_client
):
    """AC5.4 — "and other clients' requests are unaffected".

    Buckets are per key (the ratelimit module keys on client IP), so one noisy
    IP must not deny service to anyone else. A single global counter passes the
    test above and fails here, which is why both exist.
    """
    code = create_link("https://example.com/isolated")["code"]
    noisy = make_client(2)
    innocent = make_client(3)

    for _ in range(_BURST_CEILING):
        if noisy.get(f"/{code}").status_code == 429:
            break
    else:
        raise AssertionError(
            f"could not drive a client into rate limiting within {_BURST_CEILING} "
            f"requests, so isolation cannot be observed"
        )

    assert noisy.get(f"/{code}").status_code == 429, "the noisy client is still limited"

    for attempt in range(5):
        response = innocent.get(f"/{code}")
        assert response.status_code == REDIRECT_STATUS, (
            f"request {attempt} from an unrelated client IP was refused with "
            f"{response.status_code}; rate-limit buckets are not isolated per client"
        )


# --------------------------------------------------------------------------- #
# AC5.5 — sustained load
# --------------------------------------------------------------------------- #


def test_sustained_redirect_load_meets_availability_target(
    client, client_pool, create_link
):
    """AC5.5 — availability holds under sustained load, with no error growth.

    Scaled proxy, stated plainly: this is a bounded in-process run, not the
    10-minute soak the criterion describes, and it cannot certify 99.9% over
    that window. A real soak belongs in a load harness against a deployed
    instance, and its absence is a documented limitation of this suite.

    What it does catch is the failure mode a soak is actually run to find:
    degradation that accumulates. Comparing the first half against the second
    surfaces a leaking connection pool, an analytics queue that fills and never
    drains, or a rate-limit bucket that never refills — each of which is green
    on request one and red by request six hundred.
    """
    code = create_link("https://example.com/soak")["code"]

    for _ in range(20):  # warm up before measuring
        client.get(f"/{code}")

    requests = 600
    outcomes = []
    latencies = []
    for i in range(requests):
        pool_client = client_pool[i % len(client_pool)]
        start = time.perf_counter()
        response = pool_client.get(f"/{code}")
        latencies.append(time.perf_counter() - start)
        outcomes.append(response.status_code == REDIRECT_STATUS)

    success_rate = sum(outcomes) / len(outcomes)
    assert success_rate >= AVAILABILITY_TARGET, (
        f"successful-response rate {success_rate:.4%} is below the {AVAILABILITY_TARGET:.1%} "
        f"target from A9 ({outcomes.count(False)} of {requests} requests failed)"
    )

    half = requests // 2
    first_errors = outcomes[:half].count(False)
    second_errors = outcomes[half:].count(False)
    assert second_errors <= first_errors, (
        f"error rate grew over the run: {first_errors} failures in the first half, "
        f"{second_errors} in the second — the failure rate is not stable"
    )

    first_half = sorted(latencies[:half])
    second_half = sorted(latencies[half:])
    first_p95 = first_half[int(len(first_half) * 0.95)]
    second_p95 = second_half[int(len(second_half) * 0.95)]
    assert second_p95 < P95_BUDGET_SECONDS, (
        f"p95 latency drifted to {second_p95 * 1000:.1f} ms by the end of the run "
        f"(started at {first_p95 * 1000:.1f} ms), outside the AC2.3 budget"
    )
