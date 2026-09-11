"""SQLite storage. Plain sqlite3 in WAL mode - fast enough for AIS rates and
one less dependency than an async driver."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from backend import config

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS vessels (
    mmsi         INTEGER PRIMARY KEY,
    name         TEXT,
    imo          TEXT,
    callsign     TEXT,
    ship_type    INTEGER,
    length_m     REAL,
    width_m      REAL,
    draught_m    REAL,
    destination  TEXT,
    first_seen   REAL,
    last_seen    REAL
);

CREATE TABLE IF NOT EXISTS ais_positions (
    mmsi       INTEGER NOT NULL,
    ts         REAL    NOT NULL,          -- epoch seconds, UTC
    lat        REAL    NOT NULL,
    lon        REAL    NOT NULL,
    sog        REAL,                      -- speed over ground, knots
    cog        REAL,                      -- course over ground, degrees
    heading    REAL,
    nav_status INTEGER,
    source     TEXT DEFAULT 'live',       -- 'live' | 'noaa' | 'gfw'
    PRIMARY KEY (mmsi, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_pos_ts       ON ais_positions(ts);
CREATE INDEX IF NOT EXISTS idx_pos_mmsi_ts  ON ais_positions(mmsi, ts);

CREATE TABLE IF NOT EXISTS incidents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id   TEXT UNIQUE NOT NULL,   -- e.g. OTA-2026-0001
    detected_at   REAL NOT NULL,          -- satellite acquisition time
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    area_km2      REAL,
    length_km     REAL,
    width_km      REAL,
    volume_min_m3 REAL,
    volume_max_m3 REAL,
    confidence    REAL,
    status        TEXT DEFAULT 'Under Investigation',
    polygon       TEXT,                   -- GeoJSON coordinate ring
    scene_id      TEXT,                   -- Sentinel-1 product id
    source        TEXT,                   -- 'sentinel-1'
    region        TEXT,                   -- AOI key at detection time
    created_at    REAL
);

CREATE TABLE IF NOT EXISTS incident_timeline (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    ts          REAL NOT NULL,
    stage       TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_timeline_inc ON incident_timeline(incident_id, ts);

CREATE TABLE IF NOT EXISTS candidates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    mmsi        INTEGER NOT NULL,
    rank        INTEGER,
    score       REAL,
    components  TEXT,                     -- JSON: per-factor sub-scores
    evidence    TEXT,                     -- JSON: human-readable reasons
    track       TEXT,                     -- JSON: [[ts,lat,lon,sog,cog],...]
    computed_at REAL,
    UNIQUE (incident_id, mmsi)
);
CREATE INDEX IF NOT EXISTS idx_cand_inc ON candidates(incident_id, rank);

CREATE TABLE IF NOT EXISTS drift_runs (
    incident_id TEXT PRIMARY KEY,
    computed_at REAL,
    hours_back  REAL,
    particles   INTEGER,
    result      TEXT                      -- JSON: cloud, source region, envelope
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    incident_id TEXT,
    kind        TEXT,
    message     TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts DESC);
"""


def connect() -> sqlite3.Connection:
    """One connection per thread; SQLite objects are not thread-safe."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    # Additive migration for databases created before a column existed.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(incidents)")}
    if "region" not in cols:
        conn.execute("ALTER TABLE incidents ADD COLUMN region TEXT")
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(ais_positions)")}
    if "source" not in pcols:
        conn.execute("ALTER TABLE ais_positions ADD COLUMN source TEXT DEFAULT 'live'")
    # Backfill unconditionally, not only when the column is first created: a
    # server process already running with the pre-migration module in memory
    # keeps inserting NULLs for as long as it lives.
    conn.execute("UPDATE ais_positions SET source='live' WHERE source IS NULL")
    conn.commit()


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return connect().execute(sql, tuple(params)).fetchone()


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# --- write helpers -----------------------------------------------------------

def upsert_vessel(conn: sqlite3.Connection, mmsi: int, ts: float, **fields: Any) -> None:
    """Insert or merge vessel static data. COALESCE keeps previously known
    values when a later message omits them (position reports carry no name)."""
    cols = ["name", "imo", "callsign", "ship_type", "length_m",
            "width_m", "draught_m", "destination"]
    vals = [fields.get(c) for c in cols]
    conn.execute(
        f"""
        INSERT INTO vessels (mmsi, {', '.join(cols)}, first_seen, last_seen)
        VALUES (?, {', '.join('?' * len(cols))}, ?, ?)
        ON CONFLICT(mmsi) DO UPDATE SET
            {', '.join(f'{c} = COALESCE(excluded.{c}, vessels.{c})' for c in cols)},
            last_seen = MAX(COALESCE(vessels.last_seen, 0), excluded.last_seen)
        """,
        (mmsi, *vals, ts, ts),
    )


def insert_positions(conn: sqlite3.Connection, rows: list[tuple],
                     source: str = "live") -> int:
    """rows: (mmsi, ts, lat, lon, sog, cog, heading, nav_status).

    `source` matters: retention only prunes the live stream. Imported
    historical archives (NOAA, GFW) are deliberate, often old, and expensive to
    re-fetch - deleting them because they predate a rolling window would be
    silent data loss.
    """
    if not rows:
        return 0
    cur = conn.executemany(
        "INSERT OR IGNORE INTO ais_positions "
        "(mmsi, ts, lat, lon, sog, cog, heading, nav_status, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(*r, source) for r in rows],
    )
    return cur.rowcount or 0


def add_alert(conn: sqlite3.Connection, ts: float, kind: str, message: str,
              incident_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO alerts (ts, incident_id, kind, message) VALUES (?,?,?,?)",
        (ts, incident_id, kind, message),
    )


def add_timeline(conn: sqlite3.Connection, incident_id: str, ts: float,
                 stage: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO incident_timeline (incident_id, ts, stage, detail) VALUES (?,?,?,?)",
        (incident_id, ts, stage, detail),
    )


def prune_old_positions(conn: sqlite3.Connection, before_ts: float) -> int:
    """Prune the rolling live feed only. Imported archives are never touched."""
    cur = conn.execute(
        "DELETE FROM ais_positions WHERE ts < ? AND COALESCE(source,'live') = 'live'",
        (before_ts,))
    return cur.rowcount or 0


def jdump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=float)
