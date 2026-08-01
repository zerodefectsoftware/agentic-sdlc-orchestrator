"""R3 — a client can look up and remove a short link it created.

Covers AC3.1, AC3.2, AC3.3.
"""

from __future__ import annotations

import pytest

from conftest import TRICKY_URL


def test_metadata_returns_long_url_created_at_and_click_count(client, create_link):
    """AC3.1 — metadata carries the long URL, creation time and click count."""
    created = create_link(TRICKY_URL)
    code = created["code"]

    response = client.get(f"/api/links/{code}")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["long_url"] == TRICKY_URL, "metadata must return the URL as stored"
    assert body["created_at"], "metadata must carry the creation timestamp"
    assert body["click_count"] == 0, (
        f"a link with no clicks reports zero, got {body['click_count']!r}"
    )
    assert "expires_at" in body, "metadata reports the expiry state of the link (E5)"


def test_metadata_click_count_reflects_recorded_clicks(
    client, create_link, click, wait_for_total
):
    """AC3.1 — "current total click count" tracks the redirects served.

    Recording is asynchronous (A6), so the count is settled against the same
    60 s freshness bound the analytics API is held to before it is read here.
    """
    code = create_link("https://example.com/counted")["code"]

    click(client, code, times=3)
    wait_for_total(client, code, 3)

    body = client.get(f"/api/links/{code}").json()
    assert body["click_count"] == 3, (
        f"metadata click count {body['click_count']} disagrees with the 3 redirects served"
    )


def test_delete_returns_204_and_code_no_longer_redirects(client, create_link):
    """AC3.2 — DELETE returns 204 and the code stops resolving.

    The criterion accepts 404 or 410; A7/E7 resolved it to a soft delete, so
    410 Gone is the expected member of that set.
    """
    code = create_link("https://example.com/doomed")["code"]
    assert client.get(f"/{code}").status_code == 301, "link should be live before delete"

    response = client.delete(f"/api/links/{code}")

    assert response.status_code == 204, (
        f"delete should return 204, got {response.status_code}: {response.text}"
    )
    assert not response.content, "204 carries no body"

    after = client.get(f"/{code}")
    assert after.status_code in (404, 410), (
        f"a deleted code must stop redirecting, got {after.status_code}"
    )
    assert after.status_code == 410, (
        f"A7/E7 resolved delete to a soft delete returning 410 Gone, got {after.status_code}"
    )


def test_deleted_link_stops_serving_but_keeps_its_history(
    client, create_link, click, wait_for_total
):
    """AC3.2 — the soft delete retires the code without destroying analytics.

    E7 is explicit that previously collected analytics stay readable, which is
    the whole reason delete is soft. Asserting only the 410 would let an
    implementation hard-delete the row and still pass.
    """
    code = create_link("https://example.com/history")["code"]
    click(client, code, times=2)
    wait_for_total(client, code, 2)

    assert client.delete(f"/api/links/{code}").status_code == 204
    assert client.get(f"/{code}").status_code == 410

    analytics = client.get(f"/api/links/{code}/analytics")
    assert analytics.status_code == 200, (
        "analytics collected before the delete stay readable (E7), got "
        f"{analytics.status_code}"
    )


@pytest.mark.parametrize("code", ["Nn0Pp1Q", "aaaaaaa", "ZZZZZZZ"])
def test_unknown_code_metadata_returns_404(client, code):
    """AC3.3 — GET on a code that does not exist is 404."""
    response = client.get(f"/api/links/{code}")

    assert response.status_code == 404, (
        f"metadata for unissued {code!r} should be 404, got {response.status_code}"
    )


@pytest.mark.parametrize("code", ["Rr2Ss3T", "bbbbbbb", "YYYYYYY"])
def test_unknown_code_delete_returns_404(client, code):
    """AC3.3 — DELETE on a code that does not exist is 404."""
    response = client.delete(f"/api/links/{code}")

    assert response.status_code == 404, (
        f"deleting unissued {code!r} should be 404, got {response.status_code}"
    )
