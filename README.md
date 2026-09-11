# OilTrace AI

**Satellite & AIS-Based Oil Spill Detection and Vessel Attribution**

> *Detect the Spill. Trace the Vessel. Protect the Sea.*

Detect an oil spill in SAR satellite imagery, drift it backwards through real
wind and current fields to work out where it was released, then correlate that
space-time window against AIS vessel tracks to produce a **ranked, explained
shortlist** of vessels for investigators to follow up.

```
Satellite image -> spill detection -> location & time -> AIS data
                -> correlation -> ranked suspect vessels
```

Everything runs on **free** data sources. No paid API, no GPU.

---

## Documentation for the team

Detailed, plain-language docs are in [`docs/`](docs/README.md):

| | |
|---|---|
| [What is this?](docs/01-what-is-this.md) | the idea in plain words |
| [Run it](docs/02-run-it.md) | getting it working |
| [How it works](docs/03-how-it-works.md) | the science, step by step |
| [Code tour](docs/04-code-tour.md) | what every file does |
| [Data sources](docs/05-data-sources.md) | what is real, and what it costs |
| [Viva questions](docs/06-viva-questions.md) | what examiners will ask |
| [Troubleshooting](docs/07-troubleshooting.md) | when it breaks |

## Quick start

```bash
./run.sh              # installs deps, starts on http://127.0.0.1:8000
```

Then open <http://127.0.0.1:8000> and press **Fetch Sentinel-1**. That pulls
the newest real satellite scene over the active region, masks the coastline,
runs detection, back-drifts any slick through live wind and currents, and ranks
the vessels that were there.

There is **no simulator and no demo mode**. Every vessel this system stores came
from a real AIS transmission; every scene is a real satellite acquisition. If a
data source is unavailable the pipeline says so rather than inventing a
plausible substitute.

Manual start:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn backend.main:app --reload --port 8000
```

---

## Deploying

The repo ships a `Dockerfile` that runs anywhere, plus configs for the three
common free hosts. The image is ~400 MB and binds `$PORT`, so any container
host works.

```bash
docker build -t oiltrace-ai .
docker run -p 8000:8000 -v oiltrace-data:/data \
  -e AISSTREAM_API_KEY=... -e GFW_API_TOKEN=... \
  -e OILTRACE_REGION=north-sea oiltrace-ai
