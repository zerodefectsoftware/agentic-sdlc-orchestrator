"""R3 — a client can look up and remove a short link it created.

Covers AC3.1, AC3.2, AC3.3.
"""

from __future__ import annotations

import pytest

from conftest import REDIRECT_STATUS, TRICKY_URL, assert_error_envelope


def test_metadata_returns_long_url_created_at_and_total_clicks(client, create_link):
    """AC3.1 — metadata carries the long URL, creation time and click count.

    The body is main.LinkDetailResponse: everything in LinkResponse plus
    `total_clicks` and `deleted_at`. The count is spelled `total_clicks` here
    and in AnalyticsResponse — one name for one number, across both endpoints.
    """
    created = create_link(TRICKY_URL)
    code = created["code"]

    response = client.get(f"/api/links/{code}")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["long_url"] == TRICKY_URL, "metadata must return the URL as stored"
    assert body["created_at"], "metadata must carry the creation timestamp"
    assert body["total_clicks"] == 0, (
        f"a link with no clicks reports zero, got {body['total_clicks']!r}"
    )
    assert body["code"] == code
    assert "expires_at" in body, "metadata reports the expiry state of the link"
    assert body["deleted_at"] is None, "a live link is not deleted"


def test_metadata_total_clicks_reflects_recorded_clicks(
    client, create_link, click, wait_for_total
):
    """AC3.1 — "current total click count" tracks the redirects served.

    Recording is asynchronous (A6), so the count is settled against the same
    60 s freshness bound the analytics API is held to before it is read here.
    The two endpoints compose `links` and `analytics`, which know nothing about
    each other, so this is where a disagreement between them would surface.
    """
    code = create_link("https://example.com/counted")["code"]

    click(client, code, times=3)
    wait_for_total(client, code, 3)

    body = client.get(f"/api/links/{code}").json()
    assert body["total_clicks"] == 3, (
        f"metadata total_clicks {body['total_clicks']} disagrees with the 3 redirects "
        f"served, which the analytics endpoint has already settled at 3"
    )


def test_delete_returns_204_and_code_no_longer_redirects(client, create_link):
    """AC3.2 — DELETE returns 204 and the code stops resolving.

    The criterion accepts 404 or 410; A7 resolved it to a soft delete, so 410
    Gone is the expected member of that set — the code *was* issued, and the
    contract keeps NotFoundError and LinkGoneError distinct for exactly this.
    """
    code = create_link("https://example.com/doomed")["code"]
    assert client.get(f"/{code}").status_code == REDIRECT_STATUS, (
        "the link should be live before it is deleted"
    )

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
        f"A7 resolved delete to a soft delete returning 410 Gone, got {after.status_code}"
    )


def test_repeated_delete_is_idempotent(client, create_link):
    """AC3.2 — a retried DELETE still answers 204.

    The contract is explicit that deleting an already-retired code is a no-op,
    which is what makes the endpoint safe to retry. The trap is an
    implementation that reads "the code is gone" and raises NotFoundError on
    the second call — turning a retry into a spurious 404.
    """
    code = create_link("https://example.com/deleted-twice")["code"]

    assert client.delete(f"/api/links/{code}").status_code == 204

    second = client.delete(f"/api/links/{code}")
    assert second.status_code == 204, (
        f"a repeated DELETE of an already-retired code must still be 204, got "
        f"{second.status_code}: {second.text}"
    )
    assert client.get(f"/{code}").status_code == 410, (
        "the code stays retired after a repeated delete"
    )


def test_deleted_link_stops_serving_but_keeps_its_history(
    client, create_link, click, wait_for_total
):
    """AC3.2 — the soft delete retires the code without destroying analytics.

    A7 is explicit that previously collected analytics stay readable, which is
    the whole reason delete is soft. Asserting only the 410 would let an
    implementation hard-delete the row and still pass, losing the history the
    owner was promised.
    """
    code = create_link("https://example.com/history")["code"]
    click(client, code, times=2)
    wait_for_total(client, code, 2)

    assert client.delete(f"/api/links/{code}").status_code == 204
    assert client.get(f"/{code}").status_code == 410

    metadata = client.get(f"/api/links/{code}")
    assert metadata.status_code == 200, (
        f"a retired link's metadata stays readable, got {metadata.status_code}"
    )
    assert metadata.json()["deleted_at"] is not None, (
        "metadata must report that the link is retired"
    )

    settled = wait_for_total(client, code, 2)
    assert settled["total_clicks"] == 2, (
        f"analytics collected before the delete must survive it, got {settled!r}"
    )


@pytest.mark.parametrize("code", ["Nn0Pp1Q", "aaaaaaa", "ZZZZZZZ"])
def test_unknown_code_metadata_returns_404(client, code):
    """AC3.3 — GET on a code that does not exist is 404."""
    response = client.get(f"/api/links/{code}")

    assert response.status_code == 404, (
        f"metadata for unissued {code!r} should be 404, got {response.status_code}"
    )
    assert_error_envelope(response, f"metadata for unissued {code!r}")


@pytest.mark.parametrize("code", ["Rr2Ss3T", "bbbbbbb", "YYYYYYY"])
def test_unknown_code_delete_returns_404(client, code):
    """AC3.3 — DELETE on a code that does not exist is 404.

    This is the boundary against the idempotency above: never-issued is 404,
    issued-and-retired is 204, and an implementation that collapses the two
    fails one of these two tests whichever way it collapses them.
    """
    response = client.delete(f"/api/links/{code}")

    assert response.status_code == 404, (
        f"deleting unissued {code!r} should be 404, got {response.status_code}"
    )
    assert_error_envelope(response, f"deleting unissued {code!r}")
