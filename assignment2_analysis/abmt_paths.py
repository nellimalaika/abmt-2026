"""Find the local Neuchatel data directory independently of the checkout location."""

import os
from pathlib import Path


REQUIRED_FOLDERS = ("Boundary", "Neuchatel_10pct")
MICROCENSUS_FOLDERS = ("microcensus", "Mikrozensus")


def data_root():
    override = os.environ.get("ABMT_DATA_DIR")
    if override:
        candidates = [Path(override).expanduser().resolve()]
    else:
        analysis = Path(__file__).resolve().parent
        candidates = [
            analysis.parent,                             # neuchatel-data/assignment2_analysis
            analysis.parent.parent / "neuchatel-data",  # Agentbased/abmt-2026/assignment2_analysis
        ]

    for directory in candidates:
        if (
            all((directory / name).is_dir() for name in REQUIRED_FOLDERS)
            and any((directory / name).is_dir() for name in MICROCENSUS_FOLDERS)
        ):
            return directory

    raise FileNotFoundError(
        "Local Neuchatel data not found. Expected Boundary, microcensus "
        "(or Mikrozensus) and "
        "Neuchatel_10pct inside Agentbased/neuchatel-data. "
        "Alternatively set ABMT_DATA_DIR to that directory."
    )
