# Neuchatel — ABMT Assignment 2

Analysis code for the provided MATSim scenario and Swiss travel microcensus. This directory belongs **inside the existing `abmt-2026` repository**. No input datasets or generated results should be published.

## Folder structure

```text
Agentbased/
├── abmt-2026/                      # existing GitHub repository
│   ├── README.md                   # keep your existing file
│   ├── .gitignore                  # keep your existing file
│   └── assignment2_analysis/       # this folder
│       ├── abmt_paths.py
│       ├── ABMT_Neuchatel_Step2_Population.py
│       ├── ... Step3 through Step12 ...
│       ├── ABMT_Neuchatel_Step13_GEH_MethodAudit.py
│       ├── requirements.txt
│       ├── .gitignore
│       └── results/                # generated locally; ignored by Git
├── abmt-kurs/
├── eqasim-java/
└── neuchatel-data/                  # outside GitHub; never commit
    ├── Boundary/
    ├── microcensus/
    ├── Neuchatel_10pct/
    └── assignment2_analysis/       # optional old backup
```

The scripts automatically find `../../neuchatel-data` from their own directory (i.e. the `neuchatel-data` folder **next to** the `abmt-2026` repository, regardless of where `Agentbased` is located). The former structure `neuchatel-data/assignment2_analysis` also remains supported. Both `microcensus` and `Mikrozensus` are supported as names for the survey-data folder. No username or absolute Mac path is required. As an alternative, set the `ABMT_DATA_DIR` environment variable to your `neuchatel-data` folder.

## Install and run

Open Terminal in this directory (or navigate to it in VS Code). Use the Python installation that has pandas installed:

```bash
cd ~/Desktop/ETH/Agentbased/abmt-2026/assignment2_analysis
python -m pip install -r requirements.txt
python ABMT_Neuchatel_Step2_Population.py
python ABMT_Neuchatel_Step6_ModeShare.py
```

On another computer, replace the `cd` path with its local repository location. For example, a Windows checkout works with the same relative folder structure. `python` is the recommended command in the provided Conda environment. The scripts are silent on success and save figures/tables in `assignment2_analysis/results/` inside the **repository working directory**. These results remain local because of `.gitignore`. They do **not** write into the old backup under `neuchatel-data/assignment2_analysis`.

The scripts use the **already-provided simulation outputs**. No MATSim simulation must be run for Assignment 2.

## Scripts

| Script | Analysis | Output subfolder |
|---|---|---|
| Step 2 | Age, sex, household car ownership | `boundary_matched` |
| Step 3 | Spatial residents and urban/suburban/rural groups | `spatial_validation_corrected` |
| Step 4 | Activity chains for mobile persons, all and 65+ | `activity_chains` |
| Step 5 | Non-home activity types and spatial destinations | `spatial_activities` |
| Step 6 | Overall trip-based mode shares | `mode_share` |
| Step 7 | Mode shares by straight-line distance | `mode_share_distance` |
| Step 8 | Mode shares by age and household car ownership | `mode_share_groups` |
| Step 9 | Mode shares by departure time and residential grid | `mode_share_spatial_temporal` |
| Step 10 | Simulated PT boarding counts and road-count GEH | `pt_geh` |
| Step 11 | Recorded trips per person, including zero | `trips_per_person` |
| Step 12 | Home-only sensitivity analysis | `home_only_check` |
| Step 13 | Optional technical audit of GEH calculations | `pt_geh` |

Run from this directory using `python ABMT_Neuchatel_StepN_*.py` with the correct full file name. Keep all the step scripts in the same directory because some import functions from earlier steps. Step 13 should be executed only after Step 10.

## Methodological qualifications

- Microcensus: Monday–Friday respondents, `WP` sampling weights, residence inside the Neuchatel boundary (LV03 coordinates transformed to LV95). The minimum age of six is an **analysis choice**, not an explicitly stated assignment limit.
- MATSim: provided 10% sample, residence inside the same boundary; artificial/external activities and modes are excluded as applicable. Steps 2 and 3 currently have **slightly different** MATSim external-population filters. Harmonize these before finalizing comparisons; these scripts otherwise preserve the earlier validated output definitions.
- Steps 2–3 count people, including those with no recorded trips. Steps 6–9 use **trips** as denominator. Step 11 includes zero recorded standard trips; this is not necessarily proof of staying home.
- Step 4 is the mobile activity-chain analysis. Step 12 explores home-only sensitivity because no recorded survey trip does not prove a true home-only day.
- Step 10's public-transport boarding figures are simulated demand, not validation against independently observed passenger counts. GEH uses **calibration** reference counts, with reference units assumed as vehicles/hour/lane and simulated mean-hourly volumes derived from daily counts. Step 13 audits arithmetic consistency; it is not an independent data validation.

## Repository and privacy

Regardless of whether the repository is public or private, keep `Boundary/`, `microcensus/`, `Neuchatel_10pct/`, any confidential outputs, and any individual-level or station-level records **outside the Git repository**. Only copy this code directory into the checkout.

The `.gitignore` in this directory protects `results/`, CSV, XML, archives, geodata, plots and caches. It does not retroactively remove files that may already have been tracked. Check the staged files before committing:

```bash
cd ~/Desktop/ETH/Agentbased/abmt-2026
git add assignment2_analysis
git diff --cached --name-only
```

The staged list should contain only `.py`, `.md`, `requirements.txt` and `.gitignore` under `assignment2_analysis/`. If you see original data files or generated results, **stop before committing** and unstage them. Do not run `git add -f` on ignored files. Do not commit results even if they appear anonymized without checking the applicable course/data-sharing policy.

The repository root contains a separate overview `README.md`; this file documents the analysis details. The root `.gitignore` is managed separately.

## Sources

- ETH Zürich: *Agent Based Modelling in Transportation*, Assignment 2 (2026).
- [eqasim-switzerland](https://github.com/eqasim-org/eqasim-switzerland/tree/cmdp_new_version), survey and traffic-count processing.
- [eqasim-java](https://github.com/eqasim-org/eqasim-java/tree/cmdp), simulation/output reference.
