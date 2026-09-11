"""NOAA MarineCadastre - real historical AIS, free, no token whatsoever.

US waters only, but it is the highest-fidelity free AIS available anywhere:
full position tracks with speed, course, heading and complete vessel identity,
back to 2009. That makes it the right data for *developing and validating* the
correlation engine, even if the demo region ends up elsewhere.

One daily file covers all US waters and is ~360 MB zipped, so we download once,
cache it, and filter to the AOI on the way into SQLite.

    https://coast.noaa.gov/htdata/CMSP/AISDataHandler/<YYYY>/AIS_<YYYY>_<MM>_<DD>.zip

CSV columns:
    MMSI, BaseDateTime, LAT, LON, SOG, COG, Heading, VesselName, IMO,
    CallSign, VesselType, Status, Length, Width, Draft, Cargo, TransceiverClass
"""
from __future__ import annotations

import csv
import io
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from backend import config, db

_UNAVAILABLE = {"", "0.0", "511.0", "511", "360.0"}


def url_for(day: date) -> str:
    return f"{config.NOAA_AIS_BASE}/{day:%Y}/AIS_{day:%Y_%m_%d}.zip"


def download(day: date, force: bool = False,
             progress: bool = True) -> Path:
    """Fetch one daily archive into the cache. Skips if already present."""
    dest = config.CACHE_DIR / f"AIS_{day:%Y_%m_%d}.zip"
    if dest.exists() and dest.stat().st_size > 1_000_000 and not force:
        return dest
    url = url_for(day)
    tmp = dest.with_suffix(".part")
    with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as r:
        if r.status_code == 404:
            raise FileNotFoundError(f"NOAA has no AIS file for {day} ({url})")
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        last = 0.0
        with open(tmp, "wb") as fh:
            for chunk in r.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if progress and total and time.time() - last > 5:
                    last = time.time()
                    print(f"[noaa] {done/1e6:.0f}/{total/1e6:.0f} MB "
                          f"({done/total:.0%})", flush=True)
    tmp.rename(dest)
    return dest


def _num(value: str) -> float | None:
    v = (value or "").strip()
    if v in _UNAVAILABLE:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load(day: date, bbox: tuple[float, float, float, float] | None = None,
         min_interval: float | None = None,
         max_rows: int | None = None) -> dict:
    """Filter one day of real AIS into the database for the given bbox."""
    bbox = bbox or config.aoi_bbox_gis()
    lon_min, lat_min, lon_max, lat_max = bbox
    min_interval = (config.AIS_MIN_INTERVAL_SECONDS
                    if min_interval is None else min_interval)

    path = download(day)
    last_seen: dict[int, float] = {}
    vessels: dict[int, dict] = {}
    batch: list[tuple] = []
    stored = scanned = kept = 0

    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise RuntimeError(f"no CSV inside {path.name}")
        with zf.open(names[0]) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8",
                                                     errors="replace"))
            for row in reader:
                scanned += 1
                try:
                    lat = float(row["LAT"]); lon = float(row["LON"])
                except (TypeError, ValueError, KeyError):
                    continue
                if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
                    continue
                try:
                    mmsi = int(row["MMSI"])
                except (TypeError, ValueError):
                    continue
                try:
                    ts = datetime.strptime(row["BaseDateTime"],
                                           "%Y-%m-%dT%H:%M:%S").replace(
                        tzinfo=timezone.utc).timestamp()
                except (TypeError, ValueError):
                    continue

                prev = last_seen.get(mmsi)
                if prev is not None and ts - prev < min_interval:
                    continue
                last_seen[mmsi] = ts
                kept += 1

                batch.append((mmsi, round(ts, 1), lat, lon,
                              _num(row.get("SOG")), _num(row.get("COG")),
                              _num(row.get("Heading")),
                              int(_num(row.get("Status")) or 0)))
                if mmsi not in vessels:
                    vtype = _num(row.get("VesselType"))
                    vessels[mmsi] = {
                        "name": (row.get("VesselName") or "").strip() or None,
                        "imo": (row.get("IMO") or "").strip().replace("IMO", "") or None,
                        "callsign": (row.get("CallSign") or "").strip() or None,
                        "ship_type": int(vtype) if vtype else None,
                        "length_m": _num(row.get("Length")),
                        "width_m": _num(row.get("Width")),
                        "draught_m": _num(row.get("Draft")),
                        "destination": None,
                        "ts": ts,
                    }

                if len(batch) >= 20_000:
                    with db.tx() as conn:
                        stored += db.insert_positions(conn, batch, source="noaa")
                    batch.clear()
                if max_rows and kept >= max_rows:
                    break

    with db.tx() as conn:
        for mmsi, info in vessels.items():
            db.upsert_vessel(conn, mmsi, info.pop("ts"), **info)
        if batch:
            stored += db.insert_positions(conn, batch, source="noaa")

    return {"source": "noaa-marinecadastre", "real": True, "date": str(day),
            "file": path.name, "rows_scanned": scanned, "rows_in_bbox": kept,
            "positions_stored": stored, "vessels": len(vessels),
            "bbox": list(bbox)}


def available_recent(max_years_back: int = 3) -> date:
    """Newest published daily archive.

    NOAA publishes with a long lag, so probing day by day is slow. Parse the
    yearly index page instead - one request per year at worst.
    """
    import re
    today = datetime.now(timezone.utc).date()
    for year in range(today.year, today.year - max_years_back - 1, -1):
        url = f"{config.NOAA_AIS_BASE}/{year}/index.html"
        try:
            r = httpx.get(url, timeout=30.0, follow_redirects=True)
            if r.status_code != 200:
                continue
        except Exception:
            continue
        days = sorted(set(re.findall(r"AIS_(\d{4})_(\d{2})_(\d{2})\.zip", r.text)))
        if days:
            y, m, d = days[-1]
            return date(int(y), int(m), int(d))
    raise RuntimeError("could not find any published NOAA AIS file")
