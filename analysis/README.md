# Legacy DEP advisory system artifacts

## arcgis_cso_advisory_layer_20260719.json

Full attribute capture (45 waterbodies, no geometry) of NYC DEP's ArcGIS feature layer
`Waterbody_CSO_Advisory`, retrieved 2026-07-19 from:

https://services.arcgis.com/at3rDjch5X7i9Bag/ArcGIS/rest/services/cso_prd/FeatureServer/0/query?where=1%3D1&outFields=Waterbod_1,Prim_RainG,A_Curve,B_Curve,C_Curve,D_Curve,E_Curve,F_Curve,Rainfall_T,peak_inten,manual_ove,WBID,Advisory,Expires,Hours,Creation_1,Creator_1,EditDate_1,Editor_1&returnGeometry=false&f=json

This layer was the operational data store of DEP's pre-2026 advisory dashboard
(the AngularJS app served at nycwaterbodyadvisory.azurewebsites.net from at least
June 2019, per Wayback Machine captures; discovered via the archived `/api/profile`
endpoint, which pointed the dashboard's map at this layer).

Why it is preserved here:

- Rows were authored 2019-04-04/05 (`Creation_1`/`EditDate_1`), establishing that
  the 45-waterbody system dates to April 2019.
- The layer stopped receiving updates on 2026-06-26 (layer metadata "Data Last
  Edit"), mid-advisory: rows for the June 25-26 rain event still show Advisory=1
  with Expires timestamps of June 26-27, 2026, never cleared, and the July 18,
  2026 storm never touched it. DEP evidently cut over to a new pipeline around
  June 26, 2026.
- The six CSO curve coefficients (A_Curve..F_Curve) preserved here are the
  2019-era calibration. They differ from the coefficients served by DEP's live
  `/api/waterbodies` endpoint as of July 2026 for all 31 waterbodies with nonzero
  curves (same functional form and signs, recalibrated values); Spring Creek went
  from all-zero to nonzero. See `analysis/cso_params_2019_vs_2026.csv`.
- `manual_ove` (manual override flag) exists in the schema; it was 0 for all 45
  waterbodies at capture time.

Timestamp fields are Unix epoch milliseconds. `Expires` of -62135596800000
(year 0001) means "no advisory". Note that `EditDate_1` did not track live
advisory-state edits (it reads April 2019 while `Expires` was rewritten through
June 2026), so it reflects attribute authorship, not last modification.

This capture is the only known surviving copy of the 2019 parameter set; the
orphaned layer could be deleted by DEP at any time.
