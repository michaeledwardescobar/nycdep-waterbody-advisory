# Legacy DEP advisory system artifacts

## What's in this folder

`arcgis_cso_advisory_layer_20260719.json` is a snapshot of the database table
that powered NYC DEP's *old* waterbody advisory dashboard — the version that ran
from around April 2019 until June 26, 2026, when DEP switched to a new system.
It was captured on July 19, 2026 from a public DEP map server:

https://services.arcgis.com/at3rDjch5X7i9Bag/ArcGIS/rest/services/cso_prd/FeatureServer/0/query?where=1%3D1&outFields=Waterbod_1,Prim_RainG,A_Curve,B_Curve,C_Curve,D_Curve,E_Curve,F_Curve,Rainfall_T,peak_inten,manual_ove,WBID,Advisory,Expires,Hours,Creation_1,Creator_1,EditDate_1,Editor_1&returnGeometry=false&f=json

`cso_params_2019_vs_2026.csv` compares the model numbers in this 2019-era table
against the numbers DEP's live system was using in July 2026, side by side for
all 45 waterbodies.

## How DEP's advisories work, in plain terms

DEP does not test the water before posting an advisory. Instead, it uses a
weather-based prediction: rain gauges at the 14 wastewater treatment plants
measure how much rain fell each hour, and a formula converts that rainfall
into an answer to two questions — *should this waterbody be under an advisory
right now, and for how long?*

The logic works like this:

1. **Group the rain into storms.** Rainy hours that happen close together count
   as one storm. A dry break of more than a few hours ends the storm.
2. **Check whether the storm was big enough.** Each waterbody has a trigger
   amount (its threshold). If the storm's total rainfall meets or exceeds it,
   an advisory begins.
3. **Calculate how long the advisory lasts.** A formula turns the storm's size
   into a number of hours. The clock starts at the last rainy hour; when the
   hours run out, the advisory ends.

Each waterbody gets its own trigger amount and its own formula numbers, because
a small dead-end creek fed by many sewer overflows recovers far more slowly
than a wide, well-flushed river.

## The formula this repository uses (Water Quality advisories)

The main dataset in this repository reconstructs **Water Quality (WQ)
advisories**, whose duration formula we have fully decoded and verified against
DEP's live system:

    advisory duration (hours) = a × (storm rainfall in inches) ^ b

- **a** — a scale number. Bigger *a* means longer advisories overall.
- **b** — a sensitivity number. It controls how much *extra* rain lengthens the
  advisory. If b is near 1, doubling the rain roughly doubles the advisory;
  if b is smaller, extra rain adds less and less additional time.
- The result is rounded **up** to the next whole hour.

Example: Upper Newtown Creek uses a = 37.04 and b = 0.62. A storm that drops
2 inches of rain produces 37.04 × 2^0.62 ≈ 57 hours of advisory — about two
and a half days — counted from the last hour it rained.

## What the columns in the JSON file mean

Each of the 45 entries describes one waterbody. In plain terms:

- **Waterbod_1** — the waterbody's name (e.g., "Newtown Creek, Upper").
- **WBID** — DEP's ID number for that waterbody (1 through 45).
- **Prim_RainG** — which treatment-plant rain gauge this waterbody listens to
  (e.g., Newtown Creek, Coney Island).
- **A_Curve … F_Curve** — six numbers that were plugged into the old system's
  formula for **Combined Sewer Overflow (CSO) advisories**. These are the
  model's tuning knobs, set individually per waterbody. *An honest caveat:*
  we know these six numbers are the formula's inputs, but the exact equation
  the old system combined them with has not been recovered — it likely
  predicted overflow volume and duration from storm size, but we cannot state
  the recipe, only the ingredients. Waterbodies showing all zeros (13 of them)
  had no CSO curve set at all.
- **Rainfall_T** — the trigger: how much rain (in inches) a storm needs before
  a CSO advisory starts. It was 0.1 inches for every waterbody.
- **peak_inten** — a second trigger based on how *hard* it rained in the worst
  single hour (inches per hour), filtering out long drizzles that never
  overwhelm the sewers.
- **manual_ove** — a manual override switch. Its presence proves DEP staff
  *could* force an advisory on or off by hand; it was set to 0 (off) for all
  45 waterbodies at capture.
- **Advisory** — whether an advisory was active (1) or not (0) the last time
  the old system updated this table.
- **Hours** — how many advisory hours remained at that last update.
- **Expires** — when that advisory was due to end. Stored as a computer
  timestamp (milliseconds since Jan 1, 1970). The strange value
  -62135596800000 is the system's way of writing "no advisory".
- **Creation_1 / Creator_1** — when each row was created and by whom: all 45
  on April 4, 2019, by a DEP staff account. This dates the 45-waterbody
  system to spring 2019.
- **EditDate_1 / Editor_1** — an authorship stamp from April 2019. Note it did
  *not* update when the live advisory columns changed (Expires shows June 2026
  while EditDate_1 still shows 2019), so it records when the rows were
  authored, not their last modification.

## Why this snapshot matters

- **It's a fossil.** The table's final entries record advisories from a
  June 25–26, 2026 rain event that were never cleared — the old system was
  switched off mid-advisory around June 26, 2026, and nothing has written to
  the table since. The July 18, 2026 storm left no trace here.
- **It preserves the 2019 calibration.** The curve numbers here differ from
  what DEP's live system served in July 2026 for 32 of 45 waterbodies (every
  waterbody with real, nonzero curves, plus Spring Creek, which gained curves).
  The formulas kept the same shape; the numbers were re-tuned — which is what
  you'd expect as DEP gathers better data and as sewer upgrades under the
  Long-Term Control Plans change how the system behaves.
- **It may be the only surviving copy.** The layer is orphaned and could be
  deleted by DEP at any time. This repository's monthly parameter snapshots
  now track the *current* numbers going forward; this file anchors the
  historical end.

Because the parameters have provably changed over time, the reconstructed
advisory history in this repository should be read as *the advisory record as
DEP's current model sees it* — one consistent yardstick applied to the full
rainfall record — rather than a replay of exactly what the dashboard displayed
on any given day in the past.
