"""R5 — the service keeps serving redirects under failure and load.

Covers AC5.1, AC5.2, AC5.3, AC5.4, AC5.5.
"""

from __future__ import annotations

import logging
import time

from conftest import AVAILABILITY_TARGET, P95_BUDGET_SECONDS, TRICKY_URL, _ip_for

# --------------------------------------------------------------------------- #
# AC5.1 — durability
# --------------------------------------------------------------------------- #


def test_created_link_survives_process_restart(client, create_link, fresh_app):
    """AC5.1 — a link that returned 201 still redirects after a restart.

    The target is re-imported from scratch against the same datastore, which
    is the closest in-process analogue of a restart: every module-level cache,
    in-memory dict and connection pool is rebuilt. A link held only in process
    memory disappears here, which is exactly what E15's "commits durably
    before returning 201" promises it will not do.
    """
    from fastapi.testclient import TestClient

    created = create_link(TRICKY_URL)
    code = created["code"]

    restarted = fresh_app()

    with TestClient(
        restarted, client=(_ip_for("restart"), 41000), follow_redirects=False
    ) as after:
        response = after.get(f"/{code}")

        assert response.status_code == 301, (
            f"the link did not survive a restart: {response.status_code} {response.text}"
        )
        assert response.headers["location"] == TRICKY_URL

        metadata = after.get(f"/api/links/{code}")
        assert metadata.status_code == 200
        assert metadata.json()["long_url"] == TRICKY_URL


# --------------------------------------------------------------------------- #
# AC5.2 — the analytics path fails open
# --------------------------------------------------------------------------- #


def test_redirect_succeeds_when_analytics_recording_fails(
    client, create_link, break_analytics_recorder
):
    """AC5.2 — a failing analytics path does not break or slow the redirect.

    E10 and E22 both promise this: the redirect reads the link and enqueues,
    and every non-essential dependency on that path fails open. With the
    recorder raising on every call, the redirect must still be a correct 301
    inside the AC2.3 latency budget.
    """
    code = create_link("https://example.com/fail-open")["code"]
    assert client.get(f"/{code}").status_code == 301, "sanity: live before injection"

    break_analytics_recorder()

    samples = []
    for _ in range(20):
        start = time.perf_counter()
        response = client.get(f"/{code}")
        samples.append(time.perf_counter() - start)

        assert response.status_code == 301, (
            f"a failing analytics recorder broke the redirect: "
            f"{response.status_code} {response.text}"
        )
        assert response.headers["location"] == "https://example.com/fail-open"

    samples.sort()
    p95 = samples[int(len(samples) * 0.95)]
    assert p95 < P95_BUDGET_SECONDS, (
        f"redirects degraded to {p95 * 1000:.1f} ms p95 while analytics was failing, "
        f"outside the AC2.3 budget — the failure is not being absorbed"
    )


def test_analytics_failure_is_logged(client, create_link, break_analytics_recorder, caplog):
    """AC5.2 — "and the failure is logged".

    Failing open silently is the dangerous half of this criterion: the service
    looks healthy while dropping every click. E10 requires the degradation to
    surface as a log line.
    """
    code = create_link("https://example.com/logged")["code"]
    break_analytics_recorder()

    with caplog.at_level(logging.WARNING):
        response = client.get(f"/{code}")
        assert response.status_code == 301

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, (
        "the analytics recorder failed on the redirect path and nothing was logged "
        "at WARNING or above — the service would drop clicks silently"
    )


# --------------------------------------------------------------------------- #
# AC5.3 — health
# --------------------------------------------------------------------------- #


def test_health_returns_200_while_dependencies_are_reachable(client):
    """AC5.3 — healthz is 200 while the datastore serving redirects is up."""
    response = client.get("/healthz")

    assert response.status_code == 200, (
        f"health should be 200 with a working datastore, got "
        f"{response.status_code}: {response.text}"
    )


def test_health_stays_200_when_only_analytics_is_degraded(
    client, break_analytics_recorder
):
    """AC5.3 — health tracks redirect-critical dependencies only.

    E12 is explicit that analytics-writer health is reported but does not fail
    the check. A health endpoint that goes red here would take a still-serving
    instance out of the load balancer over a degraded side path.
    """
    break_analytics_recorder()

    response = client.get("/healthz")

    assert response.status_code == 200, (
        f"a degraded analytics writer must not fail the health check (E12), got "
        f"{response.status_code}: {response.text}"
    )


def test_health_returns_non_2xx_when_the_datastore_is_unreachable(
    client, break_datastore_ping
):
    """AC5.3 — "and a non-2xx status otherwise".

    The datastore is required to serve redirects, so when its reachability
    check fails the endpoint must report unhealthy. An endpoint hard-coded to
    `return {"status": "ok"}` passes the positive case and fails here, which
    is the only reason the positive case is worth anything.
    """
    break_datastore_ping()

    response = client.get("/healthz")

    assert not (200 <= response.status_code < 300), (
        f"health returned {response.status_code} while the datastore was "
        f"unreachable; it must be non-2xx"
    )


# --------------------------------------------------------------------------- #
# AC5.4 — rate limiting
# --------------------------------------------------------------------------- #

_BURST_CEILING = 1000


def test_client_exceeding_rate_limit_gets_429_with_retry_after(
    client, create_link, make_client
):
    """AC5.4 — an over-limit client gets 429 with a Retry-After header.

    The documented rate is the implementation's to choose (E13 fixes the
    mechanism, not the number), so this drives one IP hard enough that any
    limit sane for a service peaking at 100 rps (A3) must engage, and asserts
    the shape of the response when it does.
    """
    code = create_link("https://example.com/rate-limited")["code"]
    noisy = make_client(1)

    limited = None
    for i in range(_BURST_CEILING):
        response = noisy.get(f"/{code}")
        if response.status_code == 429:
            limited = response
            break

    assert limited is not None, (
        f"{_BURST_CEILING} back-to-back requests from a single client IP were never "
        f"rate limited; E13 requires a documented per-client limit"
    )

    retry_after = limited.headers.get("Retry-After") or limited.headers.get("retry-after")
    assert retry_after is not None, (
        f"a 429 must carry Retry-After, got headers {dict(limited.headers)!r}"
    )
    assert retry_after.strip(), "Retry-After must not be empty"


def test_rate_limited_client_does_not_affect_other_clients(
    client, create_link, make_client
):
    """AC5.4 — "and other clients' requests are unaffected".

    Buckets are per key (E13), so one noisy IP must not deny service to
    anyone else. A single global counter passes the test above and fails here.
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
        assert response.status_code == 301, (
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
    instance and is a documented limitation of this suite.

    What it does catch is the failure mode a soak is actually run to find:
    degradation that accumulates. Comparing the first half against the second
    half surfaces a leaking connection pool, an unbounded in-memory queue or a
    rate-limit bucket that never refills — each of which is green on request
    one and red by request six hundred.
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
        outcomes.append(response.status_code == 301)

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
