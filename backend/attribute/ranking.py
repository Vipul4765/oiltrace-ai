"""Correlate AIS tracks against the back-drift cone and rank suspect vessels.

Five independent factors, each in [0,1], combined with the weights in config:

  spatial     did the vessel pass through the probable source region?
  temporal    was it there at the *right time*, or is it just a coincidence?
              Measured as a contrast: the vessel's best time-matched score
              divided by its best score over ALL time pairings. A ship that
              transits the area daily scores high spatially but near zero here.
  persistence how long it lingered inside the region (a 12 kn transit crosses
              in minutes; loitering is different)
  behaviour   slow steaming vs its own norm, course deviation, AIS gap
  vessel_type tankers and large cargo carry the oil; fishing boats mostly don't

Nothing here proves guilt. It produces a ranked, explained shortlist for a human
investigator - which is exactly what the real CleanSeaNet workflow does too.
"""
from __future__ import annotations

import json
import math
import time

import numpy as np

from backend import config, db, geo
from backend.drift.backtrack import BackDrift
from backend.vessel_types import ship_type_name

SPATIAL_SCALE_KM = 10.0        # default e-folding distance for the kernel


def _kernel(dist_km: float, scale_km: float = SPATIAL_SCALE_KM) -> float:
    if not np.isfinite(dist_km):
        return 0.0
    return float(np.exp(-((dist_km / max(scale_km, 0.5)) ** 2)))


def _is_stationary(track: list[tuple]) -> tuple[bool, float]:
    """Fixed platforms, moored vessels and buoys never leave the source region,
    so they saturate every factor and crowd out real suspects. Detected by
    near-zero net displacement plus near-zero speed across the window.

    Found by running on real Gulf of Mexico AIS, where MATTERHORN TLP - an
    actual tension-leg oil platform - ranked in the top 8 'suspect vessels'.
    """
    if len(track) < 3:
        return False, 0.0
    lats = [p[1] for p in track]
    lons = [p[2] for p in track]
    span = geo.haversine_km(min(lats), min(lons), max(lats), max(lons))
    speeds = [p[3] for p in track if p[3] is not None]
    med_sog = float(np.median(speeds)) if speeds else 0.0
    return (span < 1.5 and med_sog < 0.6), span


def _load_tracks(t_start: float, t_end: float) -> dict[int, list[tuple]]:
    rows = db.query(
        "SELECT mmsi, ts, lat, lon, sog, cog FROM ais_positions "
        "WHERE ts BETWEEN ? AND ? ORDER BY mmsi, ts",
        (t_start, t_end),
    )
    tracks: dict[int, list[tuple]] = {}
    for r in rows:
        tracks.setdefault(r["mmsi"], []).append(
            (r["ts"], r["lat"], r["lon"], r["sog"], r["cog"])
        )
    return tracks


