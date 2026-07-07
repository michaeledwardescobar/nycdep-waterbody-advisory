# NYC DEP Waterbody Advisory Logger

DEP's [Waterbody Advisory System](https://nycwaterbodyadvisory.azurewebsites.net/)
shows *current* advisories only — there's no public history. This repo fixes
that going forward: a scheduled job polls the dashboard's API every 20 minutes
and commits the results here, building a permanent time series of advisory
status **and** the 24-hour rainfall at each treatment-plant rain gauge (the
exact input DEP's model uses).

This is the "git scraping" pattern: the repo itself is the database, and the
commit history is your audit trail. No servers, no cost.

## What you get

- `data/raw/` — every raw JSON response, timestamped (insurance against
  parser bugs; the whole log can always be rebuilt from these)
- `data/advisory_log_YYYY-MM.csv` — one tidy row per waterbody per poll:
  poll time, advisory on/off, projected end, hours remaining, rain gauge ID,
  24-h accumulated rainfall
- `analysis/analyze.py` — collapses polls into advisory *episodes* and
  estimates (a) the apparent rainfall trigger threshold and (b) the
  duration-vs-rainfall relationship for any waterbody

## Setup (one time, ~10 minutes, no coding)

1. **Create a GitHub account** if you don't have one (github.com — free).
2. **Create a new repository.** Click the "+" (top right) → *New repository*.
   Name it anything (e.g. `dep-advisory-logger`). Public or private both work.
3. **Upload these files.** On the new repo page, click *uploading an existing
   file*, then drag the entire contents of this folder in (including the
   hidden `.github` folder — if your OS hides it, use "Add file → Upload
   files" and drag the folder itself). Commit.
   - If the `.github` folder won't upload via drag-and-drop, create the
     workflow manually: *Add file → Create new file*, type
     `.github/workflows/scrape.yml` as the name, and paste in the contents.
4. **Enable the workflow.** Go to the *Actions* tab. If GitHub asks you to
   enable workflows, click the green button.
5. **Kick off the first run.** Actions tab → "Poll DEP Waterbody Advisories"
   → *Run workflow* → green *Run workflow* button.

That first run will attempt to auto-discover the API endpoint by reading the
dashboard's JavaScript. Check the run's log output:

- **If it found endpoints** — you're done. It will poll every 20 minutes
  forever. Watch `data/` fill up.
- **If discovery failed** — find the endpoint manually (2 minutes):
  1. Open https://nycwaterbodyadvisory.azurewebsites.net/ in Chrome.
  2. Press **F12** → **Network** tab → click the **Fetch/XHR** filter.
  3. Reload the page. You'll see a short list of requests appear.
  4. Click each one and look at the *Response* tab until you find the one
     containing waterbody names and advisory data (JSON).
  5. Right-click it → *Copy* → *Copy URL*.
  6. In your GitHub repo, create the file `scraper/endpoints.json` containing:
     ```json
     {"endpoints": ["PASTE_THE_URL_HERE"]}
     ```
  7. Re-run the workflow.

## Running the analysis

After the logger has captured a few storms (give it a month or two of wet
weather), download or `git clone` the repo to your computer, then:

```
pip install pandas numpy
python analysis/analyze.py                  # Newtown Creek (default)
python analysis/analyze.py "FLUSHING CREEK" # any other waterbody
```

It prints episode counts, the estimated trigger threshold bracket, and a
duration-vs-rainfall fit, and writes an episode table CSV you can take into
Excel, R, or anything else.

The pipeline was validated against synthetic data with a known model
(trigger at 0.40 in, duration = 10 + 30×peak): it recovered the threshold to
within 0.02 in and the duration relationship exactly.

## Things to know

- **GitHub disables schedules on inactive repos.** If no human touches the
  repo for ~60 days, GitHub pauses scheduled workflows and emails you first.
  Clicking "keep workflow enabled" (or making any commit) fixes it. Set a
  calendar reminder to peek at the Actions tab monthly.
- **Polling gaps are handled.** The analysis splits episodes across gaps
  longer than 3 hours rather than papering over missing data.
- **Be a good citizen.** One request every 20 minutes to a public dashboard
  is lighter than a single human visitor. Don't crank the cron below ~10 min;
  there's no analytical benefit and no reason to burden a public service.
- **Historical data (pre-logger).** For advisories before you started
  logging, the best proxies are (1) NYSDEC's Sewage Discharge Reports
  spreadsheet (SPRTK/NY-Alert records back to 2013, on DEC's Sewage Discharge
  Notifications page) and (2) the NotifyNYC archive on NYC Open Data. Both
  record discharge/advisory *issuance* events rather than advisory windows,
  so they complement rather than replace this log.
- **Rainfall for historical analysis.** NOAA hourly precipitation at
  LaGuardia is the closest public long-record gauge to the Newtown Creek
  sewershed.
