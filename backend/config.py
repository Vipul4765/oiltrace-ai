"""Central configuration. Everything tunable lives here or in .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- storage -----------------------------------------------------------------
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
# OILTRACE_DATA_DIR lets a host point storage at a mounted persistent disk.
# Without it, a container filesystem is ephemeral and every redeploy starts
# from an empty database.
DATA_DIR = Path(os.getenv("OILTRACE_DATA_DIR", DATA_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.getenv("OILTRACE_DB", DATA_DIR / "oiltrace.db"))
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# --- area of interest --------------------------------------------------------
# Free AIS coverage is regional, so the AOI is a switchable profile rather than
# a hard-coded box. See backend/regions.py for measured coverage per region.
from backend import regions as _regions            # noqa: E402

ACTIVE_REGION = os.getenv("OILTRACE_REGION", _regions.DEFAULT_REGION)
_r = _regions.get(ACTIVE_REGION)

AOI_NAME = os.getenv("AOI_NAME") or _r.name
AOI_LAT_MIN = float(os.getenv("AOI_LAT_MIN", _r.lat_min))
AOI_LAT_MAX = float(os.getenv("AOI_LAT_MAX", _r.lat_max))
AOI_LON_MIN = float(os.getenv("AOI_LON_MIN", _r.lon_min))
AOI_LON_MAX = float(os.getenv("AOI_LON_MAX", _r.lon_max))


def set_region(key: str) -> "_regions.Region":
    """Switch the active AOI at runtime. Mutates module globals on purpose -
    every consumer reads config.AOI_* at call time, so the change takes effect
    immediately. The AIS collector watches for it and resubscribes."""
    global ACTIVE_REGION, AOI_NAME, AOI_LAT_MIN, AOI_LAT_MAX, AOI_LON_MIN, AOI_LON_MAX
    r = _regions.get(key)
    ACTIVE_REGION = r.key
    AOI_NAME = r.name
    AOI_LAT_MIN, AOI_LAT_MAX = r.lat_min, r.lat_max
    AOI_LON_MIN, AOI_LON_MAX = r.lon_min, r.lon_max
    return r


def active_region() -> "_regions.Region":
    return _regions.get(ACTIVE_REGION)


def aoi_bbox() -> list[list[list[float]]]:
    """aisstream.io bounding-box format: [[[lat,lon],[lat,lon]]]."""
    return [[[AOI_LAT_MIN, AOI_LON_MIN], [AOI_LAT_MAX, AOI_LON_MAX]]]


def aoi_bbox_gis() -> tuple[float, float, float, float]:
    """(lon_min, lat_min, lon_max, lat_max) - STAC / GIS order."""
    return (AOI_LON_MIN, AOI_LAT_MIN, AOI_LON_MAX, AOI_LAT_MAX)


def aoi_center() -> tuple[float, float]:
    return ((AOI_LAT_MIN + AOI_LAT_MAX) / 2, (AOI_LON_MIN + AOI_LON_MAX) / 2)


# --- AIS ingest --------------------------------------------------------------
AISSTREAM_KEY = os.getenv("AISSTREAM_API_KEY", "").strip()
AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
# Only the message types we actually use. Fewer types = less bandwidth.
AIS_MESSAGE_TYPES = ["PositionReport", "ShipStaticData"]
# Flush buffered positions to SQLite at most this often (seconds).
AIS_FLUSH_SECONDS = float(os.getenv("AIS_FLUSH_SECONDS", 5.0))
# Drop repeat positions for the same vessel inside this window (seconds).
# A ship under way sends every 2-10s; 30s is plenty for drift correlation and
# cuts the row count by ~10x.
AIS_MIN_INTERVAL_SECONDS = float(os.getenv("AIS_MIN_INTERVAL_SECONDS", 30.0))
AIS_RETENTION_DAYS = float(os.getenv("AIS_RETENTION_DAYS", 14))
# Reconnect backoff bounds (seconds) - never hammer the endpoint.
AIS_BACKOFF_MIN = 5.0
AIS_BACKOFF_MAX = 300.0

# "live" -> aisstream.io websocket (needs AISSTREAM_API_KEY)
# "off"  -> no live collector; regions without aisstream coverage use GFW or
#           NOAA ingest instead. There is deliberately no simulator mode:
#           every vessel this system stores came from a real transmission.
AIS_SOURCE = os.getenv("AIS_SOURCE", "live" if AISSTREAM_KEY else "off")

# Global Fishing Watch: free non-commercial token, GLOBAL satellite-AIS coverage
# including Indian waters. Register at globalfishingwatch.org to get one.
GFW_TOKEN = os.getenv("GFW_API_TOKEN", "").strip()
GFW_BASE = "https://gateway.api.globalfishingwatch.org"

# NOAA MarineCadastre: free historical AIS, US waters, no token at all.
NOAA_AIS_BASE = "https://coast.noaa.gov/htdata/CMSP/AISDataHandler"

# --- environment / drift -----------------------------------------------------
OPEN_METEO_MARINE = "https://marine-api.open-meteo.com/v1/marine"
OPEN_METEO_WEATHER = "https://api.open-meteo.com/v1/forecast"
ENV_CACHE_HOURS = float(os.getenv("ENV_CACHE_HOURS", 6))

# Backward-drift model. Surface slick moves with the current plus ~3% of wind.
WIND_DRIFT_FACTOR = float(os.getenv("WIND_DRIFT_FACTOR", 0.03))
# Empirical: slicks deflect right of wind in the N hemisphere (Coriolis).
WIND_DEFLECTION_DEG = float(os.getenv("WIND_DEFLECTION_DEG", 15.0))
DRIFT_PARTICLES = int(os.getenv("DRIFT_PARTICLES", 600))
DRIFT_STEP_MINUTES = float(os.getenv("DRIFT_STEP_MINUTES", 30))
DRIFT_MAX_HOURS = float(os.getenv("DRIFT_MAX_HOURS", 24))
# Random-walk spread applied per step to represent unresolved turbulence (m/s).
DRIFT_DIFFUSION = float(os.getenv("DRIFT_DIFFUSION", 0.06))

# --- attribution scoring -----------------------------------------------------
# Weights must sum to 1.0; enforced at import so a bad edit fails loudly.
SCORE_WEIGHTS = {
    "spatial": 0.34,      # how deep inside the back-drift cone the vessel passed
    "temporal": 0.22,     # how well its passage lines up with the release window
    "persistence": 0.14,  # how long it lingered in the probable source region
    "behaviour": 0.18,    # slow steaming / course deviation / AIS gap
    "vessel_type": 0.12,  # tankers and large cargo carry more oil
}
assert abs(sum(SCORE_WEIGHTS.values()) - 1.0) < 1e-9, "SCORE_WEIGHTS must sum to 1"

# Vessels closer than this to any back-drift particle are candidates (km).
CANDIDATE_RADIUS_KM = float(os.getenv("CANDIDATE_RADIUS_KM", 25.0))
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", 25))
# Loitering is scored in a tighter box than candidacy: a vessel 20 km away is
# worth screening, but it is not "lingering at the source".
PERSISTENCE_RADIUS_KM = float(os.getenv("PERSISTENCE_RADIUS_KM", 8.0))
PERSISTENCE_FULL_MINUTES = float(os.getenv("PERSISTENCE_FULL_MINUTES", 240.0))
