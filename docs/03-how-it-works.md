# 3. How it works

Five stages. Each one is a folder in `backend/`.

```
satellite image → DETECT → DRIFT BACKWARDS → MATCH AIS → SCORE → ranked list
```

---

## Stage 1 — Detect the slick

**Code:** `backend/detect/darkspot.py`

A radar image of the sea is grainy — it looks like TV static. That graininess
is called **speckle** and it is physics, not a fault. A slick is a *large,
smooth, dark region* hiding inside that noise.

Five steps:

**1. Speckle filter (Lee filter).** Smooths the static while keeping edges
sharp. We need the slick's boundary intact, because its shape is evidence.

**2. Band-pass.** This is the trick that makes it work. We blur the image twice:
once lightly (about 1.5 km — the scale of a slick) and once heavily (about
10 km — the scale of general sea brightness). Subtracting one from the other
leaves only features that are *slick-sized*. Comparing raw pixels against a
wide average just measures the speckle.

**3. Robust threshold.** We need "how much darker than normal is suspicious?".
The obvious answer — the standard deviation — fails, because a big slick drags
the standard deviation up and raises the threshold above the thing we are
hunting. We use **median absolute deviation**, which ignores outliers, so the
slick cannot hide itself.

**4. Hysteresis.** One fixed threshold either shatters a ragged slick into
fragments (too strict) or floods the scene with noise (too loose). So we use
two: find high-confidence **cores**, then grow them outward through a looser
mask, keeping only the grown regions that contain a core.

**5. Look-alike rejection.** This is the hard part. Other things look dark on
radar: patches of low wind, algae, rain cells, ship wakes. We measure:

| Feature | Oil | Look-alike |
|---|---|---|
| Contrast (how dark, in dB) | 3–10 dB | 1–2 dB |
| Shape | ragged, elongated | round, smooth |
| Interior | evenly dark | patchy |

**Contrast gates the score.** Not "adds to" — *gates*. If contrast is weak the
score is near zero no matter how big or pretty the shape is. We learned this
the hard way: a 400 km² patch at 1.9 dB was scoring 0.67 on size alone. A patch
that large and that faint is a wind shadow, not oil.

**Land is masked first.** Land is radar-dark too. Before detecting anything we
paint out the coastline using free Natural Earth map data. Without it, the
first real run outlined 147 km² of the Louisiana shoreline as an oil spill.

---

## Stage 2 — Drift it backwards

**Code:** `backend/drift/backtrack.py` and `env_data.py`

We know where the oil is *now*. We want to know where it *started*.

A slick moves with:

```
slick velocity = ocean current + about 3% of the wind
                 (deflected ~15° right, because the Earth rotates)
```

Real wind and current come from Open-Meteo, free and without a key.

We do not calculate one backwards path. We scatter **600 imaginary particles**
across the slick and run each one backwards, and here is the important part:
**each particle gets its own slightly different wind factor and deflection
angle**, sampled from plausible ranges. Plus a small random wobble each step for
turbulence we cannot see.

Why? Because a single path would give one confident answer that is almost
certainly wrong. Six hundred slightly different paths fan out into a **cone of
uncertainty** — and the true source is somewhere in that cone. Being honest
about uncertainty is more useful than being precisely wrong.

The output is a probability over *place and time*: where could oil have been
released, and when, to end up where we saw it?

---

## Stage 3 & 4 — Match AIS and score

**Code:** `backend/attribute/ranking.py`

Every ship with AIS data in the window gets scored on five independent factors.

| Factor | Weight | The question it answers |
|---|---|---|
| `spatial` | 0.34 | Did it pass through the probable source region? |
| `temporal` | 0.22 | Was it there **at the right time**, or is that a coincidence? |
| `persistence` | 0.14 | Did it linger, or just pass straight through? |
| `behaviour` | 0.18 | Slowing down? Turning oddly? AIS switched off? |
| `vessel_type` | 0.12 | A tanker, or a fishing boat? |

### The `temporal` factor deserves attention

Without it, this is just "which ship was nearest", which is nearly worthless: a
ferry crossing the same water every single day would top the list every time.

So `temporal` is a **contrast**:

```
temporal = (best score using the CORRECT times)
           ÷ (best score using ANY time pairing)
```

A ship that happens to be in the right place, but would have scored just as
well at a completely unrelated time, gets close to **zero**. Only a ship that
was there *specifically* when the oil was released scores high.

### The `behaviour` factor

Three real signals:

- **Slow steaming** — compared with that vessel's *own* usual speed, not a
  global average. Discharging is usually done slowly.
- **Course deviation** — a sharp unexplained turn.
- **AIS gap** — the transmitter going quiet for a while. Legal reasons exist,
  but so do illegal ones.

**Stationary infrastructure is excluded.** An oil platform never moves, so it
shows a permanent "97% slowdown" and sits in every source region forever. On
our first real Gulf of Mexico run, MATTERHORN TLP — an actual tension-leg oil
platform — placed in the top 8 "suspect vessels". Now detected and zeroed.

### Every score is explained

A number alone is useless to an investigator. Each candidate carries plain
English reasons:

> - Closest approach 0.3 km from the back-drifted source region
> - Consistent position at 29 Dec 2024, 12:24 UTC (11.5 h before detection)
> - Inside the probable source region for ~1230 min
> - Slowed to 0.1 kn against a 3.0 kn norm (97% reduction)
> - AIS reporting gap of 50 min (normally every 3.5 min)

---

## The timing problem

**This is the single most important thing to understand about the project.**

A satellite pass is one instant. But attribution needs AIS from the **24 hours
before** that instant.

So you cannot detect a spill and *then* start collecting AIS. The data has to
already be there. The correct architecture is:

```
AIS streams continuously, 24/7, into the database
                ↓
A satellite passes over (every few days)
                ↓
Detect a slick → look BACK through AIS you already collected
```

This is exactly how the real CleanSeaNet works too.

It also explains the empty candidate panel you will often see: the satellite
scene is older than the AIS we hold. The dashboard says so explicitly rather
than leaving you guessing.

Two ways around it:

1. **Let the collector run** for a day, then fetch a scene.
2. **Pair archives** — take a historical satellite scene and the historical AIS
   for that same date. Both real, both aligned. This is what the demo command
   in [Run it](02-run-it.md) does.

## "Real-time" — say this carefully

Satellites are **not** real-time. Sentinel-1 revisits a given point every few
days, and the image takes hours to publish. AIS *is* real-time.

So the honest description is **"near-real-time, satellite-pass-driven"**. Saying
that shows you understand the domain. Claiming live monitoring invites a
question you cannot answer.
