# NYC Waterbody Advisory History

An open, continuously updated record of New York City's waterbody advisories, which are the official warnings that tell the public when it's unsafe to swim, kayak, or otherwise touch the water in the city's creeks, rivers, and bays.

## Why this exists

Under the Sewage Pollution Right to Know Act, NYC DEP must tell the public when rainfall pushes sewage into local waterways. In practice, that notice takes two forms: automated alert emails, and a [dashboard](https://nycwaterbodyadvisory.azurewebsites.net/) that shows the current advisories. However, there is no public history, meaning there's no easy way to understand how often a creek is under advisory, how long advisories last, whether things are getting better or worse, or how one waterbody compares to another.

Those questions are answerable. DEP's advisory system is a model: rain falls on a gauge, the gauge reading crosses a waterbody-specific threshold, and a formula computes how long the advisory lasts. This repository recovers all the ingredients of that model from DEP's own public data feeds and uses them to reconstruct the advisory history. This also keeps the record growing automatically.

Everything here comes from DEP's publicly served data.

## What's in the repo

**Data (updated automatically):**
- `data/rain_history/` — hourly rainfall at all 14 DEP treatment-plant rain gauges, January 2018 to present. These are the exact gauges the advisory model uses.
- `data/waterbody_log_*.csv` — snapshot every ~20 minutes of every waterbody: its rainfall reading, advisory threshold, and model coefficients.
- `data/advisory_log_*.csv` — DEP's own computed advisories (waterbody, type, remaining duration) captured every ~20 minutes.
- `data/model_config/` — the advisory model's parameters (thresholds, coefficients, storm-gap definitions), with a dated snapshot kept whenever DEP changes them.

**Analysis:**
- `analysis/reconstruct_all.py` — reconstructs the advisory history for all 45 waterbodies, with automated quality control.
- `analysis/waterbody_summary.csv` — one line per waterbody: advisories per year, hours per year, percent of time under advisory.
- `analysis/all_waterbody_episodes.csv` — every reconstructed advisory episode since 2018 (13,000+), with start, end, and duration.

**Machinery:**
- `scraper/scrape.py` + the *Poll* workflow — logs rainfall and live advisories every ~20 minutes via GitHub Actions.
- `scraper/harvest.py` + the *Harvest* workflow — downloads/tops up the full hourly rainfall archive.

## How the advisory model works

For each waterbody, DEP's published parameters define three things:

1. **What counts as a storm.** Rainy hours separated by fewer than 6 dry hours are one storm event.
2. **What triggers an advisory.** Each waterbody has its own rainfall threshold, ranging from a hair-trigger 0.05 inches (Coney Island Creek, Alley Creek) to a full 2 inches (Western Long Island Sound).
3. **How long it lasts.** Advisory duration follows *a × depth^b* hours after the rain stops, with coefficients specific to each waterbody. Upper Newtown Creek, for example: a 1-inch storm produces roughly 49 hours of advisory.

This reconstruction applies DEP's own rules to DEP's own rainfall record. It was validated against the live system: for the July 2026 storm, the reconstructed advisory end time for Newtown Creek matched DEP's published advisory to the hour. The 20-minute logger re-performs this check on every storm going forward, so any drift between this model and DEP's live output will be visible in the data.

## Headline findings (2018–present)

Upper Newtown Creek is under a water quality advisory about 29% of the time with roughly 40 episodes a year and a median length of 54 hours. Citywide, the most burdened waterbody is Coney Island Creek (~39% of the time under advisory); the least is Western Long Island Sound (~0.6%). The full ranking is in `analysis/waterbody_summary.csv`.

## Data quality

Rain gauges fail, and a dead gauge reads the same as a dry sky. Every gauge here is cross-checked against the other 13: readings of zero during periods when neighboring gauges recorded substantial rain are flagged as gauge outages and **excluded** from the reconstruction rather than counted as dry weather. The largest confirmed outages: North River (~600 days), Newtown Creek (~430 days, including Hurricane Ida), Coney Island (~345 days). Physically incorrect single-hour spikes (up to 13 inches in an hour, with dry neighbors) are removed. All statistics are normalized to each gauge's reliable hours.

## Caveats

This reconstructs what DEP's model *would have declared* and is not an independent measurement of water quality. It covers water quality (WQ) advisories; CSO advisories use a different formula not yet decoded (the ongoing logs are accumulating the data to do so). The duration formula was validated directly for Newtown Creek and applied uniformly to all waterbodies using each one's published parameters.

## SPRTK Discharge Reports — Cross-Validation & Data Quality Notes

NYSDEC SPRTK sewage discharge reports (`SPRTK_Data.xlsx`), cleaned via `sprtk/clean_sprtk.py`. Coverage: Jan 2021 – Nov 2025. Despite DEC's "through current" labeling there are no 2026 records as of the July 2026 download and the "December 2025" tab contains November data. 

### Cross-validation vs. reconstructed advisory model (2021–2025) 
- 98% of DEP's 523 citywide wet-weather CSO filings coincide with modeled advisory activity, 80% match a new advisory onset within ±24h, and most of the rest were filed while advisories were already ongoing (multi-day storms generate daily update filings).
- Citywide advisories do not seem to be triggered by localized rainfall. Modeled advisories touching 10+ waterbodies have a matching CSO filing 90% of the time; 1–2  waterbody clusters only 61%.
- Only 12 filings in five years had zero modeled advisories active. One is a Nov 2023 force-main break during dry-weathe, and the remaining 11 "rain condition" filings (2023–2025) may be due to rain gauge failures or changes to rainfall thresholds. These are not yet reconciled against gauge QC flags.

### Known anomalies & QC caveats
- **Timestamp precision**: most filing timestamps are date-only  (midnight). Matching resolution is ±24h; hour-level agreement cannot be  claimed from this dataset.
- **2023 filing spike**: 142 citywide CSO filings vs. ~90 in other years,  against only 80 model onsets. Likely a DEP filing-practice change (more per-storm updates), not a model discrepancy.
  - **Source data is not QC'd by DEC**: durations are free text (~95%  parseable), volumes mix total gallons and gallons/minute, and the  reason field contains duplication artifacts (e.g. "Pipe Break,Pipe  Break").
  - **Heavy duplication in the raw file**: 50,669 raw rows → 24,851 unique  records after removing blank padding (~12k rows in the Nov/Dec 2021  cumulative tabs) and cross-tab repeats.
  - **Dry weather discharges**: NYC facility-level discharge events (blockages, pipe breaks, power outages) are invisible to the rainfall-driven DEP advisory dashboard.
 
## Not affiliated with DEP or DEC

This is an independent, personal project built entirely from DEP and DEC public data feeds. It exists because the public record it assembles should be easy to see. Corrections and issues welcome.
