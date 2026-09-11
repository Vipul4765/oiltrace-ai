# 6. Viva questions

Real questions an examiner will ask, with answers you can defend.

---

### "Does this already exist?"

Yes — the European Maritime Safety Agency runs **CleanSeaNet**, which does
exactly this for European waters.

We did not invent the concept. We built a working implementation entirely on
free, open data to show it can be done without a national agency's budget, and
we documented precisely where it falls short.

Saying this first is much stronger than being caught out by it.

---

### "Is this real-time?"

Half of it. **AIS is real-time** — ships broadcast every few seconds.
**Satellites are not** — Sentinel-1 revisits a point every few days and
publishes hours later.

So it is **near-real-time, satellite-pass-driven**. The architecture reflects
that: AIS streams continuously into the database so that when a satellite image
arrives, there is already history to look back through. That is how CleanSeaNet
works too.

---

### "How do you know that dark patch is oil and not something else?"

We often do not know for certain, and the system is built to say so.

Low wind, algae, rain cells and ship wakes all look dark on radar. We separate
them on measured features, mainly **contrast**: mineral oil damps the sea by
3–10 dB, look-alikes by 1–2 dB. Contrast *gates* the confidence score, so a
faint feature scores near zero regardless of its size or shape.

Every detection carries a confidence value, and it is never presented as
certainty.

---

### "Can you prove that ship did it?"

**No, and we do not claim to.** The output is a ranked, explained shortlist for
a human investigator, who has powers we do not — boarding, inspection, records.

We turn "three hundred ships were in the area" into "these three are worth
looking at, and here is why for each one".

---

### "Why is the vessel list empty?"

Because attribution needs AIS from the **24 hours before** the satellite pass,
and in this case we do not hold it. The panel says which specific reason
applies.

We deliberately leave it empty rather than filling it with a guess. An empty
box we can explain is worth more than a populated box we cannot defend.

---

### "Why demo the North Sea / Gulf of Mexico and not India?"

Because free live AIS has **zero coverage** over Indian waters — we measured
it: 0 vessels in Mumbai and the Gulf of Kutch, versus 740 in the North Sea with
the same code and the same key.

Real Sentinel-1 imagery over Mumbai works fine. It is only the vessel tracks
that are missing, and that is a data-availability fact about the Indian Ocean,
not a limitation of our system.

For real Indian vessel data we integrated Global Fishing Watch, which does
cover India — with its own documented limits.

---

### "What is the weakest part?"

Look-alike rejection, and we would say so unprompted.

Distinguishing oil from a low-wind patch is an unsolved problem in the
literature, not just for us. Our rules come from published SAR research, but
they are heuristics, not a trained classifier, and our confidence values are
not calibrated against labelled ground truth.

The upgrade path is concrete: train a U-Net on the public MKLab Sentinel-1
oil-spill dataset (~1,100 labelled images) and blend its probability with the
feature score. The rest of the pipeline does not change.

---

### "How accurate is the drift model?"

It is a first-order model and we treat it as one.

Slick velocity = current + ~3% of wind, deflected right for the Earth's
rotation — the standard empirical approximation in operational oil-drift work.
We sample wind and current at **one representative point** for the whole area,
which ignores spatial variation across ~200 km.

That is why we run 600 particles with varying parameters rather than one path:
the output is a **cone of uncertainty**, not a confident line. Being honest
about uncertainty is the point.

---

### "How do you know your own numbers are real?"

Call `GET /api/provenance`. It reports, layer by layer, whether that layer is
real right now, with a reason when it is not. The dashboard renders the same
endpoint, so the display cannot drift from the truth.

There is **no simulator in the codebase**. If a value cannot be measured, the
system shows a dash and explains the gap.

---

### "Show me it working."

```bash
curl -X POST "localhost:8000/api/regions/gulf-of-mexico"
curl -X POST "localhost:8000/api/ingest/noaa?day=2024-12-29&bbox=-89.0,28.2,-86.6,29.6"
curl -X POST "localhost:8000/api/ingest/sentinel1?start=2024-12-29&end=2024-12-30&bbox=-89.0,28.2,-86.6,29.6"
```

A real Sentinel-1 scene from 29 Dec 2024 23:54 UTC, against 43,344 real AIS
positions from 361 real vessels on the same day, filtered out of 6.9 million
rows. Real ranked ships with real MMSI numbers and evidence.

**Set this up before the viva. Do not run a 276 MB download live.**

---

### "What did you learn?"

The most honest answer, and the one that lands best:

> Testing on real data broke four things our simulation had hidden. Coastlines
> were detected as spills. Feature size was buying confidence that contrast had
> never earned. A real oil platform topped our suspect ranking, because
> something that never moves looks like it is loitering. And our scores
> saturated at 1.00 when the source region was large.
>
> Each of those only appeared because we insisted on real inputs. The synthetic
> slick we built to test with was small, clean and high-contrast — nothing like
> the real ocean.
