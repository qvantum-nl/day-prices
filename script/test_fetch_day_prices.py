#!/usr/bin/env python3
"""Offline checks for fetch_day_prices.py. Run: python3 test_fetch_day_prices.py

No network. The ENTSO-E document below is hand-made after the public format description:
it is not a recorded answer from the real service.
"""
import datetime as dt
from zoneinfo import ZoneInfo

import fetch_day_prices as f

AMS = ZoneInfo("Europe/Amsterdam")
UTC = dt.timezone.utc


def entsoe_doc(start_utc, end_utc, resolution, points, curve="A03", seq=None):
    pts = "".join("<Point><position>%d</position><price.amount>%s</price.amount></Point>" % (p, v) for p, v in points)
    seq_xml = ("<classificationSequence_AttributeInstanceComponent.position>%s"
               "</classificationSequence_AttributeInstanceComponent.position>" % seq) if seq else ""
    return ("<TimeSeries>%s<curveType>%s</curveType><Period><timeInterval><start>%s</start><end>%s</end>"
            "</timeInterval><resolution>%s</resolution>%s</Period></TimeSeries>"
            % (seq_xml, curve, start_utc, end_utc, resolution, pts))


def wrap(*series):
    return ('<?xml version="1.0" encoding="UTF-8"?><Publication_MarketDocument '
            'xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">%s</Publication_MarketDocument>'
            % "".join(series)).encode()


def test_expected_hours():
    assert len(f.expected_hours(dt.date(2026, 11, 22), AMS)) == 24
    assert len(f.expected_hours(dt.date(2026, 3, 29), AMS)) == 23   # clock goes forward: 02:00 does not exist
    assert len(f.expected_hours(dt.date(2026, 10, 25), AMS)) == 24  # clock goes back: 02:00 happens twice


def test_buckets_quarter_to_hour_and_units():
    b = f.Buckets(AMS)
    t0 = dt.datetime(2026, 11, 21, 23, 0, tzinfo=UTC)  # 22 Nov 00:00 local
    for i in range(96):
        b.add(t0 + dt.timedelta(minutes=15 * i), 100.0 + (i % 4) * 10)  # 100,110,120,130 per hour
    days = b.days(dt.date(2026, 11, 22), dt.date(2026, 11, 22))
    assert list(days) == [dt.date(2026, 11, 22)]
    assert days[dt.date(2026, 11, 22)] == [0.115] * 24  # mean 115 EUR/MWh = 0.115 EUR/kWh


def test_incomplete_day_is_left_out():
    b = f.Buckets(AMS)
    t0 = dt.datetime(2026, 11, 21, 23, 0, tzinfo=UTC)
    for i in range(20):
        b.add(t0 + dt.timedelta(hours=i), 50.0)
    assert b.days(dt.date(2026, 11, 22), dt.date(2026, 11, 22)) == {}


def test_dst_days():
    b = f.Buckets(AMS)
    t0 = dt.datetime(2026, 3, 28, 23, 0, tzinfo=UTC)  # 29 Mar 00:00 local, 23 hours long
    for i in range(23):
        b.add(t0 + dt.timedelta(hours=i), 80.0)
    day = b.days(dt.date(2026, 3, 29), dt.date(2026, 3, 29))[dt.date(2026, 3, 29)]
    assert day[2] is None and sum(v is not None for v in day) == 23
    b = f.Buckets(AMS)
    t0 = dt.datetime(2026, 10, 24, 22, 0, tzinfo=UTC)  # 25 Oct 00:00 local, 25 hours long
    for i in range(25):
        b.add(t0 + dt.timedelta(hours=i), 100.0 if i != 3 else 200.0)  # second 02:00 hour is 200
    day = b.days(dt.date(2026, 10, 25), dt.date(2026, 10, 25))[dt.date(2026, 10, 25)]
    assert len(day) == 24 and day[2] == 0.15  # both 02:00 hours averaged


def test_price_out_of_range():
    b = f.Buckets(AMS)
    try:
        b.add(dt.datetime(2026, 11, 22, tzinfo=UTC), 99999)
    except f.SourceError:
        return
    raise AssertionError("expected SourceError")


