"""Analyse simulated PT boarding patterns and matched road counts (GEH)."""

from __future__ import annotations

import argparse
import gzip
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from abmt_paths import data_root

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

DEFAULT_BASE = data_root()

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

def pt_analysis(sim, out):
    path = sim / "pt_passenger_counts.csv.gz"
    if not path.exists():
        return None
    df = read_tabular(path)
    required = {"line_id", "boardings", "alightings", "time_bin"}
    if not required.issubset(df.columns):
        return None
    for col in ("boardings", "alightings"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["boardings", "alightings"])
    df = df[(df.boardings >= 0) & (df.alightings >= 0)].copy()
    df["line_id"] = df["line_id"].fillna("unknown").astype(str)
    if "line_name" not in df.columns:
        df["line_name"] = ""
    df["line_label"] = df.line_id + "  " + df.line_name.fillna("").astype(str)
    summary = (df.groupby("line_label", as_index=False)
               .agg(boardings=("boardings", "sum"),
                    alightings=("alightings", "sum"),
                    recorded_stop_events=("line_id", "size")))
    summary = summary.sort_values("boardings", ascending=False)
    out.mkdir(parents=True, exist_ok=True)
    summary.to_csv(out / "pt_boardings_by_line.csv", index=False)
    plot_bars(summary, "line_label", "boardings", "Neuchatel: simulated PT boardings by line", out / "pt_boardings_by_line.png")

    def hour_from_bin(v):
        m = re.match(r"\s*(\d{1,2}):\d{2}", str(v))
        return int(m.group(1)) if m else np.nan

    df["hour"] = df["time_bin"].map(hour_from_bin)
    hourly = (df.dropna(subset=["hour"]).groupby("hour", as_index=False)
              .agg(boardings=("boardings", "sum"), alightings=("alightings", "sum")))
    if not hourly.empty:
        hourly.hour = hourly.hour.astype(int)
        hourly.to_csv(out / "pt_boardings_by_hour.csv", index=False)
        fig, ax = plt.subplots(figsize=(9, 4.8))
        ax.plot(hourly.hour, hourly.boardings, marker="o", label="Boardings")
        ax.plot(hourly.hour, hourly.alightings, marker="s", label="Alightings")
        ax.set(xlabel="Hour of simulated day", ylabel="Events in 10% sample", title="Neuchatel: PT boarding and alighting patterns")
        ax.legend()
        fig.tight_layout()
        fig.savefig(out / "pt_boardings_by_hour.png", dpi=180)
        plt.close(fig)
    return summary

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

def iteration_number(p):
    hits = [int(x) for x in re.findall(r"(?:^|[/\\])it\.(\d+)(?:$|[/\\])", str(p), flags=re.I)]
    hits += [int(x) for x in re.findall(r"(?:^|[/\\])(\d+)\.countscompare", str(p), flags=re.I)]
    return max(hits) if hits else -1

def find_countscompare(sim):
    files = []
    for p in sim.rglob("*"):
        name = p.name.lower()
        if p.is_file() and "countscompare" in name and name.endswith((".txt", ".csv", ".tsv")):
            files.append(p)
    hourly = [p for p in files if not ("awtv" in p.name.lower() or "daily" in p.name.lower())]
    daily = [p for p in files if p not in hourly]
    hourly.sort(key=lambda p: (iteration_number(p), str(p)))
    daily.sort(key=lambda p: (iteration_number(p), str(p)))
    return hourly, daily

def normalized(s):
    return re.sub(r"\s+", " ", str(s).strip().lower())

