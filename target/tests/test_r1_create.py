"""R1 — a client can submit a long URL and receive a short URL identifying it.

Covers AC1.1, AC1.2, AC1.3, AC1.4.
"""

from __future__ import annotations

import re

import pytest

from conftest import (
    CODE_LENGTH,
    REDIRECT_STATUS,
    SERVICE_HOST,
    TRICKY_URL,
    assert_error_envelope,
)

# codes.ALPHABET is the 62-character base62 set; codes.CODE_LENGTH is 7. The
# contract fixes both, so the test fixes both.
CODE_RE = re.compile(rf"^[0-9A-Za-z]{{{CODE_LENGTH}}}$")


def test_valid_url_returns_201_with_short_url_code_and_long_url(client):
    """AC1.1 — a valid absolute http/https URL is accepted with 201.

    The body is main.LinkResponse: code, short_url, long_url, created_at,
    expires_at.
    """
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
    assert body["created_at"], "creation timestamp is part of the created resource"


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_both_http_and_https_are_accepted(client, scheme):
    """AC1.1 — the criterion names http *and* https as valid schemes."""
    url = f"{scheme}://example.com/ok"
    response = client.post("/api/links", json={"url": url})

    assert response.status_code == 201, response.text
    assert response.json()["long_url"] == url


def test_created_link_echoes_optional_expiry(client):
    """AC1.1 — expiry is optional and caller-supplied (A4).

    Absent: the created resource reports no expiry, because A4 fixed that there
    is no service-wide default. Present: it reports the expiry it was given, so
    the caller can confirm what was stored.
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

    Creation persists before returning 201, so there is no window in which the
    201 has been issued but the code does not yet resolve.
    """
    body = create_link(TRICKY_URL)

    response = client.get(f"/{body['code']}")

    assert response.status_code == REDIRECT_STATUS, response.text
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
def test_invalid_urls_are_rejected_with_400_and_error_envelope(client, payload):
    """AC1.2 — a missing, non-absolute or non-http(s) URL is 400 with a code.

    The machine-readable code is the {code, message, details} envelope from A13
    and errors.ErrorEnvelope, so the assertion is on the envelope's shape, not
    on the prose message.
    """
    response = client.post("/api/links", json=payload)

    assert response.status_code == 400, (
        f"{payload!r} should be rejected with 400, got {response.status_code}: {response.text}"
    )
    assert_error_envelope(response, f"a rejected creation of {payload!r}")


def test_self_referential_url_is_rejected(client):
    """AC1.2 — a URL pointing at this service's own host is refused (A12).

    A short link to the shortener is a redirect loop on the path with the
    tightest latency budget. A12 settled that creation refuses it, and the host
    is the configured base URL's, which the suite sets to the test origin.
    """
    response = client.post("/api/links", json={"url": f"http://{SERVICE_HOST}/AbCdEfG"})

    assert response.status_code == 400, (
        f"a self-referential URL must be refused (A12), got {response.status_code}: "
        f"{response.text}"
    )
    assert_error_envelope(response, "a self-referential creation")


def test_rejected_creation_does_not_create_a_link(client):
    """AC1.2 — "and no link is created".

    A rejected request must not leak a usable short link: the error envelope
    has no short_url, and its `code` is the error code, never a minted one.
    """
    response = client.post("/api/links", json={"url": "ftp://example.com/file"})

    assert response.status_code == 400, response.text
    body = assert_error_envelope(response, "a rejected creation")

    assert "short_url" not in body, f"a rejected creation returned a short URL: {body!r}"
    assert not CODE_RE.match(str(body["code"])), (
        f"the error code {body['code']!r} looks like a minted short code"
    )


def test_two_urls_get_distinct_codes_each_resolving_to_its_own(client, create_link):
    """AC1.3 — two creations yield different codes that do not cross over."""
    first_url = "https://example.com/first"
    second_url = "https://example.com/second"

    first = create_link(first_url)
    second = create_link(second_url)

    assert first["code"] != second["code"]

    assert client.get(f"/{first['code']}").headers["location"] == first_url
    assert client.get(f"/{second['code']}").headers["location"] == second_url


def test_same_url_twice_gets_distinct_codes(client, create_link):
    """AC1.3 — no deduplication by long URL (A5).

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
        assert len(code) == CODE_LENGTH, (
            f"code {code!r} has length {len(code)}, expected {CODE_LENGTH}"
        )
        assert CODE_RE.match(code), f"code {code!r} is outside base62 [0-9A-Za-z]"


def test_generated_codes_are_unique_across_many_creations(client, create_link):
    """AC1.4 — creation never returns a code already in use by a live link.

    A primary-key constraint with bounded retry is what guarantees this; the
    test asserts the guarantee, not the mechanism. It also catches the failure
    a fixed seed or a non-random source would produce.
    """
    codes = [create_link(f"https://example.com/unique/{i}")["code"] for i in range(50)]

    duplicates = {code for code in codes if codes.count(code) > 1}
    assert not duplicates, f"creation reissued live codes: {sorted(duplicates)}"
    assert len(set(codes)) == 50


def test_generate_code_draws_from_the_documented_alphabet(app):
    """AC1.4 — "when a short code is generated" holds at the minting function.

    The criterion is about generated codes, not only about codes that reached
    an HTTP response, so it is asserted where they are minted. `codes` has no
    dependencies in the contract, which is what makes this checkable directly.
    """
    from shortener.codes import ALPHABET, CODE_LENGTH as CONTRACT_LENGTH, generate_code

    assert CONTRACT_LENGTH == CODE_LENGTH, "the documented code length is 7"
    assert len(set(ALPHABET)) == 62, f"base62 needs 62 distinct characters, got {ALPHABET!r}"

    minted = [generate_code() for _ in range(200)]
    for code in minted:
        assert len(code) == CODE_LENGTH, f"{code!r} is not {CODE_LENGTH} characters"
        assert set(code) <= set(ALPHABET), (
            f"{code!r} uses characters outside the documented alphabet"
        )

    # A generator that ignored its randomness would still satisfy the shape
    # assertions above; the whole space is 62**7, so 200 draws colliding into a
    # handful of values means the source is not random.
    assert len(set(minted)) > 190, (
        f"200 minted codes produced only {len(set(minted))} distinct values — "
        f"the code source does not look random"
    )


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("", id="empty"),
        pytest.param("abcdef", id="too-short"),
        pytest.param("abcdefgh", id="too-long"),
        pytest.param("abcde-f", id="hyphen"),
        pytest.param("abcde f", id="space"),
        pytest.param("abcde/f", id="slash"),
        pytest.param("abcdé fg"[:7], id="non-ascii"),
    ],
)
def test_is_valid_code_rejects_malformed_codes(app, code):
    """AC1.4 — the documented character set and length are actually enforced.

    `is_valid_code` is the contract's answer to "does this match the documented
    form"; a version that returns True for everything would let every other
    caller of it wave through malformed input.
    """
    from shortener.codes import is_valid_code

    assert is_valid_code(code) is False, (
        f"{code!r} does not match the documented 7-character base62 form, "
        f"but is_valid_code accepted it"
    )


def test_is_valid_code_accepts_a_minted_code(app):
    """AC1.4 — the validator agrees with the generator.

    Paired with the rejection cases above: a validator that returns False for
    everything passes those and fails here.
    """
    from shortener.codes import generate_code, is_valid_code

    for _ in range(20):
        code = generate_code()
        assert is_valid_code(code) is True, (
            f"generate_code() minted {code!r} but is_valid_code rejected it"
        )
