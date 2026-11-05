# Neuchatel — reconciled Assignment 2 code

Updated 5 November 2026, revision 1.1. This package implements the decisions agreed for Nelli's analysis. It combines reviewed population/trip readers with Nelli's analysis layout and step names. It contains code and artificial regression-test generators only; no original survey records, population records, events or generated study results are included.

## Install and run

1. Preserve your old analysis folder and results, then copy this complete `assignment2_analysis` directory into your existing `abmt-2026` project. All files beginning with `_abmt_` are required helper modules and must remain beside the step scripts. Do not copy just one updated Step file.
2. Keep the provided data outside the Git repository, for example:

```text
Agentbased/
  abmt-2026/
    assignment2_analysis/
  neuchatel-data/
    Boundary/
    microcensus/                 (Mikrozensus is also supported)
    Neuchatel_10pct/
      neuchatel_population.xml.gz
      simulation_output/
      network_calibration_files/
```

3. Open a terminal in `assignment2_analysis` and run:

```bash
python -m pip install -r requirements.txt
python run_all.py --base ../../neuchatel-data
```

Use the actual data-directory path if yours differs. `neuchatel_data` with an underscore also works when explicitly passed. Automatic directory detection supports both spellings next to the repository. Alternatively set `ABMT_DATA_DIR` to the absolute data-directory path. The scripts use the provided simulation outputs; they never start MATSim.

A complete `run_all.py` execution preserves an existing `results` directory as `results_previous_TIMESTAMP` and writes a fresh output set. This prevents old 20-km maps or earlier definitions from being mixed with new figures. A partial execution updates only the selected steps:

```bash
python run_all.py --base ../../neuchatel-data --steps 6 7 8 9
python run_all.py --base ../../neuchatel-data --steps 10 13
```

For a single script, use the same Python environment and set `ABMT_DATA_DIR` if automatic detection does not find your data. Step13 must follow a successful updated Step10.

## Revision 1.1: duplicate input-file lookup

Input files are now resolved from their designated folders first: `Boundary/neuchatel.shp`, `microcensus/` or `Mikrozensus/`, `Neuchatel_10pct/neuchatel_population.xml.gz`, and `Neuchatel_10pct/simulation_output/`. A copy elsewhere under the data root no longer makes these direct paths ambiguous. All Step-level lookup functions use the same resolver. Truly ambiguous alternatives within the selected scope still cause a descriptive error; no arbitrary first match is selected. No input files need to be deleted for this fix.

Replace the updated code files in your analysis folder and rerun the same `run_all.py` command. Dependency installation is not needed again for this path-resolution correction.

## Agreed definitions

- Resident cohort: actual home inside `Boundary/neuchatel.shp`, integer ages 6-120, freight excluded. `isOutside`, `isCrossBorder` and `isExternalFR` alone do not remove a resident. No home is inferred from a cut-out border activity. Other filters remain explicit in the method outputs.
- Microcensus: Monday-Friday, positive finite person weights `WP`. Each retained trip carries the respondent's `WP`.
- Age groups: **6-17, 18-34, 35-64, 65+**.
- Raster: **10 km**, one globally aligned LV95 lattice for all maps. Cells are clipped to the study boundary. The former automatic 20-km fallback has been removed. A displayed difference requires **30 distinct persons in each data source**. Repeated visits or trips from one person do not increase the contributor count. This is a support rule, not a significance test.
- Internal trips: both substantive endpoints inside; artificial outside/border activities and modes interrupt the sequence. They are never deleted to join unrelated inside activities. Connected transfer rows are collapsed; mixed-mode blocks without a justified main mode stay unknown.
- Primary modal split in Steps6-9: the same five eqasim groups `car`, `car_passenger`, `pt`, `bike`, `walk`, with positive finite Euclidean endpoint distance >1 m. Known loop trips, `remote_walk`, other/unknown modes and coincident endpoints are reported separately, outside this conditional five-mode denominator. Excluded weights and counts remain visible in the coverage files.
- Reference mode grouping follows `eqasim-switzerland`, branch `cmdp_new_version`, `data/microcensus/trips.py`: plane/taxi fall into the model's PT group, motorbike/mofa into car, vehicle-like devices into bike. These are model groups, not literal vehicle definitions.
- Passenger definition: any car-passenger stage yields `car_passenger` in the eqasim main definition. A separately saved **strict** sensitivity preserves the stated survey main mode and resolves driver/passenger only for a car main trip. The two definitions are not silently mixed.
- Distance: straight-line LV95 distance between substantive endpoints for both data sources; this is not network distance. Time-specific missingness is reported separately and does not alter the overall or home-grid base population of trips.
- Household car ownership and personal driver access are separate variables. Main personal access includes “always” or “after agreement”; an “always only” sensitivity is also produced. Ownership summaries weighted by persons describe residents' household circumstances, not a household-weighted distribution.
- Activity chains: the main denominator is all eligible residents. Confirmed home-only `H`, home-to-home travel `H-H`, outside, unknown and other classified chains remain distinct. The initial survey activity is verified. Business/work are pooled as work for the agreed display. Nelli's conditional closed-mobile-chain graphs are saved additionally, with the conditional denominator in their label.
- Trips per resident: selected synthetic **input plans** vs survey diaries; this belongs to population/scenario validation. Identified physical modes and loops are counted independently of the primary five-mode modal-split subset. Incomplete counts are unknown, never filled with zero. Plans containing `remote_walk` are conservatively marked unknown for physical trip-frequency inference: removing the outward virtual leg cannot establish whether a colocated return leg is physical. This is exposed in the trip audit. Zero internal trips is not proof of a home-only day.
- Destination maps: arrivals at real non-home activities inside the boundary, excluding the initial activity and artificial outside/transfer activities. Origins may be outside. Repeated arrivals count repeatedly. Visits per 100 residents and coverage are saved separately from destination shares.
- Road counts: all road users at the supplied target links with inside-boundary midpoints; no residence filter. Average-hourly whole-link GEH is primary. Supplied per-lane target rates are multiplied by the link's comparison lane count. Sample-equivalent event counts are expanded by the configured population sample fraction and divided by the counting-window hours. Buses use sample-equivalent weighting because the full timetable is simulated. The supplied daily-count file is checked against the event-derived count, not substituted silently.
- Per-lane GEH is supplementary; GEH is scale-dependent. Both variants have explicit units. Original reference dates/day types and independent holdout provenance are not established by the supplied files alone. GEH<5 fractions are descriptive; average-hourly rates do not validate individual hourly profiles.
- PT: line/time boardings and alightings at inside stops are descriptive simulated activity. Arrival/departure delay diagnostics use inside-stop events and exclude identified transit drivers from passenger event counts. Neither replaces a comparison with observed passenger demand or punctuality.

