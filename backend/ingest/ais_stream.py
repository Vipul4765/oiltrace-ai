"""Live AIS collector for aisstream.io.

Bandwidth discipline (aisstream applies per-user limits and drops your messages
if you blow past them, so this is not optional):
  * one tight bounding box, not the globe
  * FilterMessageTypes so we only receive what we store
  * permessage-deflate compression negotiated on the socket
  * per-vessel throttle: at most one stored position per AIS_MIN_INTERVAL_SECONDS
  * buffered batch writes instead of a transaction per message
  * exponential backoff on reconnect, capped - never hammer the endpoint
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime

import websockets

from backend import config, db

_STATS = {
    "connected": False,
    "connected_since": None,
    "messages_received": 0,
    "positions_stored": 0,
    "static_updates": 0,
    "throttled": 0,
    "out_of_area": 0,
    "reconnects": 0,
    "last_message_at": None,
    "last_error": None,
}


def stats() -> dict:
    return dict(_STATS)


def _parse_time(meta: dict) -> float:
    """aisstream stamps time_utc like '2026-09-10 09:36:22.999538 +0000 UTC'."""
    raw = (meta or {}).get("time_utc")
    if not raw:
        return time.time()
    cleaned = str(raw).replace(" UTC", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(cleaned, fmt).timestamp()
        except ValueError:
            continue
    return time.time()


def _sane(value, unavailable, lo=None, hi=None):
    """AIS uses sentinel values for 'not available' (511 heading, 360 COG,
    102.3 SOG). Turn those into None instead of storing nonsense."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if abs(v - unavailable) < 1e-6:
        return None
    if lo is not None and v < lo:
        return None
    if hi is not None and v > hi:
        return None
    return v


class AisStreamCollector:
    def __init__(self) -> None:
        self.buffer: list[tuple] = []
        self.static: dict[int, dict] = {}
        self.last_stored: dict[int, float] = {}
        self.last_flush = time.time()

    # --- message handling ----------------------------------------------------
    def handle(self, msg: dict) -> None:
        _STATS["messages_received"] += 1
        _STATS["last_message_at"] = time.time()
        mtype = msg.get("MessageType")
        meta = msg.get("MetaData") or {}
        body = (msg.get("Message") or {}).get(mtype) or {}
        mmsi = meta.get("MMSI") or body.get("UserID")
        if not mmsi:
            return
        mmsi = int(mmsi)

        if mtype == "ShipStaticData":
            dim = body.get("Dimension") or {}
            self.static[mmsi] = {
                "name": (body.get("Name") or meta.get("ShipName") or "").strip() or None,
                "imo": str(body["ImoNumber"]) if body.get("ImoNumber") else None,
                "callsign": (body.get("CallSign") or "").strip() or None,
                "ship_type": body.get("Type"),
                "length_m": (dim.get("A") or 0) + (dim.get("B") or 0) or None,
                "width_m": (dim.get("C") or 0) + (dim.get("D") or 0) or None,
                "draught_m": _sane(body.get("MaximumStaticDraught"), 0.0),
                "destination": (body.get("Destination") or "").strip() or None,
                "ts": _parse_time(meta),
            }
            _STATS["static_updates"] += 1
            return

        lat = body.get("Latitude", meta.get("latitude"))
        lon = body.get("Longitude", meta.get("longitude"))
        if lat is None or lon is None:
            return
        lat, lon = float(lat), float(lon)
        if not (config.AOI_LAT_MIN <= lat <= config.AOI_LAT_MAX
                and config.AOI_LON_MIN <= lon <= config.AOI_LON_MAX):
            _STATS["out_of_area"] += 1
            return

        ts = _parse_time(meta)
        prev = self.last_stored.get(mmsi)
        if prev is not None and ts - prev < config.AIS_MIN_INTERVAL_SECONDS:
            _STATS["throttled"] += 1
            return
        self.last_stored[mmsi] = ts

        self.buffer.append((
            mmsi, round(ts, 1), lat, lon,
            _sane(body.get("Sog"), 102.3, 0, 102.2),
            _sane(body.get("Cog"), 360.0, 0, 359.9),
            _sane(body.get("TrueHeading"), 511, 0, 359),
            body.get("NavigationalStatus"),
        ))
        # Remember the name even when only position reports arrive.
        name = (meta.get("ShipName") or "").strip()
        if name and mmsi not in self.static:
            self.static[mmsi] = {"name": name, "ts": ts}

    # --- persistence ---------------------------------------------------------
    def flush(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_flush < config.AIS_FLUSH_SECONDS:
            return
        self.last_flush = now
        if not self.buffer and not self.static:
            return
        rows, static = self.buffer, self.static
        self.buffer, self.static = [], {}
        try:
            with db.tx() as conn:
                for mmsi, info in static.items():
                    db.upsert_vessel(conn, mmsi, info.pop("ts", now), **info)
                stored = db.insert_positions(conn, rows)
            _STATS["positions_stored"] += stored
        except Exception as exc:
            _STATS["last_error"] = f"write failed: {exc}"

    # --- socket loop ---------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        if not config.AISSTREAM_KEY:
            _STATS["last_error"] = "AISSTREAM_API_KEY is not set"
            return
        subscribe = json.dumps({
            "APIKey": config.AISSTREAM_KEY,
            "BoundingBoxes": config.aoi_bbox(),
            "FilterMessageTypes": config.AIS_MESSAGE_TYPES,
        })
        backoff = config.AIS_BACKOFF_MIN
        while not stop.is_set():
            try:
                async with websockets.connect(
                    config.AISSTREAM_URL,
                    compression="deflate",        # stay inside the bandwidth cap
                    ping_interval=20,
                    ping_timeout=20,
                    max_queue=512,
                ) as ws:
                    await ws.send(subscribe)
                    _STATS.update(connected=True, connected_since=time.time(),
                                  last_error=None)
                    backoff = config.AIS_BACKOFF_MIN
                    while not stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=60)
                        try:
                            self.handle(json.loads(raw))
                        except Exception as exc:
                            _STATS["last_error"] = f"parse: {exc}"
                        self.flush()
            except asyncio.TimeoutError:
                _STATS["last_error"] = "no data for 60s - reconnecting"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _STATS["last_error"] = f"{type(exc).__name__}: {exc}"
            finally:
                _STATS["connected"] = False
                self.flush(force=True)

            if stop.is_set():
                break
            _STATS["reconnects"] += 1
            # Full jitter backoff: spreads reconnects instead of synchronising them.
            wait = random.uniform(config.AIS_BACKOFF_MIN, backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, config.AIS_BACKOFF_MAX)


async def run_collector(stop: asyncio.Event) -> None:
    await AisStreamCollector().run(stop)
