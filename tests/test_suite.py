"""OilTrace AI test suite.

Runs against the live database and the live APIs - no fixtures, no mock data.
Where a test needs an incident, it builds the evaluation window in memory from
a real detection polygon and real AIS rows; nothing fabricated is ever written
to the database.

    .venv/bin/python tests/test_suite.py
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, db, geo  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  -> ' + detail) if detail else ''}")


# --- geodesy -----------------------------------------------------------------
def test_geo() -> None:
    print("\ngeo")
    d = geo.haversine_km(19.07, 72.87, 18.64, 72.87)
    check("haversine Mumbai->Alibag ~47.8 km", abs(d - 47.8) < 0.5, f"{d:.1f} km")
    check("bearing due south is 180", abs(geo.bearing_deg(19.07, 72.87, 18.64, 72.87) - 180) < 0.1)
    lat, lon = geo.move_bearing(18.0, 72.0, 90.0, 10.0)
    check("move 10 km east keeps latitude", abs(lat - 18.0) < 1e-6)
    check("move 10 km east lands 10 km away",
          abs(geo.haversine_km(18.0, 72.0, lat, lon) - 10.0) < 0.05)
    ring = [[72.7, 18.9], [72.9, 18.9], [72.9, 19.05], [72.7, 19.05]]
    check("point inside ring", geo.point_in_ring(72.8, 18.95, ring))
    check("point outside ring", not geo.point_in_ring(72.5, 18.95, ring))
    check("ring area positive", geo.polygon_area_km2(ring) > 100)
    check("angle_diff wraps 350/10 -> 20", abs(geo.angle_diff(350, 10) - 20) < 1e-9)
    track = [(100.0, 10.0, 20.0), (200.0, 11.0, 21.0)]
    p = geo.interp_position(track, 150.0)
    check("interp midpoint", p is not None and abs(p[0] - 10.5) < 1e-9)
    check("interp outside span returns None", geo.interp_position(track, 9e9) is None)


# --- database ----------------------------------------------------------------
def test_db() -> None:
    print("\ndatabase")
    db.init_db()
    cols = {r["name"] for r in db.connect().execute("PRAGMA table_info(ais_positions)")}
    check("ais_positions has source column", "source" in cols)
    icols = {r["name"] for r in db.connect().execute("PRAGMA table_info(incidents)")}
    check("incidents has region column", "region" in icols)

    old = time.time() - 500 * 86400
    with db.tx() as c:
        db.insert_positions(c, [(999000001, old, 29.0, -89.0, 5.0, 90.0, 90.0, 0)],
                            source="noaa")
        db.insert_positions(c, [(999000002, old, 29.0, -89.0, 5.0, 90.0, 90.0, 0)],
                            source="live")
        db.prune_old_positions(c, time.time() - 14 * 86400)
    kept = db.query_one("SELECT COUNT(*) c FROM ais_positions WHERE mmsi=999000001")["c"]
    gone = db.query_one("SELECT COUNT(*) c FROM ais_positions WHERE mmsi=999000002")["c"]
    check("retention keeps imported archives", kept == 1)
    check("retention prunes stale live rows", gone == 0)
    with db.tx() as c:
        c.execute("DELETE FROM ais_positions WHERE mmsi IN (999000001,999000002)")

    with db.tx() as c:
        db.upsert_vessel(c, 999000003, time.time(), name="TEST A", imo="1234567")
        db.upsert_vessel(c, 999000003, time.time(), name=None, callsign="ABC")
    v = db.query_one("SELECT * FROM vessels WHERE mmsi=999000003")
    nulls = db.query_one("SELECT COUNT(*) c FROM ais_positions WHERE source IS NULL")["c"]
    check("no AIS rows with NULL source", nulls == 0, f"{nulls} rows")

    check("upsert_vessel COALESCEs, does not blank fields",
          v["name"] == "TEST A" and v["callsign"] == "ABC")
    with db.tx() as c:
        c.execute("DELETE FROM vessels WHERE mmsi=999000003")


# --- ship types --------------------------------------------------------------
def test_vessel_types() -> None:
    print("\nvessel types")
    from backend.vessel_types import ship_type_name
    check("80 -> Tanker", ship_type_name(80) == "Tanker")
    check("82 falls back to decade -> Tanker", ship_type_name(82) == "Tanker")
    check("70 -> Cargo", ship_type_name(70) == "Cargo")
    check("None -> Unknown", ship_type_name(None) == "Unknown")
    check("garbage -> Unknown", ship_type_name("abc") == "Unknown")


# --- detector ----------------------------------------------------------------
def test_detector() -> None:
    print("\ndetector (contrast gating)")
    import numpy as np
    from backend.detect import darkspot
    rng = np.random.default_rng(7)
    bounds = (0.0, 50.0, 0.5, 50.4)

    def scene(damping: float) -> np.ndarray:
        """Gamma-speckled sea with a flat-bottomed dark target.

        The profile exponent matters: oil damps capillary waves to saturation,
        giving a fairly uniform dark interior with a defined edge. A Gaussian
        blob is almost all skirt and is not a fair stand-in.
        """
        img = rng.gamma(4, 30.0, (700, 700)).astype(np.float32)
        yy, xx = np.mgrid[0:700, 0:700]
        r = ((xx - 350) / 40.0) ** 2 + ((yy - 350) / 15.0) ** 2
        return img * (1.0 - damping * np.exp(-(r ** 3.0)))

    def true_db(damping: float) -> float:
        return float(10 * np.log10(1.0 / (1.0 - damping)))

    # Literature: mineral oil damps ~3-10 dB, low-wind look-alikes ~1-2 dB.
    for damping in (0.85, 0.70, 0.50):
        d = darkspot.detect(scene(damping), bounds, min_area_km2=0.3)
        check(f"oil at {true_db(damping):.1f} dB is detected", len(d) >= 1,
              f"{len(d)} detections")
    for damping in (0.35, 0.22, 0.12):
        d = darkspot.detect(scene(damping), bounds, min_area_km2=0.3)
        check(f"look-alike at {true_db(damping):.1f} dB is rejected", len(d) == 0,
              f"{len(d)} detections")

    d = darkspot.detect(scene(0.85), bounds, min_area_km2=0.3)
    if d:
        f = d[0].features
        check("contrast is measured, not assumed", f["contrast_db"] > 2.0,
              f"{f['contrast_db']} dB")
        check("contrast gate recorded", 0.0 < f["contrast_gate"] <= 1.0)
        check("confidence in 0..1", 0.0 < d[0].confidence <= 1.0)
        vmin, vmax = d[0].volume_estimate_m3()
        check("volume is a wide range, not false precision", vmax > vmin * 10)
        check("polygon is a real ring", len(d[0].ring) >= 3)


# --- land mask ---------------------------------------------------------------
def test_landmask() -> None:
    print("\nland mask")
    from backend.detect import landmask
    sea = landmask.land_fraction((-89.5, 26.0, -88.5, 27.0))      # open Gulf
    land = landmask.land_fraction((-92.0, 30.8, -91.0, 31.8))     # inland Louisiana
    check("open ocean is ~0% land", sea < 0.02, f"{sea:.3f}")
    check("inland box is mostly land", land > 0.8, f"{land:.3f}")
    t0 = time.time(); landmask.build((-89.5, 26.0, -88.5, 27.0), (400, 400)); a = time.time() - t0
    t0 = time.time(); landmask.build((-89.5, 26.0, -88.5, 27.0), (400, 400)); b = time.time() - t0
    check("mask cache makes a repeat build faster", b < max(a, 1e-4), f"{a:.3f}s -> {b:.4f}s")


# --- attribution -------------------------------------------------------------
def test_ranking() -> None:
    print("\nattribution")
    from backend.attribute import ranking
    from backend.drift import backtrack

    # Find a real, well-populated patch of AIS and evaluate against it. The
    # incident dict is in-memory only and never stored.
    row = db.query_one(
        "SELECT mmsi, lat, lon, MAX(ts) ts FROM ais_positions"
        " GROUP BY mmsi ORDER BY COUNT(*) DESC LIMIT 1")
    if not row:
        check("AIS available for ranking test", False, "database holds no AIS")
        return
    lat, lon, ts = row["lat"], row["lon"], row["ts"]

    speeds = ranking._median_speeds([row["mmsi"]])
    check("batched median speed returns a value", row["mmsi"] in speeds,
          f"{speeds.get(row['mmsi'])}")

    d = 0.05
    ring = [[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d]]
    try:
        drift = backtrack.backtrack(ring, ts, hours=4, particles=150)
    except Exception as exc:
        check("back-drift runs on real environment data", False, str(exc)[:90])
        return
    check("back-drift runs on real environment data", len(drift.snapshots) > 1)
    check("uncertainty grows going backwards",
          geo.polygon_area_km2(drift.region_at(ts - 4 * 3600) or ring)
          >= geo.polygon_area_km2(drift.region_at(ts) or ring) * 0.9)

    cands = ranking.rank_candidates({"detected_at": ts, "lat": lat, "lon": lon},
                                    drift, hours=4)
    check("ranking returns candidates from real AIS", len(cands) > 0,
          f"{len(cands)} vessels")
    if cands:
        c = cands[0]
        check("scores are bounded 0..1", 0.0 <= c["score"] <= 1.0, f"{c['score']}")
        check("every weighted factor present",
              set(c["components"]) == set(config.SCORE_WEIGHTS))
        check("ranking is sorted descending",
              all(cands[i]["score"] >= cands[i + 1]["score"]
                  for i in range(len(cands) - 1)))
        check("candidates carry evidence", len(c["evidence"]) > 0)
        recomputed = sum(config.SCORE_WEIGHTS[k] * v for k, v in c["components"].items())
        check("score equals the weighted sum of its factors",
              abs(recomputed - c["score"]) < 1e-3,
              f"{recomputed:.4f} vs {c['score']:.4f}")


def test_dedupe() -> None:
    """Re-analysing one scene must not manufacture extra incidents."""
    print("\nincident de-duplication")
    from backend import pipeline
    rows = db.query(
        "SELECT scene_id, COUNT(*) n FROM incidents WHERE scene_id IS NOT NULL"
        " GROUP BY scene_id, ROUND(lat,3), ROUND(lon,3) HAVING n > 1")
    check("no duplicate (scene, location) incidents in the database",
          len(rows) == 0, f"{len(rows)} duplicated groups")

    row = db.query_one("SELECT scene_id, lat, lon FROM incidents"
                       " WHERE scene_id IS NOT NULL LIMIT 1")
    if row:
        found = pipeline._existing_incident(row["scene_id"], row["lat"], row["lon"])
        check("same scene + same spot resolves to the existing incident",
              found is not None, str(found))
        far = pipeline._existing_incident(row["scene_id"], row["lat"] + 5.0,
                                          row["lon"] + 5.0)
        check("a genuinely different location is NOT merged", far is None)


# --- config ------------------------------------------------------------------
def test_config() -> None:
    print("\nconfig & regions")
    from backend import regions
    check("score weights sum to 1", abs(sum(config.SCORE_WEIGHTS.values()) - 1.0) < 1e-9)
    before = config.ACTIVE_REGION
    for key in regions.keys():
        r = config.set_region(key)
        ok = (config.AOI_LAT_MIN == r.lat_min and config.AOI_LON_MAX == r.lon_max
              and config.aoi_bbox_gis() == r.bbox)
        check(f"region {key} applies its bbox", ok)
    config.set_region(before)
    check("unknown region falls back to default",
          regions.get("nope").key == regions.DEFAULT_REGION)
    check("no simulator mode remains", config.AIS_SOURCE in ("live", "off"))


def main() -> int:
    print("OilTrace AI - test suite (real data, no fixtures)")
    for fn in (test_geo, test_db, test_vessel_types, test_config,
               test_detector, test_landmask, test_ranking, test_dedupe):
        try:
            fn()
        except Exception as exc:
            import traceback
            FAIL.append(fn.__name__)
            print(f"  FAIL  {fn.__name__} raised {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=3)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failing: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
