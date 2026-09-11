"""Backward drift: given where a slick was seen, where could it have come from?

Method: seed particles across the observed slick, then integrate the surface
drift field backwards in time. Each particle carries its own wind-drift factor
and deflection angle sampled from plausible ranges, so the cloud fans out into
an uncertainty cone instead of collapsing to one wrong line. Unresolved
turbulence is added as a per-step random walk.

The output is a probability field over (place, time) that the attribution step
queries: "how likely is it that oil released *here*, *then*, ended up where we
saw it?"
"""
from __future__ import annotations

import math
import time

import numpy as np

from backend import config, geo
from backend.drift.env_data import EnvField


def _convex_hull(points: np.ndarray) -> list[list[float]]:
    """Monotone-chain hull of an (N,2) [lon,lat] array -> ring of [lon,lat]."""
    if len(points) < 3:
        return [[float(p[0]), float(p[1])] for p in points]
    pts = sorted({(float(p[0]), float(p[1])) for p in points})
    if len(pts) < 3:
        return [list(p) for p in pts]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [list(p) for p in lower[:-1] + upper[:-1]]


def _trimmed(points: np.ndarray, keep: float = 0.9) -> np.ndarray:
    """Drop the furthest-from-centroid tail before hulling, so a couple of
    runaway particles don't inflate the region tenfold."""
    if len(points) < 8:
        return points
    c = points.mean(axis=0)
    d = np.hypot(points[:, 0] - c[0], points[:, 1] - c[1])
    return points[d <= np.quantile(d, keep)]