def read_comparison(path, hourly):
    df = pd.read_csv(path, sep="\t", dtype=str, encoding="utf-8-sig")
    if len(df.columns) == 1:
        df = pd.read_csv(path, sep=r"\s{2,}", engine="python", dtype=str, encoding="utf-8-sig")
    cols = {normalized(c): c for c in df.columns}
    essential = ["matsim volumes", "count volumes", "link id"] + (["hour"] if hourly else [])
    missing = [x for x in essential if x not in cols]
    if missing:
        raise ValueError(f"Cannot recognize the MATSim comparison columns: missing {missing}; found {list(df.columns)}")
    res = pd.DataFrame({
        "link_id": df[cols["link id"]].astype(str),
        "station_id": df[cols.get("count station id", cols["link id"])].astype(str),
        "simulated": pd.to_numeric(df[cols["matsim volumes"]], errors="coerce"),
        "observed": pd.to_numeric(df[cols["count volumes"]], errors="coerce"),
    })
    if hourly:
        res["hour"] = pd.to_numeric(df[cols["hour"]], errors="coerce")
        res = res[res.hour.between(1, 24)]
    res = res.dropna(subset=["simulated", "observed"])
    res = res[(res.simulated >= 0) & (res.observed >= 0)]
    if res.empty:
        raise ValueError("No non-negative matched observed/simulated values found.")
    if hourly and res.duplicated(["link_id", "hour"]).any():
        raise ValueError("Duplicate link-hour pairs in countscompare file.")
    return res

def observed_from_xml(path):
    rows = []
    if not path.exists():
        return pd.DataFrame(columns=["link_id", "station_id", "observed", "n_hours"])
    with gzip.open(path, "rb") if path.suffix == ".gz" else open(path, "rb") as f:
        for event, element in ET.iterparse(f, events=("end",)):
            if localname(element.tag) == "count":
                link = element.get("loc_id") or element.get("id") or element.get("linkId")
                station = element.get("cs_id") or element.get("name") or link
                vals = {}
                for vol in element.iter():
                    if localname(vol.tag) != "volume":
                        continue
                    try:
                        h, value = int(vol.get("h")), float(vol.get("val"))
                    except (TypeError, ValueError):
                        continue
                    if 1 <= h <= 24 and np.isfinite(value) and value >= 0:
                        vals[h] = value
                if link is not None:
                    rows.append({"link_id": str(link), "station_id": str(station),
                                 "observed": sum(vals.values()), "n_hours": len(vals)})
                element.clear()
    return pd.DataFrame(rows)

def daily_flow_from_csv(path):
    if not path.exists():
        return None
    df = read_tabular(path)
    if not {"linkId", "dailyCount"}.issubset(df.columns):
        return None
    df = df[["linkId", "dailyCount"]].rename(columns={"linkId": "link_id", "dailyCount": "raw_sample_volume"})
    df["link_id"] = df.link_id.astype(str)
    df.raw_sample_volume = pd.to_numeric(df.raw_sample_volume, errors="coerce")
    return df.dropna(subset=["raw_sample_volume"])

def geh(m, c):
    m, c = np.asarray(m, dtype=float), np.asarray(c, dtype=float)
    denom = m + c
    return np.sqrt(np.divide(2 * (m - c) ** 2, denom,
                             out=np.zeros(len(m), dtype=float), where=denom > 0))