def _median_speeds(mmsis: list[int]) -> dict[int, float]:
    """Baseline speed per vessel, in ONE query.

    This used to run a correlated subquery per candidate - fine for 8 demo
    vessels, but the live feed holds thousands and it turned ranking into
    hundreds of round trips.
    """
    if not mmsis:
        return {}
    out: dict[int, float] = {}
    chunk = 500
    for i in range(0, len(mmsis), chunk):
        part = mmsis[i:i + chunk]
        q = ",".join("?" * len(part))
        rows = db.query(
            f"SELECT mmsi, sog FROM ais_positions"
            f" WHERE mmsi IN ({q}) AND sog IS NOT NULL", part)
        buckets: dict[int, list[float]] = {}
        for r in rows:
            buckets.setdefault(r["mmsi"], []).append(float(r["sog"]))
        for mmsi, vals in buckets.items():
            vals.sort()
            out[mmsi] = vals[len(vals) // 2]
    return out


def _behaviour(track: list[tuple], mmsi: int,
               window: tuple[float, float],
               baselines: dict[int, float]) -> tuple[float, dict, list[str]]:
    """Slow steaming, course deviation and AIS gaps inside the encounter window."""
    t0, t1 = window
    seg = [p for p in track if t0 - 3600 <= p[0] <= t1 + 3600]
    notes: list[str] = []
    if len(seg) < 2:
        return 0.0, {"slow_steaming": 0.0, "course_deviation": 0.0, "ais_gap": 0.0}, notes

    # --- slow steaming, relative to the vessel's own normal speed ---
    speeds = [p[3] for p in seg if p[3] is not None]
    baseline = baselines.get(mmsi, 0.0)
    slow = 0.0
    if speeds and baseline > 1.0:
        ratio = min(speeds) / baseline
        slow = float(np.clip(1.0 - ratio, 0.0, 1.0))
        if slow > 0.35:
            notes.append(
                f"Slowed to {min(speeds):.1f} kn against a {baseline:.1f} kn norm "
                f"({slow * 100:.0f}% reduction) inside the probable source region"
            )

    # --- course deviation ---
    cogs = [(p[0], p[4]) for p in seg if p[4] is not None]
    dev = 0.0
    if len(cogs) >= 3:
        worst = max(geo.angle_diff(cogs[i][1], cogs[i - 1][1])
                    for i in range(1, len(cogs)))
        dev = float(np.clip(worst / 45.0, 0.0, 1.0))
        if worst > 20:
            notes.append(f"Course deviation of {worst:.0f}deg during the window")

    # --- AIS gap ---
    gaps = [seg[i][0] - seg[i - 1][0] for i in range(1, len(seg))]
    gap = 0.0
    if gaps:
        longest = max(gaps)
        typical = float(np.median(gaps))
        if longest > max(600.0, typical * 4):
            gap = float(np.clip(longest / 3600.0, 0.0, 1.0))
            notes.append(
                f"AIS reporting gap of {longest / 60:.0f} min "
                f"(normally every {typical / 60:.1f} min)"
            )

    score = 0.4 * slow + 0.3 * dev + 0.3 * gap
    return score, {"slow_steaming": round(slow, 3),
                   "course_deviation": round(dev, 3),
                   "ais_gap": round(gap, 3)}, notes


def _type_score(vessel: dict) -> tuple[float, str]:
    code = vessel.get("ship_type")
    name = ship_type_name(code)
    base = {"Tanker": 1.0, "Cargo": 0.7, "Tug": 0.5,
            "Passenger": 0.3, "Fishing": 0.25}.get(name, 0.4)
    length = vessel.get("length_m") or 0.0
    size = 0.6 + 0.4 * min(1.0, length / 250.0)
    return base * size, name


def rank_candidates(incident: dict, drift: BackDrift,
                    hours: float | None = None) -> list[dict]:
    hours = hours or config.DRIFT_MAX_HOURS
    detected_at = incident["detected_at"]
    t_start, t_end = detected_at - hours * 3600, detected_at

    snap_times = [ts for ts, _ in drift.snapshots if ts >= t_start - 1]
    clouds = {ts: drift.cloud_at(ts) for ts in snap_times}
    tracks = _load_tracks(t_start - 3600, t_end + 600)
    vessels = {r["mmsi"]: dict(r) for r in db.query("SELECT * FROM vessels")}
    baselines = _median_speeds(list(tracks))

    # Scale the spatial kernel to the size of the probable source region.
    # A fixed 10 km made every vessel score 1.00 when the region was 400 km2:
    # discrimination has to be relative to how well-constrained the source is.
    areas = [r.get("area_km2", 0) for r in (drift.to_dict().get("regions") or [])]
    region_r_km = math.sqrt(max(np.median(areas) if areas else 80.0, 1.0) / math.pi)
    scale_km = float(np.clip(region_r_km * 0.55, 3.0, 20.0))

    results: list[dict] = []
    for mmsi, track in tracks.items():
        if len(track) < 3:
            continue

        # Distance from the vessel to the back-drifted cloud, at every step.
        matched: list[tuple[float, float]] = []      # (ts, distance_km)
        positions: dict[float, tuple[float, float]] = {}
        for ts in snap_times:
            pos = geo.interp_position(track, ts)
            if pos is None:
                continue
            cloud = clouds.get(ts)
            if cloud is None or len(cloud) == 0:
                continue
            positions[ts] = pos
            lat, lon = pos
            kx = geo.km_per_deg_lon(lat)
            d = np.hypot((cloud[:, 0] - lon) * kx,
                         (cloud[:, 1] - lat) * geo.KM_PER_DEG_LAT)
            matched.append((ts, float(np.min(d))))

        if not matched:
            continue
        best_ts, best_km = min(matched, key=lambda x: x[1])
        if best_km > config.CANDIDATE_RADIUS_KM:
            continue                                  # not a candidate at all

        spatial = _kernel(best_km, scale_km)

        # --- temporal contrast: matched pairing vs the best of ALL pairings ---
        best_any = best_km
        for ts_v, (lat, lon) in positions.items():
            kx = geo.km_per_deg_lon(lat)
            for ts_c in snap_times:
                cloud = clouds.get(ts_c)
                if cloud is None or len(cloud) == 0:
                    continue
                d = float(np.min(np.hypot((cloud[:, 0] - lon) * kx,
                                          (cloud[:, 1] - lat) * geo.KM_PER_DEG_LAT)))
                best_any = min(best_any, d)
        temporal = spatial / max(_kernel(best_any, scale_km), 1e-6)
        temporal = float(np.clip(temporal, 0.0, 1.0))

        # --- persistence: minutes spent inside the candidate radius ---
        step_min = config.DRIFT_STEP_MINUTES
        inside = [ts for ts, d in matched if d <= config.PERSISTENCE_RADIUS_KM]
        minutes_inside = len(inside) * step_min
        persistence = float(np.clip(minutes_inside / config.PERSISTENCE_FULL_MINUTES,
                                    0.0, 1.0))

        window = (min(inside) if inside else best_ts,
                  max(inside) if inside else best_ts)
        behaviour, behaviour_parts, notes = _behaviour(track, mmsi, window,
                                                       baselines)

        # A platform that never moves shows a "97% slowdown" and a "179 deg
        # course deviation" that mean nothing. Zero those factors instead of
        # letting fixed infrastructure win the ranking.
        stationary, span_km = _is_stationary(track)
        if stationary:
            behaviour = 0.0
            behaviour_parts = {k: 0.0 for k in behaviour_parts}
            notes = [f"Stationary through the whole window (moved {span_km:.1f} km) "
                     f"- fixed platform, moored vessel or buoy. Speed and course "
                     f"anomalies are not meaningful; behaviour scored 0."]
            persistence = 0.0

        vessel = vessels.get(mmsi, {"mmsi": mmsi})
        vtype_score, vtype_name = _type_score(vessel)

        comps = {"spatial": spatial, "temporal": temporal,
                 "persistence": persistence, "behaviour": behaviour,
                 "vessel_type": vtype_score}
        score = sum(config.SCORE_WEIGHTS[k] * v for k, v in comps.items())

        evidence = [
            f"Closest approach {best_km:.1f} km from the back-drifted source region",
            f"Consistent position at {time.strftime('%d %b %Y, %H:%M UTC', time.gmtime(best_ts))}"
            f" ({(detected_at - best_ts) / 3600:.1f} h before detection)",
        ]
        if minutes_inside >= step_min * 2:
            evidence.append(
                f"Inside the probable source region for ~{minutes_inside:.0f} min")
        evidence.extend(notes)
        if vtype_name == "Tanker":
            evidence.append(
                f"{vtype_name}, {vessel.get('length_m') or '?'} m LOA - "
                "cargo type consistent with a mineral-oil slick")
        if temporal < 0.35:
            evidence.append(
                "Timing is a weak match - the vessel passes through this area at "
                "other times too, which lowers confidence")

        sub = [p for p in track if window[0] - 1800 <= p[0] <= window[1] + 1800] or track
        stride = max(1, len(sub) // 120)
        results.append({
            "mmsi": mmsi,
            "name": vessel.get("name") or f"MMSI {mmsi}",
            "imo": vessel.get("imo"),
            "callsign": vessel.get("callsign"),
            "vessel_type": vtype_name,
            "length_m": vessel.get("length_m"),
            "destination": vessel.get("destination"),
            "score": round(score, 4),
            "components": {k: round(v, 4) for k, v in comps.items()},
            "behaviour_detail": behaviour_parts,
            "closest_km": round(best_km, 2),
            "closest_ts": best_ts,
            "hours_before_detection": round((detected_at - best_ts) / 3600, 2),
            "minutes_in_region": minutes_inside,
            "stationary": stationary,
            "spatial_scale_km": round(scale_km, 1),
            "evidence": evidence,
            "track": [[round(p[0], 1), round(p[1], 5), round(p[2], 5),
                       p[3], p[4]] for p in sub[::stride]],
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(results[:config.MAX_CANDIDATES], start=1):
        r["rank"] = i
    return results[:config.MAX_CANDIDATES]


def store_candidates(incident_id: str, candidates: list[dict]) -> None:
    now = time.time()
    with db.tx() as conn:
        conn.execute("DELETE FROM candidates WHERE incident_id=?", (incident_id,))
        conn.executemany(
            "INSERT INTO candidates (incident_id, mmsi, rank, score, components,"
            " evidence, track, computed_at) VALUES (?,?,?,?,?,?,?,?)",
            [(incident_id, c["mmsi"], c["rank"], c["score"],
              db.jdump(c["components"]), db.jdump(c["evidence"]),
              db.jdump(c["track"]), now) for c in candidates],
        )
