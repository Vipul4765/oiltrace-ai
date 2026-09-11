# 4. Code tour

Every file, what it does, and when you would open it.

```
backend/
  config.py          all settings in one place
  regions.py         the six areas of interest
  db.py              SQLite tables and helpers
  geo.py             distance / bearing maths
  vessel_types.py    AIS ship-type codes
  jobs.py            background job progress
  pipeline.py        glues the stages together
  main.py            the web API

  ingest/            getting data IN
    ais_stream.py      live ship positions
    sentinel1.py       satellite images
    noaa_ais.py        historical ship positions
    gfw.py             global ship activity

  detect/            finding slicks
    darkspot.py        the detector
    landmask.py        painting out the coastline

  drift/             where did it come from
    env_data.py        wind and current
    backtrack.py       backwards drift model

  attribute/         who did it
    ranking.py         the five-factor scoring

frontend/
  index.html         page structure and styling
  app.js             all the behaviour
  vendor/            Leaflet map library, stored locally on purpose
```

---

## The core

### `config.py`
Every tunable number lives here — scoring weights, drift parameters, thresholds,
AOI bounds. Change behaviour here, not scattered through the code.

Reads `.env` for secrets. `set_region()` switches the area of interest at
runtime.

### `regions.py`
Six regions, each with its bounding box and — importantly — its **measured**
AIS coverage. Those numbers are real measurements taken with our own API key,
not guesses. Mumbai says `ais_live="none"` because we tested it and saw zero
vessels.

### `db.py`
SQLite in WAL mode. Six tables: `incidents`, `ais_positions`, `vessels`,
`candidates`, `incident_timeline`, `alerts`, `drift_runs`.

One detail worth knowing: `ais_positions.source` marks each row as `live`,
`noaa` or `gfw`. Retention only deletes old `live` rows. Imported historical
archives are expensive to re-fetch and are never pruned. Without this, loading
a 2024 NOAA archive would see it silently deleted within the hour.

### `geo.py`
Distance, bearing, point-in-polygon, track interpolation. The area is only
~200 km across, so a flat-earth approximation is accurate to well under AIS
noise — no heavy GIS library needed.

### `pipeline.py`
The orchestrator: scene → detection → incident → drift → ranking. If you want
to understand the flow, read this file first.

### `main.py`
FastAPI. ~20 endpoints. Interactive docs at `/docs` when running.

Everything is **region-scoped** — the incident list, the KPI tiles, the alerts,
the vessels on the map, and the reports all filter to the active region. They
used to disagree with each other, which made the dashboard contradict itself.

---

## Getting data in (`ingest/`)

### `ais_stream.py` — live ship positions
WebSocket to aisstream.io. Built to be frugal, because they throttle heavy
users: one tight bounding box, filtered message types, compression, one stored
position per vessel per 30 seconds, batched writes, and backoff on reconnect.

Handles AIS's "not available" sentinel values — heading 511, course 360, speed
102.3 — converting them to `None` instead of storing nonsense.

### `sentinel1.py` — satellite images
Searches Microsoft Planetary Computer, then reads **only our area** out of the
scene using HTTP range requests. That is why a fetch moves a few MB instead of
a 1 GB product.

One subtlety: Sentinel-1 GRD is in **radar geometry** — no map projection, just
a grid of ground control points. It needs `WarpedVRT` to convert. A plain read
fails with "CRS is invalid: None".

### `noaa_ais.py` — historical ship positions
Free daily archives of US-waters AIS, no key at all, back to 2009. ~300 MB per
day, downloaded once and cached, then filtered to our box. This is the best
free AIS anywhere — full position tracks with complete vessel identity.

### `gfw.py` — global ship activity
Global Fishing Watch — the only free source covering Indian waters.

Read the docstring before using it. A standard token only opens the
fishing-effort dataset; the events API and the all-vessels dataset both return
403. And it returns **gridded daily activity**, not continuous tracks, so you
can place a vessel in a region during a window but cannot compute its course.

---

## Finding slicks (`detect/`)

### `darkspot.py`
The detector. Explained in [How it works](03-how-it-works.md#stage-1--detect-the-slick).

Set `OILTRACE_DETECT_DEBUG=1` and it prints why each candidate region was kept
or rejected. Use this when a detection you expect vanishes.

The trickiest part is measuring contrast. Three attempts failed before the
current one worked, and the reasoning is written into the comments:

- A background ring pressed against the slick edge sits inside its own diffuse
  skirt → read a 7 dB target as 1.2 dB.
- The median across the whole grown region includes rim pixels → read an 8.2 dB
  target as 1.7 dB.
- The high-contrast core mask tracks the *gradient at the rim*, not the flat
  interior → also wrong.
- **What works:** the 25th percentile inside (the sustained dark level) against
  an annulus offset 0.6–3 km away, sized in kilometres, read with a median.

### `landmask.py`
Downloads Natural Earth coastline polygons once, rasterises them onto the scene
grid, buffers the coast by ~1.5 km. Cached, because rasterising ~4,000 rings
takes seconds and the same area gets asked for repeatedly.

---

## Drift (`drift/`)

### `env_data.py`
Wind and current from Open-Meteo, cached on disk.

Watch the direction conventions — they are opposite and easy to get backwards:
- wind direction is where it blows **FROM**
- current direction is where it flows **TO**

If the data is missing, this module **raises**. It does not substitute an
average. A confident drift path computed from invented weather is worse than an
honest failure.

### `backtrack.py`
Monte Carlo backwards drift. 600 particles, each with its own wind factor and
deflection. Outputs the uncertainty cone, the mean path, and probable source
regions at several times back.

---

## Scoring (`attribute/`)

### `ranking.py`
The five factors. Read `rank_candidates()` top to bottom — it follows the
logic in order.

The `temporal` contrast and the stationary-infrastructure check are the two
pieces most worth understanding; both are explained in
[How it works](03-how-it-works.md#stage-3--4--match-ais-and-score).

---

## Frontend

### `index.html`
Structure and all CSS. Six views, switched by the nav.

### `app.js`
Everything else. Key pieces:

- `showView()` — the router. Moves the single Leaflet map between the dashboard
  panel and the full-page map view rather than keeping two maps in sync.
- `runJob()` — polls a real backend job. The progress bar only moves when the
  server reports a stage that actually started. **There is no timer-driven
  animation.**
- `renderProvenance()` — the honesty strip along the top.
- `moveMap()` — animates only when the map is visible. Animating a hidden map
  gives it zero size and Leaflet throws `Invalid LatLng (NaN, NaN)`.

### `vendor/`
Leaflet is stored locally, not loaded from a CDN. If the wifi dies during your
demo you lose map tiles, not the whole application.

---

## Tests and tools

| File | What it does |
|---|---|
| `tests/test_suite.py` | 56 checks against real data, no mock fixtures |
| `tools/audit.py` | database integrity, cross-region consistency |
| `tools/api_scan.py` | every route, parameter matrix, response types |
| `tools/ui_test.py` | drives the real page in Chrome, catches JS errors |

Run all four before pushing. They exist because each one caught a real bug that
code review missed.
