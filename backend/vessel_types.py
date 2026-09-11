"""AIS ship-type codes (ITU-R M.1371).

Lives here rather than in an ingest module because every source - aisstream,
NOAA, GFW - reports the same code space, and the attribution engine needs to
read it regardless of where a vessel came from.
"""
from __future__ import annotations

SHIP_TYPES = {30: "Fishing", 31: "Towing", 32: "Towing", 33: "Dredger",
              34: "Diving", 35: "Military", 36: "Sailing", 37: "Pleasure",
              50: "Pilot", 51: "Search & Rescue", 52: "Tug", 53: "Port Tender",
              55: "Law Enforcement", 58: "Medical",
              60: "Passenger", 70: "Cargo", 80: "Tanker", 90: "Other"}


def ship_type_name(code: int | None) -> str:
    """Human label for an AIS ship-type code. Falls back to the decade bucket:
    70-79 are all cargo, 80-89 all tanker, and so on."""
    if code is None:
        return "Unknown"
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "Unknown"
    if code in SHIP_TYPES:
        return SHIP_TYPES[code]
    return SHIP_TYPES.get(code // 10 * 10, "Other")