def write_geh(df, out, source, scope, safe):
    out.mkdir(parents=True, exist_ok=True)
    df = df.copy()
    df["geh"] = geh(df.simulated, df.observed)
    df["geh_under_5"] = df.geh < 5
    df.to_csv(out / f"geh_{scope}_by_station.csv", index=False)
    n, n_links = len(df), df.link_id.nunique()
    pct = df.geh_under_5.mean() * 100
    summary = [
        f"Source: {source}", f"GEH time basis: {scope}", f"Pairs evaluated: {n}",
        f"Unique counted links: {n_links}", f"Median GEH: {df.geh.median():.3f}",
        f"GEH under 5: {df.geh_under_5.sum()}/{n} = {pct:.1f}%",
        f"Evidence status: {'MATSim-paired comparison' if safe else 'PROVISIONAL calibrated-count reconstruction (NOT an independent holdout)'}",
        "Warning: Hourly and 24-hour GEH values are not interchangeable.",
        "A good match of traffic counts does not by itself validate PT ridership.",
    ]
    (out / "geh_summary.txt").write_text("\n".join(summary)+"\n", encoding="utf-8")
    fig, ax = plt.subplots(figsize=(6.5, 6))
    ax.scatter(df.observed, df.simulated, s=12, alpha=0.5)
    upper = max(1, float(df[["observed", "simulated"]].max().max()) * 1.02)
    ax.plot([0, upper], [0, upper], "--", linewidth=1)
    ax.set(xlabel="Measured volume", ylabel="Simulated volume (matched scale)",
           title=f"Neuchatel: observed versus simulated ({scope})")
    ax.set_xlim(0, upper)
    ax.set_ylim(0, upper)
    fig.tight_layout()
    fig.savefig(out / "geh_observed_vs_simulated.png", dpi=180)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    ax.hist(df.geh, bins=min(30, max(5, int(np.sqrt(n)))))
    ax.axvline(5, linestyle="--", label="GEH = 5")
    ax.set(xlabel=f"GEH ({scope})", ylabel="Number of observations",
           title="Neuchatel: GEH distribution")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "geh_distribution.png", dpi=180)
    plt.close(fig)
    return df

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

def reference_counts_from_csv(path):

    df = read_tabular(path)
    columns = {str(x).strip().lower(): x for x in df.columns}
    id_col = next((columns[k] for k in ("linkid", "link_id", "link") if k in columns), None)
    count_col = next((columns[k] for k in ("count", "counts") if k in columns), None)
    if id_col is None or count_col is None:
        raise ValueError("Reference CSV missing required count or link ID columns")
    raw = df[[id_col, count_col]].copy()
    raw.columns = ["link_id", "reference_hourly_per_lane"]
    raw["link_id"] = raw.link_id.astype(str).str.strip()
    raw.reference_hourly_per_lane = pd.to_numeric(raw.reference_hourly_per_lane, errors="coerce")
    raw = raw[(raw.reference_hourly_per_lane > 0) & np.isfinite(raw.reference_hourly_per_lane)]
    raw = raw[raw.link_id.ne("") & raw.link_id.ne("nan")].copy()
    if raw.duplicated("link_id").any():
        raise ValueError("Multiple observed counts for the same network link; cannot pick one automatically")
    return raw

def network_candidates(sim):
    base = sim.parent
    paths = [sim / "output_network.xml.gz", sim / "output_network.xml",
             base / "neuchatel_network.xml.gz", base / "neuchatel_network.xml",
             base / "network.xml.gz", base / "network.xml"]
    paths.extend(p for p in base.glob("*network*.xml*") if p not in paths)
    return [p for p in paths if p.is_file()]

def read_network_lanes(sim, link_ids):

    candidates = network_candidates(sim)
    if not candidates:
        return None
    for path in candidates:
        result = {}
        try:
            opening = gzip.open if path.suffix == ".gz" else open
            with opening(path, "rb") as f:
                context = ET.iterparse(f, events=("start", "end"))
                root = None
                for event, elem in context:
                    if root is None:
                        root = elem
                    if event != "end":
                        continue
                    if localname(elem.tag) == "link":
                        identifier = elem.get("id")
                        if identifier in link_ids:
                            lanes = elem.get("permlanes") or elem.get("lanes")
                            try:
                                lanes = float(lanes)
                                if np.isfinite(lanes) and lanes > 0:
                                    result[identifier] = lanes
                            except (ValueError, TypeError):
                                pass
                        elem.clear()
                        if root is not None:
                            root.clear()
                if result:
                    return pd.DataFrame([{"link_id": k, "lanes": v} for k, v in result.items()])
        except (ET.ParseError, OSError, EOFError):
            continue
    return None

