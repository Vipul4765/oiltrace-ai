"""Global Fishing Watch - real satellite-AIS derived vessel activity.

This is the only free source with genuine coverage of Indian waters, so it is
the answer for the Mumbai / Gulf of Kutch regions where aisstream sees nothing.

Get a token: register at https://globalfishingwatch.org, request an API token
(free, non-commercial / research), then put it in .env as GFW_API_TOKEN.

**What a standard token actually grants** (measured 2026-09-11, not guessed):

    /v3/vessels/search                              200  OK
    /v3/4wings/report  public-global-fishing-effort  200  OK
    /v3/4wings/report  public-global-all-vessels-presence   403
    /v3/events         loitering/gaps/encounters/
                       port-visits/fishing                  403

So the working path is the **gridded 4wings report over fishing-effort**, which
does return real positions and identities: lat, lon, mmsi, imo, callsign,
shipName, flag, vesselType, entry/exit timestamps and activity hours.

Two consequences worth stating plainly:

1. Coverage is **fishing vessels** (plus craft GFW classes as INCONCLUSIVE).
   Tankers and cargo ships - the vessels that actually cause oil spills - are
   in the `all-vessels-presence` dataset, which a standard token cannot read.
   Ask GFW to add that dataset to your token if you need them.
2. It is **gridded activity**, not a dense breadcrumb track: one position per
   vessel per cell per day, with an entry and exit time. Enough to place a
   vessel in a region during a window; not enough to interpolate a course.

Both limits are real and are surfaced in the ingest result rather than hidden.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx

from backend import config, db

EVENT_DATASETS = {
    "loitering": "public-global-loitering-events:latest",
    "encounter": "public-global-encounters-events:latest",
    "port_visit": "public-global-port-visits-events:latest",
    "gap": "public-global-gaps-events:latest",
    "fishing": "public-global-fishing-events:latest",
}
PRESENCE_DATASET = "public-global-all-vessels-presence:latest"
IDENTITY_DATASET = "public-global-vessel-identity:latest"


class GFWError(RuntimeError):
    pass


def _headers() -> dict:
    if not config.GFW_TOKEN:
        raise GFWError(
            "GFW_API_TOKEN is not set. Register free at globalfishingwatch.org, "
            "request an API token, and add it to .env as GFW_API_TOKEN.")
    return {"Authorization": f"Bearer {config.GFW_TOKEN}",
            "Accept": "application/json"}


def _get(path: str, params: list[tuple] | dict, timeout: float = 60.0) -> dict:
    r = httpx.get(f"{config.GFW_BASE}{path}", params=params,
                  headers=_headers(), timeout=timeout)
    if r.status_code == 401:
        raise GFWError("GFW rejected the token (401). Check GFW_API_TOKEN.")
    if r.status_code == 403:
        raise GFWError("GFW token lacks access to this dataset (403).")
    r.raise_for_status()
    return r.json()


def _post(path: str, params: list[tuple], body: dict, timeout: float = 90.0) -> dict:
    r = httpx.post(f"{config.GFW_BASE}{path}", params=params, json=body,
                   headers=_headers(), timeout=timeout)
    if r.status_code == 401:
        raise GFWError("GFW rejected the token (401). Check GFW_API_TOKEN.")
    r.raise_for_status()
    return r.json()


def _bbox_geojson(bbox: tuple[float, float, float, float]) -> dict:
    lon_min, lat_min, lon_max, lat_max = bbox
    return {"type": "Polygon", "coordinates": [[
        [lon_min, lat_min], [lon_max, lat_min], [lon_max, lat_max],
        [lon_min, lat_max], [lon_min, lat_min]]]}


PRESENCE_DATASET = "public-global-all-vessels-presence:latest"   # 403 on standard tokens
EFFORT_DATASET = "public-global-fishing-effort:latest"           # works
IDENTITY_DATASET = "public-global-vessel-identity:latest"


class GFWError(RuntimeError):
    pass


def _headers() -> dict:
    if not config.GFW_TOKEN:
        raise GFWError(
            "GFW_API_TOKEN is not set. Register free at globalfishingwatch.org, "
            "request an API token, and add it to .env as GFW_API_TOKEN.")
    return {"Authorization": f"Bearer {config.GFW_TOKEN}",
            "Accept": "application/json", "Content-Type": "application/json"}


def _bbox_geojson(bbox: tuple[float, float, float, float]) -> dict:
    lon_min, lat_min, lon_max, lat_max = bbox
    return {"type": "Polygon", "coordinates": [[
        [lon_min, lat_min], [lon_max, lat_min], [lon_max, lat_max],
        [lon_min, lat_max], [lon_min, lat_min]]]}


def _parse_ts(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def activity(bbox: tuple[float, float, float, float], start: datetime,
             end: datetime, dataset: str = EFFORT_DATASET,
             resolution: str = "HIGH") -> list[dict]:
    """Gridded vessel activity with positions and identity.

    This is the endpoint that actually works on a standard token. Returns one
    row per vessel per cell per day.
    """
    params = [
        ("spatial-resolution", resolution),
        ("temporal-resolution", "DAILY"),
        ("group-by", "VESSEL_ID"),
        ("datasets[0]", dataset),
        ("date-range", f"{start:%Y-%m-%d},{end:%Y-%m-%d}"),
        ("format", "JSON"),
    ]
    r = httpx.post(f"{config.GFW_BASE}/v3/4wings/report", params=params,
                   json={"geojson": _bbox_geojson(bbox)},
                   headers=_headers(), timeout=120.0)
    if r.status_code == 401:
        raise GFWError("GFW rejected the token (401). Check GFW_API_TOKEN.")
    if r.status_code == 403:
        raise GFWError(
            f"Token lacks permission for {dataset}. A standard token covers "
            "public-global-fishing-effort only; ask GFW to add "
            "public-global-all-vessels-presence for tankers and cargo.")
    r.raise_for_status()
    rows: list[dict] = []
    for group in (r.json().get("entries") or []):
        for _ds, items in (group or {}).items():
            rows.extend(items or [])
    return rows


def search_vessel(query: str, limit: int = 10) -> list[dict]:
    r = httpx.get(f"{config.GFW_BASE}/v3/vessels/search",
                  params={"query": query, "datasets[0]": IDENTITY_DATASET,
                          "limit": limit},
                  headers=_headers(), timeout=60.0)
    r.raise_for_status()
    out = []
    for e in r.json().get("entries", []):
        si = (e.get("selfReportedInfo") or [{}])[0]
        out.append({"name": si.get("shipname"), "mmsi": si.get("ssvid"),
                    "imo": si.get("imo"), "flag": si.get("flag"),
                    "vessel_id": e.get("id")})
    return out


def ingest_activity(bbox: tuple[float, float, float, float] | None = None,
                    days: float = 30.0,
                    dataset: str = EFFORT_DATASET) -> dict:
    """Pull real GFW vessel activity into the AIS tables.

    Each gridded row carries a cell position plus an entry and an exit time, so
    we store two fixes per row. Sparse next to a live feed - but real, and the
    only free vessel data that covers Indian waters at all.
    """
    bbox = bbox or config.aoi_bbox_gis()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    rows = activity(bbox, start, end, dataset=dataset)

    positions, vessels = [], {}
    skipped = 0
    for row in rows:
        lat, lon = row.get("lat"), row.get("lon")
        mmsi = row.get("mmsi")
        if lat is None or lon is None or not mmsi:
            skipped += 1
            continue
        try:
            mmsi = int(mmsi)
        except (TypeError, ValueError):
            skipped += 1
            continue
        t_in = _parse_ts(row.get("entryTimestamp"))
        t_out = _parse_ts(row.get("exitTimestamp"))
        stamps = [t for t in (t_in, t_out) if t is not None]
        if not stamps:
            t = _parse_ts(row.get("date"))
            stamps = [t] if t else []
        for ts in stamps:
            positions.append((mmsi, round(ts, 1), float(lat), float(lon),
                              None, None, None, None))
        if stamps:
            vessels[mmsi] = {
                "name": (row.get("shipName") or "").strip() or None,
                "imo": str(row["imo"]) if row.get("imo") else None,
                "callsign": (row.get("callsign") or "").strip() or None,
                "ship_type": None,
                "length_m": None, "width_m": None, "draught_m": None,
                "destination": (row.get("flag") or None),
                "ts": max(stamps),
            }

    stored = 0
    if positions:
        with db.tx() as conn:
            for mmsi, info in vessels.items():
                db.upsert_vessel(conn, mmsi, info.pop("ts"), **info)
            stored = db.insert_positions(conn, positions, source="gfw")

    return {"source": "global-fishing-watch", "real": True, "dataset": dataset,
            "rows_returned": len(rows), "rows_skipped": skipped,
            "positions_stored": stored, "vessels": len(vessels),
            "bbox": list(bbox), "days": days,
            "limitation": "fishing-effort dataset only: fishing vessels plus "
                          "INCONCLUSIVE craft. Tankers and cargo need the "
                          "all-vessels-presence dataset (403 on a standard token).",
            "granularity": "gridded activity, one fix per vessel per cell per "
                           "day (entry + exit), not a continuous track"}


def status() -> dict:
    return {"configured": bool(config.GFW_TOKEN),
            "base": config.GFW_BASE,
            "working_dataset": EFFORT_DATASET,
            "note": "standard token: fishing-effort only; events and "
                    "all-vessels-presence return 403"}
