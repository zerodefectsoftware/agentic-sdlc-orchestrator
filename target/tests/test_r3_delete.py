"""R3 — an operator can delete a short URL so that it no longer resolves.

Each test names the acceptance criterion it covers as ``Covers: ACx.y``.
"""

from __future__ import annotations

UNISSUED_CODE = "Qw7Lm2B"


def test_deleting_a_live_link_returns_204_and_stops_resolution(client, create):
    """Covers: AC3.1."""
    link = create("https://example.com/delete-me")
    assert client.get(f"/{link['code']}").status_code == 302, "should resolve before deletion"

    response = client.delete(f"/links/{link['code']}")

    assert response.status_code == 204, response.text
    assert response.content in (b"", None), "204 must carry no body"
    after = client.get(f"/{link['code']}")
    assert after.status_code != 302, "a deleted link still redirects"
    assert after.status_code == 404, after.text


def test_deleting_a_code_that_does_not_exist_returns_404(client):
    """Covers: AC3.2."""
    response = client.delete(f"/links/{UNISSUED_CODE}")

    assert response.status_code == 404, response.text


def test_deleting_twice_is_idempotent_in_effect(client, create):
    """Covers: AC3.3 — the criterion defines the second delete by reference to the
    delete of a non-existent code, so both are measured here rather than assumed."""
    link = create("https://example.com/delete-twice")
    unknown = client.delete(f"/links/{UNISSUED_CODE}").status_code

    first = client.delete(f"/links/{link['code']}")
    second = client.delete(f"/links/{link['code']}")

    assert first.status_code == 204, first.text
    assert second.status_code == unknown, (
        f"second delete returned {second.status_code}; deleting an unknown code "
        f"returns {unknown}. AC3.3 requires them to agree."
    )
    assert client.get(f"/{link['code']}").status_code == 404
