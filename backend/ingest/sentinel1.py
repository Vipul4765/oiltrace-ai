"""Real Sentinel-1 SAR from the Microsoft Planetary Computer.

Why this source: the GRD scenes are hosted as Cloud-Optimised GeoTIFFs on Azure
and the SAS signing endpoint is open, so we can pull a *windowed subset* over
HTTP without an account and without downloading a 1 GB product. Copernicus CDSE
and ASF hold the same data but gate downloads behind a login.

    search()      -> real scene metadata over an area/date range
    read_window() -> a georeferenced pixel array for just our AOI

Free, anonymous, and genuinely the same imagery an operational service uses.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np

from backend import config

STAC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SAS_TOKEN = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{account}/{container}"

_token_cache: dict[str, tuple[float, str]] = {}


@dataclass
class Scene:
    id: str
    datetime: str
    ts: float
    polarizations: list[str]
    orbit_state: str
    assets: dict
    bbox: list[float]

    @property
    def acquired(self) -> datetime:
        return datetime.fromtimestamp(self.ts, tz=timezone.utc)

    def age_days(self) -> float:
        return (time.time() - self.ts) / 86400


def search(bbox: tuple[float, float, float, float], days: int = 30,
           limit: int = 12, mode: str = "IW",
           start: datetime | None = None,
           end: datetime | None = None) -> list[Scene]:
    """Real Sentinel-1 GRD scenes intersecting bbox=(lon_min,lat_min,lon_max,lat_max).

    Pass explicit start/end to search a historical window; otherwise the last
    `days` days. Searching "the last 600 days" with a small limit just returns
    the newest scenes, which is not the same thing as a window around a date.
    """
    end = end or datetime.now(timezone.utc)
    start = start or (end - timedelta(days=days))
    body = {
        "collections": ["sentinel-1-grd"],
        "bbox": list(bbox),
        "datetime": f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
        "limit": limit,
        "query": {"sar:instrument_mode": {"eq": mode}},
    }
    r = httpx.post(STAC_SEARCH, json=body, timeout=45.0)
    r.raise_for_status()
    out: list[Scene] = []
    for f in r.json().get("features", []):
        p = f.get("properties", {})
        dt = p.get("datetime")
        try:
            ts = datetime.fromisoformat(dt.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        out.append(Scene(id=f["id"], datetime=dt, ts=ts,
                         polarizations=p.get("sar:polarizations", []),
                         orbit_state=p.get("sat:orbit_state", ""),
                         assets=f.get("assets", {}), bbox=f.get("bbox", list(bbox))))
    out.sort(key=lambda s: s.ts, reverse=True)
    return out


def _sign(href: str) -> str:
    """Attach an anonymous SAS token. Tokens are short-lived, so cache by
    container and refresh a minute before expiry."""
    if "blob.core.windows.net" not in href:
        return href
    rest = href.split("blob.core.windows.net/", 1)
    account = rest[0].split("//", 1)[1].split(".")[0]
    container = rest[1].split("/", 1)[0]
    # account and container are interpolated into a URL path. They come from a
    # STAC response rather than from a user, but validate anyway: a value
    # containing "../" or a slash would redirect the token request elsewhere on
    # the host. Azure names are lowercase alphanumeric with hyphens.
    if not (re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", account)
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", container)):
        raise ValueError(
            f"refusing to sign: implausible storage account/container "
            f"({account!r}/{container!r})")
    key = f"{account}/{container}"
    hit = _token_cache.get(key)
    if hit and hit[0] > time.time() + 60:
        return f"{href}?{hit[1]}"
    r = httpx.get(SAS_TOKEN.format(account=account, container=container), timeout=30.0)
    r.raise_for_status()
    d = r.json()
    token = d["token"]
    exp = datetime.fromisoformat(d["msft:expiry"].replace("Z", "+00:00")).timestamp()
    _token_cache[key] = (exp, token)
    return f"{href}?{token}"


# A read is float32 and the detector builds roughly a dozen intermediate arrays
# of the same shape, so peak memory scales with pixel count. `max_pixels` caps
# only the longest side, which a wide bbox slips straight past: a whole-world
# request cost ~200 MB of extra RSS and would OOM a 512 MB free tier.
# Measured with the current pipeline: 4.0M px -> +127 MB, 2.5M px -> +98 MB.
MAX_TOTAL_PIXELS = 2_500_000


def read_window(scene: Scene, bbox: tuple[float, float, float, float],
                polarization: str = "vv", max_pixels: int = 1600
                ) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    """Read just our AOI out of the scene. Returns (amplitude, actual_bounds).

    Reads over HTTP range requests via GDAL's vsicurl, so we transfer a few MB
    instead of the full ~1 GB product.
    """
    import os
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.tiff")
    os.environ.setdefault("GDAL_HTTP_MULTIRANGE", "YES")
    os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")

    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds as window_from_bounds

    asset = scene.assets.get(polarization)
    if not asset:
        raise ValueError(f"scene {scene.id} has no '{polarization}' asset "
                         f"(has {list(scene.assets)})")
    url = _sign(asset["href"])

    with rasterio.open(f"/vsicurl/{url}") as ds:
        # Sentinel-1 GRD is delivered in radar geometry: no CRS, no affine
        # transform, just a grid of ground control points. So warp it on the
        # fly to EPSG:4326 - GDAL does this lazily, still over range requests.
        gcps, gcp_crs = ds.gcps
        if ds.crs is None and gcps:
            ctx = WarpedVRT(ds, src_crs=gcp_crs, crs="EPSG:4326",
                            resampling=Resampling.average)
        elif ds.crs is not None:
            ctx = WarpedVRT(ds, crs="EPSG:4326", resampling=Resampling.average) \
                if ds.crs.to_epsg() != 4326 else None
        else:
            raise ValueError(f"scene {scene.id} has neither a CRS nor GCPs")

        src = ctx if ctx is not None else ds
        try:
            win = window_from_bounds(*bbox, src.transform)
            win = win.intersection(Window(0, 0, src.width, src.height))
            if win.width < 8 or win.height < 8:
                raise ValueError("AOI does not overlap this scene")

            scale = max(1.0, max(win.width, win.height) / max_pixels)
            out_h = max(8, int(win.height / scale))
            out_w = max(8, int(win.width / scale))
            # Second, independent cap on area: a wide bbox can stay under the
            # per-side limit while still being enormous.
            if out_h * out_w > MAX_TOTAL_PIXELS:
                shrink = math.sqrt(out_h * out_w / MAX_TOTAL_PIXELS)
                out_h = max(8, int(out_h / shrink))
                out_w = max(8, int(out_w / shrink))
            arr = src.read(1, window=win, out_shape=(out_h, out_w),
                           resampling=Resampling.average).astype(np.float32)
            wb = rasterio.windows.bounds(win, src.transform)
            actual = transform_bounds(src.crs, "EPSG:4326", *wb, densify_pts=21)
        finally:
            if ctx is not None:
                ctx.close()

    # GRD pixels are DN amplitude with 0 = no data outside the swath.
    arr = np.where(arr <= 0, np.nan, arr)
    if np.all(np.isnan(arr)):
        raise ValueError("window is entirely no-data (outside the swath)")
    # Fill gaps with the local median so the detector's blurs stay well behaved.
    med = float(np.nanmedian(arr))
    arr = np.nan_to_num(arr, nan=med)
    return arr, (actual[0], actual[1], actual[2], actual[3])


def fetch_latest(bbox: tuple[float, float, float, float], days: int = 30,
                 polarization: str = "vv", max_pixels: int = 1600,
                 start: datetime | None = None, end: datetime | None = None
                 ) -> tuple[np.ndarray, tuple, Scene]:
    """Most recent real scene covering the AOI, as a ready-to-detect array.

    start/end restrict the search to a historical window - needed when pairing
    imagery with an archived AIS day.
    """
    scenes = search(bbox, days=days, start=start, end=end, limit=60)
    if not scenes:
        raise RuntimeError(f"no Sentinel-1 IW scenes over {bbox} in the last {days} days")
    errors = []
    for s in scenes:
        if polarization.upper() not in [p.upper() for p in s.polarizations]:
            continue
        try:
            arr, actual = read_window(s, bbox, polarization, max_pixels)
            return arr, actual, s
        except Exception as exc:
            errors.append(f"{s.id}: {exc}")
            continue
    raise RuntimeError("no usable scene. tried:\n  " + "\n  ".join(errors[:5]))
