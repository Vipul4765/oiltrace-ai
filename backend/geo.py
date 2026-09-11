"""Small geodesy helpers. The AOI spans ~200 km, so a local equirectangular
projection is accurate to well under the AIS position noise - no need for pyproj."""
from __future__ import annotations

import math

EARTH_R_M = 6_371_000.0
KM_PER_DEG_LAT = 110.574


def km_per_deg_lon(lat_deg: float) -> float:
    return 111.320 * math.cos(math.radians(lat_deg))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a)) / 1000.0


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def offset_km(lat: float, lon: float, dnorth_km: float, deast_km: float) -> tuple[float, float]:
    """Move a point by a north/east offset in kilometres."""
    return lat + dnorth_km / KM_PER_DEG_LAT, lon + deast_km / km_per_deg_lon(lat)


def move_bearing(lat: float, lon: float, bearing: float, dist_km: float) -> tuple[float, float]:
    b = math.radians(bearing)
    return offset_km(lat, lon, dist_km * math.cos(b), dist_km * math.sin(b))


def angle_diff(a: float, b: float) -> float:
    """Smallest absolute difference between two compass bearings, 0-180."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def knots_to_ms(kn: float) -> float:
    return kn * 0.514444


def ms_to_knots(ms: float) -> float:
    return ms / 0.514444


def polygon_area_km2(ring: list[list[float]]) -> float:
    """Shoelace area of a [[lon,lat],...] ring, projected locally."""
    if len(ring) < 3:
        return 0.0
    lat0 = sum(p[1] for p in ring) / len(ring)
    kx, ky = km_per_deg_lon(lat0), KM_PER_DEG_LAT
    pts = [(p[0] * kx, p[1] * ky) for p in ring]
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon for a [[lon,lat],...] ring."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            xin = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < xin:
                inside = not inside
    return inside


def ring_centroid(ring: list[list[float]]) -> tuple[float, float]:
    """Returns (lat, lon) of the ring centroid."""
    lon = sum(p[0] for p in ring) / len(ring)
    lat = sum(p[1] for p in ring) / len(ring)
    return lat, lon


def interp_position(track: list[tuple], ts: float) -> tuple[float, float] | None:
    """Linear interpolation of (lat, lon) at `ts` from a time-sorted track of
    (ts, lat, lon, ...) tuples. Returns None outside the track's span."""
    if not track:
        return None
    if ts <= track[0][0]:
        return (track[0][1], track[0][2]) if abs(ts - track[0][0]) < 1800 else None
    if ts >= track[-1][0]:
        return (track[-1][1], track[-1][2]) if abs(ts - track[-1][0]) < 1800 else None
    lo, hi = 0, len(track) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if track[mid][0] <= ts:
            lo = mid
        else:
            hi = mid
    t0, la0, lo0 = track[lo][0], track[lo][1], track[lo][2]
    t1, la1, lo1 = track[hi][0], track[hi][1], track[hi][2]
    if t1 == t0:
        return la0, lo0
    f = (ts - t0) / (t1 - t0)
    return la0 + f * (la1 - la0), lo0 + f * (lo1 - lo0)
