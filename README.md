# day-prices

Day-ahead electricity prices for one bidding zone as a small JSON file, refreshed every day.

- File: <https://qvantum-nl.github.io/day-prices/day-prices-NL.json>
- Data: [Bundesnetzagentur | SMARD.de](https://www.smard.de/), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Changes made: converted from EUR/MWh to EUR/kWh and reduced to one value per hour, the mean of the four quarter-hour prices.
- Prices exclude taxes and supplier fees.
- Status: trial.

If you show the prices, keep the attribution "Bundesnetzagentur | SMARD.de" with them.

## How it works

A scheduled workflow runs `script/fetch_day_prices.py` every half hour from early afternoon until late evening. GitHub starts scheduled runs late and skips many of them, so there are many attempts; one is enough. The first run that finds tomorrow's prices writes `docs/day-prices-NL.json` and publishes it. Later runs that day change nothing.

The day a run checks follows the local clock: before noon today, from noon on tomorrow. When that day is still missing from 22:00 local time, or in the morning of the day itself, the workflow fails once for that day so that someone gets a message (`trial/alarm.txt` remembers the day). Each run also looks back a week and adds days the source delivered late. The file keeps the eight most recent days.

When the source has no prices for a day, that day is simply not in the file. A page that reads the file should show the last known day with its date.

`trial/log.csv` gets a line each time the state of the file changes: which day it was, and whether today and tomorrow were in the file.

## File format

```json
{
  "schema": 1,
  "zone": "NL",
  "zone_label": "Nederland",
  "timezone": "Europe/Amsterdam",
  "unit": "EUR/kWh",
  "updated": "2026-11-21T13:40:04+01:00",
  "sources": {"smard": {"name": "Bundesnetzagentur | SMARD.de", "licence": "CC BY 4.0"}},
  "days": [{"date": "2026-11-22", "source": "smard", "hourly": [0.19099, 0.19035]}],
  "examples": [{"id": "sunny", "date": "2026-04-26", "source": "smard", "hourly": [0.12, 0.1]}]
}
```

`hourly` holds 24 values by local clock hour. On the day the clock goes forward, the hour that does not exist is `null`. On the day the clock goes back, the hour that happens twice is the mean of both.

## Run it yourself

Python 3.9 or newer, standard library only.

```bash
python3 script/test_fetch_day_prices.py
python3 script/fetch_day_prices.py
python3 script/fetch_day_prices.py --date 2026-11-22
```

Exit code 0: the file holds the target day. Exit code 3: the target day is not published yet. Exit code 4: the target day is overdue, reported once per day when `--alarm-state` is given. Exit code 1: error, the file is left as it is.

## Another bidding zone

Zones are listed in `script/day-prices.config.json` with a label, a time zone and the code at the source. Set `"zone"` there, or pass `--zone`. SMARD only covers Germany and its neighbours. For other zones the script can read the ENTSO-E Transparency Platform (`--source entsoe`), which needs a token in the environment variable `ENTSOE_TOKEN`. That path has only been tested against a hand-made sample, and the re-use terms of ENTSO-E are stricter: check them first.
