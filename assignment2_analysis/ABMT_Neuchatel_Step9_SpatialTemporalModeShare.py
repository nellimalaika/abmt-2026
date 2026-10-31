"""Compare trip mode shares by departure time and home location."""
from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.cm import ScalarMappable
from matplotlib.colors import Normalize, TwoSlopeNorm
from matplotlib.patches import Polygon as MplPolygon
from matplotlib.collections import PatchCollection
from pyproj import Transformer, CRS
from shapely.geometry import shape, box
from shapely.ops import transform as transform_geom, unary_union
import shapefile

from ABMT_Neuchatel_Step6_ModeShare import (
    MODES, MZ_MODE_MAP, find_file, read_csv_header,
    read_survey_residents, read_sim_resident_ids, load_boundary,
    classify_sim_mode, local_tag,
)
from ABMT_Neuchatel_Step8_ModeShareGroups import compare_by

OUT = Path(__file__).resolve().parent / "results" / "mode_share_spatial_temporal"
GRID_M = 10_000
TIME_EDGES = [0, 6, 9, 12, 15, 18, 21, 24, 30]
TIME_LABELS = ["00–06", "06–09", "09–12", "12–15", "15–18", "18–21", "21–24", "24–30"]
MIN_MZ = 30
MIN_SIM = 30
MODENAMES = {
    "car": "Car (driver)", "car_passenger": "Car passenger",
    "pt": "Public transport", "bike": "Bike", "walk": "Walk"
}

def load_boundary_polygon():
    shp = find_file("neuchatel.shp", "Boundary")
    prj = shp.with_suffix(".prj")
    with shapefile.Reader(str(shp)) as reader:
        polygon = unary_union([shape(s.__geo_interface__) for s in reader.shapes()])
    src = CRS.from_wkt(prj.read_text(encoding="utf-8")).to_2d()
    dst = CRS.from_epsg(2056)
    if src != dst:
        tr = Transformer.from_crs(src, dst, always_xy=True)
        polygon = transform_geom(tr.transform, polygon)
    if polygon.is_empty or not polygon.is_valid:
        raise ValueError("Boundary polygon is empty or invalid.")
    return polygon

def survey_home_coordinates(eligible):
    hh = pd.read_csv(find_file("haushalte.csv", "Mikrozensus"), encoding="latin1",
                     dtype={"HHNR": "string"}, usecols=["HHNR", "W_X_CH1903", "W_Y_CH1903"])
    if hh["HHNR"].duplicated().any():
        raise ValueError("Duplicate HHNR in household CSV")
    hh = hh.loc[hh["HHNR"].isin(eligible["HHNR"])].copy()
    hh["W_X_CH1903"] = pd.to_numeric(hh["W_X_CH1903"], errors="coerce")
    hh["W_Y_CH1903"] = pd.to_numeric(hh["W_Y_CH1903"], errors="coerce")
    tr = Transformer.from_crs("EPSG:21781", "EPSG:2056", always_xy=True)
    hh["home_x"], hh["home_y"] = tr.transform(hh["W_X_CH1903"].to_numpy(),
                                                hh["W_Y_CH1903"].to_numpy())
    return hh[["HHNR", "home_x", "home_y"]]

def sim_home_coordinates(eligible_ids):
    path = find_file("neuchatel_population.xml.gz", "Neuchatel_10pct")
    records = []
    with gzip.open(path, "rb") as stream:
        root = None
        for event, elem in ET.iterparse(stream, events=("start", "end")):
            if root is None:
                root = elem
            if event != "end" or local_tag(elem.tag) != "person":
                continue
            person = str(elem.get("id", ""))
            if person in eligible_ids:
                attrs = {}
                for child in elem:
                    if local_tag(child.tag) == "attributes":
                        for a in child:
                            if local_tag(a.tag) == "attribute":
                                attrs[a.get("name", "")] = (a.text or "").strip()
                x = attrs.get("home_x") or attrs.get("home_coordinate_x")
                y = attrs.get("home_y") or attrs.get("home_coordinate_y")
                records.append((person, x, y))
            root.clear()
    people = pd.DataFrame(records, columns=["person", "home_x", "home_y"])
    if people["person"].duplicated().any():
        raise ValueError("Duplicate MATSim person ID")
    people["home_x"] = pd.to_numeric(people["home_x"], errors="coerce")
    people["home_y"] = pd.to_numeric(people["home_y"], errors="coerce")
    return people

