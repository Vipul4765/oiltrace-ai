"""OilTrace AI - FastAPI backend.

Run:  .venv/bin/uvicorn backend.main:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend import config, db, jobs, pipeline, regions
from backend.vessel_types import ship_type_name
from backend.ingest import ais_stream

_stop = asyncio.Event()
_tasks: list[asyncio.Task] = []
_collector: asyncio.Task | None = None


def _start_collector() -> str:
    """(Re)start the AIS collector for the currently active region.

    MUST be called from the event loop (async context). Cancel-then-create is
    ordered so a failure cannot leave us with no collector at all.
    """
    global _collector
    asyncio.get_running_loop()         # fail loudly, not silently, off-loop
    if _collector and not _collector.done():
        _collector.cancel()
    if config.AIS_SOURCE == "live":
        _collector = asyncio.create_task(ais_stream.run_collector(_stop))
        return "live"
    _collector = None
    return "off"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    _stop.clear()
    mode = _start_collector()
    print(f"[oiltrace] region: {config.ACTIVE_REGION} ({config.AOI_NAME})")
    print(f"[oiltrace] AIS: {mode}")
    _tasks.append(asyncio.create_task(_housekeeping()))
    yield
    _stop.set()
    for t in _tasks + ([_collector] if _collector else []):
        t.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()


async def _housekeeping() -> None:
    """Drop AIS older than the retention window so the DB stays small."""
    while not _stop.is_set():
        try:
            cutoff = time.time() - config.AIS_RETENTION_DAYS * 86400
            with db.tx() as conn:
                n = db.prune_old_positions(conn, cutoff)
            if n:
                print(f"[oiltrace] pruned {n} AIS positions older than "
                      f"{config.AIS_RETENTION_DAYS} days")
        except Exception as exc:
            print(f"[oiltrace] housekeeping failed: {exc}")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


app = FastAPI(title="OilTrace AI",
              description="Satellite & AIS-based oil spill detection and "
                          "vessel attribution",
              version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


def _loads(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# --- system ------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "ais_source": config.AIS_SOURCE,
            "region": config.ACTIVE_REGION,
            "aoi": {"name": config.AOI_NAME,
                    "lat_min": config.AOI_LAT_MIN, "lat_max": config.AOI_LAT_MAX,
                    "lon_min": config.AOI_LON_MIN, "lon_max": config.AOI_LON_MAX}}


@app.get("/api/ais/status")
def ais_status() -> dict:
    row = db.query_one(
        "SELECT COUNT(*) c, COUNT(DISTINCT mmsi) v, MAX(ts) latest FROM ais_positions")
    here = db.query_one(
        "SELECT COUNT(*) c, COUNT(DISTINCT mmsi) v FROM ais_positions"
        " WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (config.AOI_LAT_MIN, config.AOI_LAT_MAX,
         config.AOI_LON_MIN, config.AOI_LON_MAX))
    live = ais_stream.stats() if config.AIS_SOURCE == "live" else {}
    return {"source": config.AIS_SOURCE,
            "positions": row["c"], "vessels": row["v"], "latest_ts": row["latest"],
            "region": config.ACTIVE_REGION,
            "positions_in_region": here["c"], "vessels_in_region": here["v"],
            "collector": live}


@app.get("/api/stats")
def stats(days: int = Query(30, ge=1, le=365),
          region: str = Query(None, description="AOI key, or 'all'")) -> dict:
    """Scoped to the active region, matching /api/incidents. When these two
    disagreed the KPI tiles claimed 22 incidents while the table listed 10."""
    since = time.time() - days * 86400
    scope = config.ACTIVE_REGION if region is None else region
    rfilter = "" if scope == "all" else " AND region = ?"
    rparams = () if scope == "all" else (scope,)
    inc = db.query_one(
        "SELECT COUNT(*) total, SUM(CASE WHEN status='Confirmed' THEN 1 ELSE 0 END)"
        f" confirmed FROM incidents WHERE detected_at >= ?{rfilter}",
        (since, *rparams))
    jfilter = "" if scope == "all" else " AND i.region = ?"
    ves = db.query_one(
        "SELECT COUNT(DISTINCT mmsi) v FROM candidates c JOIN incidents i"
        f" USING(incident_id) WHERE i.detected_at >= ?{jfilter}",
        (since, *rparams))
    # Real latency: satellite acquisition -> this system produced a result.
    # Measured from actual clock values, never from assumed stage offsets.
    lat = db.query_one(
        "SELECT AVG(i.created_at - i.detected_at) s FROM incidents i"
        f" WHERE i.detected_at >= ? AND i.created_at IS NOT NULL{jfilter}",
        (since, *rparams))
    return {
        "window_days": days,
        "region": scope,
        "total_incidents": inc["total"] or 0,
        "confirmed_spills": inc["confirmed"] or 0,
        "vessels_analysed": ves["v"] or 0,
        "avg_detection_hours": (round(lat["s"] / 3600, 1)
                                if lat and lat["s"] is not None else None),
    }


@app.get("/api/alerts")
def alerts(limit: int = Query(12, ge=1, le=100),
           region: str = Query(None, description="AOI key, or 'all'")) -> list[dict]:
    """Region-scoped like everything else, so the feed matches the map."""
    scope = config.ACTIVE_REGION if region is None else region
    if scope == "all":
        return db.rows_to_dicts(
            db.query("SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)))
    return db.rows_to_dicts(db.query(
        "SELECT a.* FROM alerts a LEFT JOIN incidents i USING(incident_id)"
        " WHERE a.incident_id IS NULL OR i.region = ?"
        " ORDER BY a.ts DESC LIMIT ?", (scope, limit)))


# --- incidents ---------------------------------------------------------------

@app.get("/api/incidents")
def list_incidents(limit: int = Query(50, ge=1, le=500),
                   region: str = Query(None, description="AOI key, or 'all'")
                   ) -> list[dict]:
    """Scoped to the active region by default - showing a Louisiana incident
    while the map sits over Mumbai is simply misleading."""
    scope = config.ACTIVE_REGION if region is None else region
    where = "" if scope == "all" else " WHERE i.region = ?"
    params = () if scope == "all" else (scope,)
    rows = db.query(
        "SELECT i.*, (SELECT COUNT(*) FROM candidates c"
        "   WHERE c.incident_id=i.incident_id) candidate_count,"
        " (SELECT v.name FROM candidates c JOIN vessels v USING(mmsi)"
        "   WHERE c.incident_id=i.incident_id ORDER BY c.rank LIMIT 1) top_vessel,"
        " (SELECT c.score FROM candidates c WHERE c.incident_id=i.incident_id"
        "   ORDER BY c.rank LIMIT 1) top_score"
        f" FROM incidents i{where} ORDER BY i.detected_at DESC LIMIT ?",
        (*params, limit))
    out = []
    for r in rows:
        d = dict(r)
        d["polygon"] = _loads(d.pop("polygon"), [])
        out.append(d)
    return out


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str) -> dict:
    row = db.query_one("SELECT * FROM incidents WHERE incident_id=?", (incident_id,))
    if not row:
        raise HTTPException(404, f"unknown incident {incident_id}")
    inc = dict(row)
    inc["polygon"] = _loads(inc.pop("polygon"), [])
    # The incident's OWN region, not whatever the UI happens to be showing.
    from backend import regions as _regmod
    inc["region_name"] = (_regmod.get(inc["region"]).name
                          if inc.get("region") else None)

    drift_row = db.query_one("SELECT * FROM drift_runs WHERE incident_id=?",
                             (incident_id,))
    drift = _loads(drift_row["result"], None) if drift_row else None

    cands = []
    for c in db.query(
            "SELECT c.*, v.name, v.imo, v.callsign, v.ship_type, v.length_m,"
            " v.width_m, v.draught_m, v.destination FROM candidates c"
            " LEFT JOIN vessels v USING(mmsi) WHERE c.incident_id=?"
            " ORDER BY c.rank", (incident_id,)):
        d = dict(c)
        d["components"] = _loads(d.pop("components"), {})
        d["evidence"] = _loads(d.pop("evidence"), [])
        d["track"] = _loads(d.pop("track"), [])
        d["vessel_type"] = ship_type_name(d.get("ship_type"))
        d["name"] = d.get("name") or f"MMSI {d['mmsi']}"
        cands.append(d)

    timeline = db.rows_to_dicts(db.query(
        "SELECT ts, stage, detail FROM incident_timeline WHERE incident_id=?"
        " ORDER BY ts", (incident_id,)))

    return {"incident": inc, "drift": drift, "candidates": cands,
            "timeline": timeline}


@app.post("/api/incidents/{incident_id}/attribute")
def rerun_attribution(incident_id: str,
                      hours: float = Query(None, ge=1, le=72)) -> dict:
    try:
        r = pipeline.run_attribution(incident_id, hours=hours)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"incident_id": incident_id, "candidates": len(r["candidates"]),
            "drift_ms": r["drift_ms"]}


@app.post("/api/incidents/{incident_id}/status")
def set_status(incident_id: str, status: str = Query(...)) -> dict:
    allowed = {"Under Investigation", "Confirmed", "Dismissed", "Closed"}
    if status not in allowed:
        raise HTTPException(400, f"status must be one of {sorted(allowed)}")
    with db.tx() as conn:
        cur = conn.execute("UPDATE incidents SET status=? WHERE incident_id=?",
                           (status, incident_id))
        if not cur.rowcount:
            raise HTTPException(404, f"unknown incident {incident_id}")
        db.add_alert(conn, time.time(), "status",
                     f"{incident_id} marked {status}", incident_id)
    return {"incident_id": incident_id, "status": status}


# --- vessels -----------------------------------------------------------------

@app.get("/api/vessels/live")
def vessels_live(minutes: int = Query(60, ge=1, le=1440),
                 in_aoi: bool = Query(True)) -> list[dict]:
    """Latest known position per vessel, inside the active AOI.

    The AOI filter is the point: without it, switching to Mumbai still plotted
    every North Sea vessel collected earlier, thousands of kilometres off the
    map being displayed.
    """
    since = time.time() - minutes * 60
    # Unqualified column names: this clause goes INSIDE the subquery, where the
    # outer alias `p` is not in scope.
    box = ("AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?" if in_aoi else "")
    bparams = ([config.AOI_LAT_MIN, config.AOI_LAT_MAX,
                config.AOI_LON_MIN, config.AOI_LON_MAX] if in_aoi else [])
    rows = db.query(
        "SELECT p.mmsi, p.ts, p.lat, p.lon, p.sog, p.cog, p.heading,"
        " v.name, v.ship_type, v.length_m, v.destination"
        " FROM ais_positions p JOIN (SELECT mmsi, MAX(ts) mts FROM ais_positions"
        f"   WHERE ts >= ? {box} GROUP BY mmsi) last"
        "  ON p.mmsi=last.mmsi AND p.ts=last.mts"
        " LEFT JOIN vessels v ON v.mmsi=p.mmsi ORDER BY v.name",
        (since, *bparams))
    out = []
    for r in rows:
        d = dict(r)
        d["vessel_type"] = ship_type_name(d.get("ship_type"))
        d["name"] = d.get("name") or f"MMSI {d['mmsi']}"
        out.append(d)
    return out


@app.get("/api/vessels")
def list_vessels(limit: int = Query(300, ge=1, le=2000),
                 hours: float = Query(72, ge=0.5, le=8760),
                 q: str = Query(None, description="name / MMSI / IMO filter"),
                 in_aoi: bool = Query(True)) -> list[dict]:
    """Every vessel we hold real positions for, newest activity first."""
    since = time.time() - hours * 3600
    where = ["p.ts >= ?"]
    params: list = [since]
    if in_aoi:
        where.append("p.lat BETWEEN ? AND ? AND p.lon BETWEEN ? AND ?")
        params += [config.AOI_LAT_MIN, config.AOI_LAT_MAX,
                   config.AOI_LON_MIN, config.AOI_LON_MAX]
    if q:
        where.append("(v.name LIKE ? OR CAST(p.mmsi AS TEXT) LIKE ? OR v.imo LIKE ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    rows = db.query(
        "SELECT p.mmsi, COUNT(*) fixes, MAX(p.ts) last_seen, MIN(p.ts) first_seen,"
        " AVG(p.sog) avg_sog, MAX(p.sog) max_sog,"
        " v.name, v.imo, v.callsign, v.ship_type, v.length_m, v.destination"
        " FROM ais_positions p LEFT JOIN vessels v ON v.mmsi = p.mmsi"
        f" WHERE {' AND '.join(where)}"
        " GROUP BY p.mmsi ORDER BY last_seen DESC LIMIT ?", (*params, limit))
    out = []
    for r in rows:
        d = dict(r)
        d["vessel_type"] = ship_type_name(d.get("ship_type"))
        d["name"] = d.get("name") or f"MMSI {d['mmsi']}"
        out.append(d)
    return out


@app.get("/api/reports/summary")
def reports_summary(days: int = Query(90, ge=1, le=3650),
                    region: str = Query(None, description="AOI key, or 'all'")
                    ) -> dict:
    """Real aggregates. Every figure is a COUNT or AVG over stored records.

    Region-scoped by default, matching /api/incidents and /api/stats. A report
    showing Mumbai totals while the dashboard sits on Gulf of Kutch is the same
    contradiction the KPI tiles used to have.
    """
    since = time.time() - days * 86400
    scope = config.ACTIVE_REGION if region is None else region
    rf = "" if scope == "all" else " AND region = ?"
    rp = () if scope == "all" else (scope,)
    by_region = db.rows_to_dicts(db.query(
        "SELECT COALESCE(region,'unknown') region, COUNT(*) n,"
        " ROUND(AVG(area_km2),1) avg_area, ROUND(AVG(confidence),3) avg_conf"
        f" FROM incidents WHERE detected_at >= ?{rf} GROUP BY region ORDER BY n DESC",
        (since, *rp)))
    by_status = db.rows_to_dicts(db.query(
        f"SELECT status, COUNT(*) n FROM incidents WHERE detected_at >= ?{rf}"
        " GROUP BY status", (since, *rp)))
    scenes = db.rows_to_dicts(db.query(
        "SELECT scene_id, COUNT(*) detections, MIN(detected_at) acquired"
        f" FROM incidents WHERE detected_at >= ? AND scene_id IS NOT NULL{rf}"
        " GROUP BY scene_id ORDER BY acquired DESC LIMIT 20", (since, *rp)))
    # AIS holdings are counted inside the AOI unless explicitly global.
    if scope == "all":
        ais = db.query_one(
            "SELECT COUNT(*) positions, COUNT(DISTINCT mmsi) vessels,"
            " MIN(ts) oldest, MAX(ts) newest FROM ais_positions")
    else:
        r = regions.get(scope)
        ais = db.query_one(
            "SELECT COUNT(*) positions, COUNT(DISTINCT mmsi) vessels,"
            " MIN(ts) oldest, MAX(ts) newest FROM ais_positions"
            " WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (r.lat_min, r.lat_max, r.lon_min, r.lon_max))
    conf = db.rows_to_dicts(db.query(
        "SELECT CASE WHEN confidence >= 0.7 THEN 'high (>=70%)'"
        "             WHEN confidence >= 0.45 THEN 'medium (45-70%)'"
        "             ELSE 'low (<45%)' END band, COUNT(*) n"
        f" FROM incidents WHERE detected_at >= ?{rf} GROUP BY band", (since, *rp)))
    return {"window_days": days, "region": scope, "by_region": by_region,
            "by_status": by_status, "confidence_bands": conf,
            "recent_scenes": scenes, "ais": dict(ais) if ais else {}}


@app.get("/api/vessels/{mmsi}/track")
def vessel_track(mmsi: int, hours: float = Query(24, ge=0.5, le=8760)) -> dict:
    """Track window matches /api/vessels (up to a year). They were inconsistent:
    a vessel listed from a 1-year window could not have its track fetched."""
    since = time.time() - hours * 3600
    v = db.query_one("SELECT * FROM vessels WHERE mmsi=?", (mmsi,))
    rows = db.query(
        "SELECT ts, lat, lon, sog, cog FROM ais_positions"
        " WHERE mmsi=? AND ts>=? ORDER BY ts", (mmsi, since))
    vessel = dict(v) if v else {"mmsi": mmsi}
    vessel["vessel_type"] = ship_type_name(vessel.get("ship_type"))
    return {"vessel": vessel,
            "track": [[r["ts"], r["lat"], r["lon"], r["sog"], r["cog"]] for r in rows]}


# --- regions -----------------------------------------------------------------

@app.get("/api/regions")
def list_regions() -> dict:
    from backend import regions as regmod
    return {"active": config.ACTIVE_REGION,
            "regions": [r.to_dict() for r in regmod.REGIONS.values()]}


@app.post("/api/regions/{key}")
async def switch_region(key: str) -> dict:
    # async on purpose: _start_collector calls asyncio.create_task, which needs
    # a running event loop. A sync def would be dispatched to a worker thread
    # and would kill the collector without being able to start a new one.
    from backend import regions as regmod
    if key not in regmod.REGIONS:
        raise HTTPException(404, f"unknown region '{key}'. "
                                 f"Options: {regmod.keys()}")
    r = config.set_region(key)
    mode = _start_collector()          # resubscribe to the new bounding box
    await asyncio.sleep(0)             # let the new task get on the loop
    return {"active": config.ACTIVE_REGION, "region": r.to_dict(),
            "ais_mode": mode,
            "note": "AIS collector resubscribed to the new bounding box"}


@app.get("/api/provenance")
def provenance() -> dict:
    """Exactly which layers are real right now, and which are not.

    Exposed as an endpoint so the UI can never quietly drift from the truth.
    """
    from backend.ingest import gfw
    region = config.active_region()
    # "live mode" is not the same as "actually receiving data": aisstream is
    # configured globally but has zero receivers over some regions. Only claim
    # real AIS when the active region genuinely has coverage.
    ais_configured_live = config.AIS_SOURCE == "live"
    ais_real = ais_configured_live and region.ais_live != "none"
    row = db.query_one(
        "SELECT COUNT(*) c, MAX(ts) latest FROM ais_positions"
        " WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (config.AOI_LAT_MIN, config.AOI_LAT_MAX,
         config.AOI_LON_MIN, config.AOI_LON_MAX))
    scenes = db.query_one(
        "SELECT COUNT(*) c FROM incidents WHERE source='sentinel-1'")
    return {
        "region": region.to_dict(),
        "layers": {
            "sar_imagery": {
                "real": True, "source": "Sentinel-1 GRD via Microsoft Planetary Computer",
                "note": "anonymous SAS access, windowed COG reads",
                "real_incidents": scenes["c"] or 0},
            "environment": {
                "real": True, "source": "Open-Meteo marine + weather",
                "note": "live wind, current, wave height, SST"},
            "drift_model": {"real": True, "source": "Monte Carlo advection",
                            "note": "physics, not data"},
            "attribution": {"real": True, "source": "five-factor scoring",
                            "note": "algorithm, not data"},
            "ais": {
                "real": ais_real,
                "configured_mode": config.AIS_SOURCE,
                "source": ("aisstream.io live feed" if ais_real else
                           "aisstream.io live feed (NO COVERAGE in this region)"
                           if ais_configured_live else "AIS collector disabled"),
                "note": region.ais_note,
                "coverage": region.ais_live,
                "positions": row["c"] or 0,
                "latest_ts": row["latest"]},
        },
        "optional_sources": {
            "global_fishing_watch": gfw.status(),
            "gfw_positions": (db.query_one(
                "SELECT COUNT(*) c FROM ais_positions p WHERE p.lat BETWEEN ? AND ?"
                " AND p.lon BETWEEN ? AND ?",
                (config.AOI_LAT_MIN, config.AOI_LAT_MAX,
                 config.AOI_LON_MIN, config.AOI_LON_MAX))["c"] or 0),
            "noaa_marinecadastre": {"configured": True,
                                    "note": "no token required, US waters only"},
        },
        "fully_real": ais_real,
        "caveat": (None if ais_real else
                   f"AIS is not real for '{region.key}': {region.ais_note}"),
    }


# --- Sentinel-1 --------------------------------------------------------------

@app.post("/api/ingest/sentinel1")
def ingest_sentinel1(days: int = Query(45, ge=1, le=180),
                     polarization: str = Query("vv", pattern="^(vv|vh|hh|hv)$"),
                     min_area_km2: float = Query(1.0, ge=0.1, le=500),
                     max_pixels: int = Query(1400, ge=256, le=4000),
                     start: str = Query(None, description="ISO date, historical search"),
                     end: str = Query(None, description="ISO date, historical search"),
                     hours_back: float = Query(None, ge=1, le=72),
                     bbox: str = Query(None, description="lon_min,lat_min,lon_max,lat_max"),
                     background: bool = Query(False, description="return a job id "
                                              "and report real progress")
                     ) -> dict:
    """Fetch and analyse a REAL Sentinel-1 scene over the AOI.

    With no start/end this takes the newest scene. Supply both to target a
    historical window - needed when pairing imagery with an archived AIS day.
    """
    from datetime import datetime as _dt, timezone as _tz

    def _parse(v):
        if not v:
            return None
        d = _dt.fromisoformat(v)
        return d if d.tzinfo else d.replace(tzinfo=_tz.utc)

    try:
        box = None
        if bbox:
            parts = [float(x) for x in bbox.split(",")]
            if len(parts) != 4:
                raise ValueError("bbox needs 4 comma-separated numbers: "
                                 "lon_min,lat_min,lon_max,lat_max")
            box = tuple(parts)
    except ValueError as exc:
        raise HTTPException(400, f"bad request: {exc}")
    kwargs = dict(bbox=box, days=days, polarization=polarization,
                  min_area_km2=min_area_km2, max_pixels=max_pixels,
                  start=_parse(start), end=_parse(end), hours_back=hours_back)

    if background:
        job = jobs.create("sentinel1", jobs.SENTINEL_STAGES)

        def _run() -> None:
            try:
                jobs.finish(job, pipeline.ingest_real_scene(job=job, **kwargs))
            except Exception as exc:
                jobs.fail(job, exc)

        threading.Thread(target=_run, daemon=True,
                         name=f"ingest-{job.id}").start()
        return {"job_id": job.id, "status": "running",
                "poll": f"/api/jobs/{job.id}"}

    try:
        return pipeline.ingest_real_scene(**kwargs)
    except Exception as exc:
        raise HTTPException(502, f"Sentinel-1 ingest failed: {exc}")


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    """Real progress for a background job - stages that actually completed."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"unknown job {job_id}")
    return job.to_dict()


