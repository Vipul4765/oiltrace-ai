# 5. Where the data comes from

Everything is free. Total cost of this project: **₹0**.

| Layer | Source | Key needed? |
|---|---|---|
| Satellite radar images | Sentinel-1 via Microsoft Planetary Computer | **no** |
| Wind, current, waves, sea temperature | Open-Meteo | **no** |
| Coastline | Natural Earth | **no** |
| Live ship positions | aisstream.io | free, GitHub login |
| Historical ship positions | NOAA MarineCadastre | **no** |
| Global ship activity | Global Fishing Watch | free, registration |

---

## Satellite images — Sentinel-1

European Space Agency radar satellites. Free and open, forever.

We use **Microsoft Planetary Computer**, which hosts the images in a format
that lets us download *only our area* instead of the whole 1 GB scene. No
account needed.

Copernicus and NASA's ASF host the identical data but require a login, which is
the only reason we do not use them.

**Limitation:** a satellite passes over a given point every **few days**, and
the image publishes hours later. This is not live monitoring.

---

## Weather and ocean — Open-Meteo

Wind speed and direction, ocean current speed and direction, wave height, sea
surface temperature. Free, no key, no registration.

This is genuinely real data. When the dashboard says *"10.1 knots SW"*, that is
the actual measured wind at the spill location.

---

## Ship positions — the hard part

Ships broadcast AIS publicly, but somebody has to be *listening*. Free AIS
services rely on volunteers running receivers at home. Coverage follows
volunteers, not shipping.

We measured it with our own key. Vessels seen in about 14 seconds:

| Region | Vessels | Verdict |
|---|---|---|
| North Sea | **~740** | excellent |
| English Channel | ~48 | good |
| Gulf of Mexico | ~42 | good |
| Singapore / Malacca | ~5 | sparse |
| **Mumbai / Arabian Sea** | **0** | none |
| **Gulf of Kutch (Sikka)** | **0** | none |

### India has zero free live AIS coverage

Not a bug, not our code, not the API key. Nobody in India runs a volunteer
receiver that feeds the free stream. We verified the key works by pointing the
identical code at the North Sea and receiving 740 vessels.

**This is worth saying out loud in the viva.** It is a genuine finding about
data availability in the Indian Ocean, and it is exactly the kind of thing a
real feasibility study would report.

### The three AIS sources we support

**aisstream.io** — live, global where volunteers exist. Free key via GitHub
login. Gives full position tracks in real time.

**NOAA MarineCadastre** — historical, US waters only, no key whatsoever. Daily
archives back to 2009, ~300 MB each. The best free AIS in existence: complete
position tracks with full vessel identity. This is what we use to demonstrate
the full pipeline.

**Global Fishing Watch** — satellite-derived, global **including Indian
waters**. Free token after registration. Two real limits, measured not assumed:

- A standard token opens only the *fishing-effort* dataset. The events API and
  the all-vessels dataset both return **403**. Tankers and cargo ships are in
  the dataset we cannot read — you can email GFW and ask for access.
- It returns **gridded daily activity**, not continuous tracks. One position
  per vessel per cell per day. Enough to place a vessel in a region during a
  window; not enough to compute a course change.

It still gave us real Indian-waters vessels: MATSYA VRUSHTI (IMO 9298844),
ONE CONTRIBUTION and BROOKLYN BRIDGE — the last two being Japanese container
ships that appear because GFW classes them as "inconclusive" gear type.

---

## Coastline — Natural Earth

Public domain map data. We need it because **land is radar-dark too**. Without
masking, the coastline reads as a giant oil spill — our first real Mississippi
Delta run outlined 147 km² of Louisiana as oil.

**Known gap:** Natural Earth covers the *coast*, not lakes, reservoirs or
marsh. Inland water still reads dark. Prefer an open-water bounding box.

---

## How to check what is real, right now

Do not take anyone's word for it, including ours:

```bash
curl localhost:8000/api/provenance
```

It reports, layer by layer, whether that layer is real at this moment, and
gives a specific reason when it is not. The dashboard strip renders the same
data, so the UI cannot drift away from the truth.
