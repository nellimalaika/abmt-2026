# PT integration handoff

Steps14–15 are integrated into the existing application. The joint report has
not been edited. Use regenerated aggregate results for the next joint review.

## Install the complete code folder

This ZIP contains the complete `assignment2_analysis` folder. Follow
`START_HIER.txt` in the ZIP: keep a backup outside the repository, then copy the
complete new folder into `abmt-2026`. No Git patch or GitHub upload is required.
The remote repository is unchanged; the attempted write was rejected with HTTP403.

| Changed files | Purpose |
|---|---|
| `ABMT_Neuchatel_Step14_PTSubscriptions.py`, `_abmt_subscriptions.py` | Supported subscription analysis, main/sensitivity figures and tables, transitions, metadata; Hatim's missingness and age logic retained |
| `ABMT_Neuchatel_Step15_CommonAccessibility.py`, `_abmt_accessibility.py` | Pinned ARE2015 assignment to actual homes, supplied/common comparisons, transitions, global10km maps, coverage and metadata |
| `abmt_common.py` | Shared checked questionnaire linkage and synthetic attributes attached to canonical resident IDs; method revision1.2 |
| `ABMT_Neuchatel_Step2_Population.py`, `ABMT_Neuchatel_Step3_SpatialPopulation.py` | Shared survey eligibility, with demographic/municipality fields linked afterwards |
| `_abmt_trips.py` | Disconnected transfer blocks remain unresolved, using the modal reader's1m continuity check |
| `run_all.py`, `collect_review.py`, `.gitignore` | Complete Steps2–15 execution/archiving; collect aggregate review outputs; exclude cache and old results |
| `tests/test_pt_extensions.py`, `tests/make_fixture.py`, `tests/test_rules.py` | Added27 regressions and PT fixture fields; isolate existing fixture module paths |
| `../README.md`, `README.md`, `PT_PROVENANCE.md`, `VALIDATION.txt`, `CHANGES.txt`, `INTEGRATION_HANDOFF.md` | Methods, exact source hashes, run/review instructions and verification |

No new dependency is needed: existing requirements include Shapely>=2, and
GeoPackage reading uses standard-library SQLite. The official archive and
GeoPackage checksums were verified. The four new figures were visually checked.

Verification: **67 tests passed**; all **14 entry points, Steps2–15**, completed
on artificial inputs and the verified public ARE layer, producing63 CSVs,
43 figures and16 method JSONs. Independent checks confirm120 residents/source,
survey WP sum240, synthetic weight sum120, category totals100%, unchanged
reclassification weights and correct grid/support rules. See `VALIDATION.txt`
for the detailed scope.

After updating the checkout, run this exact command from `assignment2_analysis`
with the documented sibling `neuchatel-data` directory:

```bash
python run_all.py --base ../../neuchatel-data
```

Use the Python environment in which `requirements.txt` is installed. The run
uses existing simulation outputs, archives the previous results and runs all
Steps2–15. Step15 downloads the fixed5.7MB public archive on first use and checks
both pins; use a prepopulated verified `reference_data/are_2015` cache offline,
or set `ABMT_ARE_REFERENCE_DIR` to an existing cache outside the private inputs.

After the run:

```bash
python collect_review.py
```

Share the resulting local `assignment2_review.zip` through the agreed private
review channel. It contains CSVs, figures and method JSONs from `results/`,
including `pt_subscriptions/` and `pt_accessibility_common/`; it excludes the
inputs, reference cache and previous result archives. Keep it out of Git.
Review unknown categories, both subscription definitions, supplied/common gaps,
transition totals, grid support masks and coverage before revising the report.

Limitations: private original data were not available here; real numerical
results, mandatory questionnaire fields and map coverage require the local run.
The exact supplied generator version and original supplied class-layer vintages
remain unverified. Model product groups do not imply identical fare rights;
`none` means none of the represented products. Common-reference accessibility
compares residential locations and does not establish current PT service
quality, simulated accessibility or punctuality. Existing age classes, mode
definitions, car-access and GEH conventions are retained.
