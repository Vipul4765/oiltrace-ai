# 7. When it breaks

Most of these are not bugs. Read the first section before assuming something is
broken.

---

## "Top Candidate Vessels is empty"

**Almost always correct behaviour.** The panel tells you which of these applies:

| Reason | Fix |
|---|---|
| Region has no AIS coverage | Switch region, or load GFW data |
| Nothing collected yet | Let the collector run |
| History doesn't reach back to the scene | Pair with an archived AIS day |
| Gap over the needed window | Same |
| AIS present, no vessel near the source | Nothing to fix — that is the answer |

Remember: attribution needs AIS from the **24 hours before** the satellite
pass. See [How it works](03-how-it-works.md#the-timing-problem).

---

## "No detections found"

Also usually correct. A clean sea *should* produce zero detections.

To see the detector's reasoning:

```bash
OILTRACE_DETECT_DEBUG=1 .venv/bin/uvicorn backend.main:app --port 8000
```

It then prints why each candidate region was kept or rejected — area, contrast,
shape, and which threshold it failed.

---

## "0 vessels on the map"

Check which region you are on. Mumbai and Gulf of Kutch have **zero** free live
AIS coverage — this is a fact about data availability, not a fault.

```bash
curl localhost:8000/api/provenance    # tells you exactly what is real
curl localhost:8000/api/ais/status    # collector connection and counts
```

Switch to North Sea to see thousands of real vessels.

---

## "Sentinel-1 fetch failed"

| Error | Cause |
|---|---|
| `No module named 'rasterio'` | Dependencies not installed — run `./run.sh` |
| `libexpat.so.1: cannot open shared object` | In Docker, the image needs `libexpat1` |
| `CRS is invalid: None` | Sentinel-1 GRD is in radar geometry; needs `WarpedVRT` |
| `no usable scene` | No satellite pass over this area in the time window — widen `days` |

---

## "Open-Meteo returned no wind data"

The drift model **raises** rather than substituting an average. That is
deliberate: a confident drift path computed from invented weather is worse than
an honest failure.

Usually a transient network problem. Retry. Cached responses are reused when
available.

---

## "Port 8000 already in use"

```bash
ss -ltnp 'sport = :8000'      # find what is holding it
./run.sh 8080                 # or just use another port
```

---

## "Everything looks wrong / I broke something"

Run the four checks. They will tell you what, specifically:

```bash
.venv/bin/python tests/test_suite.py   # 56 checks
.venv/bin/python tools/audit.py        # data integrity
.venv/bin/python tools/api_scan.py     # API surface
.venv/bin/python tools/ui_test.py      # browser + JS errors
```

To start completely fresh (this deletes all collected data):

```bash
rm -f data/oiltrace.db*
```

Incidents are re-derivable from real satellite scenes. AIS history is not —
it has to re-accumulate.

---

## Deployed site behaves differently from local

Expected, for two reasons:

1. **Free hosting sleeps.** Render's free tier spins down after ~15 minutes
   idle, and the AIS collector stops with it. A woken instance has satellite
   imagery but no vessel history.
2. **Free hosting has no persistent disk on Render**, so the database resets on
   every redeploy.

Use the deployed site to prove it ships. **Demo locally.**

---

## Security: never interpolate AIS text into HTML unescaped

Vessel names, call signs and destinations come from **AIS, an open radio
broadcast**. Anyone with a transmitter can put any string in those fields, and
AIS spoofing happens in the wild.

A vessel named `<img src=x onerror=...>` executed its payload in this dashboard
before we fixed it. That is stored XSS, and the attacker is anyone with a VHF
transmitter.

**The rule:** anything that is not a number you computed goes through `esc()`
before it touches `innerHTML`.

```js
el.innerHTML = `<b>${esc(v.name)}</b>`;   // correct
el.innerHTML = `<b>${v.name}</b>`;        // vulnerable
```

Check it after any frontend change:

```bash
.venv/bin/python tools/xss_test.py     # exits non-zero if a renderer is unsafe
```

## Rules for changing code

1. **Never add fake data.** Not as a placeholder, not "temporarily". If a value
   cannot be computed, return `null` and let the UI show a dash.
2. **Run the four checks before pushing.** Each exists because it caught a real
   bug that reading the code had missed.
3. **Put settings in `config.py`**, not scattered through the codebase.
4. **When you fix something subtle, write down why** in a comment. Several
   fixes here look wrong until you know what failed before them.
5. **Never commit `.env`.** It holds the API keys.
6. **Escape untrusted text.** See the security section above. Run
   `tools/xss_test.py` after any change to rendering code.
