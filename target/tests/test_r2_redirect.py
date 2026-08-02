"""R2 — visiting a short URL sends the visitor to the original long URL.

Covers AC2.1, AC2.2, AC2.3.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from conftest import (
    P95_BUDGET_SECONDS,
    REDIRECT_STATUS,
    TRICKY_URL,
    assert_error_envelope,
    read_total,
)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://example.com/plain", id="plain"),
        pytest.param(TRICKY_URL, id="query-and-fragment"),
        pytest.param("https://example.com/trailing/", id="trailing-slash"),
        pytest.param("http://example.com:8080/port?b=2&a=1", id="port-and-param-order"),
        pytest.param("https://example.com/%E2%9C%93/percent", id="percent-encoded"),
        pytest.param("https://example.com/path#frag?not=query", id="fragment-before-query"),
    ],
)
def test_redirect_location_is_byte_identical_to_stored_url(client, create_link, url):
    """AC2.1 — the Location header is byte-identical to the stored long URL.

    Each case is a normalisation a URL library will happily perform for you:
    dropping a trailing slash, sorting query parameters, decoding percent
    escapes, stripping a fragment. Any of them breaks "byte-identical", which
    is why the contract says the long URL is stored and returned exactly as
    submitted.
    """
    body = create_link(url)

    response = client.get(f"/{body['code']}")

    assert 300 <= response.status_code < 400, (
        f"expected an HTTP redirect, got {response.status_code}: {response.text}"
    )
    assert response.headers["location"] == url, (
        f"Location was normalised: stored {url!r}, returned "
        f"{response.headers['location']!r}"
    )


def test_redirect_uses_301_permanent(client, create_link):
    """AC2.1 — the redirect is 301 Moved Permanently.

    A1 was resolved to 301 by a human, with the consequence recorded: caches
    absorb repeat visits, so click counts under-report. That trade-off was
    accepted deliberately, which makes the status a contract rather than an
    implementation choice — `main.REDIRECT_STATUS` names it once.
    """
    body = create_link("https://example.com/permanent")

    response = client.get(f"/{body['code']}")

    assert response.status_code == REDIRECT_STATUS, (
        f"A1 fixed the redirect at {REDIRECT_STATUS}, got {response.status_code}"
    )


def test_expired_code_returns_410(client, create_link):
    """AC2.1 — an expired code no longer resolves; it is 410 Gone (A4).

    Expiry is evaluated at redirect time, so the code must stop resolving on
    its own without a sweeper having run. 410 rather than 404 because the code
    *was* issued — the distinction the contract draws between NotFoundError and
    LinkGoneError.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    body = create_link("https://example.com/expires", expires_at=expires_at.isoformat())
    code = body["code"]

    assert client.get(f"/{code}").status_code == REDIRECT_STATUS, (
        "the link should be live before its expiry passes"
    )

    time.sleep(1.5)

    response = client.get(f"/{code}")
    assert response.status_code == 410, (
        f"an expired code must be 410 Gone, got {response.status_code}"
    )


def test_unknown_code_returns_404(client):
    """AC2.2 — a short code that was never issued resolves to 404."""
    response = client.get("/Zz9Qx7A")

    assert response.status_code == 404, (
        f"a never-issued code must be 404, got {response.status_code}: {response.text}"
    )


def test_unknown_code_returns_the_error_envelope(client):
    """AC2.2 — the 404 is the documented envelope, not an ad-hoc body.

    A13 fixed one shape for every non-2xx response; the redirect path is the
    one most likely to grow its own, because it is the only route that is not
    under /api.
    """
    response = client.get("/Qq8Rr7S")

    assert response.status_code == 404
    assert_error_envelope(response, "a redirect to an unissued code")


def test_unknown_code_records_no_analytics_event(client, create_link, wait_for_total):
    """AC2.2 — "and no analytics event is recorded".

    Two observable consequences: the unknown code discloses no analytics of its
    own, and a real link's count is untouched by traffic to codes that do not
    exist. Without the second assertion, an implementation that records a click
    before resolving the code would pass.
    """
    code = create_link("https://example.com/untouched")["code"]

    client.get(f"/{code}")
    baseline = read_total(wait_for_total(client, code, 1))

    for unknown in ("Aa0Bb1C", "Dd2Ee3F", "Gg4Hh5I"):
        assert client.get(f"/{unknown}").status_code == 404
        assert client.get(f"/api/links/{unknown}/analytics").status_code in (403, 404), (
            "an unknown code must not expose an analytics record"
        )

    # Give any (incorrectly) enqueued event time to drain before re-reading.
    time.sleep(1.0)
    after = read_total(wait_for_total(client, code, 1))
    assert after == baseline == 1, (
        f"clicks on unknown codes leaked into {code!r}: {baseline} -> {after}"
    )


def test_redirect_p95_latency_under_budget(client, client_pool, create_link):
    """AC2.3 — p95 redirect latency is under 100 ms.

    Scaled proxy, and worth being explicit about the gap: this measures the
    in-process ASGI boundary over a burst, not a deployed service under a
    sustained 50 rps. It cannot certify the production number, and that is a
    stated limitation of this suite.

    It can still fail for the right reason, which is why it earns its place:
    the design puts click recording off the redirect path behind a bounded
    queue precisely to protect this budget. An implementation that writes the
    click synchronously, or that resolves the code with a table scan instead of
    the primary-key lookup the schema assumes, blows the budget here long
    before it reaches a load generator.
    """
    code = create_link("https://example.com/latency")["code"]

    for _ in range(20):  # warm up connections, lazy imports and query compilation
        client.get(f"/{code}")

    samples = []
    for i in range(200):
        pool_client = client_pool[i % len(client_pool)]
        start = time.perf_counter()
        response = pool_client.get(f"/{code}")
        samples.append(time.perf_counter() - start)
        assert response.status_code == REDIRECT_STATUS, (
            f"redirect {i} failed under load: {response.status_code} {response.text}"
        )

    samples.sort()
    p95 = samples[int(len(samples) * 0.95)]
    assert p95 < P95_BUDGET_SECONDS, (
        f"p95 redirect latency {p95 * 1000:.1f} ms exceeds the 100 ms budget "
        f"(median {samples[len(samples) // 2] * 1000:.1f} ms, "
        f"max {samples[-1] * 1000:.1f} ms)"
    )