## Steps and principal outputs

| Step | Analysis | Subfolder of results |
|---|---|---|
| 2 | Age, sex, residents' household car ownership; joint ownership/personal-access tables and sensitivities | `boundary_matched`, `car_access` |
| 3 | Residential 10-km maps, older residents, matched municipality types | `spatial_validation_corrected` |
| 4 | All-resident named/structural activity chains and additional conditional mobile chains | `activity_chains` |
| 5 | Real non-home arrivals, purpose shares, 10-km destination maps | `spatial_activities` |
| 6 | Five-mode overall comparison, strict passenger sensitivity, selection coverage and audits | `mode_share` |
| 7 | Mode shares by straight-line distance | `mode_share_distance` |
| 8 | Mode shares by age, household ownership, personal access and access sensitivity | `mode_share_groups` |
| 9 | Mode shares by departure time and residential 10-km cell | `mode_share_spatial_temporal` |
| 10 | Inside-stop PT activity/delays, event-derived whole-link GEH and per-lane sensitivity | `pt_geh` |
| 11 | Internal physical trips per resident from input plans vs survey, including unknown | `trips_per_person` |
| 12 | Confirmed home-only vs home-home/outside/unknown/other activity-chain coverage | `home_only_check` |
| 13 | Saved GEH arithmetic, link-set and scale checks | `pt_geh` |

Read `reconciled_method.json` in the output subfolders. The canonical active definitions are also listed in `abmt_common.METHOD`; old unmodified result files should not be reused. Some filenames/table schemas, especially Steps4,11,12 and GEH, have changed intentionally. This is a new reproducible output set rather than a way to preserve old numerical results.

The input files must include the fields used by the documented scenario: actual household/person home coordinates and weights; travel/nontravel responses; ordered Weg identifiers, origin/destination coordinates, purposes, `pseudo` and `wmittel`; stage roles and the origin-purpose fallback; and simulation trip identifiers, coordinates, activity types, main mode and departure time. Missing or ambiguous mandatory schema information stops the relevant analysis rather than creating guessed values.

Step3's municipality-type charts use the matched-municipality subset and report its coverage. The 30-person rule can leave small groups unplotted; this is not evidence of zero share.

## Local verification

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

Tests generate entirely artificial local data. They cover border barriers, resident flags, missing and zero diaries, home-only and loops, virtual homeoffice, transfer reconstruction, passenger definitions, weighted denominators, shared trip selection, map alignment/contributor support, event counts and GEH unit conversion.

The complete Steps2-13 workflow was also run against an artificial population/network/timetable/event fixture. It has **not** been run against your private original data in this environment. The integration check establishes code/schema interaction for the fixture; it does not establish the correctness of the real study's numerical results. See `VALIDATION.txt` for the recorded checks.

## Provenance

The named step scripts and retained plotting/config helpers originate from Nelli's `nellimalaika/abmt-2026`, inspected commit `ad3d05911c3ea1141fb034abb9b7db19836886fc`. Internal `_abmt_*` modules adapt the reviewed Hatim `validate_*` snapshots. They are combined explicitly in this reconciled version; no claim of an independent second implementation is made. These source snapshots need not contain every uncommitted change in either local working copy. No remote repository has been modified by delivering this package.

The original generation pipeline sometimes reconstructs detailed loop trips from stages; this package does not claim exact row-by-row equivalence to every generation transformation. The scope and loop exclusions of the primary analysis are therefore explicit, and coverage audits must be inspected before interpreting differences.

Keep private inputs and generated study outputs out of Git. The supplied `.gitignore` protects these file types; inspect staged files before committing code changes.