```

**Render** - `render.yaml` is a Blueprint: New -> Blueprint -> pick this repo.
Set `AISSTREAM_API_KEY` and `GFW_API_TOKEN` in the dashboard (they are marked
`sync: false`, so they are never committed).

**Fly.io** - `flyctl launch --no-deploy`, then
`flyctl secrets set AISSTREAM_API_KEY=... GFW_API_TOKEN=...`,
`flyctl volumes create oiltrace_data --size 1`, then `flyctl deploy`.

**Railway** - New Project -> Deploy from GitHub. It reads the `Dockerfile`
automatically; add the two secrets as variables.

### Free-tier caveats, stated plainly

1. **Free instances sleep.** Render's free plan spins down after ~15 minutes of
   inactivity, and the AIS collector stops with it. Attribution needs AIS from
   the 24 h *before* a satellite pass, so a freshly woken instance has nothing
   to correlate against until history rebuilds. Fly with
   `min_machines_running = 1` avoids this.
2. **Free plans have no persistent disk on Render**, so the SQLite database is
   wiped on redeploy. Incidents are re-derivable from real scenes; AIS history
   is not. Use Fly with a volume, or a paid Render plan, if that matters.
3. **Set the region to one with real AIS coverage.** The default deploy config
   uses `north-sea`. Deploying with `OILTRACE_REGION=mumbai` gives you real
   satellite imagery and no vessel tracks, because free AIS does not cover
   India (see the coverage table above).

## What is real

Every layer below runs on genuine data. There is no simulation in the default
path any more.

| Layer | Source | Key needed |
|---|---|---|
| **SAR imagery** | Sentinel-1 GRD via Microsoft Planetary Computer | **none** |
| **Wind / current / wave / SST** | Open-Meteo marine + weather | **none** |
| **Coastline masking** | Natural Earth 1:10m land polygons | **none** |
| **Live AIS** | aisstream.io | free, GitHub login |
| **Historical AIS** | NOAA MarineCadastre (US waters) | **none** |
| **Global AIS events** | Global Fishing Watch (incl. Indian waters) | free, registration |
| Drift model / attribution | our own physics + scoring | n/a |

`GET /api/provenance` reports, per layer, whether it is real *right now*, and
the dashboard renders that as a strip across the top. It cannot quietly drift
from the truth, because the UI reads the same endpoint.

There is no simulator to fall back on. If Open-Meteo is unreachable and no
cached response exists, the drift model raises rather than fabricating weather -
a confident wrong answer is worse than an honest failure.

## Regions, and the AIS coverage problem

Free AIS is fed by volunteer shore receivers, so coverage is wildly uneven.
Measured with this project's own key on 2026-09-10, vessels seen in ~14 s:

| Region | Live AIS | Notes |
|---|---|---|
| North Sea (NL/DE/DK) | **~740 vessels** | Densest free coverage; EMSA CleanSeaNet's real operating area |
| English Channel | ~48 vessels | Busiest lane in the world |
| Gulf of Mexico | ~42 vessels | Plus free NOAA historical AIS back to 2009 |
| Singapore / Malacca | ~5 vessels | Huge real traffic, almost no receivers |
| **Mumbai / Arabian Sea** | **0 vessels** | No Indian shore receivers feed the free stream |
| **Gulf of Kutch (Sikka)** | **0 vessels** | India's biggest oil terminals; still zero |

This is a data-availability fact, not a bug, and it is worth saying plainly in a
viva. Switch regions live from the dashboard dropdown or:

```bash
curl -X POST localhost:8000/api/regions/north-sea
```

### Real Indian-waters vessel data: Global Fishing Watch

GFW is the only free source that covers India. Register at
globalfishingwatch.org, put the token in `.env` as `GFW_API_TOKEN`, then:

```bash
curl -X POST localhost:8000/api/regions/mumbai
curl -X POST "localhost:8000/api/ingest/gfw?days=60"
```

That returns real vessels off Mumbai - verified: MATSYA VRUSHTI (IND,
IMO 9298844), ONE CONTRIBUTION (JPN, IMO 9629914), BROOKLYN BRIDGE
(JPN, IMO 9458999).

**What a standard GFW token actually grants** - measured, not assumed:

| Endpoint / dataset | Result |
|---|---|
| `/v3/vessels/search` | 200 OK |
| `/v3/4wings/report` + `public-global-fishing-effort` | **200 OK** |
| `/v3/4wings/report` + `public-global-all-vessels-presence` | 403 |
| `/v3/events` (loitering, gaps, encounters, port visits) | 403 |

So the working path is the gridded 4wings fishing-effort report, which does
return real `lat`, `lon`, `mmsi`, `imo`, `callsign`, `shipName`, `flag` and
entry/exit timestamps. Two honest caveats:

1. **Coverage skews to fishing vessels** (plus craft GFW classes as
   INCONCLUSIVE, which is how the two container ships above appear). Dedicated
   tanker and cargo coverage lives in `all-vessels-presence`, which a standard
   token cannot read - ask GFW to add that dataset if you need it.
2. **Gridded, not a track**: one fix per vessel per cell per day, with entry and
   exit times. Enough to place a vessel in a region during a window; not enough
   to interpolate a course or compute a course deviation.

Both limits are returned in the ingest response rather than hidden.

## The pairing trick: real imagery x real AIS

A satellite pass is a single instant, but attribution needs AIS from the
**24 hours before** it. Start collecting live AIS today and your first scene has
nothing to correlate against. Two ways round it:

1. **Let it run.** Leave the collector up for a day, then ingest a scene.
2. **Pair archives.** Take a historical Sentinel-1 scene and the NOAA AIS for
   that same day. Both real, both time-aligned, works immediately:

```bash
curl -X POST localhost:8000/api/regions/gulf-of-mexico
curl -X POST "localhost:8000/api/ingest/noaa?day=2024-12-29&bbox=-89.0,28.2,-86.6,29.6"
curl -X POST "localhost:8000/api/ingest/sentinel1?start=2024-12-29&end=2024-12-30&bbox=-89.0,28.2,-86.6,29.6"
```

That run pulls a real S1A scene from 2024-12-29 23:54 UTC and 43,344 real AIS
positions from 361 real vessels, filtered out of 6.9 million rows.

## What running on real data changed

The synthetic demo passed cleanly. Real Sentinel-1 broke it in four ways, and
each fix is in the code with the reason next to it:

1. **Coastlines read as spills.** The first real Mississippi Delta run outlined
   147 km2 of Louisiana shoreline as oil. Land and sheltered near-shore water
   are radar-dark too. Fixed with `backend/detect/landmask.py` (Natural Earth
   polygons, rasterised, coast buffered ~1.5 km). Same scene now: 0 detections.
2. **Area was buying confidence that contrast never earned.** A 400 km2 patch at
   1.9 dB scored 0.67 on size and smoothness alone. Contrast now *gates* the
   score multiplicatively; those wind shadows are correctly rejected.
3. **Fixed platforms won the ranking.** MATTERHORN TLP - a real tension-leg oil
   platform - placed in the top 8 "suspects" because a structure that never
   moves shows a permanent 97% "slowdown" and sits in the source region forever.
   Stationary infrastructure is now detected and its behaviour factors zeroed.
4. **Scores saturated.** With a large diffuse source region every vessel scored
   1.00 on spatial, temporal and persistence. The spatial kernel now scales to
   the source region's actual size, so discrimination is relative to how
   well-constrained the source really is.

Points 2-4 were invisible in simulation because the synthetic slick was small,
clean and high-contrast. That is the honest argument for testing on real data.

## Backend audit (2026-09-11)

A systematic pass over the API and data layer, each item verified by test:

1. **KPI tiles contradicted the table.** `/api/incidents` was region-scoped but
   `/api/stats` and `/api/alerts` were not, so Mumbai listed 10 incidents while
   the tile above it read 22. All three are scoped consistently now.
2. **Retention silently deleted imported archives.** Housekeeping pruned every
   position older than 14 days, which would have destroyed NOAA 2024 data an
   hour after loading it - breaking the archive-pairing workflow this README
   recommends. `ais_positions.source` now distinguishes the rolling live feed
   from deliberate imports; only the former is pruned.
3. **Live vessels ignored the region.** `/api/vessels/live` returned every
   vessel ever collected, so switching to Mumbai plotted 6,306 North Sea ships
   thousands of kilometres off the visible map. Now bounded to the active AOI.
4. **N+1 query in ranking.** Baseline speed ran one correlated subquery per
   candidate. Batched into a single query.
5. **Land mask rebuilt every ingest** (~7 s rasterising 4,000 coastline rings).
   LRU-cached by bounds and shape.
6. **Bad input returned 502.** Malformed `bbox` or `day` now returns 400.
7. **Detector silently discarded strong targets.** The serious one - see below.

### The detector bug

Against a controlled 8.2 dB target the detector reported **1.7 dB and rejected
it**. Three compounding causes, each fixed:

- The background ring was `dilate(region, 35x35)` - a **fixed pixel size**
  regardless of scene resolution, so it sampled inside the slick's own diffuse
  skirt. It is now an annulus offset by 0.6 km and extending to 3 km, sized in
  kilometres, excluding other detections, and read with a median.
- Contrast was the median across the **whole grown region**. Hysteresis
  deliberately grows the footprint to capture a ragged slick's extent, so that
  median sat in the rim. It now uses the 25th percentile - the sustained dark
  interior.
- Using the high-contrast core instead also failed, because that mask tracks
  the **gradient at the rim**, not the flat interior. Worth knowing if you
  revisit this.

Calibrated against controlled targets, the detector now reads systematically
**0.7-3 dB below true damping** and separates cleanly:

| True damping | Measured | Verdict |
|---|---|---|
| 8.2 dB | 4.9 dB | detected |
| 5.2 dB | 3.8 dB | detected |
| 3.0 dB | 2.3 dB | detected |
| 1.9 dB | 1.6 dB | rejected |
| 1.1 dB | 1.2 dB | rejected |
| 0.6 dB | 1.0 dB | rejected |

Thresholds are set on *measured* values. Set `OILTRACE_DETECT_DEBUG=1` to print
why each candidate region was kept or rejected.

### Tests

```bash
.venv/bin/python tests/test_suite.py     # 56 checks, real data, no fixtures
```

Runs against the live database and live APIs. The only synthetic input is the
detector calibration target, which is confined to the test file and never
reachable from the application.

## How it works

### 1. Detection — `backend/detect/darkspot.py`

Oil damps the short capillary waves that generate radar backscatter, so a slick
appears dark against a wind-roughened sea.

- **Lee speckle filter** — edge-preserving, because slick boundaries are the
  feature we care about.
- **Band-pass contrast** — smooth at the slick scale (~1.5 km) and compare
  against the sea background scale (~10 km). Differencing a raw speckly image
  against a wide background just measures speckle, not the slick.
- **Robust threshold (MAD)** — a plain standard deviation is inflated by the
  slick itself, pushing the threshold above the thing you are looking for.
- **Hysteresis segmentation** — high-confidence cores grown through a permissive
  mask. One fixed threshold either shatters a ragged slick or floods the scene.
- **Look-alike rejection** on contrast (dB), shape complexity, elongation,
  homogeneity and area. Low-wind cells damp the sea ~1–2 dB; mineral oil
  typically 3–10 dB, and that gap does most of the work.

No training data and no GPU required. To upgrade, train a U-Net on the public
[MKLab/CERTH Sentinel-1 oil spill dataset](https://m4d.iti.gr/oil-spill-detection-dataset/)
(~1,100 labelled SAR images) and wire it into `classify_stub()` — nothing else
in the pipeline changes.

### 2. Back-drift — `backend/drift/`

Particles seeded across the observed slick are integrated **backwards** through

```
slick velocity = surface current + ~3% of wind, deflected ~15 deg right (Coriolis)
```

which is the standard empirical approximation in operational oil-drift models.
Each particle carries its own wind factor and deflection angle sampled from
plausible ranges, so the cloud fans into a genuine uncertainty cone instead of
collapsing to one confident, wrong line. Unresolved turbulence is added as a
per-step random walk.

Output is a probability field over *(place, time)*: where could oil have been
released, and when, to end up where we saw it?

### 3. Attribution — `backend/attribute/ranking.py`

Every vessel with AIS coverage in the window is scored on five independent
factors:

| Factor | Weight | Question it answers |
|---|---|---|
| `spatial` | 0.34 | Did it pass through the probable source region? |
| `temporal` | 0.22 | Was it there *at the right time*, or is it a coincidence? |
| `persistence` | 0.14 | Did it linger, or just transit through? |
| `behaviour` | 0.18 | Slow steaming, course deviation, AIS gap? |
| `vessel_type` | 0.12 | Tanker/large cargo, or a fishing boat? |

The `temporal` factor deserves a note, because it is what stops the whole thing
being a proximity search. It is a **contrast**: the vessel's best time-matched
score divided by its best score across *all* time pairings. A ship that transits
this lane every day scores high spatially — and near zero here, because it would
have scored just as well at a time unrelated to the spill.

Every candidate carries plain-language evidence explaining its score.

---

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Service + AOI |
| GET | `/api/stats` | KPI tiles |
| GET | `/api/incidents` | Incident list |
| GET | `/api/incidents/{id}` | Incident + drift + candidates + timeline |
| POST | `/api/incidents/{id}/attribute` | Re-run drift + ranking |
| POST | `/api/incidents/{id}/status` | Confirm / dismiss |
| GET | `/api/vessels/live` | Latest position per vessel |
| GET | `/api/vessels/{mmsi}/track` | One vessel's track |
| GET | `/api/alerts` | Alert feed |
| GET | `/api/ais/status` | Collector health + counts |
| GET | `/api/provenance` | Per-layer real-data audit |
| GET/POST | `/api/regions[/{key}]` | List / switch area of interest |
| POST | `/api/ingest/sentinel1` | Real Sentinel-1 scene -> detection |
| POST | `/api/ingest/gfw` | Real GFW vessel activity (incl. India) |
| POST | `/api/ingest/noaa` | Real NOAA historical AIS (US waters) |

Interactive docs at `/docs`.

---

## Honest limitations

Read these before the viva — an examiner will ask, and having the answer ready
is worth more than pretending they don't exist.

1. **"Real-time" is per satellite pass, not continuous.** Sentinel-1 revisits a
   given point every few days, with hours of product latency. AIS *is* real
   time. So the architecture is: AIS streams continuously into the database;
   satellite arrives per pass; when a spill is detected you query AIS history
   you already collected. The real EMSA CleanSeaNet works exactly this way.
2. **Look-alikes are not solved.** Low-wind cells, biogenic films, rain cells
   and wakes all darken SAR. The feature rules here are heuristics from the
   literature, not a trained classifier, and the confidence is not calibrated
   against labelled data.
3. **The drift field is a single point in space.** Wind and current are sampled
   at one representative location for the whole AOI, ignoring spatial shear
   across ~200 km. Reasonable for a 24 h backtrack in open water; not a
   hydrodynamic model.
4. **SAR cannot measure slick thickness.** Volume is reported as a wide range
   (1–50 µm) on purpose. A single number would be false precision.
5. **No ground truth exists.** There is no public dataset of "which vessel was
   actually responsible," so the ranking can be assessed qualitatively but not
   scored against labels.
6. **Free AIS coverage over the Arabian Sea is patchy.** Terrestrial receivers
   cover coasts well and open ocean poorly. Satellite AIS is commercial.
7. **This ranks; it does not accuse.** The output is a prioritised shortlist for
   a human investigator. Nothing here is evidence of guilt.
8. **Inland water is not masked.** Natural Earth's coastline covers the coast,
   not lakes, reservoirs or marsh. In a scene that is largely land - the Gulf
   of Mexico AOI is 76% masked - sheltered inland water still reads dark and
   can produce a large false detection. Prefer an open-water bbox, or add an
   inland-water layer to `landmask.py`.
9. **The detector under-reads contrast by 0.7-3 dB** against controlled targets
   (see the audit section). Thresholds are calibrated for that, but it means
   recall falls off below roughly 2.5 dB of true damping.

---

## Layout

```
backend/
  config.py            all tunables, one place
  db.py                SQLite schema + helpers (WAL mode)
  geo.py               local-projection geodesy
  pipeline.py          scene -> incident -> drift -> ranking
  main.py              FastAPI app
  regions.py           switchable AOI profiles + measured AIS coverage
  vessel_types.py      AIS ship-type codes
  ingest/
    ais_stream.py      live aisstream.io collector
    sentinel1.py       real Sentinel-1 GRD (Planetary Computer)
    gfw.py             Global Fishing Watch activity
    noaa_ais.py        NOAA MarineCadastre historical AIS
  detect/
    darkspot.py        SAR dark-spot detection
    landmask.py        Natural Earth coastline masking
  drift/
    env_data.py        Open-Meteo wind/current, disk-cached
    backtrack.py       Monte Carlo backward drift
  attribute/
    ranking.py         correlation + five-factor scoring
frontend/
  index.html, app.js   dashboard (Leaflet vendored locally)
```

Leaflet is vendored in `frontend/vendor/` on purpose — the app shell loads with
no internet, so a dead wifi during a demo costs you map tiles, not the whole app.
