"""R4 — the service records each use of a short URL and exposes analytics for it.

Counting is asynchronous and at-least-once (A4/E16), so the count is reached by
polling rather than read once: a test that reads immediately would be asserting
the queue's timing, not the criterion.

Each test names the acceptance criterion it covers as ``Covers: ACx.y``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

UNISSUED_CODE = "Nb4Tz8K"


def as_date(value):
    """Accept a date or a datetime, in either ISO form."""
    return date.fromisoformat(str(value)[:10])


def as_datetime(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def visit(client, code, times):
    for _ in range(times):
        response = client.get(f"/{code}")
        assert response.status_code == 302, f"redirect failed mid-test: {response.status_code}"


def test_a_code_with_no_visits_reports_zero_clicks(client, create):
    """Covers: AC4.1 — E17: zero is a count, not an error."""
    link = create("https://example.com/never-visited")

    response = client.get(f"/links/{link['code']}/analytics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == link["code"]
    assert body["total_clicks"] == 0


def test_every_redirect_is_counted(client, create, wait_for_clicks):
    """Covers: AC4.2."""
    link = create("https://example.com/counted")
    visit(client, link["code"], 3)

    body = wait_for_clicks(link["code"], 3)

    assert body is not None, "analytics never returned 200 for an existing code"
    assert body["total_clicks"] == 3, (
        f"3 redirects were performed; analytics reports {body['total_clicks']}"
    )


def test_analytics_for_a_code_that_never_existed_is_404(client):
    """Covers: AC4.3 — E14."""
    response = client.get(f"/links/{UNISSUED_CODE}/analytics")

    assert response.status_code == 404, response.text


def test_per_day_counts_sum_to_the_total(client, create, wait_for_clicks):
    """Covers: AC4.4.

    The suite cannot advance the service clock, so the multi-day case is
    exercised through the invariant the criterion actually states — the day
    series sums to the total — over a series this run can produce. E15 makes
    that invariant exact rather than incidental: the total is computed from the
    same per-day rows.
    """
    link = create("https://example.com/per-day")
    visit(client, link["code"], 4)

    body = wait_for_clicks(link["code"], 4)

    assert body is not None, "analytics never returned 200 for an existing code"
    days = body["days"]
    assert isinstance(days, list) and days, f"no per-day breakdown in {body!r}"
    for entry in days:
        assert as_date(entry["date"]) is not None
        assert isinstance(entry["count"], int) and entry["count"] >= 0
    assert sum(entry["count"] for entry in days) == body["total_clicks"], (
        f"per-day counts {days} do not sum to total {body['total_clicks']}"
    )


def test_the_day_series_is_bounded_and_the_range_is_honoured(client, create):
    """Covers: AC4.4 — E18: the series defaults to the last 30 days and accepts a range,
    so a response cannot grow without limit as history accumulates."""
    link = create("https://example.com/ranged")

    default = client.get(f"/links/{link['code']}/analytics")
    assert default.status_code == 200, default.text
    body = default.json()
    span = as_date(body["to"]) - as_date(body["from"])
    assert timedelta(days=29) <= span <= timedelta(days=31), (
        f"default window is {span.days} days, not the documented 30 "
        f"({body['from']} → {body['to']})"
    )

    start, end = date.today() - timedelta(days=3), date.today()
    ranged = client.get(
        f"/links/{link['code']}/analytics",
        params={"from": start.isoformat(), "to": end.isoformat()},
    )
    assert ranged.status_code == 200, ranged.text
    scoped = ranged.json()
    assert as_date(scoped["from"]) == start
    assert as_date(scoped["to"]) == end
    for entry in scoped["days"]:
        assert start <= as_date(entry["date"]) <= end, f"{entry} falls outside the range asked for"


def test_the_response_states_how_far_counting_is_complete(client, create, wait_for_clicks):
    """Covers: AC4.5 — E17: without a watermark, a caller cannot tell 'no clicks'
    from 'not counted yet'."""
    link = create("https://example.com/watermark")

    fresh = client.get(f"/links/{link['code']}/analytics").json()
    assert fresh["approximate"] is True, "E16/A4: the count is approximate and must say so"
    assert as_datetime(fresh["counted_through"]) is not None

    visit(client, link["code"], 2)
    counted = wait_for_clicks(link["code"], 2)

    assert counted is not None, "analytics never returned 200 for an existing code"
    assert counted["total_clicks"] == 2
    assert as_datetime(counted["counted_through"]) >= as_datetime(fresh["counted_through"]), (
        "the completeness watermark went backwards"
    )
