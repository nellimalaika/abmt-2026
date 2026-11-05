# Neuchâtel transport baseline analysis

Python analyses of the Neuchâtel 10% MATSim scenario, comparing the synthetic population and existing simulation outputs with the Swiss travel microcensus and supplied traffic-count references.

The analysis was reconciled by **Nelli Reiff and Hatim Abdel Ghaffar**. Code and artificial test generators are included; restricted input data and generated study results remain local.

See [the analysis README](assignment2_analysis/README.md) for the full methods, entry points and input requirements.

## What is included

- Resident demographics, household car ownership and personal driver access.
- Residential distribution, activity chains, destinations, trips per resident and home-only checks.
- Mode shares by distance, demographic group, departure time and residential location.
- Simulated PT activity and timetable diagnostics; whole-link traffic GEH with a per-lane sensitivity.
- **Step14:** PT subscription groups and a separate three-item sensitivity.
- **Step15:** residential PT classes under one checksum-pinned official ARE 2015 polygon layer, including supplied/common comparisons and 10 km maps.

The code uses existing simulation outputs. It does not start a MATSim simulation.

## Local folder structure

Keep restricted inputs outside the Git repository:

```text
Agentbased/
  abmt-2026/
    README.md
    assignment2_analysis/
      README.md
      requirements.txt
      ABMT_Neuchatel_Step2_Population.py
      ... Step3 through Step15 ...
      run_all.py
      refresh_plot_style.py
      collect_review.py
      results/                  # generated locally; ignored by Git
      reference_data/           # public reference cache; ignored by Git
  neuchatel-data/                # restricted local inputs; never commit
    Boundary/
    microcensus/                # Mikrozensus/ also supported
    Neuchatel_10pct/
      neuchatel_population.xml.gz
      simulation_output/
      network_calibration_files/
```

The data resolver also supports a sibling folder named `neuchatel_data`. Use `--base` or `ABMT_DATA_DIR` for a different input location.

## Install and run all analyses

From the repository root:

```bash
cd assignment2_analysis
python -m pip install -r requirements.txt
python run_all.py
```

The default run executes **Steps2–15**. It preserves an existing `results/` directory as `results_previous_TIMESTAMP/` before generating a fresh output set.

To select a data location or a subset of steps:

```bash
python run_all.py --base ../../neuchatel-data
python run_all.py --steps 14 15
```

Partial runs update only the requested steps. Step13 audits the results written by Step10 and must follow a successful Step10 run.

Step15 downloads a fixed public ARE 2015 archive on first use and verifies the archive and GeoPackage checksums. No input records or home coordinates are sent. An already verified local cache supports offline use; details are in the analysis README.

## Redraw figures from existing results

```bash
python refresh_plot_style.py
```

This redraws eight figures using saved aggregate CSVs and the public study boundary. It overwrites the corresponding PNGs and writes `results/plot_style_method.json` while preserving the CSVs and existing method JSONs.

Comparison bars use **blue for Mikrozensus** and **orange for MATSim**. Common-accessibility maps retain the original **YlOrRd** absolute scales and **RdBu_r** difference scale. Titles and explanatory text are kept compact. The updated drawing functions are also used by regular Step runs.

## Collect aggregate outputs for review

```bash
python collect_review.py
```

This creates `assignment2_review.zip` containing CSVs, PNGs and method JSONs from `results/`. Inputs, the reference cache and previous result archives are excluded. Keep the ZIP out of Git and review the contents before sharing through the agreed private channel.

## Methods and interpretation

- Residents are selected by actual home location, integer ages 6–120 and the shared eligibility rules. Outside/cross-border flags alone do not exclude residents. The survey uses weekdays and positive finite person weights `WP`.
- Household ownership and personal driver access are separate quantities.
- Primary GEH uses average-hourly **whole-link** volumes. Per-lane GEH is supplementary; the scales are not interchangeable.
- PT subscription groups do not imply identical fare rights. `none` means none of the represented products.
- Common-reference accessibility compares residential locations under ARE 2015. It does not establish current service quality, simulated accessibility or punctuality.
- Simulated PT activity and delay diagnostics are not comparisons with observed passenger demand or punctuality.

Detailed definitions and limitations are documented in [assignment2_analysis/README.md](assignment2_analysis/README.md), [PT_PROVENANCE.md](assignment2_analysis/PT_PROVENANCE.md) and generated method JSONs.

## Verification

From `assignment2_analysis`:

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

Recorded software checks include 67 artificial-data regressions and a complete Steps2–15 artificial-data workflow. These checks do not establish the correctness of real study results.

## Data protection

Commit only intended code, tests and documentation. Keep private inputs, generated results, reference caches and exported review archives out of Git. The analysis `.gitignore` covers these outputs, but it does not remove files that were already tracked. Inspect `git diff --cached --name-only` before committing.

Documentation updated **6 November 2026**.

