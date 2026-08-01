"""R5 — the service stays available and behaves predictably under load and partial failure.

Two criteria here are about scale and infrastructure that a functional suite
cannot reproduce honestly, and each says so in its docstring: AC5.1 is measured
in-process rather than at 50 rps for five minutes, and AC5.5 replaces an
instance rather than terminating a process behind a load balancer. Both still
fail for the right reason — a redirect that got slow, or state that lived in the
instance instead of the datastore.

Each test names the acceptance criterion it covers as ``Covers: ACx.y``.
"""

from __future__ import annotations

import time

REDIRECT_SAMPLES = 100
P95_BUDGET_S = 0.100
DEPENDENCY_WORDS = ("database", "datastore", "storage", "sqlite", "db")


def percentile_95(samples):
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(round(0.95 * len(ordered))) - 1)]


def test_redirects_stay_fast_and_error_free_under_sustained_traffic(client, create):
    """Covers: AC5.1.

    A proxy for the stated 50 rps / 5-minute window: the same p95 budget and
    error budget, applied to a burst of redirects through the real ASGI app.
    It cannot prove the service holds up at that rate on real hardware; it does
    catch the thing that would break the budget first — per-redirect work that
    is not a primary-key lookup and an enqueue (E16, E26).
    """
    link = create("https://example.com/hot-path")
    durations = []
    failures = 0

    for _ in range(REDIRECT_SAMPLES):
        started = time.perf_counter()
        response = client.get(f"/{link['code']}")
        durations.append(time.perf_counter() - started)
        if response.status_code != 302:
            failures += 1

    assert failures == 0, f"{failures}/{REDIRECT_SAMPLES} redirects failed"
    p95 = percentile_95(durations)
    assert p95 < P95_BUDGET_S, f"p95 redirect latency was {p95 * 1000:.1f} ms, budget 100 ms"


def test_redirect_survives_an_unavailable_analytics_path(client, create, break_analytics):
    """Covers: AC5.2 — E25: analytics loss is tolerated, redirect failure is not."""
    url = "https://example.com/still-redirects"
    link = create(url)
    break_analytics()

    response = client.get(f"/{link['code']}")

    assert response.status_code == 302, (
        f"the redirect returned {response.status_code} because analytics was down"
    )
    assert response.headers["location"] == url


def test_health_is_200_when_dependencies_are_reachable(client):
    """Covers: AC5.3."""
    response = client.get("/health")

    assert response.status_code == 200, response.text


def test_health_reports_the_dependency_it_cannot_reach(client, break_datastore_probe):
    """Covers: AC5.3 — E19: non-2xx, and it names what failed."""
    break_datastore_probe()

    response = client.get("/health")

    assert not 200 <= response.status_code < 300, (
        f"health returned {response.status_code} with the datastore unreachable"
    )
    assert response.status_code == 503, response.text
    body = response.text.lower()
    assert any(word in body for word in DEPENDENCY_WORDS), (
        f"the failing dependency is not named in the health body: {response.text!r}"
    )


def test_unhandled_failure_returns_a_structured_5xx_with_a_correlation_id(
    make_app, make_client, failing_route
):
    """Covers: AC5.4 — E20."""
    app = make_app()
    path = failing_route(app)

    with make_client(app) as client:
        response = client.get(path)

    assert 500 <= response.status_code < 600, f"expected a 5xx, got {response.status_code}"
    body = response.json()
    assert isinstance(body, dict), f"error body is not an object: {body!r}"
    assert isinstance(body.get("error"), str) and body["error"], f"no error code in {body!r}"
    assert isinstance(body.get("message"), str) and body["message"], f"no message in {body!r}"
    assert isinstance(body.get("correlation_id"), str) and body["correlation_id"], (
        f"no correlation id in {body!r} — the log line cannot be found without it"
    )


def test_the_error_body_leaks_no_internal_detail(make_app, make_client, failing_route):
    """Covers: AC5.4 — E20: a stack trace in a response is an information disclosure,
    and the criterion rules it out explicitly."""
    app = make_app()
    path = failing_route(app)

    with make_client(app) as client:
        response = client.get(path)

    leaked = response.text.lower()
    for fragment in ("traceback", "runtimeerror", "injected unhandled failure", ".py", 'file "'):
        assert fragment not in leaked, f"the error body exposes {fragment!r}: {response.text!r}"


def test_codes_survive_the_replacement_of_an_instance(make_app, make_client):
    """Covers: AC5.5.

    A proxy for terminating one instance behind a load balancer: an instance is
    stood up, drained through its own shutdown, and replaced by a second one over
    the same datastore (E23). What it really tests is that no link state lives in
    the process — which is the property that makes the real termination safe.
    """
    first = make_app()
    with make_client(first) as client:
        created = client.post("/links", json={"url": "https://example.com/survivor"})
        assert created.status_code == 201, created.text
        code = created.json()["code"]
        in_flight = [client.get(f"/{code}").status_code for _ in range(5)]

    assert all(status < 500 for status in in_flight), (
        f"requests failed while the instance was serving: {in_flight}"
    )

    second = make_app()
    with make_client(second) as client:
        resolved = client.get(f"/{code}")
        health = client.get("/health")

    assert resolved.status_code == 302, (
        f"a code created before the instance was replaced no longer resolves "
        f"({resolved.status_code})"
    )
    assert resolved.headers["location"] == "https://example.com/survivor"
    assert health.status_code == 200


def test_create_is_rate_limited_per_client(make_app, make_client):
    """Covers: no acceptance criterion — E21/A9 is a design element with no criterion
    of its own, and an unauthenticated create path with no quota is what exhausts the
    keyspace. Asserted here because the design commits to 429 with Retry-After.
    """
    app = make_app(rate_limit=3, window_seconds=60)

    with make_client(app) as client:
        responses = [
            client.post("/links", json={"url": f"https://example.com/burst/{index}"})
            for index in range(6)
        ]
    statuses = [response.status_code for response in responses]

    assert statuses[:3] == [201, 201, 201], f"the first 3 creates were rejected: {statuses}"
    assert 429 in statuses, f"6 creates against a limit of 3 produced {statuses}"
    throttled = next(response for response in responses if response.status_code == 429)
    assert throttled.headers.get("retry-after", "").strip(), (
        "429 carries no Retry-After, so a client cannot know when to come back"
    )
