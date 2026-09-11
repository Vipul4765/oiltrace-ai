"""Coastline masking for SAR scenes.

Land is radar-dark in places too - wet soil, smooth sand, sheltered water
behind barrier islands - so an unmasked coastal scene reports the shoreline as
a giant oil slick. This was not theoretical: the first real run over the
Mississippi Delta outlined the Louisiana coast as a 147 km2 "spill".

Source: Natural Earth 1:10m land polygons (public domain, no key, ~10 MB),
cached on first use. Rasterised onto the scene grid with OpenCV, so no
shapefile or GIS stack is required.
"""
from __future__ import annotations

import json
from collections import OrderedDict

import cv2
import numpy as np
import httpx

from backend import config, geo

NE_LAND_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
               "master/geojson/ne_10m_land.geojson")
NE_ISLANDS_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
                  "master/geojson/ne_10m_minor_islands.geojson")

_cache: dict[str, list] = {}
_mask_cache: "OrderedDict[tuple, np.ndarray]" = OrderedDict()
_MASK_CACHE_MAX = 8


def _load(url: str, name: str) -> list:
    """Fetch and cache one Natural Earth layer as a list of lon/lat rings."""
    if name in _cache:
        return _cache[name]
    path = config.CACHE_DIR / f"{name}.geojson"
    if not path.exists():
        r = httpx.get(url, timeout=180.0, follow_redirects=True)
        r.raise_for_status()
        path.write_bytes(r.content)
    data = json.loads(path.read_text())

    rings: list = []
    for feat in data.get("features", []):
        geom = feat.get("geometry") or {}
        gtype, coords = geom.get("type"), geom.get("coordinates")
        if gtype == "Polygon":
            polys = [coords]
        elif gtype == "MultiPolygon":
            polys = coords
        else:
            continue
        for poly in polys:
            if poly and poly[0]:
                rings.append(poly[0])          # exterior ring only
    _cache[name] = rings
    return rings


def land_rings(include_islands: bool = True) -> list:
    rings = list(_load(NE_LAND_URL, "ne_10m_land"))
    if include_islands:
        try:
            rings += _load(NE_ISLANDS_URL, "ne_10m_minor_islands")
        except Exception as exc:
            print(f"[landmask] minor islands unavailable ({exc}) - continuing")
    return rings


def build(bounds: tuple[float, float, float, float], shape: tuple[int, int],
          buffer_km: float = 1.5) -> np.ndarray:
    """Binary land mask (1 = land) for a scene.

    bounds = (lon_min, lat_min, lon_max, lat_max); shape = (height, width),
    row 0 at lat_max - matching the detector's pixel convention.
    """
    key = (round(bounds[0], 4), round(bounds[1], 4), round(bounds[2], 4),
           round(bounds[3], 4), shape[0], shape[1], round(buffer_km, 2))
    hit = _mask_cache.get(key)
    if hit is not None:
        _mask_cache.move_to_end(key)
        return hit.copy()

    lon_min, lat_min, lon_max, lat_max = bounds
    h, w = shape
    mask = np.zeros((h, w), dtype=np.uint8)

    sx = (w - 1) / max(lon_max - lon_min, 1e-9)
    sy = (h - 1) / max(lat_max - lat_min, 1e-9)

    drawn = 0
    for ring in land_rings():
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        # Cheap bbox reject before touching every vertex.
        if (max(lons) < lon_min or min(lons) > lon_max
                or max(lats) < lat_min or min(lats) > lat_max):
            continue
        pts = np.array([[(p[0] - lon_min) * sx, (lat_max - p[1]) * sy]
                        for p in ring], dtype=np.int32)
        cv2.fillPoly(mask, [pts], 1)
        drawn += 1

    if drawn and buffer_km > 0:
        # Grow the coast a little: near-shore returns are unreliable and the
        # 1:10m coastline is not precise to the pixel.
        km_per_px = (lon_max - lon_min) * geo.km_per_deg_lon(
            (lat_min + lat_max) / 2) / max(w, 1)
        k = int(np.clip(round(buffer_km / max(km_per_px, 1e-6)), 1, 61)) | 1
        mask = cv2.dilate(mask, np.ones((k, k), np.uint8))

    # Rasterising ~4000 coastline rings takes seconds; the same AOI is asked
    # for repeatedly, so keep a few recent results.
    _mask_cache[key] = mask
    _mask_cache.move_to_end(key)
    while len(_mask_cache) > _MASK_CACHE_MAX:
        _mask_cache.popitem(last=False)
    return mask.copy()


def land_fraction(bounds: tuple[float, float, float, float],
                  shape: tuple[int, int] = (256, 256)) -> float:
    """Share of a box that is land - useful for picking an open-water AOI."""
    return float(build(bounds, shape, buffer_km=0).mean())
