"""R4 — the owner can see how often and when a link was visited.

Covers AC4.1, AC4.2, AC4.3, AC4.4, AC4.5.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from conftest import (
    DESKTOP_UA,
    FRESHNESS_SECONDS,
    MAX_INTERVALS,
    MOBILE_UA,
    REDIRECT_STATUS,
    assert_error_envelope,
    read_breakdown,
    read_series,
    read_total,
)


def _iso(moment):
    return moment.isoformat()


# --------------------------------------------------------------------------- #
# AC4.1 — total click count
# --------------------------------------------------------------------------- #


def test_total_click_count_equals_number_of_redirects(
    client, create_link, click, wait_for_total
):
    """AC4.1 — a link that received N successful redirects reports N."""
    code = create_link("https://example.com/counted")["code"]

    responses = click(client, code, times=7)
    assert all(r.status_code == REDIRECT_STATUS for r in responses), (
        "all 7 redirects must succeed for the count to mean anything"
    )

    body = wait_for_total(client, code, 7)
    assert read_total(body) == 7


def test_total_click_count_is_zero_for_a_link_with_no_clicks(
    client, create_link, analytics
):
    """AC4.1 — the empty case: a link nobody visited reports zero, not 404.

    A link that exists but has no clicks is a legitimate state, and the natural
    bug is an implementation that treats "no rows" as "no such link".
    """
    code = create_link("https://example.com/unvisited")["code"]

    body = analytics(client, code)

    assert read_total(body) == 0, "a link with no clicks reports a total of 0"
    assert read_series(body) is not None
    assert sum(count for _, count in read_series(body)) == 0
    assert read_breakdown(body, "referrers") == {}
    assert read_breakdown(body, "devices") == {}
    assert read_breakdown(body, "browsers") == {}


def test_click_counts_are_scoped_to_their_own_link(
    client, create_link, click, wait_for_total
):
    """AC4.1 — counts are per code (A5), never pooled by long URL.

    Both links point at the same target, so an implementation that keyed
    analytics on the URL instead of the code would report 5 for each.
    """
    url = "https://example.com/shared-target"
    first = create_link(url)["code"]
    second = create_link(url)["code"]

    click(client, first, times=3)
    click(client, second, times=2)

    assert read_total(wait_for_total(client, first, 3)) == 3
    assert read_total(wait_for_total(client, second, 2)) == 2


# --------------------------------------------------------------------------- #
# AC4.2 — per-interval counts over a window
# --------------------------------------------------------------------------- #


def test_time_series_counts_cover_only_the_window_and_sum_to_its_clicks(
    client, create_link, click, wait_for_total, analytics
):
    """AC4.2 — per-interval counts cover only the window and sum to its clicks.

    Clicks cannot be back-dated through the public API, so this asserts the
    window semantics the criterion is really about: every interval returned
    lies inside the half-open [start, end), and the counts add up to the clicks
    in it — and to the total the same response reports.
    """
    code = create_link("https://example.com/series")["code"]
    click(client, code, times=4)
    wait_for_total(client, code, 4)

    start = datetime.now(timezone.utc) - timedelta(hours=2)
    end = datetime.now(timezone.utc) + timedelta(hours=1)
    body = analytics(client, code, start=_iso(start), end=_iso(end), interval="hour")

    series = read_series(body)
    assert series, "a window containing clicks must return intervals"

    total_in_window = sum(count for _, count in series)
    assert total_in_window == 4, (
        f"per-interval counts sum to {total_in_window}, expected the 4 clicks in the window"
    )
    assert read_total(body) == total_in_window, (
        f"the reported total {read_total(body)} disagrees with its own series "
        f"({total_in_window}); both are scoped to the same window"
    )

    for interval_start, _ in series:
        parsed = datetime.fromisoformat(str(interval_start).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        assert start <= parsed < end, (
            f"interval {interval_start!r} falls outside the requested "
            f"[{_iso(start)}, {_iso(end)}) window"
        )


def test_time_series_window_excluding_clicks_sums_to_zero(
    client, create_link, click, wait_for_total, analytics
):
    """AC4.2 — "covering only that window" has a negative side.

    A window that predates every click must report no clicks. Without this, an
    implementation that ignores start and end entirely passes the positive case
    with flying colours.
    """
    code = create_link("https://example.com/outside-window")["code"]
    click(client, code, times=3)
    wait_for_total(client, code, 3)

    start = datetime.now(timezone.utc) - timedelta(days=10)
    end = datetime.now(timezone.utc) - timedelta(days=9)
    body = analytics(client, code, start=_iso(start), end=_iso(end), interval="day")

    series = read_series(body)
    assert sum(count for _, count in series) == 0, (
        "a window before the first click must report no clicks in its intervals"
    )
    assert read_total(body) == 0, (
        f"the total is scoped to the window too, got {read_total(body)}"
    )


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"interval": "day"}, id="no-start-no-end"),
        pytest.param({"start": "2026-01-01T00:00:00+00:00", "interval": "day"}, id="no-end"),
        pytest.param({"end": "2026-01-02T00:00:00+00:00", "interval": "day"}, id="no-start"),
    ],
)
def test_time_series_requires_an_explicit_window(client, create_link, params):
    """AC4.2 — the window is required, so a partial one is rejected.

    A11 bounds analytics queries so they cannot become a self-inflicted load
    source; an implicit "all time" window is exactly the unbounded scan that
    bound exists to prevent.
    """
    code = create_link("https://example.com/needs-window")["code"]

    response = client.get(f"/api/links/{code}/analytics", params=params)

    assert response.status_code == 400, (
        f"a partial window {params!r} should be rejected with 400, got "
        f"{response.status_code}: {response.text}"
    )


def test_time_series_rejects_more_intervals_than_the_documented_cap(client, create_link):
    """AC4.2 — more than 366 intervals is 400 (A11).

    Both sides of the boundary are checked: the cap itself must be accepted, or
    the bound has been implemented one interval too tight and a legitimate
    year-long query fails.
    """
    code = create_link("https://example.com/too-many-intervals")["code"]
    end = datetime.now(timezone.utc)

    at_cap = client.get(
        f"/api/links/{code}/analytics",
        params={
            "start": _iso(end - timedelta(days=MAX_INTERVALS)),
            "end": _iso(end),
            "interval": "day",
        },
    )
    assert at_cap.status_code == 200, (
        f"{MAX_INTERVALS} intervals is the documented cap and must be accepted, "
        f"got {at_cap.status_code}: {at_cap.text}"
    )

    over_cap = client.get(
        f"/api/links/{code}/analytics",
        params={
            "start": _iso(end - timedelta(days=MAX_INTERVALS + 30)),
            "end": _iso(end),
            "interval": "day",
        },
    )
    assert over_cap.status_code == 400, (
        f"more than {MAX_INTERVALS} intervals must be rejected with 400, "
        f"got {over_cap.status_code}"
    )
    assert_error_envelope(over_cap, "an over-cap analytics window")


@pytest.mark.parametrize("interval", ["week", "month", "minute", "", "DAY!"])
def test_time_series_rejects_an_unsupported_interval(client, create_link, interval):
    """AC4.2 — the bucket widths a caller may ask for are 'hour' and 'day'.

    `analytics.VALID_INTERVALS` fixes the set. An unbounded interval vocabulary
    is the other half of A11's bound: 'second' over a year is the same scan the
    interval cap refuses.
    """
    code = create_link("https://example.com/bad-interval")["code"]
    end = datetime.now(timezone.utc)

    response = client.get(
        f"/api/links/{code}/analytics",
        params={
            "start": _iso(end - timedelta(days=1)),
            "end": _iso(end),
            "interval": interval,
        },
    )

    assert response.status_code == 400, (
        f"interval {interval!r} is outside the documented set and must be rejected "
        f"with 400, got {response.status_code}: {response.text}"
    )


def test_time_series_rejects_an_inverted_window(client, create_link):
    """AC4.2 — a window whose end precedes its start is not a window.

    The half-open [start, end) has no meaning reversed, and the natural bug is
    an implementation that returns an empty series instead of refusing — which
    reports "no clicks" for a query that was never valid.
    """
    code = create_link("https://example.com/inverted-window")["code"]
    now = datetime.now(timezone.utc)

    response = client.get(
        f"/api/links/{code}/analytics",
        params={
            "start": _iso(now),
            "end": _iso(now - timedelta(days=1)),
            "interval": "day",
        },
    )

    assert response.status_code == 400, (
        f"an inverted window must be refused with 400, got {response.status_code}: "
        f"{response.text}"
    )


# --------------------------------------------------------------------------- #
# AC4.3 — breakdowns
# --------------------------------------------------------------------------- #


def test_breakdown_by_referrer_groups_clicks(
    client, create_link, click, wait_for_total, analytics
):
    """AC4.3 — clicks are grouped by referrer with the right counts."""
    code = create_link("https://example.com/referrers")["code"]

    click(client, code, times=3, referer="https://news.example.org/story")
    click(client, code, times=2, referer="https://social.example.net/post")
    wait_for_total(client, code, 5)

    body = analytics(client, code)
    referrers = read_breakdown(body, "referrers")

    def count_for(needle):
        return sum(n for label, n in referrers.items() if needle in label)

    assert count_for("news.example.org") == 3, f"referrer breakdown was {referrers!r}"
    assert count_for("social.example.net") == 2, f"referrer breakdown was {referrers!r}"
    assert sum(referrers.values()) == 5, (
        f"referrer counts must account for all 5 clicks, got {referrers!r}"
    )


def test_breakdown_by_device_and_browser_class(
    client, create_link, click, wait_for_total, analytics
):
    """AC4.3 — clicks are grouped by inferred device and browser class.

    The criterion asks for grouping by an *inferred* class; nothing in the
    register or the contract fixes the vocabulary of those classes, so this
    asserts the grouping rather than the spelling. A desktop Chrome and a
    mobile Safari user-agent must land in different buckets, and the buckets
    must partition the clicks 3/2.

    Collapsing every user-agent into one class is the failure this catches —
    and that bucket is what A10 relies on to make crawler traffic visible
    separately, so a single "unknown" class defeats the whole breakdown.
    """
    code = create_link("https://example.com/devices")["code"]

    click(client, code, times=3, user_agent=DESKTOP_UA)
    click(client, code, times=2, user_agent=MOBILE_UA)
    wait_for_total(client, code, 5)

    body = analytics(client, code)

    devices = read_breakdown(body, "devices")
    assert sum(devices.values()) == 5, f"device counts must cover all 5 clicks: {devices!r}"
    assert len(devices) >= 2, (
        f"a desktop and a mobile user-agent must infer different device classes, "
        f"got a single bucket: {devices!r}"
    )
    assert sorted(devices.values(), reverse=True)[:2] == [3, 2], (
        f"the two user-agent families should partition the clicks 3/2, got {devices!r}"
    )

    browsers = read_breakdown(body, "browsers")
    assert sum(browsers.values()) == 5, f"browser counts must cover all 5 clicks: {browsers!r}"
    assert len(browsers) >= 2, (
        f"Chrome and Safari must infer different browser classes, got {browsers!r}"
    )
    assert sorted(browsers.values(), reverse=True)[:2] == [3, 2], (
        f"the two browsers should partition the clicks 3/2, got {browsers!r}"
    )


def test_breakdowns_are_scoped_to_the_requested_window(
    client, create_link, click, wait_for_total, analytics
):
    """AC4.3 — the breakdowns cover the same window as the rest of the response.

    AC4.2 scopes the series and the total to [start, end); a breakdown computed
    over all time while the total is windowed is internally inconsistent, and
    the two would silently disagree in every report built on them.
    """
    code = create_link("https://example.com/windowed-breakdown")["code"]
    click(client, code, times=2, referer="https://news.example.org/story")
    wait_for_total(client, code, 2)

    past = datetime.now(timezone.utc) - timedelta(days=9)
    body = analytics(
        client,
        code,
        start=_iso(past - timedelta(days=1)),
        end=_iso(past),
        interval="day",
    )

    assert read_total(body) == 0, "sanity: the window excludes every click"
    assert sum(read_breakdown(body, "referrers").values()) == 0, (
        "the referrer breakdown must be scoped to the window, like the total"
    )
    assert sum(read_breakdown(body, "devices").values()) == 0
    assert sum(read_breakdown(body, "browsers").values()) == 0


# --------------------------------------------------------------------------- #
# AC4.4 — freshness
# --------------------------------------------------------------------------- #


def test_click_is_visible_within_freshness_bound(client, create_link, analytics):
    """AC4.4 — a recorded click appears within the documented bound (A6: 60 s).

    Recording is asynchronous by design so the redirect keeps its latency
    budget; A6 is the price agreed for that, and 60 s is the ceiling. A click
    that never surfaces means the drain worker is not running at all — the
    failure that makes every other count in this file meaningless.
    """
    code = create_link("https://example.com/freshness")["code"]

    assert client.get(f"/{code}").status_code == REDIRECT_STATUS

    deadline = time.monotonic() + FRESHNESS_SECONDS
    elapsed = None
    while time.monotonic() < deadline:
        if read_total(analytics(client, code)) == 1:
            elapsed = FRESHNESS_SECONDS - (deadline - time.monotonic())
            break
        time.sleep(0.05)

    assert elapsed is not None, (
        f"the click was still not visible after {FRESHNESS_SECONDS}s, exceeding the "
        f"freshness bound documented in A6"
    )


# --------------------------------------------------------------------------- #
# AC4.5 — no disclosure for links that are not the caller's to read
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", ["Uu4Vv5W", "zzzzzzz", "0000000"])
def test_analytics_for_unknown_code_returns_404_and_no_data(client, code):
    """AC4.5 — an unknown link discloses nothing.

    A2 removed authentication, so the ownership check reduces to an existence
    check: 404, and no analytics body. The assertion is on the disclosure, not
    only on the status, because a 404 carrying a click count would still leak.
    """
    response = client.get(f"/api/links/{code}/analytics")

    assert response.status_code in (403, 404), (
        f"analytics for unissued {code!r} should be 404 or 403, got {response.status_code}"
    )

    body = assert_error_envelope(response, f"analytics for unissued {code!r}")
    assert "total_clicks" not in body, f"a refused analytics request leaked a count: {body!r}"
    assert "series" not in body, f"a refused analytics request leaked a series: {body!r}"
