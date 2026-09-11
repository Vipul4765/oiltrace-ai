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
    """Fetch and cache one Natural Earth layer as lon/lat rings.

    Rings are stored as numpy arrays, not nested Python lists. Natural Earth
    10m has ~480,000 coordinate pairs; as `[[lon, lat], ...]` that is ~84 MB of
    Python objects held for the life of the process, which matters on a 512 MB
    host. As float32 arrays it is under 5 MB.
    """
    if name in _cache:
        return _cache[name]
    # Prefer a compact .npz of already-converted rings. Parsing the 10 MB
    # GeoJSON builds ~480,000 nested Python lists, and that transient spike -
    # not the retained data - is what dominates peak memory. After the first
    # run we never parse it again.
    npz = config.CACHE_DIR / f"{name}.npz"
    if npz.exists():
        try:
            with np.load(npz) as z:
                rings = [z[k] for k in z.files]
            _cache[name] = rings
            return rings
        except Exception:
            npz.unlink(missing_ok=True)        # corrupt cache, rebuild below

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
                arr = np.asarray(poly[0], dtype=np.float32)   # exterior ring
                if arr.ndim == 2 and arr.shape[0] >= 3:
                    rings.append(arr)
    del data                                   # drop the parsed JSON promptly
    try:
        np.savez_compressed(npz, *rings)
    except Exception as exc:
        print(f"[landmask] could not cache {name}.npz ({exc})")
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
        # Cheap bbox reject before projecting every vertex.
        lo = ring.min(axis=0)
        hi = ring.max(axis=0)
        if (hi[0] < lon_min or lo[0] > lon_max
                or hi[1] < lat_min or lo[1] > lat_max):
            continue
        pts = np.empty(ring.shape, dtype=np.int32)
        pts[:, 0] = ((ring[:, 0] - lon_min) * sx).astype(np.int32)
        pts[:, 1] = ((lat_max - ring[:, 1]) * sy).astype(np.int32)
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
