"""R1 — a client can submit a long URL and receive a short URL identifying it.

Covers AC1.1, AC1.2, AC1.3, AC1.4.
"""

from __future__ import annotations

import re

import pytest

from conftest import TRICKY_URL

# E11: 7 characters of base62 drawn from a CSPRNG. The design fixes both the
# charset and the length, so the test fixes them too.
CODE_RE = re.compile(r"^[0-9A-Za-z]{7}$")


def test_valid_url_returns_201_with_short_url_code_and_long_url(client):
    """AC1.1 — a valid absolute http/https URL is accepted with 201."""
    url = "https://example.com/some/path?q=1"
    response = client.post("/api/links", json={"url": url})

    assert response.status_code == 201, response.text
    body = response.json()

    assert body["long_url"] == url
    assert CODE_RE.match(body["code"]), f"code {body['code']!r} is not 7-char base62"
    assert isinstance(body["short_url"], str) and body["short_url"]
    assert body["short_url"].endswith(body["code"]), (
        f"short_url {body['short_url']!r} should address code {body['code']!r}"
    )
    assert body["created_at"], "creation timestamp is part of the created resource (E1)"


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_both_http_and_https_are_accepted(client, scheme):
    """AC1.1 — the criterion names http *and* https as valid schemes."""
    url = f"{scheme}://example.com/ok"
    response = client.post("/api/links", json={"url": url})

    assert response.status_code == 201, response.text
    assert response.json()["long_url"] == url


def test_created_link_echoes_optional_expiry(client):
    """AC1.1 — expiry is optional and caller-supplied (A4/E1/E17).

    Absent: the created resource reports no expiry. Present: it reports the
    expiry it was given, so the caller can confirm what was stored.
    """
    without = client.post("/api/links", json={"url": "https://example.com/no-expiry"})
    assert without.status_code == 201, without.text
    assert without.json()["expires_at"] is None, "no expiry was requested (A4: no default)"

    expiry = "2999-01-01T00:00:00+00:00"
    with_expiry = client.post(
        "/api/links", json={"url": "https://example.com/expiring", "expires_at": expiry}
    )
    assert with_expiry.status_code == 201, with_expiry.text
    assert with_expiry.json()["expires_at"] is not None, "a supplied expiry must be stored"


def test_created_link_is_immediately_resolvable(client, create_link):
    """AC1.1 — the short URL returned must actually identify the long URL.

    Creation commits durably before returning 201 (E15), so there is no window
    in which the 201 has been issued but the code does not yet resolve.
    """
    body = create_link(TRICKY_URL)

    response = client.get(f"/{body['code']}")

    assert response.status_code == 301, response.text
    assert response.headers["location"] == TRICKY_URL


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="url-missing"),
        pytest.param({"url": None}, id="url-null"),
        pytest.param({"url": ""}, id="url-empty"),
        pytest.param({"url": "   "}, id="url-blank"),
        pytest.param({"url": "example.com/no-scheme"}, id="not-absolute"),
        pytest.param({"url": "/relative/path"}, id="relative-path"),
        pytest.param({"url": "ftp://example.com/file"}, id="scheme-ftp"),
        pytest.param({"url": "javascript:alert(1)"}, id="scheme-javascript"),
        pytest.param({"url": "data:text/html,<h1>x</h1>"}, id="scheme-data"),
        pytest.param({"url": "https://"}, id="no-host"),
        pytest.param({"url": 12345}, id="url-not-a-string"),
    ],
)
def test_invalid_urls_are_rejected_with_400_and_error_code(client, payload):
    """AC1.2 — a missing, non-absolute or non-http(s) URL is 400 with a code.

    The machine-readable code is the {code, message, details?} envelope from
    A13/E14, so the assertion is on the envelope, not on the prose message.
    """
    response = client.post("/api/links", json=payload)

    assert response.status_code == 400, (
        f"{payload!r} should be rejected with 400, got {response.status_code}: {response.text}"
    )
    body = response.json()
    assert isinstance(body.get("code"), str) and body["code"], (
        f"error body needs a machine-readable code (A13), got {body!r}"
    )
    assert isinstance(body.get("message"), str) and body["message"], (
        f"error body needs a message (A13), got {body!r}"
    )
    assert set(body) <= {"code", "message", "details"}, (
        f"the error envelope is exactly {{code, message, details?}} (E14), got {sorted(body)}"
    )


def test_rejected_creation_does_not_create_a_link(client):
    """AC1.2 — "and no link is created".

    A rejected request must not leak a usable short link: no short_url or
    resolvable code may come back in the error body.
    """
    response = client.post("/api/links", json={"url": "ftp://example.com/file"})

    assert response.status_code == 400, response.text
    body = response.json()
    assert "short_url" not in body, f"a rejected creation returned a short URL: {body!r}"
    # `code` here is the error code from the envelope; it must not be a short code.
    assert not CODE_RE.match(str(body.get("code", ""))), (
        f"the error code {body.get('code')!r} looks like a minted short code"
    )


def test_two_urls_get_distinct_codes_each_resolving_to_its_own(client, create_link):
    """AC1.3 — two creations yield different codes that do not cross over."""
    first_url = "https://example.com/first"
    second_url = "https://example.com/second"

    first = create_link(first_url)
    second = create_link(second_url)

    assert first["code"] != second["code"]

    first_redirect = client.get(f"/{first['code']}")
    second_redirect = client.get(f"/{second['code']}")

    assert first_redirect.headers["location"] == first_url
    assert second_redirect.headers["location"] == second_url


def test_same_url_twice_gets_distinct_codes(client, create_link):
    """AC1.3 — no deduplication by long URL (A5/E20).

    Two campaigns pointing at the same target keep separate codes, which is
    what keeps their click histories separate under R4.
    """
    url = "https://example.com/same-target"

    first = create_link(url)
    second = create_link(url)

    assert first["code"] != second["code"], (
        "resubmitting a URL must mint a new code, not return the existing one (A5)"
    )
    assert client.get(f"/{first['code']}").headers["location"] == url
    assert client.get(f"/{second['code']}").headers["location"] == url


def test_code_matches_documented_charset_and_length(client, create_link):
    """AC1.4 — the code matches the documented character set and length."""
    for _ in range(10):
        code = create_link("https://example.com/charset")["code"]
        assert len(code) == 7, f"code {code!r} has length {len(code)}, expected 7 (E11)"
        assert CODE_RE.match(code), f"code {code!r} is outside base62 [0-9A-Za-z] (E11)"


def test_generated_codes_are_unique_across_many_creations(client, create_link):
    """AC1.4 — creation never returns a code already in use by a live link.

    A unique constraint with bounded retry (E11) is what guarantees this; the
    test asserts the guarantee, not the mechanism. It also catches the failure
    a fixed seed or a non-CSPRNG source would produce.
    """
    codes = [create_link(f"https://example.com/unique/{i}")["code"] for i in range(50)]

    duplicates = {code for code in codes if codes.count(code) > 1}
    assert not duplicates, f"creation reissued live codes: {sorted(duplicates)}"
    assert len(set(codes)) == 50
