# OilTrace AI — team documentation

Read these in order. Each one is self-contained, so you can also jump straight
to whatever you are working on.

| # | Document | Read this if you want to… |
|---|---|---|
| 1 | [What is this?](01-what-is-this.md) | understand the idea in plain words |
| 2 | [Run it](02-run-it.md) | get it working on your machine |
| 3 | [How it works](03-how-it-works.md) | understand the science, step by step |
| 4 | [Code tour](04-code-tour.md) | know what every file does |
| 5 | [Where the data comes from](05-data-sources.md) | know what is real and what it costs |
| 6 | [Viva questions](06-viva-questions.md) | prepare for what examiners will ask |
| 7 | [When it breaks](07-troubleshooting.md) | fix a problem |

## The 30-second version

A ship illegally dumps oily water at sea and sails away. Nobody saw it.

A radar satellite passes overhead and photographs the ocean. The oil shows up
as a dark patch. But the patch has been drifting on wind and current for hours,
so it is no longer where it was released — and the ship is long gone.

**OilTrace AI works backwards.** It finds the dark patch, drifts it backwards
through real wind and current data to work out where the oil probably entered
the water and when, then looks up which ships were in that place at that time
using their public radio broadcasts. It ranks them by how suspicious their
behaviour was and hands an investigator a shortlist with reasons.

```
satellite photo → find the slick → drift it backwards → which ships were there?
                                → rank them → explain why
```

## One rule for this project

**No fake data. Ever.**

Not in the database, not on the dashboard, not "just as a placeholder". If a
number cannot be measured or computed from real data, the system shows a dash
and explains why it is missing.

This is not perfectionism. A plausible-looking fake number survives into a
report and then gets destroyed by one question in a viva. An honest gap can be
explained in one sentence.

Every screen carries a **data provenance** strip across the top showing, layer
by layer, whether it is real right now. It reads from the same API the rest of
the page uses, so it cannot quietly drift out of date.
