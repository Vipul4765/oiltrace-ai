"""Region profiles.

Free AIS coverage is wildly uneven: aisstream.io is fed by volunteer shore
receivers, so NW Europe is saturated and the Indian coast has literally nobody.
Rather than pretend otherwise, each profile states its real coverage so the UI
can tell the truth about what a given region can and cannot show.

Measured with the project's own key on 2026-09-10 (vessels seen in ~14 s).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Region:
    key: str
    name: str
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    ais_live: str            # none | sparse | good | excellent
    ais_note: str
    sources: list[str] = field(default_factory=list)
    blurb: str = ""

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """(lon_min, lat_min, lon_max, lat_max) - GIS/STAC order."""
        return (self.lon_min, self.lat_min, self.lon_max, self.lat_max)

    @property
    def center(self) -> tuple[float, float]:
        return ((self.lat_min + self.lat_max) / 2, (self.lon_min + self.lon_max) / 2)

    def to_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "lat_min": self.lat_min,
                "lat_max": self.lat_max, "lon_min": self.lon_min,
                "lon_max": self.lon_max, "ais_live": self.ais_live,
                "ais_note": self.ais_note, "sources": self.sources,
                "blurb": self.blurb, "center": list(self.center)}


REGIONS: dict[str, Region] = {
    "mumbai": Region(
        key="mumbai", name="Arabian Sea - Mumbai",
        lat_min=17.90, lat_max=19.80, lon_min=71.50, lon_max=73.30,
        ais_live="none",
        ais_note="aisstream: 0 vessels (no Indian shore receivers). Real "
                 "vessel data here comes from Global Fishing Watch instead - "
                 "gridded satellite-AIS activity, verified working.",
        sources=["sentinel1", "openmeteo", "gfw"],
        blurb="Approaches to Mumbai Harbour and JNPT. Real Sentinel-1 coverage "
              "every ~12 days; no free live AIS."),
    "gulf-of-kutch": Region(
        key="gulf-of-kutch", name="Gulf of Kutch - Sikka / Vadinar",
        lat_min=21.50, lat_max=23.50, lon_min=68.50, lon_max=70.50,
        ais_live="none",
        ais_note="aisstream: 0 vessels. India's largest crude terminals, and "
                 "no free live AIS. Use Global Fishing Watch here too.",
        sources=["sentinel1", "openmeteo", "gfw"],
        blurb="India's busiest oil terminals (Sikka, Vadinar). High spill risk, "
              "zero free live AIS."),
    "north-sea": Region(
        key="north-sea", name="North Sea - Netherlands / Germany / Denmark",
        lat_min=51.50, lat_max=56.50, lon_min=2.00, lon_max=9.00,
        ais_live="excellent",
        ais_note="~740 distinct vessels in 15 s. Densest free AIS coverage "
                 "anywhere, and the operational area of EMSA CleanSeaNet.",
        sources=["sentinel1", "openmeteo", "aisstream"],
        blurb="Europe's busiest shipping. The reference region for a fully "
              "real, live end-to-end demonstration."),
    "english-channel": Region(
        key="english-channel", name="English Channel - Dover Strait",
        lat_min=49.50, lat_max=51.50, lon_min=-2.00, lon_max=2.50,
        ais_live="good",
        ais_note="~48 vessels in 14 s. Dense traffic separation scheme, "
                 "well covered by shore receivers.",
        sources=["sentinel1", "openmeteo", "aisstream"],
        blurb="The world's busiest shipping lane and a designated "
              "MARPOL Special Area."),
    "gulf-of-mexico": Region(
        key="gulf-of-mexico", name="Gulf of Mexico - Louisiana shelf",
        lat_min=25.00, lat_max=30.50, lon_min=-95.00, lon_max=-88.00,
        ais_live="good",
        ais_note="~42 vessels in 14 s live, plus free NOAA historical AIS "
                 "covering all US waters back to 2009.",
        sources=["sentinel1", "openmeteo", "aisstream", "noaa"],
        blurb="Offshore oil country - Deepwater Horizon, thousands of "
              "platforms. The only region here with free historical AIS."),
    "singapore": Region(
        key="singapore", name="Singapore Strait / Malacca",
        lat_min=0.50, lat_max=2.00, lon_min=102.50, lon_max=105.00,
        ais_live="sparse",
        ais_note="~5 vessels in 15 s. Enormous real traffic, but very few "
                 "volunteer receivers feed the free stream.",
        sources=["sentinel1", "openmeteo", "gfw"],
        blurb="World's busiest bunkering port and a chronic slick hotspot."),
}

DEFAULT_REGION = "mumbai"


def get(key: str | None) -> Region:
    return REGIONS.get((key or DEFAULT_REGION).lower(), REGIONS[DEFAULT_REGION])


def keys() -> list[str]:
    return list(REGIONS)