def mz_trips(eligible):
    path = find_file("wege.csv", "Mikrozensus")
    cols = ["HHNR", "WEGNR", "wmittel", "f51100",
            "S_X_CH1903", "S_Y_CH1903", "Z_X_CH1903", "Z_Y_CH1903"]
    df = pd.read_csv(path, encoding="latin1", dtype={"HHNR": "string", "WEGNR": "string"},
                     usecols=cols, low_memory=False)
    df = df[df["HHNR"].isin(eligible["HHNR"])].copy()
    if df.duplicated(["HHNR", "WEGNR"]).any():
        raise ValueError("Duplicate Mikrozensus way IDs")
    df = df.merge(eligible[["HHNR", "WP"]], on="HHNR", validate="many_to_one")
    df["wmittel"] = pd.to_numeric(df["wmittel"], errors="coerce")
    df["mode"] = df["wmittel"].map(MZ_MODE_MAP)

    stages = pd.read_csv(find_file("etappen.csv", "Mikrozensus"), encoding="latin1",
                         dtype={"HHNR": "string", "WEGNR": "string"},
                         usecols=["HHNR", "WEGNR", "f51300"])
    stages["f51300"] = pd.to_numeric(stages["f51300"], errors="coerce")
    passengers = stages.loc[stages["f51300"].eq(8), ["HHNR", "WEGNR"]].drop_duplicates()
    passengers["car_passenger"] = True
    df = df.merge(passengers, how="left", on=["HHNR", "WEGNR"], validate="many_to_one")
    df.loc[df["car_passenger"].eq(True), "mode"] = "car_passenger"

    df = df[df["mode"].isin(MODES)].copy()
    for col in ("S_X_CH1903", "S_Y_CH1903", "Z_X_CH1903", "Z_Y_CH1903"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    good = (df["S_X_CH1903"].between(400_000, 900_000) &
            df["Z_X_CH1903"].between(400_000, 900_000) &
            df["S_Y_CH1903"].between(0, 400_000) &
            df["Z_Y_CH1903"].between(0, 400_000))
    df = df[good].copy()
    tr = Transformer.from_crs("EPSG:21781", "EPSG:2056", always_xy=True)
    sx, sy = tr.transform(df["S_X_CH1903"].to_numpy(), df["S_Y_CH1903"].to_numpy())
    zx, zy = tr.transform(df["Z_X_CH1903"].to_numpy(), df["Z_Y_CH1903"].to_numpy())
    df["distance_m"] = np.hypot(zx - sx, zy - sy)
    df = df[df["distance_m"].gt(1) & np.isfinite(df["distance_m"])].copy()
    df["time_seconds"] = pd.to_numeric(df["f51100"], errors="coerce") * 60.0
    return df[["HHNR", "WP", "mode", "distance_m", "time_seconds"]]

def parse_time_series(values):

    s = values.astype("string").str.strip()
    numeric = pd.to_numeric(s, errors="coerce")
    need_clock = numeric.isna() & s.notna()
    result = numeric.astype("float64")
    if need_clock.any():
        timedeltas = pd.to_timedelta(s.loc[need_clock], errors="coerce")
        result.loc[need_clock] = timedeltas.dt.total_seconds().to_numpy()
    return result

def matsim_trips(resident_ids):
    path = find_file("output_trips.csv.gz", "simulation_output")
    cols, sep = read_csv_header(path)
    person_col = next((c for c in ("person", "person_id", "personId") if c in cols), None)
    mode_col = next((c for c in ("main_mode", "mainMode", "mode") if c in cols), None)
    distance_col = next((c for c in ("euclidean_distance", "euclideanDistance") if c in cols), None)
    departure_col = next((c for c in ("dep_time", "departure_time", "departureTime") if c in cols), None)
    if not all((person_col, mode_col, distance_col, departure_col)):
        raise ValueError("Expected MATSim person, main_mode, euclidean_distance and dep_time. "
                         "Available headers: " + repr(cols))
    activity_cols = [c for c in ("start_activity_type", "end_activity_type") if c in cols]
    use = [person_col, mode_col, distance_col, departure_col] + activity_cols
    df = pd.read_csv(path, compression="gzip", sep=sep, low_memory=False,
                     dtype={person_col: "string"}, usecols=use)
    df = df[df[person_col].isin(resident_ids)].copy()
    for col in activity_cols:
        forbidden = df[col].fillna("").astype("string").str.lower().str.contains(
            r"outside|crossborder|freight|^border", regex=True).fillna(False)
        df = df.loc[~forbidden].copy()
    df["mode"] = df[mode_col].map(classify_sim_mode)
    unknowns = df.loc[df["mode"].eq("unknown"), mode_col].value_counts(dropna=False)
    if len(unknowns):
        raise ValueError(f"Unknown MATSim main modes: {unknowns.to_dict()}; do not guess mapping")
    df = df[df["mode"].isin(MODES)].copy()
    df["distance_m"] = pd.to_numeric(df[distance_col], errors="coerce")
    df = df[df["distance_m"].gt(1) & np.isfinite(df["distance_m"])].copy()
    df["time_seconds"] = parse_time_series(df[departure_col])
    df = df.rename(columns={person_col: "person"})
    return df[["person", "mode", "distance_m", "time_seconds"]]

def time_bins(df):
    x = df.copy()
    valid = x["time_seconds"].notna() & np.isfinite(x["time_seconds"])
    valid &= x["time_seconds"].ge(0) & x["time_seconds"].lt(30 * 3600)
    x = x[valid].copy()
    x["time_bin"] = pd.cut(x["time_seconds"] / 3600.0,
                           bins=TIME_EDGES, labels=TIME_LABELS,
                           include_lowest=True, right=False).astype("string")
    return x

def grid_cells(df, minx, miny):
    x = df.copy()
    valid = x["home_x"].notna() & x["home_y"].notna()
    x = x[valid].copy()
    x["ix"] = np.floor((x["home_x"].to_numpy() - minx) / GRID_M).astype(int)
    x["iy"] = np.floor((x["home_y"].to_numpy() - miny) / GRID_M).astype(int)
    x["spatial_bin"] = x["ix"].astype(str) + "," + x["iy"].astype(str)
    return x

def plot_temporal(table):
    fig, axes = plt.subplots(3, 2, figsize=(12.7, 11))
    x = np.arange(len(TIME_LABELS))
    for i, mode in enumerate(MODES):
        ax = axes.flat[i]
        values = table.loc[table["mode"].eq(mode)].set_index("group").reindex(TIME_LABELS)
        valid = values["comparison_minimum_sample_met"].fillna(False)
        ax.plot(x, values["microcensus_weighted_pct"].where(valid), "o-", label="Mikrozensus")
        ax.plot(x, values["matsim_pct"].where(valid), "s-", label="MATSim")
        ax.set_xticks(x, TIME_LABELS, rotation=25)
        ax.set_title(MODENAMES[mode])
        ax.set_ylabel("Share of trips in time bin (%)")
        ax.grid(axis="y", alpha=.2)
        if i == 0:
            ax.legend()
    axes.flat[-1].axis("off")
    fig.suptitle("Neuchatel – mode shares by departure time")
    fig.text(.5, .015, "Groups with fewer than 30 respondents or simulated people omitted.",
             ha="center", fontsize=9)
    fig.tight_layout(rect=(0,.035,1,.965))
    fig.savefig(OUT / "mode_share_by_departure_time.png", dpi=180)
    plt.close(fig)
    pt = table[table["mode"].eq("pt")].set_index("group").reindex(TIME_LABELS)
    valid = pt["comparison_minimum_sample_met"].fillna(False)
    fig, ax = plt.subplots(figsize=(10,5))
    ax.plot(x, pt["microcensus_weighted_pct"].where(valid), "o-", label="Mikrozensus (weighted)")
    ax.plot(x, pt["matsim_pct"].where(valid), "s-", label="MATSim (10% sample)")
    ax.set_xticks(x, TIME_LABELS)
    ax.set_xlabel("Departure time (hours after midnight)")
    ax.set_ylabel("Public transport share of departing trips (%)")
    ax.set_title("Neuchatel – public transport share by departure time")
    ax.grid(axis="y", alpha=.2)
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "pt_mode_share_by_departure_time.png", dpi=180)
    plt.close(fig)

