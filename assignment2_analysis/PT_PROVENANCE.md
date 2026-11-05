# PT integration provenance — 5 November 2026

This adapts Hatim's five supplied implementation references into Nelli's current
`assignment2_analysis` application at upstream commit
`86c5f46f3d3f715377166ebc8b014c49c1c03d0f`.
It does not introduce an independently selected validation population. Source
snapshots are identified below without copying private records or outputs.

| Hatim reference | SHA-256 of supplied script | Role in this integration |
|---|---|---|
| `validate_pt_subscriptions.py` | `b2c5c6db866f513167bc4eaaa56d2ee804ec89195e6d221b2d9d4539cff37a1f` | `_abmt_subscriptions.py`: three-valued item logic, generator and literal three-item definitions, all-resident weighted summaries, labels and method explanation |
| `validate_pt_accessibility_common.py` | `d057b4d49b9223a0199d6e2717efafc7e75abcdd2d0da8228c1bbe6711da06f2` | `_abmt_accessibility.py`: pinned archive/GeoPackage verification, SQLite geometry decoding, boundary-inclusive best-grade assignment, transitions and supplied/common plots |
| `validate_pt_accessibility.py` | `cb60154fa6ac56a09c9d3f876ecd7f9f049835d190cd60f8549654d667f359e9` | Supplied class mapping, sanitized category audits, class summaries and within-cell classified-resident denominators |
| `validate_spatial_residents.py` | `e7817546b652b5ab394c8e40caf486043b3dadc17fa021041235d77510dacf8e` | Reviewed coordinate priority/grid behavior; reuse the existing `_abmt_spatial` grid and polygon plotting helpers |
| `validate_age.py` | `9398a911c15f5b7d2aacab387cf4f2907512b22813b9284763f178adc53b2248` | Reviewed original eligibility/weight reference; reuse the existing shared `_abmt_age` survey and `_abmt_modes` resident-ID readers |

The source scripts' generator URLs and inspected Git blob SHAs are retained in
helper constants and generated method metadata. They are definition references;
the supplied scenario's exact generator version remains unverified. Nelli's
step names, input resolution, `ABMT_DATA_DIR`, results layout and method metadata
conventions are retained.

Deliberate adaptations: global LV95 10 km cells replace the references' 5 km
defaults; displayed differences require at least 30 distinct classified residents
in each source. Questionnaire fields are attached to the shared cohort using
checked one-to-one linkage. Synthetic homes/attributes are attached only to
IDs admitted by the existing shared reader. No new subscription, travel or
accessibility eligibility filter is applied. Codes, dates, source checksums and
interpretive limitations are preserved. Aggregate mapping transitions and
coverage are made explicit in saved tables and method metadata.

The official reference is the ARE 2015 archive, catalogue timestamp
`2015-03-17T00:00:00Z`, in EPSG:2056:

- <https://data.geo.admin.ch/ch.are.gueteklassen_oev/gueteklassen_oev_2015/gueteklassen_oev_2015_2056.gpkg.zip>
- Archive SHA-256: `dfede4a7a2e2c66536c476d958f9daafb9a2dd292ccadbb3fc8d8d419bdaa0b1`
- GeoPackage SHA-256: `ef7ad0511c68a410c50caa41cfa63a8b24b6cb2d4c069227a302806569428129`

Both hashes were checked against the downloaded public files during integration.
The archive is a fixed common reference, without claiming it is the exact layer
originally supplied with MZMV or the synthetic population. This compares
residential locations; it establishes neither current PT service quality nor
simulated accessibility or punctuality.
