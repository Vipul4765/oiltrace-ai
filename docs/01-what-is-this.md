# 1. What is this?

## The problem

Ships produce oily waste water. Legally they must keep it on board or pass it
through a separator. Illegally — and often — they pump it into the sea at
night, far from shore, and keep sailing.

This is called **operational discharge**. It is not a dramatic tanker wreck. It
is routine, small-scale, constant, and it adds up to more oil in the ocean each
year than the famous disasters do.

Catching it is hard for a simple reason: **by the time anyone notices the oil,
the ship has gone.**

## The idea

Three facts make a solution possible.

**Fact 1 — radar satellites can see oil at night, through cloud.**
A radar satellite bounces radio waves off the sea. A rough sea scatters them
back and looks bright. Oil flattens the tiny ripples on the surface, so the
waves bounce away instead of returning, and the oil looks **dark**. This works
in darkness and through cloud, which ordinary cameras cannot do.

**Fact 2 — oil drifts in a predictable way.**
A slick moves with the ocean current, plus roughly 3% of the wind speed. Both
wind and current are measured and published for free. So if you know where the
oil is *now* and what the weather has been doing, you can calculate backwards
to where it probably started.

**Fact 3 — ships broadcast their position publicly.**
Every large ship carries **AIS** (Automatic Identification System), a radio
transmitter that announces its identity, position, speed and heading every few
seconds. Anyone with a receiver can listen. It exists for collision avoidance,
but it also means ships leave a public trail.

## Putting them together

```
1. Radar satellite photo of the sea
2. Find the dark patch  →  that is the slick, and we know when the photo was taken
3. Drift the slick BACKWARDS through real wind and current
       →  a region and time window where the oil probably entered the water
4. Look up the AIS trail  →  which ships were in that region during that window?
5. Score each one and rank them
6. Show a human investigator the shortlist, with the reason for each score
```

## What the system does NOT do

It does **not** decide who is guilty. It produces a **prioritised shortlist**
so that a human investigator, who has powers we do not have, knows which three
ships to look at instead of three hundred.

Say it that way. "Our system identifies the polluter" is a claim you cannot
defend. "Our system narrows hundreds of vessels down to a ranked, explained
shortlist for an investigator" is true and is exactly what the real operational
systems do.

## Does this exist already?

Yes — and knowing that is a strength, not a weakness.

The European Maritime Safety Agency runs **CleanSeaNet**, which does exactly
this for European waters. Our contribution is not inventing the concept; it is
building a working implementation on entirely free and open data, and being
rigorous about what it can and cannot prove.

If an examiner says "this already exists", the answer is: *"Yes, CleanSeaNet.
We rebuilt the pipeline from free public sources to show it can be done without
a national agency's budget, and we documented exactly where it falls short."*