def draw_polygon(ax, geom, facecolor, edgecolor="none", linewidth=0.0):
    geometries = [geom] if geom.geom_type == "Polygon" else list(getattr(geom, "geoms", []))
    patches = []
    for polygon in geometries:
        if polygon.geom_type == "Polygon" and not polygon.is_empty:
            patches.append(MplPolygon(np.asarray(polygon.exterior.coords), closed=True))
    if patches:
        ax.add_collection(PatchCollection(patches, facecolor=facecolor,
                                          edgecolor=edgecolor, linewidth=linewidth))

def plot_spatial_pt(table, boundary_poly, minx, miny, categories):
    pt = table.loc[table["mode"].eq("pt")].set_index("group").reindex(categories)
    valid = pt["comparison_minimum_sample_met"].fillna(False).astype(bool)
    maxpct = float(np.nanmax(pt[["microcensus_weighted_pct", "matsim_pct"]].to_numpy()))
    maxpct = max(10, maxpct)
    cmap = plt.get_cmap("viridis")
    norm = Normalize(vmin=0, vmax=maxpct)
    diffdata = pt.loc[valid, "difference_pp"].to_numpy()
    maxdiff = max(2.0, float(np.nanmax(np.abs(diffdata))) if len(diffdata) else 2.0)
    diffnorm = TwoSlopeNorm(vmin=-maxdiff, vcenter=0, vmax=maxdiff)
    diffcmap = plt.get_cmap("coolwarm")
    fig, axes = plt.subplots(1,3,figsize=(17,6),sharex=True,sharey=True)
    for idx, cat in enumerate(categories):
        ix, iy = map(int, cat.split(","))
        cell = box(minx+ix*GRID_M, miny+iy*GRID_M,
                   minx+(ix+1)*GRID_M, miny+(iy+1)*GRID_M)
        clipped = cell.intersection(boundary_poly)
        if clipped.is_empty:
            continue
        good = bool(valid.loc[cat])
        for j, key in enumerate(("microcensus_weighted_pct", "matsim_pct", "difference_pp")):
            z = pt.loc[cat, key]
            if not good or not np.isfinite(z):
                color = "#d4d8dc"
            else:
                color = (diffcmap(diffnorm(float(z))) if j==2 else cmap(norm(float(z))))
            draw_polygon(axes[j], clipped, color, "#f3f3f3", .4)
    for ax, title in zip(axes, ("Mikrozensus (weighted)","MATSim (10% sample)","MATSim − Mikrozensus")):
        draw_polygon(ax, boundary_poly, "none", "#233444", .9)
        ax.set_title(title)
        ax.set_aspect("equal")
        ax.set_xlabel("LV95 easting (m)")
    axes[0].set_ylabel("LV95 northing (m)")
    minx2,miny2,maxx,maxy=boundary_poly.bounds
    axes[0].set_xlim(minx2-800, maxx+800)
    axes[0].set_ylim(miny2-800, maxy+800)
    fig.colorbar(ScalarMappable(norm=norm,cmap=cmap),ax=list(axes[:2]),shrink=.78,
                 label="Public transport share within grid cell (%)")
    fig.colorbar(ScalarMappable(norm=diffnorm,cmap=diffcmap),ax=axes[2],shrink=.78,
                 label="Difference (percentage points)")
    fig.suptitle("Neuchatel – public transport mode share by residents’ HOME location (10-km grid)")
    fig.text(.5,.02,"Gray: <30 distinct MZ respondents or <30 simulated persons; trip shares conditional on residents of each cell.",
             ha="center",fontsize=9)
    fig.savefig(OUT / "pt_mode_share_by_home_grid.png",dpi=190,bbox_inches="tight")
    plt.close(fig)

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    boundary = load_boundary()
    polygon = load_boundary_polygon()
    eligible = read_survey_residents(boundary)
    ids = read_sim_resident_ids(boundary)
    mz = mz_trips(eligible)
    sim = matsim_trips(ids)

    mz_time = time_bins(mz)
    sim_time = time_bins(sim)
    comparison, sizes = compare_by(mz_time, sim_time, "time_bin", TIME_LABELS)
    comparison.to_csv(OUT / "mode_share_by_departure_time.csv", index=False)
    sizes.to_csv(OUT / "departure_time_coverage.csv", index=False)
    plot_temporal(comparison)

    mz = mz.merge(survey_home_coordinates(eligible), on="HHNR", how="left", validate="many_to_one")
    sim = sim.merge(sim_home_coordinates(ids), on="person", how="left", validate="many_to_one")
    minx, miny, maxx, maxy = polygon.bounds
    mz = grid_cells(mz, minx, miny)
    sim = grid_cells(sim, minx, miny)
    all_cells = sorted(set(mz["spatial_bin"]).union(sim["spatial_bin"]),
                       key=lambda s:tuple(map(int,s.split(","))))
    spatial, counts = compare_by(mz, sim, "spatial_bin", all_cells)
    spatial.to_csv(OUT / "mode_share_by_home_grid.csv", index=False)
    counts.to_csv(OUT / "home_grid_coverage.csv", index=False)
    plot_spatial_pt(spatial,polygon,minx,miny,all_cells)

    notes = [
        "ABMT Assignment 2, Step 9 – temporal and spatial trip-based mode share",
        "MZ persons: age >=6, weekdays, residence inside Boundary/neuchatel.shp, weighted by WP.",
        "MATSim: resident synthetic 10% sample age>=6 inside same boundary, unweighted.",
        "Both: same five main modes and valid crow-fly trip distance >1m as Steps 7/8.",
        "Special MATSim _loop/remote_walk and outside/crossborder/freight trips excluded.",
        "Temporal: MZ f51100 minutes after midnight; MATSim output_trips dep_time seconds or HH:MM:SS.",
        "Spatial: each trip assigned to the person's HOME 10-km LV95 grid cell.",
        "Values: share of the trips within each time interval or home grid cell, not within all trips.",
        "Gray/omitted if <30 distinct survey respondents or <30 simulated persons.",
        "Small survey samples and non-independent trip observations limit confidence.",
        "These statistics alone do not establish route-level public-transport validity.",
        "Reference: Swiss eqasim cmdp_new_version; analysis/mode_shares/utils.py, data/microcensus/trips.py",
    ]
    (OUT / "methodology.txt").write_text("\n".join(notes)+"\n",encoding="utf8")

if __name__ == "__main__":
    main()
