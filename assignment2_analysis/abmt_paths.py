"""Find the local Neuchatel data directory independently of the checkout location."""

import os
from pathlib import Path


REQUIRED_FOLDERS = ("Boundary", "Neuchatel_10pct")
MICROCENSUS_FOLDERS = ("microcensus", "Mikrozensus")


def find_input_file(root, filename, folder=None):
    """Resolve a scenario input before looking recursively for legacy layouts.

    A backup elsewhere below the data root must not make a canonical input
    ambiguous. Multiple candidates *within* the selected input scope remain an
    error: their order is not evidence of which version belongs to this run.
    """
    root = Path(root).expanduser().resolve()
    if Path(filename).name != filename:
        raise ValueError("Pass an input basename, not a relative path.")
    name = filename.casefold()
    hint = str(folder or "").casefold()
    survey_names = {"haushalte.csv", "zielpersonen.csv", "wege.csv", "etappen.csv"}
    if name in survey_names or hint in {x.casefold() for x in MICROCENSUS_FOLDERS}:
        relatives = list(MICROCENSUS_FOLDERS)
    elif name.startswith("neuchatel.") or hint == "boundary":
        relatives = ["Boundary"]
    elif name.startswith("output_") or name == "pt_passenger_counts.csv.gz" or hint == "simulation_output":
        relatives = ["Neuchatel_10pct/simulation_output"]
    elif name.startswith("neuchatel_") or hint == "neuchatel_10pct":
        relatives = ["Neuchatel_10pct"]
    elif name in {"target_counts.csv", "target_flow.csv"} or hint == "network_calibration_files":
        relatives = ["Neuchatel_10pct/network_calibration_files"]
    else:
        relatives = [str(folder)] if folder else []

    def distinct(paths):
        # Case aliases or linked input trees can identify the SAME file.
        # Do not count one physical file twice (including on macOS).
        result, seen = [], set()
        for p in sorted({p.resolve() for p in paths if p.is_file()}, key=str):
            stat = p.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity not in seen:
                result.append(p)
                seen.add(identity)
        return result

    def choose(paths, scope):
        matches = distinct(paths)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            details = ", ".join(str(p.relative_to(root)) if p.is_relative_to(root) else str(p)
                                for p in matches[:5])
            raise ValueError(f"Ambiguous {filename} within {scope}: {details}. Select one input dataset; no file was chosen.")
        return None

    folders = sorted({(root / relative).resolve() for relative in relatives
                      if (root / relative).is_dir()}, key=str)
    # Canonical direct paths win, including when a second copy exists in a
    # backup, a nested extraction, a course folder, or __MACOSX metadata.
    direct = [p for directory in folders for p in directory.iterdir()
              if p.is_file() and p.name.casefold() == name]
    selected = choose(direct, "the canonical input folders")
    if selected is not None:
        return selected
    if folders:
        scoped = [p for directory in folders for p in directory.rglob("*")
                  if p.is_file() and p.name.casefold() == name
                  and "__MACOSX" not in p.relative_to(directory).parts]
        selected = choose(scoped, "the selected input folders")
        if selected is not None:
            return selected
        expected = ", ".join(str(Path(relative) / filename) for relative in relatives)
        raise FileNotFoundError(f"Missing {filename}. Expected under {root}: {expected}.")
    # Retain support for old layouts only when the conventional folders are
    # absent, and still require a genuinely unambiguous candidate.
    legacy = [p for p in root.rglob("*") if p.is_file() and p.name.casefold() == name
              and "__MACOSX" not in p.relative_to(root).parts]
    selected = choose(legacy, "the legacy data layout")
    if selected is not None:
        return selected
    raise FileNotFoundError(f"Missing {filename} below {root}.")


def data_root():
    override = os.environ.get("ABMT_DATA_DIR")
    if override:
        candidates = [Path(override).expanduser().resolve()]
    else:
        analysis = Path(__file__).resolve().parent
        candidates = [
            analysis.parent,                             # neuchatel-data/assignment2_analysis
            analysis.parent.parent / "neuchatel-data",
            analysis.parent.parent / "neuchatel_data",  # Agentbased/abmt-2026/assignment2_analysis
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
