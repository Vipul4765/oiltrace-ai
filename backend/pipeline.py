"""Orchestration: scene -> detection -> incident -> back-drift -> ranked vessels."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import numpy as np

from backend import config, db, geo
from backend.attribute import ranking
from backend.detect import darkspot
from backend.drift import backtrack


def next_incident_id() -> str:
    year = datetime.now(timezone.utc).year
    row = db.query_one(
        "SELECT incident_id FROM incidents WHERE incident_id LIKE ? "
        "ORDER BY incident_id DESC LIMIT 1", (f"OTA-{year}-%",))
    seq = int(row["incident_id"].split("-")[-1]) + 1 if row else 1
    return f"OTA-{year}-{seq:04d}"


def _existing_incident(scene_id: str, lat: float, lon: float,
                       radius_km: float = 1.5) -> str | None:
    """Has this scene already produced a detection at essentially this spot?

    Re-analysing a scene must not manufacture a second spill. Without this,
    pressing "Fetch Sentinel-1" four times turned 5 real detections into 20
    incidents and inflated every count on the dashboard.
    """
    if not scene_id:
        return None
    for r in db.query(
            "SELECT incident_id, lat, lon FROM incidents WHERE scene_id = ?",
            (scene_id,)):
        if geo.haversine_km(lat, lon, r["lat"], r["lon"]) <= radius_km:
            return r["incident_id"]
    return None


def create_incident(det: darkspot.Detection, detected_at: float,
                    scene_id: str = "", source: str = "sar") -> str:
    lat0, lon0 = det.centroid
    existing = _existing_incident(scene_id, lat0, lon0)
    if existing:
        # Same scene, same place: refresh the measurements in place. Detector
        # tuning can change area or confidence; the incident is still one event.
        vmin0, vmax0 = det.volume_estimate_m3()
        with db.tx() as conn:
            conn.execute(
                "UPDATE incidents SET lat=?, lon=?, area_km2=?, length_km=?,"
                " width_km=?, volume_min_m3=?, volume_max_m3=?, confidence=?,"
                " polygon=?, region=? WHERE incident_id=?",
                (lat0, lon0, det.area_km2, det.length_km, det.width_km,
                 vmin0, vmax0, det.confidence, db.jdump(det.ring),
                 config.ACTIVE_REGION, existing))
        return existing

    iid = next_incident_id()
    vmin, vmax = det.volume_estimate_m3()
    lat, lon = det.centroid
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO incidents (incident_id, detected_at, lat, lon, area_km2,"
            " length_km, width_km, volume_min_m3, volume_max_m3, confidence,"
            " status, polygon, scene_id, source, region, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, detected_at, lat, lon, det.area_km2, det.length_km, det.width_km,
             vmin, vmax, det.confidence, "Under Investigation",
             db.jdump(det.ring), scene_id, source, config.ACTIVE_REGION, now))
        # Real clock only. detected_at is the satellite acquisition time from
        # the scene metadata; `now` is when this process actually ran. Earlier
        # versions wrote invented offsets here (+45 min, +90 min...) purely to
        # make the dashboard look like a reference mockup. That was fabricated
        # data and it is gone.
        db.add_timeline(conn, iid, detected_at, "Scene acquired (Sentinel-1)",
                        f"{scene_id or 'n/a'}")
        db.add_timeline(conn, iid, now, "Detection & segmentation",
                        f"{det.area_km2:.1f} km2, contrast "
                        f"{det.features.get('contrast_db','?')} dB, "
                        f"confidence {det.confidence:.0%} "
                        f"({(now - detected_at) / 3600:.1f} h after acquisition)")
        db.add_alert(conn, detected_at, "detection",
                     f"New spill detected - {det.area_km2:.1f} km2 at "
                     f"{lat:.3f}N {lon:.3f}E", iid)
    return iid


def run_attribution(incident_id: str, hours: float | None = None) -> dict:
    """Back-drift the slick, then rank every vessel that was near the source."""
    row = db.query_one("SELECT * FROM incidents WHERE incident_id=?", (incident_id,))
    if not row:
        raise ValueError(f"unknown incident {incident_id}")
    incident = dict(row)
    hours = hours or config.DRIFT_MAX_HOURS

    import json
    ring = json.loads(incident["polygon"])
    t0 = time.time()
    drift = backtrack.backtrack(ring, incident["detected_at"], hours=hours)
    drift_dict = drift.to_dict()
    drift_done_ts = time.time()
    drift_ms = (drift_done_ts - t0) * 1000

    t1 = time.time()
    candidates = ranking.rank_candidates(incident, drift, hours=hours)
    ranking.store_candidates(incident_id, candidates)
    rank_ms = (time.time() - t1) * 1000

    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO drift_runs (incident_id, computed_at, hours_back,"
            " particles, result) VALUES (?,?,?,?,?)"
            " ON CONFLICT(incident_id) DO UPDATE SET computed_at=excluded.computed_at,"
            " hours_back=excluded.hours_back, particles=excluded.particles,"
            " result=excluded.result",
            (incident_id, now, hours, drift_dict["particles"], db.jdump(drift_dict)))
        conn.execute("DELETE FROM incident_timeline WHERE incident_id=? AND stage IN"
                     " ('Trajectory modelling','AIS correlation & ranking')",
                     (incident_id,))
        db.add_timeline(conn, incident_id, drift_done_ts, "Trajectory modelling",
                        f"{drift_dict['particles']} particles, {hours:.0f} h backward"
                        f" - took {drift_ms:.0f} ms")
        db.add_timeline(conn, incident_id, now, "AIS correlation & ranking",
                        f"{len(candidates)} candidate vessels scored"
                        f" - took {rank_ms:.0f} ms")
        if candidates:
            top = candidates[0]
            db.add_alert(conn, now, "ranking",
                         f"Vessel ranking updated - top candidate {top['name']}"
                         f" ({top['score']:.0%})", incident_id)
    return {"incident_id": incident_id, "drift": drift_dict,
            "candidates": candidates, "drift_ms": round(drift_ms)}


def process_scene(image: np.ndarray, bounds: tuple[float, float, float, float],
                  detected_at: float, scene_id: str = "",
                  source: str = "sar") -> list[str]:
    """Full ingest of one SAR scene. Returns the incident ids created."""
    ids = []
    for det in darkspot.detect(image, bounds):
        ids.append(create_incident(det, detected_at, scene_id, source))
    return ids


def ingest_real_scene(bbox: tuple[float, float, float, float] | None = None,
                      days: int = 45, polarization: str = "vv",
                      max_pixels: int = 1400,
                      min_area_km2: float = 1.0,
                      attribute: bool = True,
                      start: "datetime | None" = None,
                      end: "datetime | None" = None,
                      hours_back: float | None = None,
                      job=None) -> dict:
    """Pull the most recent REAL Sentinel-1 GRD over the AOI and run detection.

    No account, no key: the Planetary Computer serves the GRD as a COG and
    signs reads with an anonymous SAS token, so we stream a windowed subset
    over HTTP instead of downloading the full ~1 GB product.
    """
    from backend.ingest import sentinel1
    from backend import jobs

    bbox = bbox or (config.AOI_LON_MIN, config.AOI_LAT_MIN,
                    config.AOI_LON_MAX, config.AOI_LAT_MAX)
    t0 = time.time()
    jobs.advance(job, "search", f"{config.AOI_NAME}")
    scenes = sentinel1.search(bbox, days=days, start=start, end=end, limit=60)
    if not scenes:
        raise RuntimeError(
            f"no Sentinel-1 IW scenes over this area in the last {days} days")

    jobs.advance(job, "download", f"{len(scenes)} scenes found, reading newest")
    image, actual_bounds, scene = sentinel1.fetch_latest(
        bbox, days=days, polarization=polarization, max_pixels=max_pixels,
        start=start, end=end)
    fetch_s = time.time() - t0

    # Mask the coastline before detecting. Land and sheltered near-shore water
    # are radar-dark, and without this a coastal scene reports the shoreline as
    # a spill - observed on the very first real Mississippi Delta run.
    jobs.advance(job, "landmask", f"scene {scene.acquired:%Y-%m-%d %H:%M} UTC")
    mask = None
    land_pct = None
    try:
        from backend.detect import landmask
        mask = landmask.build(actual_bounds, image.shape)
        land_pct = round(float(mask.mean()) * 100, 1)
    except Exception as exc:
        print(f"[pipeline] land mask unavailable ({exc}) - detecting unmasked")

    jobs.advance(job, "detect", f"{land_pct}% of the scene masked as land"
                 if land_pct is not None else "")
    dets = darkspot.detect(image, actual_bounds, min_area_km2=min_area_km2,
                           land_mask=mask)
    ids = [create_incident(d, scene.ts, scene.id, source="sentinel-1") for d in dets]

    results = []
    if attribute:
        jobs.advance(job, "attribute",
                     f"{len(ids)} detection(s) to correlate" if ids
                     else "no detections - nothing to correlate")
        for n, iid in enumerate(ids, 1):
            jobs.advance(job, "attribute", f"incident {n} of {len(ids)}")
            try:
                r = run_attribution(iid, hours=hours_back)
                results.append({"incident_id": iid,
                                "candidates": len(r["candidates"])})
            except Exception as exc:                 # AIS may be empty
                results.append({"incident_id": iid, "error": str(exc)})

    return {
        "scene_id": scene.id,
        "acquired": scene.acquired.isoformat(),
        "age_days": round(scene.age_days(), 2),
        "polarizations": scene.polarizations,
        "orbit_state": scene.orbit_state,
        "bounds": [round(v, 5) for v in actual_bounds],
        "image_shape": list(image.shape),
        "land_masked_pct": land_pct,
        "fetch_seconds": round(fetch_s, 1),
        "detections": [{"incident_id": i, "area_km2": d.area_km2,
                        "confidence": d.confidence, "centroid": d.centroid,
                        "features": d.features} for i, d in zip(ids, dets)],
        "attribution": results,
        "real": True,
    }