def test_entsoe_quarter_with_gaps_a03():
    # 96 quarters; only positions 1 and 49 given: A03 repeats the last price
    doc = wrap(entsoe_doc("2026-11-21T23:00Z", "2026-11-22T23:00Z", "PT15M", [(1, "100.00"), (49, "200.00")]))
    days = f.parse_entsoe(doc, AMS, dt.date(2026, 11, 22), dt.date(2026, 11, 22))
    day = days[dt.date(2026, 11, 22)]
    assert day[:12] == [0.1] * 12 and day[12:] == [0.2] * 12


def test_entsoe_finest_resolution_wins_and_second_auction_skipped():
    hourly = entsoe_doc("2026-11-21T23:00Z", "2026-11-22T23:00Z", "PT60M", [(i, "50.00") for i in range(1, 25)])
    quarter = entsoe_doc("2026-11-21T23:00Z", "2026-11-22T23:00Z", "PT15M", [(1, "80.00")], seq="1")
    other = entsoe_doc("2026-11-21T23:00Z", "2026-11-22T23:00Z", "PT15M", [(1, "999.00")], seq="2")
    days = f.parse_entsoe(wrap(hourly, quarter, other), AMS, dt.date(2026, 11, 22), dt.date(2026, 11, 22))
    assert days[dt.date(2026, 11, 22)] == [0.08] * 24


def test_entsoe_no_data():
    doc = (b'<?xml version="1.0"?><Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:'
           b'acknowledgementdocument:7:0"><Reason><code>999</code><text>No matching data found</text></Reason>'
           b'</Acknowledgement_MarketDocument>')
    assert f.parse_entsoe(doc, AMS, dt.date(2026, 11, 22), dt.date(2026, 11, 22)) == {}


def test_trial_log_only_on_change():
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "trial", "log.csv")
        now = dt.datetime(2026, 11, 21, 13, 20, tzinfo=AMS)
        today = dt.date(2026, 11, 21)
        assert f.log_state(path, now, today, ["2026-11-19"]) is True
        assert f.log_state(path, now, today, ["2026-11-19"]) is False          # same state: no line
        assert f.log_state(path, now, today, ["2026-11-19", "2026-11-22"]) is True   # tomorrow arrived
        lines = open(path, encoding="utf-8").read().splitlines()
        assert lines[0] == f.LOG_HEADER.strip() and len(lines) == 3
        assert lines[1] == "2026-11-21T12:20Z,2026-11-21,0,0,2026-11-19,1"
        assert lines[2] == "2026-11-21T12:20Z,2026-11-21,0,1,2026-11-22,2"


def test_needed_day_follows_the_clock():
    # the evening check that GitHub started at 01:50 on 23 Nov must look for 23 Nov, not 24 Nov
    assert f.needed_day(dt.datetime(2026, 11, 23, 2, 50, tzinfo=AMS)) == dt.date(2026, 11, 23)
    assert f.needed_day(dt.datetime(2026, 11, 23, 11, 59, tzinfo=AMS)) == dt.date(2026, 11, 23)
    assert f.needed_day(dt.datetime(2026, 11, 23, 12, 13, tzinfo=AMS)) == dt.date(2026, 11, 24)
    assert f.needed_day(dt.datetime(2026, 11, 23, 23, 43, tzinfo=AMS)) == dt.date(2026, 11, 24)


def test_overdue_window():
    assert f.is_overdue(dt.datetime(2026, 11, 23, 22, 13, tzinfo=AMS))
    assert f.is_overdue(dt.datetime(2026, 11, 24, 0, 43, tzinfo=AMS))
    assert not f.is_overdue(dt.datetime(2026, 11, 23, 12, 13, tzinfo=AMS))
    assert not f.is_overdue(dt.datetime(2026, 11, 23, 21, 43, tzinfo=AMS))


def test_alarm_once_per_day():
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "trial", "alarm.txt")
        assert f.alarm_once(path, dt.date(2026, 11, 24)) is True
        assert f.alarm_once(path, dt.date(2026, 11, 24)) is False
        assert f.alarm_once(path, dt.date(2026, 11, 25)) is True


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok  " + t.__name__)
    print("%d checks passed" % len(tests))
