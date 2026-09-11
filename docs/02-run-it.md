# 2. Run it

## Requirements

- Python 3.10 or newer
- About 1 GB of disk
- Internet (the whole point is that it fetches real data)

## Start it

```bash
git clone https://github.com/Vipul4765/oiltrace-ai.git
cd oiltrace-ai
./run.sh
```

`run.sh` creates a virtual environment, installs dependencies, and starts the
server. First run takes a few minutes — `rasterio` and `opencv` are large.

Open **http://127.0.0.1:8000**

To stop it: `Ctrl+C`. To use a different port: `./run.sh 8080`.

## Add the API keys

Without keys it still runs — real satellite images, real weather, real
coastlines. You just get no live vessel traffic.

```bash
cp .env.example .env
```

Then edit `.env`:

```
AISSTREAM_API_KEY=...     # free: sign in at aisstream.io with GitHub
GFW_API_TOKEN=...         # free: globalfishingwatch.org/our-apis/tokens
OILTRACE_REGION=north-sea
AIS_SOURCE=live
```

Restart after editing. **Never commit `.env`** — it is in `.gitignore` for a
reason.

## The demo that actually shows everything

The dashboard will often show **no candidate vessels**, and that is correct
behaviour, not a bug — see [How it works](03-how-it-works.md#the-timing-problem).

To see the full pipeline including ranked ships, pair a satellite scene with an
archived AIS day from the *same date*:

```bash
curl -X POST "localhost:8000/api/regions/gulf-of-mexico"

# Real AIS for 29 Dec 2024 (downloads ~276 MB the first time, then cached)
curl -X POST "localhost:8000/api/ingest/noaa?day=2024-12-29&bbox=-89.0,28.2,-86.6,29.6"

# Real satellite scene from the same day and place
curl -X POST "localhost:8000/api/ingest/sentinel1?start=2024-12-29&end=2024-12-30&bbox=-89.0,28.2,-86.6,29.6"
```

Now the Top Candidate Vessels panel fills with real ships — real names, real
MMSI numbers, real evidence.

## Checking it is healthy

```bash
.venv/bin/python tests/test_suite.py   # 56 checks against real data
.venv/bin/python tools/audit.py        # database + cross-region integrity
.venv/bin/python tools/api_scan.py     # every API route and response type
.venv/bin/python tools/ui_test.py      # drives the real page, reports JS errors
```

All four should exit clean. Run them after any change — that is what they are
for. If you break something, they will tell you before your teammates do.
