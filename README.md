# Agent-Based Modelling in Transportation (ABMT 2026)

Coursework repository for *Agent-Based Modelling in Transportation* at ETH Zürich.

The main project is the analysis of the **Neuchâtel 10% MATSim scenario** for Assignment 2. The Python scripts compare a synthetic population and the provided simulation outputs with the Swiss travel microcensus. Earlier MATSim/Java exercise files may also be present in the local checkout.

## Assignment 2 — Neuchâtel

The analysis code is in [`assignment2_analysis/`](assignment2_analysis/README.md). It covers:

- **Synthetic population:** demographics, spatial distribution, activity chains and locations, trips per person, and a home-only sensitivity check.
- **Simulation:** overall mode shares and comparisons by distance, population group, home area, and departure time.
- **Transport counts:** simulated public transport boardings and road traffic GEH; a separate script checks the GEH calculation.

The analysis uses the **provided MATSim outputs**; running a new simulation is not necessary.

## Local directory structure

The repository and restricted input data must be separate sibling directories:

```text
Agentbased/
├── abmt-2026/                      # Git repository
│   ├── README.md
│   ├── .gitignore
│   └── assignment2_analysis/
│       ├── README.md
│       ├── requirements.txt
│       ├── abmt_paths.py
│       ├── ABMT_Neuchatel_Step2_Population.py
│       ├── ... Step3 through Step13 ...
│       └── results/                # generated locally; ignored by Git
└── neuchatel-data/                  # local inputs; never commit
    ├── Boundary/
    ├── microcensus/                # Mikrozensus/ is also supported
    └── Neuchatel_10pct/
```

The scripts locate `neuchatel-data` relative to the repository. An alternative data location can be specified with the `ABMT_DATA_DIR` environment variable. No personal or machine-specific path is hard-coded.

## Getting started

From the repository root, using a Python environment with the required packages:

```bash
cd assignment2_analysis
python -m pip install -r requirements.txt
python ABMT_Neuchatel_Step6_ModeShare.py
```

Other analyses can be run using their respective `ABMT_Neuchatel_Step*.py` filenames. Run Step 10 before the optional Step 13 audit. Successful scripts normally print nothing and write tables and figures under `assignment2_analysis/results/`.

See the [analysis README](assignment2_analysis/README.md) for the complete script overview, data filters, outputs, and methodological limitations.

## Data protection

**Do not commit or upload** the microcensus, MATSim scenario files, observed traffic counts, person- or station-level data, or generated analysis results. All such inputs remain local and are **not included** in this repository. Generated results are excluded through `assignment2_analysis/.gitignore`.

The `.gitignore` only prevents *untracked* files from being added accidentally; it cannot make previously committed data private. Before pushing changes, check `git diff --cached --name-only` and verify that only intended code and documentation are staged. This applies even if the GitHub repository is private.

## Interpretation notes

- Microcensus analyses use weekday observations and the survey weights; the minimum age of six is an analysis choice.
- People with zero recorded trips are not necessarily confirmed to have stayed at home; Step 12 treats this as a sensitivity question.
- The GEH comparison uses calibration count references, not independent validation data. Reference units remain an assumption documented in the analysis README.
- The external-population filters in Steps 2 and 3 still need to be harmonized before final reporting.