def geh_from_eqasim_calibration(sim, out, configfile, mods, daily):
    """Compare full-sample mean hourly link flows with calibration counts."""
    if daily is None:
        return False
    source, strategy = find_reference_counts_csv(sim, configfile, mods)
    if source is None:
        return False
    eq = mods.get("eqasim", {})
    flow = mods.get("eqasim:flow", {})
    try:
        sample = float(eq["sampleSize"])
        start = float(flow.get("startTime", "0"))
        end = float(flow.get("endTime", "86400"))
        hours = (end - start) / 3600.0
    except (KeyError, TypeError, ValueError):
        return False
    if not (0.0 < sample <= 1.0 and abs(start) < 1e-9 and abs(end - 86400.0) < 1e-9):
        return False
    try:
        ref = reference_counts_from_csv(source)
        merged = ref.merge(daily, on="link_id", validate="one_to_one")
        merged = merged[(merged.raw_sample_volume >= 0) & np.isfinite(merged.raw_sample_volume)].copy()
        if merged.empty:
            return False
        lanes = read_network_lanes(sim, set(merged.link_id))
        if lanes is None:
            return False
        matched = merged.merge(lanes, on="link_id", validate="one_to_one")
        if matched.empty:
            return False
        matched["observed"] = matched["reference_hourly_per_lane"]
        matched["simulated"] = matched["raw_sample_volume"] / (
            hours * sample * matched["lanes"].clip(lower=1.0)
        )
        matched = matched[(matched.observed > 0) & (matched.simulated >= 0)]
        if matched.empty:
            return False
        write_geh(matched[["link_id", "observed", "simulated"]], out,
                  "eqasim network-calibration count CSV + sampled daily link counts + network lanes",
                  "mean_hourly_from_24h_calibration", False)
        return True
    except (OSError, ValueError, KeyError) as ex:
        return False

def analyze_geh(sim, out):
    configfile, mods = read_config_modules(sim)
    counts_params = mods.get("counts", {})
    eq_params = mods.get("eqasim", {})
    flow_params = mods.get("eqasim:flow", {})
    h, d = find_countscompare(sim)
    for files, hourly in ((h, True), (d, False)):
        if not files:
            continue
        path = files[-1]
        try:
            frame = read_comparison(path, hourly)
            write_geh(frame, out, str(path.relative_to(sim)), "station_hour" if hourly else "aggregated", True)
            return
        except (ValueError, KeyError, OSError):
            continue

    obs = observed_from_xml(sim / "output_counts.xml.gz")
    daily = daily_flow_from_csv(sim / "output_traffic_flow_daily_counts.csv")
    if len(obs) == 0 or daily is None:
        if geh_from_eqasim_calibration(sim, out, configfile, mods, daily):
            return
        return
    try:
        sample = float(eq_params["sampleSize"])
        start = float(flow_params.get("startTime", "0"))
        end = float(flow_params.get("endTime", "86400"))
    except (KeyError, ValueError, TypeError):
        return
    if not (0 < sample <= 1 and abs(start) < 1e-7 and abs(end - 86400) < 1e-7):
        return
    full = obs[obs.n_hours == 24].copy()
    full = full.merge(daily, on="link_id", how="inner", validate="one_to_one")
    full = full[(full.observed >= 0) & (full.raw_sample_volume >= 0)].copy()
    if full.empty:
        if geh_from_eqasim_calibration(sim, out, configfile, mods, daily):
            return
        return
    full["simulated"] = full.raw_sample_volume / sample
    write_geh(full, out, "output_counts.xml.gz + output_traffic_flow_daily_counts.csv",
              "24h_provisional", False)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE, help="Local directory containing Neuchatel_10pct")
    args = parser.parse_args()
    base = args.base.expanduser().resolve()
    sim = get_sim(base)
    out = Path(__file__).resolve().parent / "results" / "pt_geh"
    out.mkdir(parents=True, exist_ok=True)
    pt_analysis(sim, out)
    analyze_geh(sim, out)
    return 0

if __name__ == "__main__":
    main()
