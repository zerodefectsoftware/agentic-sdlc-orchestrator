"""R2 — visiting a short URL sends the visitor to the original long URL.

Each test names the acceptance criterion it covers as ``Covers: ACx.y``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

UNISSUED_CODE = "aZ3kQ9x"
EXPIRY_SECONDS = 2


def in_a_moment(seconds=EXPIRY_SECONDS):
    """An absolute expiry just far enough ahead to create against, then outlive."""
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def assert_no_redirect(response):
    assert "location" not in {k.lower() for k in response.headers}, (
        f"a non-resolving code still returned Location: {response.headers.get('location')!r}"
    )


def test_existing_code_redirects_to_the_stored_url(client, create):
    """Covers: AC2.1 — 302 (E8/A8) with the byte-exact stored URL."""
    url = "https://example.com/deep/path?a=1&b=two#frag"
    link = create(url)

    response = client.get(f"/{link['code']}")

    assert response.status_code == 302, response.text
    assert response.headers["location"] == url


def test_redirect_is_not_cached(client, create):
    """Covers: AC2.1 — E8: caching would hide repeat visits and outlive a delete."""
    link = create("https://example.com/uncached")

    response = client.get(f"/{link['code']}")

    assert response.status_code == 302
    assert "no-store" in response.headers.get("cache-control", "").lower(), (
        f"Cache-Control was {response.headers.get('cache-control')!r}"
    )
    assert "no-cache" in response.headers.get("pragma", "").lower(), (
        f"Pragma was {response.headers.get('pragma')!r}"
    )


def test_code_that_was_never_issued_returns_404(client):
    """Covers: AC2.2 — E10."""
    response = client.get(f"/{UNISSUED_CODE}")

    assert response.status_code == 404, response.text
    assert_no_redirect(response)


def test_deleted_code_returns_404_and_does_not_redirect(client, create):
    """Covers: AC2.3 — E10 fixes deleted at 404: the link is gone, and its prior
    existence is not disclosed."""
    link = create("https://example.com/to-be-deleted")
    assert client.delete(f"/links/{link['code']}").status_code == 204

    response = client.get(f"/{link['code']}")

    assert response.status_code == 404, response.text
    assert_no_redirect(response)


def test_expired_code_returns_410_and_does_not_redirect(client, create):
    """Covers: AC2.3 — E10/E22 fix expired at 410: the code was real, its lifetime ended."""
    link = create("https://example.com/short-lived", expires_at=in_a_moment())
    assert client.get(f"/{link['code']}").status_code == 302, "should resolve before it lapses"

    time.sleep(EXPIRY_SECONDS + 0.5)
    response = client.get(f"/{link['code']}")

    assert response.status_code == 410, response.text
    assert_no_redirect(response)


def test_the_404_and_410_convention_holds_on_the_metadata_endpoint(client, create):
    """Covers: AC2.3 — the criterion asks for consistency, so E13 applies the same
    convention: expired is 410, deleted is 404."""
    expiring = create("https://example.com/metadata-expiring", expires_at=in_a_moment())
    deleted = create("https://example.com/metadata-deleted")
    assert client.delete(f"/links/{deleted['code']}").status_code == 204

    time.sleep(EXPIRY_SECONDS + 0.5)

    assert client.get(f"/links/{expiring['code']}").status_code == 410
    assert client.get(f"/links/{deleted['code']}").status_code == 404


def test_a_case_variant_of_a_code_is_a_different_code(client, create):
    """Covers: AC2.4 — A13/E9: codes are case-sensitive, so a case variant is simply
    unknown, and says so identically on every call."""
    for index in range(20):
        link = create(f"https://example.com/case/{index}")
        variant = next(
            (
                link["code"][:i] + c.swapcase() + link["code"][i + 1 :]
                for i, c in enumerate(link["code"])
                if c.isalpha()
            ),
            None,
        )
        if variant is not None:
            break
    assert variant is not None, "20 codes with no letter in any of them — check the alphabet"
    assert variant != link["code"]

    first = client.get(f"/{variant}")
    second = client.get(f"/{variant}")

    assert first.status_code == 404, (
        f"{variant!r} is a case variant of {link['code']!r} and must not resolve"
    )
    assert second.status_code == first.status_code, "resolution of a code is not deterministic"
    assert_no_redirect(first)
