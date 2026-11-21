#!/usr/bin/env python3
"""Fetch day-ahead electricity prices for one bidding zone and write a small JSON file.

The page block "A day with SmartControl" reads that JSON file. Run this once a day,
shortly after the prices for tomorrow are published (around 13:00 CET).

    python3 fetch_day_prices.py                     # today, tomorrow and late-filled days
    python3 fetch_day_prices.py --date 2026-11-22   # one day
    python3 fetch_day_prices.py --zone DE-LU        # another bidding zone from the config
    python3 fetch_day_prices.py --compare energyzero  # cross-check only, writes nothing extra

Standard library only (Python 3.9+). No keys in this file or in the config:
the optional ENTSO-E source reads its token from the environment variable ENTSOE_TOKEN.

Exit codes: 0 = file holds the target day, 3 = target day not published yet (run again later),
1 = error (the existing file is left untouched).
"""
import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

SCHEMA = 1
USER_AGENT = "day-prices-fetch/1.0"
UTC = dt.timezone.utc
HERE = os.path.dirname(os.path.abspath(__file__))
# Harmonised limits of the European day-ahead auction, in EUR/MWh. Anything outside is a data error.
PRICE_MIN, PRICE_MAX = -500.0, 4000.0

SOURCES = {
    "smard": {
        "name": "Bundesnetzagentur | SMARD.de",
        "url": "https://www.smard.de/",
        "licence": "CC BY 4.0",
        "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    },
    "entsoe": {
        "name": "ENTSO-E Transparency Platform",
        "url": "https://transparency.entsoe.eu/",
        "licence": "",
        "licence_url": "",
    },
}


class SourceError(Exception):
    pass


# ---------- small helpers ----------

def http_get(url, accept="application/json", timeout=25, tries=3):
    """GET with a generic user agent. Never prints the URL: it can carry a token."""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise SourceError("HTTP %s from %s" % (e.code, urllib.parse.urlsplit(url).netloc))
            last = "HTTP %s" % e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e.__class__.__name__
        time.sleep(2 * (attempt + 1))
    raise SourceError("no answer from %s (%s)" % (urllib.parse.urlsplit(url).netloc, last))


def local_midnight(day, tz):
    return dt.datetime(day.year, day.month, day.day, tzinfo=tz)


def expected_hours(day, tz):
    """Local clock hours that exist on this day (23 distinct on the spring DST day, 24 otherwise)."""
    t = local_midnight(day, tz).astimezone(UTC)
    end = local_midnight(day + dt.timedelta(days=1), tz).astimezone(UTC)
    hours = set()
    while t < end:
        hours.add(t.astimezone(tz).hour)
        t += dt.timedelta(hours=1)
    return hours


class Buckets:
    """Collects prices (EUR/MWh, any resolution) and returns hourly means in EUR/kWh per local day."""

    def __init__(self, tz):
        self.tz = tz
        self.d = {}

    def add(self, utc_start, eur_per_mwh):
        if eur_per_mwh is None:
            return
        v = float(eur_per_mwh)
        if not (PRICE_MIN <= v <= PRICE_MAX):
            raise SourceError("price out of range: %s EUR/MWh" % v)
        loc = utc_start.astimezone(self.tz)
        self.d.setdefault(loc.date(), {}).setdefault(loc.hour, []).append(v / 1000.0)

    def days(self, start, end):
        """{date: [24 values or None]} for complete days only. Hour = mean of all prices in that clock hour."""
        out = {}
        for day, hours in self.d.items():
            if not (start <= day <= end):
                continue
            if not expected_hours(day, self.tz).issubset(hours.keys()):
                continue  # incomplete day: leave it out, the page then shows the last known day
            out[day] = [round(sum(hours[h]) / len(hours[h]), 5) if h in hours else None for h in range(24)]
        return out


# ---------- sources ----------

def fetch_smard(zone, start, end, tz):
    """Bundesnetzagentur SMARD. Public, no key. Weekly files that start on Monday 00:00 local time."""
    flt = zone.get("smard_filter")
    if not flt:
        raise SourceError("this zone has no smard_filter in the config")
    base = "https://www.smard.de/app/chart_data/%d/DE/" % int(flt)
    index = json.loads(http_get(base + "index_hour.json"))["timestamps"]
    lo = local_midnight(start, tz).timestamp() * 1000
    hi = local_midnight(end + dt.timedelta(days=1), tz).timestamp() * 1000
    wanted = [t for i, t in enumerate(index) if t < hi and (i + 1 == len(index) or index[i + 1] > lo)]
    b = Buckets(tz)
    for t in wanted:
        data = json.loads(http_get(base + "%d_DE_hour_%d.json" % (int(flt), t)))
        for ts, value in data.get("series", []):
            if value is not None:
                b.add(dt.datetime.fromtimestamp(ts / 1000, UTC), value)
    return b.days(start, end)


def _strip_ns(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _iso_duration_minutes(text):
    table = {"PT15M": 15, "PT30M": 30, "PT60M": 60, "PT1H": 60}
    if text not in table:
        raise SourceError("unknown resolution %s" % text)
    return table[text]


def parse_entsoe(xml_bytes, tz, start, end):
    """Parse a day-ahead price document (documentType A44). Prices are EUR/MWh.

    Handles 15, 30 and 60 minute resolution and curve type A03, where a missing position
    repeats the previous price. If several resolutions cover a day, the finest one wins.
    """
    root = _strip_ns(ET.fromstring(xml_bytes))
    if root.tag.startswith("Acknowledgement"):
        return {}  # "No matching data found"
    by_res = {}
    for ts in root.iter("TimeSeries"):
        seq = ts.findtext("classificationSequence_AttributeInstanceComponent.position")
        if seq not in (None, "1"):
            continue  # keep the first auction only
        for period in ts.iter("Period"):
            p_start = dt.datetime.strptime(period.findtext("timeInterval/start"), "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)
            p_end = dt.datetime.strptime(period.findtext("timeInterval/end"), "%Y-%m-%dT%H:%MZ").replace(tzinfo=UTC)
            step = _iso_duration_minutes(period.findtext("resolution"))
            n = int((p_end - p_start).total_seconds() // (step * 60))
            points = {int(p.findtext("position")): float(p.findtext("price.amount")) for p in period.iter("Point")}
            value = None
            for pos in range(1, n + 1):
                value = points.get(pos, value)
                if value is not None:
                    by_res.setdefault(step, {})[p_start + dt.timedelta(minutes=step * (pos - 1))] = value
    out = {}
    for step in sorted(by_res, reverse=True):  # coarse first, so finer resolutions overwrite
        b = Buckets(tz)
        for t, v in by_res[step].items():
            b.add(t, v)
        out.update(b.days(start, end))
    return out


def fetch_entsoe(zone, start, end, tz):
    """ENTSO-E Transparency Platform. Needs a free token in the environment variable ENTSOE_TOKEN."""
    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if not token:
        raise SourceError("ENTSOE_TOKEN is not set in the environment")
    eic = zone.get("entsoe_eic")
    if not eic:
        raise SourceError("this zone has no entsoe_eic in the config")
    fmt = "%Y%m%d%H%M"
    query = urllib.parse.urlencode({
        "securityToken": token,
        "documentType": "A44",
        "in_Domain": eic,
        "out_Domain": eic,
        "periodStart": local_midnight(start, tz).astimezone(UTC).strftime(fmt),
        "periodEnd": local_midnight(end + dt.timedelta(days=1), tz).astimezone(UTC).strftime(fmt),
    })
    return parse_entsoe(http_get("https://web-api.tp.entsoe.eu/api?" + query, accept="application/xml"), tz, start, end)


def fetch_energyzero(zone, start, end, tz):
    """EnergyZero public endpoint, NL only. Used for --compare: no published terms for re-use."""
    if zone.get("id") != "NL":
        raise SourceError("the EnergyZero cross-check only exists for zone NL")
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    query = urllib.parse.urlencode({
        "fromDate": local_midnight(start, tz).astimezone(UTC).strftime(fmt),
        "tillDate": (local_midnight(end + dt.timedelta(days=1), tz).astimezone(UTC) - dt.timedelta(seconds=1)).strftime(fmt),
        "interval": 4, "usageType": 1, "inclBtw": "false",
    })
    data = json.loads(http_get("https://api.energyzero.nl/v1/energyprices?" + query))
    b = Buckets(tz)
    for row in data.get("Prices", []):
        t = dt.datetime.strptime(row["readingDate"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        b.add(t, row["price"] * 1000.0)
    return b.days(start, end)


FETCHERS = {"smard": fetch_smard, "entsoe": fetch_entsoe}
CHECKERS = {"energyzero": fetch_energyzero}


# ---------- file handling ----------

def load_existing(path, zone_id):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("schema") == SCHEMA and doc.get("zone") == zone_id:
            return doc
    except (OSError, ValueError):
        pass
    return None


def write_atomic(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
        os.chmod(tmp, 0o644)  # a web server or a git checkout must be able to read it
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


LOG_HEADER = "checked_at_utc,date_local,have_today,have_tomorrow,newest_day,days_in_file\n"


def log_state(path, now, today, have):
    """Trial log: one CSV line each time the state of the file changes (new date, a day arrived)."""
    tomorrow = today + dt.timedelta(days=1)
    state = [today.isoformat(), str(int(today.isoformat() in have)), str(int(tomorrow.isoformat() in have)),
             max(have) if have else "", str(len(have))]
    last = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        if len(lines) > 1:
            last = lines[-1].split(",")[1:]
    if last == state:
        return False
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    is_new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if is_new:
            f.write(LOG_HEADER)
        f.write(",".join([now.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")] + state) + "\n")
    return True


def parse_day(text, today):
    if text in ("today", "vandaag"):
        return today
    if text in ("tomorrow", "morgen"):
        return today + dt.timedelta(days=1)
    return dt.date.fromisoformat(text)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch day-ahead prices for one bidding zone into a JSON file.")
    ap.add_argument("--config", default=os.path.join(HERE, "day-prices.config.json"))
    ap.add_argument("--zone", help="bidding zone id from the config (default: the config's zone)")
    ap.add_argument("--source", choices=sorted(FETCHERS), help="where to fetch (default: the config's source)")
    ap.add_argument("--fallback-source", choices=sorted(FETCHERS), help="second source for days the first one lacks")
    ap.add_argument("--date", help="fetch one day: YYYY-MM-DD, today or tomorrow (default: a window up to tomorrow)")
    ap.add_argument("--out", help="output file (default: the config's output)")
    ap.add_argument("--keep", type=int, help="number of most recent days to keep in the file")
    ap.add_argument("--compare", choices=sorted(CHECKERS), help="cross-check the fetched days against another source")
    ap.add_argument("--log-csv", help="trial log: append a line to this CSV file each time the state changes")
    ap.add_argument("--now", help="pretend it is this moment (ISO, local to the zone), for testing")
    ap.add_argument("--dry-run", action="store_true", help="fetch and report, do not write")
    args = ap.parse_args(argv)

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    zone_id = args.zone or cfg["zone"]
    if zone_id not in cfg["zones"]:
        print("unknown zone %s; known: %s" % (zone_id, ", ".join(sorted(cfg["zones"]))), file=sys.stderr)
        return 1
    zone = dict(cfg["zones"][zone_id], id=zone_id)
    tz = ZoneInfo(zone["timezone"])
    source = args.source or cfg.get("source", "smard")
    keep = args.keep or int(cfg.get("keep_days", 8))
    out_path = args.out or os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(args.config)),
                                                         cfg["output"].replace("{zone}", zone_id)))
    now = dt.datetime.fromisoformat(args.now).replace(tzinfo=tz) if args.now else dt.datetime.now(tz)
    today = now.date()

    if args.date:
        start = end = target = parse_day(args.date, today)
    else:
        target = today + dt.timedelta(days=1)
        start, end = today - dt.timedelta(days=max(keep - 2, 0)), target

    try:
        fetched = {d: (source, h) for d, h in FETCHERS[source](zone, start, end, tz).items()}
        if args.fallback_source and args.fallback_source != source:
            missing = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
            missing = [d for d in missing if d not in fetched]
            if missing:
                try:
                    extra = FETCHERS[args.fallback_source](zone, min(missing), max(missing), tz)
                    for d, h in extra.items():
                        fetched.setdefault(d, (args.fallback_source, h))
                except SourceError as e:
                    print("fallback source %s: %s" % (args.fallback_source, e), file=sys.stderr)
    except SourceError as e:
        print("source %s: %s. The existing file is left as it is." % (source, e), file=sys.stderr)
        return 1

    doc = load_existing(out_path, zone_id) or {"schema": SCHEMA, "zone": zone_id, "days": [], "examples": []}
    days = {d["date"]: d for d in doc.get("days", [])}
    before = json.dumps([days, doc.get("examples", [])], sort_keys=True)
    for day, (src, hourly) in sorted(fetched.items()):
        days[day.isoformat()] = {"date": day.isoformat(), "source": src, "hourly": hourly}
    kept = [days[k] for k in sorted(days)[-keep:]]

    # fixed example days (a sunny day, a winter day): fetched once, then kept in the file
    examples = {e["id"]: e for e in doc.get("examples", [])}
    for ex in cfg.get("examples", []):
        have = examples.get(ex["id"])
        if have and have.get("date") == ex["date"]:
            continue
        try:
            d = dt.date.fromisoformat(ex["date"])
            got = FETCHERS[source](zone, d, d, tz)
            if d in got:
                examples[ex["id"]] = {"id": ex["id"], "date": ex["date"], "source": source, "hourly": got[d]}
            else:
                print("example day %s is not available from %s" % (ex["date"], source), file=sys.stderr)
        except SourceError as e:
            print("example day %s: %s" % (ex["date"], e), file=sys.stderr)
    ex_list = [examples[e["id"]] for e in cfg.get("examples", []) if e["id"] in examples]

    used = sorted({d["source"] for d in kept + ex_list})
    new_doc = {
        "schema": SCHEMA,
        "zone": zone_id,
        "zone_label": zone.get("label", zone_id),
        "timezone": zone["timezone"],
        "unit": "EUR/kWh",
        "price_kind": "day-ahead, hourly mean, excluding taxes and supplier fees",
        "updated": now.replace(microsecond=0).isoformat(),
        "sources": {s: SOURCES[s] for s in used},
        "days": kept,
        "examples": ex_list,
    }
    changed = json.dumps([{d["date"]: d for d in kept}, ex_list], sort_keys=True) != before
    if not changed and doc.get("updated"):
        new_doc["updated"] = doc["updated"]

    have = [d["date"] for d in kept]
    print("zone %s, source %s, window %s to %s" % (zone_id, source, start, end))
    print("fetched complete days: %s" % (", ".join(d.isoformat() for d in sorted(fetched)) or "none"))
    gaps = [(start + dt.timedelta(days=i)).isoformat() for i in range((end - start).days + 1)
            if (start + dt.timedelta(days=i)).isoformat() not in have]
    if gaps:
        print("not available (yet): %s" % ", ".join(gaps))

    if args.compare:
        try:
            other = CHECKERS[args.compare](zone, start, end, tz)
            for day in sorted(set(other) | set(fetched)):
                a, b = fetched.get(day, (None, None))[1], other.get(day)
                if a and b:
                    diff = max(abs(x - y) for x, y in zip(a, b) if x is not None and y is not None)
                    print("compare %s %s: max difference %.5f EUR/kWh" % (args.compare, day, diff))
                else:
                    print("compare %s %s: only in %s" % (args.compare, day, source if a else args.compare))
        except SourceError as e:
            print("compare %s: %s" % (args.compare, e), file=sys.stderr)

    if args.dry_run:
        print("dry run: nothing written")
    elif changed or not os.path.exists(out_path):
        write_atomic(out_path, new_doc)
        print("written %s (%d days, %d examples)" % (out_path, len(kept), len(ex_list)))
    else:
        print("no change: %s left as it is" % out_path)

    if args.log_csv and not args.dry_run and log_state(args.log_csv, now, today, have):
        print("trial log: line added to %s" % args.log_csv)

    if target.isoformat() in have:
        return 0
    print("target day %s is not in the file yet: run again later" % target)
    return 3


if __name__ == "__main__":
    sys.exit(main())