def _seed(ring: list[list[float]], n: int, rng: np.random.Generator) -> np.ndarray:
    """Rejection-sample n points inside the slick polygon ([lon,lat] ring)."""
    arr = np.array(ring, dtype=float)
    lo, hi = arr.min(axis=0), arr.max(axis=0)
    out: list[list[float]] = []
    attempts = 0
    while len(out) < n and attempts < n * 200:
        lon = rng.uniform(lo[0], hi[0])
        lat = rng.uniform(lo[1], hi[1])
        attempts += 1
        if geo.point_in_ring(lon, lat, ring):
            out.append([lon, lat])
    if not out:                                    # degenerate polygon
        c = arr.mean(axis=0)
        return np.tile(c, (n, 1))
    while len(out) < n:                            # top up if sampling starved
        out.append(out[len(out) % max(1, len(out) // 2 + 1)])
    return np.array(out[:n], dtype=float)


class BackDrift:
    """Result of a backward-drift run. Snapshots go from t=detection backwards."""

    def __init__(self, snapshots: list[tuple[float, np.ndarray]],
                 env: EnvField, detected_at: float) -> None:
        self.snapshots = snapshots                 # [(ts, (N,2) lon/lat), ...]
        self.env = env
        self.detected_at = detected_at
        self._times = np.array([s[0] for s in snapshots])

    # --- queries -------------------------------------------------------------
    def cloud_at(self, ts: float) -> np.ndarray | None:
        if len(self._times) == 0:
            return None
        i = int(np.argmin(np.abs(self._times - ts)))
        if abs(self._times[i] - ts) > config.DRIFT_STEP_MINUTES * 60 * 1.5:
            return None
        return self.snapshots[i][1]

    def density_at(self, lat: float, lon: float, ts: float,
                   radius_km: float = config.CANDIDATE_RADIUS_KM) -> float:
        """Fraction of back-drifted particles within `radius_km` of a point at
        time `ts`. This is the spatial likelihood the vessel was at the source."""
        cloud = self.cloud_at(ts)
        if cloud is None or len(cloud) == 0:
            return 0.0
        kx = geo.km_per_deg_lon(lat)
        dx = (cloud[:, 0] - lon) * kx
        dy = (cloud[:, 1] - lat) * geo.KM_PER_DEG_LAT
        return float(np.mean(np.hypot(dx, dy) <= radius_km))

    def nearest_km(self, lat: float, lon: float, ts: float) -> float:
        cloud = self.cloud_at(ts)
        if cloud is None or len(cloud) == 0:
            return float("inf")
        kx = geo.km_per_deg_lon(lat)
        dx = (cloud[:, 0] - lon) * kx
        dy = (cloud[:, 1] - lat) * geo.KM_PER_DEG_LAT
        return float(np.min(np.hypot(dx, dy)))

    def region_at(self, ts: float) -> list[list[float]]:
        cloud = self.cloud_at(ts)
        if cloud is None:
            return []
        return _convex_hull(_trimmed(cloud))

    def envelope(self) -> list[list[float]]:
        """Hull of every snapshot - the full back-trajectory cone for the map."""
        allpts = np.vstack([s[1] for s in self.snapshots]) if self.snapshots else np.empty((0, 2))
        return _convex_hull(_trimmed(allpts, keep=0.97)) if len(allpts) else []

    def centreline(self) -> list[list[float]]:
        """Mean particle position per step - the headline backward trajectory."""
        return [[float(c.mean(axis=0)[0]), float(c.mean(axis=0)[1])]
                for _, c in self.snapshots]

    def to_dict(self, region_hours: tuple[float, ...] = (2, 4, 6, 8, 12, 18, 24)) -> dict:
        regions = []
        for h in region_hours:
            ts = self.detected_at - h * 3600
            if ts < self._times.min() - 1:
                continue
            ring = self.region_at(ts)
            if len(ring) >= 3:
                regions.append({
                    "hours_back": h,
                    "ts": ts,
                    "ring": ring,
                    "area_km2": round(geo.polygon_area_km2(ring), 1),
                })
        env_now = self.env.sample(self.detected_at)
        return {
            "detected_at": self.detected_at,
            "particles": len(self.snapshots[0][1]) if self.snapshots else 0,
            "hours_back": round((self.detected_at - float(self._times.min())) / 3600, 1),
            "centreline": self.centreline(),
            "envelope": self.envelope(),
            "regions": regions,
            "environment": env_now,
        }


def backtrack(ring: list[list[float]], detected_at: float,
              hours: float | None = None,
              particles: int | None = None,
              env: EnvField | None = None) -> BackDrift:
    """Integrate the slick backwards. `ring` is a [[lon,lat],...] polygon."""
    hours = hours or config.DRIFT_MAX_HOURS
    particles = particles or config.DRIFT_PARTICLES
    lat0, lon0 = geo.ring_centroid(ring)
    env = env or EnvField(lat0, lon0, past_days=max(2, int(hours / 24) + 2))

    rng = np.random.default_rng(20260910)
    pos = _seed(ring, particles, rng)                        # (N,2) lon/lat

    # Per-particle model uncertainty, sampled once and held for the whole run.
    wind_factor = rng.uniform(config.WIND_DRIFT_FACTOR * 0.65,
                              config.WIND_DRIFT_FACTOR * 1.35, particles)
    deflection = rng.normal(config.WIND_DEFLECTION_DEG, 8.0, particles)

    dt = config.DRIFT_STEP_MINUTES * 60.0
    steps = max(1, int(hours * 3600 / dt))
    snapshots: list[tuple[float, np.ndarray]] = [(detected_at, pos.copy())]

    ts = detected_at
    for _ in range(steps):
        s = env.sample(ts)
        cur = math.radians(s["current_to_deg"])
        cur_n = s["current_speed_ms"] * math.cos(cur)
        cur_e = s["current_speed_ms"] * math.sin(cur)

        wdir = np.radians((s["wind_to_deg"] + deflection) % 360.0)
        wmag = s["wind_speed_ms"] * wind_factor
        vn = cur_n + wmag * np.cos(wdir)                     # m/s north
        ve = cur_e + wmag * np.sin(wdir)                     # m/s east

        # Random walk for unresolved turbulence.
        vn = vn + rng.normal(0, config.DRIFT_DIFFUSION, particles)
        ve = ve + rng.normal(0, config.DRIFT_DIFFUSION, particles)

        # Backwards in time: subtract the displacement.
        dlat = -(vn * dt) / 1000.0 / geo.KM_PER_DEG_LAT
        kx = geo.km_per_deg_lon(float(np.mean(pos[:, 1])))
        dlon = -(ve * dt) / 1000.0 / max(kx, 1e-6)
        pos = pos + np.column_stack([dlon, dlat])

        ts -= dt
        snapshots.append((ts, pos.copy()))

    return BackDrift(snapshots, env, detected_at)
