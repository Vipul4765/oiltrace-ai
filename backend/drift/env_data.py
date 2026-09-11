"""Wind and surface-current fields from Open-Meteo.

Free, no API key, no registration. We cache aggressively on disk because the
drift model asks for the same cell repeatedly and there is no reason to make
the same request twice.

Direction conventions (easy to get backwards, so stated explicitly):
  * wind_direction_10m       - meteorological, the direction the wind blows FROM
  * ocean_current_direction  - oceanographic, the direction the current flows TO
Everything below is normalised to "flowing TOWARDS" bearings.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone

import httpx

from backend import config

_MEM: dict[str, tuple[float, dict]] = {}


def _cache_key(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def _cached_get(url: str, params: dict, ttl_hours: float) -> dict | None:
    key = _cache_key(url, sorted(params.items()))
    now = time.time()
    hit = _MEM.get(key)
    if hit and now - hit[0] < ttl_hours * 3600:
        return hit[1]

    path = config.CACHE_DIR / f"env_{key}.json"
    if path.exists() and now - path.stat().st_mtime < ttl_hours * 3600:
        try:
            data = json.loads(path.read_text())
            _MEM[key] = (now, data)
            return data
        except Exception:
            pass

    try:
        r = httpx.get(url, params=params, timeout=20.0)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        print(f"[env] fetch failed ({exc}) - trying cache")
        if path.exists():                       # stale cache beats no data
            try:
                return json.loads(path.read_text())
            except Exception:
                return None
        return None

    path.write_text(json.dumps(data))
    _MEM[key] = (now, data)
    return data


def _hourly_series(data: dict | None, name: str) -> dict[float, float]:
    """Map epoch-second -> value for one hourly variable."""
    if not data:
        return {}
    hourly = data.get("hourly") or {}
    times, values = hourly.get("time") or [], hourly.get(name) or []
    out: dict[float, float] = {}
    for t, v in zip(times, values):
        if v is None:
            continue
        try:
            dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out[dt.timestamp()] = float(v)
    return out


class EnvField:
    """Time-varying wind and current at a single representative point.

    One point for the whole AOI is a real simplification: it ignores spatial
    shear across ~200 km. For a 24 h backtrack in open water it is a reasonable
    first-order model, and it is honest to say so rather than imply a full
    hydrodynamic solution.
    """

    def __init__(self, lat: float, lon: float, past_days: int = 3,
                 forecast_days: int = 1) -> None:
        self.lat, self.lon = lat, lon

        wx = _cached_get(config.OPEN_METEO_WEATHER, {
            "latitude": round(lat, 3), "longitude": round(lon, 3),
            "hourly": "wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "ms", "timezone": "UTC",
            "past_days": past_days, "forecast_days": forecast_days,
        }, config.ENV_CACHE_HOURS)

        mar = _cached_get(config.OPEN_METEO_MARINE, {
            "latitude": round(lat, 3), "longitude": round(lon, 3),
            "hourly": "ocean_current_velocity,ocean_current_direction,"
                      "wave_height,sea_surface_temperature",
            "timezone": "UTC",
            "past_days": past_days, "forecast_days": forecast_days,
        }, config.ENV_CACHE_HOURS)

        self.wind_speed = _hourly_series(wx, "wind_speed_10m")        # m/s
        self.wind_from = _hourly_series(wx, "wind_direction_10m")     # deg FROM
        # Open-Meteo returns current velocity in km/h; convert to m/s.
        self.cur_speed = {k: v / 3.6 for k, v in
                          _hourly_series(mar, "ocean_current_velocity").items()}
        self.cur_to = _hourly_series(mar, "ocean_current_direction")  # deg TOWARDS
        self.wave = _hourly_series(mar, "wave_height")
        self.sst = _hourly_series(mar, "sea_surface_temperature")

        missing = [name for name, series in (
            ("wind speed", self.wind_speed), ("wind direction", self.wind_from),
            ("ocean current speed", self.cur_speed),
            ("ocean current direction", self.cur_to)) if not series]
        if missing:
            raise RuntimeError(
                f"Open-Meteo returned no {', '.join(missing)} for "
                f"{lat:.3f},{lon:.3f} and no cached response is available. "
                "Refusing to substitute invented values - the drift model "
                "would produce a confident, wrong answer.")

    # --- sampling ------------------------------------------------------------
    @staticmethod
    def _lookup(series: dict[float, float], ts: float,
                default: float | None = None) -> float | None:
        """Interpolate a real series. Returns None when there is no data.

        This used to take an invented default (6 m/s wind, 28 C sea...) and
        hand it back silently, which meant a partial Open-Meteo response
        produced a drift track computed from numbers nobody measured. Missing
        data now reads as missing.
        """
        if not series:
            return default
        keys = sorted(series)
        if ts <= keys[0]:
            return series[keys[0]]
        if ts >= keys[-1]:
            return series[keys[-1]]
        lo, hi = 0, len(keys) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if keys[mid] <= ts:
                lo = mid
            else:
                hi = mid
        t0, t1 = keys[lo], keys[hi]
        f = (ts - t0) / (t1 - t0) if t1 > t0 else 0.0
        return series[t0] + f * (series[t1] - series[t0])

    def sample(self, ts: float) -> dict:
        # Required for the drift physics - absence is a hard error, checked in
        # __init__, so these are always real measured values here.
        ws = self._lookup(self.wind_speed, ts)
        wf = self._lookup(self.wind_from, ts)
        cs = self._lookup(self.cur_speed, ts)
        ct = self._lookup(self.cur_to, ts)
        return {
            "wind_speed_ms": ws,
            "wind_from_deg": wf,
            "wind_to_deg": (wf + 180.0) % 360.0,
            "current_speed_ms": cs,
            "current_to_deg": ct,
            # Display-only. None means Open-Meteo had no value; the UI shows
            # a dash rather than a number we made up.
            "wave_height_m": self._lookup(self.wave, ts),
            "sst_c": self._lookup(self.sst, ts),
        }

    def drift_vector(self, ts: float) -> tuple[float, float]:
        """Net surface-slick velocity (north_ms, east_ms).

        Slick velocity = current + ~3% of wind, deflected right of the wind in
        the northern hemisphere. This is the standard empirical approximation
        used in operational oil-drift models.
        """
        s = self.sample(ts)
        cur = math.radians(s["current_to_deg"])
        cn = s["current_speed_ms"] * math.cos(cur)
        ce = s["current_speed_ms"] * math.sin(cur)
        wdir = math.radians((s["wind_to_deg"] + config.WIND_DEFLECTION_DEG) % 360.0)
        wmag = s["wind_speed_ms"] * config.WIND_DRIFT_FACTOR
        return cn + wmag * math.cos(wdir), ce + wmag * math.sin(wdir)