@app.get("/api/sentinel1/scenes")
def sentinel1_scenes(days: int = Query(45, ge=1, le=365)) -> list[dict]:
    """List real Sentinel-1 scenes available over the AOI."""
    from backend.ingest import sentinel1
    try:
        return [{"id": s.id, "datetime": s.datetime, "age_days": round(s.age_days(), 2),
                 "polarizations": s.polarizations, "orbit_state": s.orbit_state}
                for s in sentinel1.search(config.aoi_bbox_gis(), days=days)]
    except Exception as exc:
        raise HTTPException(502, f"Sentinel-1 search failed: {exc}")


# --- extra AIS sources -------------------------------------------------------

@app.post("/api/ingest/gfw")
def ingest_gfw(days: float = Query(30, ge=1, le=365),
               bbox: str = Query(None, description="lon_min,lat_min,lon_max,lat_max")
               ) -> dict:
    """Real GFW vessel activity - the only free vessel data covering India."""
    from backend.ingest import gfw
    try:
        box = tuple(float(x) for x in bbox.split(",")) if bbox else None
        return gfw.ingest_activity(bbox=box, days=days)
    except gfw.GFWError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"GFW ingest failed: {exc}")


@app.post("/api/ingest/noaa")
def ingest_noaa(day: str = Query(None, description="YYYY-MM-DD; default newest"),
                max_rows: int = Query(400_000, ge=1000, le=5_000_000),
                bbox: str = Query(None, description="lon_min,lat_min,lon_max,lat_max")
                ) -> dict:
    """Real historical AIS from NOAA MarineCadastre (US waters, no token).

    The daily archive is ~360 MB, so the first call for a given day downloads
    and caches it; later calls reuse the cache.
    """
    from datetime import date as _date
    from backend.ingest import noaa_ais
    try:
        d = _date.fromisoformat(day) if day else noaa_ais.available_recent()
        box = tuple(float(x) for x in bbox.split(",")) if bbox else None
    except ValueError as exc:
        raise HTTPException(400, f"bad request: {exc}")
    try:
        return noaa_ais.load(d, bbox=box, max_rows=max_rows)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"NOAA ingest failed: {exc}")


# --- frontend ----------------------------------------------------------------

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(FRONTEND / "index.html")
