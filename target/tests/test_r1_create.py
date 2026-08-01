"""R1 — a client can submit a long URL and receive a unique short URL.

Each test names the acceptance criterion it covers as ``Covers: ACx.y``.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

CODE_PATTERN = re.compile(r"^[0-9A-Za-z]{7}$")
ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")

# E3/A12: 2048 characters is the accepted maximum, so 2048 is legal and 2049 is not.
PREFIX = "https://example.com/"
URL_AT_LIMIT = PREFIX + "a" * (2048 - len(PREFIX))
URL_OVER_LIMIT = PREFIX + "a" * (2049 - len(PREFIX))


def parse_timestamp(value):
    """Accept either offset or trailing-Z ISO 8601; reject anything else."""
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def assert_structured_error(response):
    """E4: a rejection is a machine-readable body, not prose."""
    body = response.json()
    assert isinstance(body, dict), f"error body is not an object: {body!r}"
    assert isinstance(body.get("error"), str) and body["error"], f"no error code in {body!r}"
    assert ERROR_CODE_PATTERN.match(body["error"]), (
        f"error code {body['error']!r} is not machine-readable "
        f"(expected lower-case, no whitespace, e.g. 'url_scheme_unsupported')"
    )
    assert isinstance(body.get("message"), str) and body["message"], f"no message in {body!r}"
    assert body.get("correlation_id"), f"no correlation id in {body!r}"
    return body


def assert_no_code_issued(response):
    """AC1.3/AC1.4: a rejected request must not consume keyspace."""
    body = response.json()
    assert "code" not in body, f"a rejected create still returned a code: {body!r}"
    assert "short_url" not in body, f"a rejected create still returned a short url: {body!r}"


def test_valid_url_is_accepted_with_short_url_code_and_original(client):
    """Covers: AC1.1."""
    response = client.post("/links", json={"url": "https://example.com/docs/page"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["long_url"] == "https://example.com/docs/page"
    assert isinstance(body["code"], str) and body["code"]
    assert isinstance(body["short_url"], str)
    assert body["short_url"].startswith(("http://", "https://")), body["short_url"]
    assert body["short_url"].endswith(body["code"]), (
        f"short_url {body['short_url']!r} does not end in its code {body['code']!r}"
    )


def test_created_code_is_seven_base62_characters(client):
    """Covers: AC1.1 — E5 fixes the code shape at 7 case-sensitive Base62 chars."""
    code = client.post("/links", json={"url": "https://example.com/one"}).json()["code"]

    assert CODE_PATTERN.match(code), f"code {code!r} is not 7 Base62 characters"


def test_creation_response_carries_timestamps(client):
    """Covers: AC1.1 — E1 names created_at and expires_at in the response body."""
    body = client.post("/links", json={"url": "https://example.com/two"}).json()

    assert parse_timestamp(body["created_at"]) is not None
    assert "expires_at" in body, f"expires_at missing from create response: {body!r}"
    assert body["expires_at"] is None, "no expiry was requested, so none should be reported"


def test_two_different_urls_get_two_different_codes(client):
    """Covers: AC1.2."""
    first = client.post("/links", json={"url": "https://example.com/first"})
    second = client.post("/links", json={"url": "https://example.com/second"})

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["code"] != second.json()["code"]


def test_the_same_url_twice_gets_two_different_codes(client):
    """Covers: AC1.2 — E6/A6: no deduplication, so each create mints a fresh code."""
    url = "https://example.com/repeated"
    first = client.post("/links", json={"url": url})
    second = client.post("/links", json={"url": url})

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["code"] != second.json()["code"]


def test_url_at_the_length_limit_is_accepted(client):
    """Covers: AC1.3 — the boundary: 2048 characters is inside the limit (E3)."""
    response = client.post("/links", json={"url": URL_AT_LIMIT})

    assert len(URL_AT_LIMIT) == 2048
    assert response.status_code == 201, response.text
    assert response.json()["long_url"] == URL_AT_LIMIT


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("field missing", {}),
        ("empty string", {"url": ""}),
        ("null", {"url": None}),
        ("not a string", {"url": 12345}),
        ("list", {"url": ["https://example.com"]}),
        ("whitespace only", {"url": "   "}),
        ("not a url", {"url": "not a url at all"}),
        ("no scheme", {"url": "example.com/path"}),
        ("relative", {"url": "/relative/path"}),
        ("over the length limit", {"url": URL_OVER_LIMIT}),
    ],
)
def test_malformed_payloads_are_rejected_without_issuing_a_code(client, label, payload):
    """Covers: AC1.3."""
    response = client.post("/links", json=payload)

    assert response.status_code == 400, f"{label}: expected 400, got {response.status_code}"
    assert_structured_error(response)
    assert_no_code_issued(response)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html;base64,PHNjcmlwdD48L3NjcmlwdD4=",
        "ftp://example.com/file.txt",
        "mailto:someone@example.com",
    ],
)
def test_unsupported_schemes_are_rejected(client, url):
    """Covers: AC1.4 — E3 admits http and https only."""
    response = client.post("/links", json={"url": url})

    assert response.status_code == 400, f"{url} was not rejected: {response.status_code}"
    assert_structured_error(response)
    assert_no_code_issued(response)


def test_metadata_returns_the_original_url_and_creation_time(client, create):
    """Covers: AC1.5."""
    link = create("https://example.com/metadata/target")

    response = client.get(f"/links/{link['code']}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == link["code"]
    assert body["long_url"] == "https://example.com/metadata/target"
    assert parse_timestamp(body["created_at"]) == parse_timestamp(link["created_at"])


def test_metadata_for_a_code_that_was_never_issued_is_404(client):
    """Covers: AC1.5 — E13: metadata for an unknown code is 404, not an empty record."""
    response = client.get("/links/Zz9Qw2X")

    assert response.status_code == 404, response.text
