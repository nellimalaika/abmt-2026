"""Reviewed count-file/config helpers retained from Nelli; no standalone entry point."""
from __future__ import annotations


import argparse


import gzip


import re


import xml.etree.ElementTree as ET


from pathlib import Path


from abmt_paths import data_root


import matplotlib


import matplotlib.pyplot as plt


import numpy as np


import pandas as pd


def localname(tag):
    return str(tag).split("}")[-1]


def read_tabular(path):

    opening = gzip.open if path.suffix == ".gz" else open
    with opening(path, "rt", encoding="utf-8-sig", errors="replace") as f:
        first = f.readline()
    sep = max((";", ",", "\t"), key=lambda x: first.count(x))
    return pd.read_csv(path, sep=sep, compression="infer", low_memory=False, dtype=str)


def get_sim(base):
    preferred = base / "Neuchatel_10pct" / "simulation_output"
    if preferred.is_dir():
        return preferred
    matches = [p for p in base.rglob("simulation_output") if p.is_dir() and (p / "output_trips.csv.gz").exists()]
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(
        f"Could not identify one Neuchatel simulation_output directory under {base} (found {len(matches)})."
    )


def plot_bars(frame, category, measure, title, out, limit=12):
    frame = frame.sort_values(measure, ascending=False).head(limit)
    if frame.empty:
        return
    fig, ax = plt.subplots(figsize=(10, max(5, len(frame) * 0.35)))
    ax.barh(frame[category].astype(str), frame[measure])
    ax.invert_yaxis()
    ax.set_xlabel(measure.replace("_", " ").title() + " (10% sample, not extrapolated)")
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(out, dpi=180)
    plt.close(fig)


def read_config_modules(sim):
    for p in (sim / "output_config.xml", sim.parent / "neuchatel_config.xml", sim / "output_config_reduced.xml"):
        if not p.exists():
            continue
        try:
            root = ET.parse(p).getroot()
            modules = {}
            for mod in root.iter():
                if localname(mod.tag) != "module":
                    continue
                params = {}
                for child in mod:
                    if localname(child.tag) == "param":
                        params[str(child.get("name", ""))] = str(child.get("value", ""))
                modules[str(mod.get("name", ""))] = params
            return p, modules
        except (ET.ParseError, OSError):
            continue
    return None, {}


def geh(m, c):
    m, c = np.asarray(m, dtype=float), np.asarray(c, dtype=float)
    denom = m + c
    return np.sqrt(np.divide(2 * (m - c) ** 2, denom,
                             out=np.zeros(len(m), dtype=float), where=denom > 0))


def csv_header(path):

    try:
        opening = gzip.open if path.suffix == ".gz" else open
        with opening(path, "rt", encoding="utf-8-sig", errors="replace") as f:
            first = f.readline().strip("\r\n")
        sep = max((";", ",", "\t"), key=lambda x: first.count(x))
        return [c.strip().strip('"').lower() for c in first.split(sep)]
    except (OSError, UnicodeError):
        return []


def find_reference_counts_csv(sim, configfile, mods):
    """Use a configured or unambiguous calibration-count file."""
    base = sim.parent.parent
    raw_path = mods.get("eqasim:networkCalibration", {}).get("countsFile", "").strip()
    options = []
    if raw_path:
        original = Path(raw_path).expanduser()
        options += [original]
        for root in (configfile.parent if configfile else sim.parent, sim.parent, base, base.parent):
            options.append(root / original)
        for directory in (sim.parent, base, base.parent):
            options.append(directory / "network_calibration_files" / original.name)
        options += list(base.rglob(original.name))
    def valid(path):
        if not path.is_file() or path.suffix.lower() not in (".csv", ".gz", ".txt"):
            return False
        cols = set(csv_header(path))
        return (bool(cols & {"linkid", "link_id", "link"})
                and bool(cols & {"count", "counts"}))
    for candidate in options:
        if valid(candidate):
            return candidate, "configured"

    candidates = []
    folders = [sim.parent / "network_calibration_files", base / "network_calibration_files"]
    for f in folders:
        if f.is_dir():
            candidates.extend(p for p in f.rglob("*") if p.is_file() and p.suffix.lower() in (".csv", ".txt"))
    candidates = list(dict.fromkeys(p.resolve() for p in candidates if valid(p)))
    if len(candidates) == 1:
        return candidates[0], "unique_calibration_candidate"
    return None, "ambiguous" if len(candidates) > 1 else "not_found"


def network_candidates(sim):
    base = sim.parent
    paths = [sim / "output_network.xml.gz", sim / "output_network.xml",
             base / "neuchatel_network.xml.gz", base / "neuchatel_network.xml",
             base / "network.xml.gz", base / "network.xml"]
    paths.extend(p for p in base.glob("*network*.xml*") if p not in paths)
    return [p for p in paths if p.is_file()]


