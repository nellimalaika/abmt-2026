"""Compare residential patterns in Neuchatel using the Mikrozensus and MATSim."""

from pathlib import Path
from abmt_paths import data_root
import gzip
import xml.etree.ElementTree as ET

import matplotlib.pyplot as plt
from matplotlib.collections import PatchCollection
from matplotlib.patches import Patch, Polygon as MplPolygon
import numpy as np
import pandas as pd
import shapefile
from pyproj import CRS, Transformer
from shapely.geometry import Point, box, shape
from shapely.ops import transform, unary_union
from shapely.prepared import prep


BASE = data_root()
OUTPUT = Path(__file__).resolve().parent / "results" / "spatial_validation_corrected"
GRID_SIZE = 10_000
MIN_MZ = 30
MIN_MATSIM = 30


def find_file(name, folder):
    from abmt_paths import find_input_file
    return find_input_file(BASE, name, folder)


def read_boundary():
    folder = BASE / "Boundary"
    crs = CRS.from_wkt((folder / "neuchatel.prj").read_text()).to_2d()
    with shapefile.Reader(str(folder / "neuchatel.shp")) as reader:
        boundary = unary_union([shape(item.__geo_interface__) for item in reader.shapes()])
    if crs != CRS.from_epsg(2056):
        converter = Transformer.from_crs(crs, 2056, always_xy=True)
        boundary = transform(converter.transform, boundary)
    if boundary.is_empty or not boundary.is_valid:
        raise ValueError("Invalid Neuchatel boundary.")
    return boundary


def read_microcensus(boundary):
    households = pd.read_csv(
        find_file("haushalte.csv", "Mikrozensus"), encoding="latin1",
        usecols=["HHNR", "W_X_CH1903", "W_Y_CH1903", "W_BFS"],
        dtype={"HHNR": "string"},
    )
    persons = pd.read_csv(
        find_file("zielpersonen.csv", "Mikrozensus"), encoding="latin1",
        usecols=["HHNR", "alter", "WP", "tag"], dtype={"HHNR": "string"},
    )
    if households.HHNR.duplicated().any() or persons.HHNR.duplicated().any():
        raise ValueError("Duplicate household/person identifiers.")

    mz = persons.merge(households, on="HHNR", validate="one_to_one")
    columns = ["alter", "WP", "tag", "W_X_CH1903", "W_Y_CH1903", "W_BFS"]
    mz[columns] = mz[columns].apply(pd.to_numeric, errors="coerce")
    mz = mz.loc[
        mz.tag.between(1, 5) & mz.alter.between(6, 120) & mz.WP.gt(0)
        & mz.W_X_CH1903.gt(0) & mz.W_Y_CH1903.gt(0)
    ].copy()
    if mz.empty or not 400_000 <= mz.W_X_CH1903.median() <= 900_000:
        raise ValueError("Unexpected Microcensus coordinates.")

    converter = Transformer.from_crs(21781, 2056, always_xy=True)
    x, y = converter.transform(mz.W_X_CH1903.to_numpy(), mz.W_Y_CH1903.to_numpy())
    region = prep(boundary)
    inside = [region.covers(Point(px, py)) for px, py in zip(x, y)]
    mz["x"], mz["y"] = x, y
    mz = mz.loc[inside, ["x", "y", "alter", "WP", "W_BFS"]]
    return mz.rename(columns={"alter": "age", "WP": "weight", "W_BFS": "municipality_id"})


def tag(element):
    return element.tag.rsplit("}", 1)[-1]


def attributes(element):
    for child in element:
        if tag(child) == "attributes":
            return {
                item.get("name"): (item.text or "").strip()
                for item in child if tag(item) == "attribute" and item.get("name")
            }
    return {}


def home_attributes(person):
    plans = [child for child in person if tag(child) == "plan"]
    selected = [p for p in plans if p.get("selected", "").lower() in ("yes", "true", "1")]
    for plan in selected + [p for p in plans if p not in selected]:
        for activity in plan:
            if tag(activity) in ("act", "activity") and activity.get("type", "").lower().startswith("home"):
                details = attributes(activity)
                if details:
                    return details
    return {}


def as_number(value):
    try:
        number = float(value)
        return number if np.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def read_population(boundary):
    records = []
    region = prep(boundary)
    with gzip.open(find_file("neuchatel_population.xml.gz", "Neuchatel_10pct"), "rb") as file:
        iterator = ET.iterparse(file, events=("start", "end"))
        root = None
        for event, person in iterator:
            if root is None:
                root = person
            if event != "end" or tag(person) != "person":
                continue

            details = attributes(person)
            excluded = any(
                str(details.get(flag, "")).strip().lower() in ("true", "yes", "1")
                for flag in ("isFreight",)
            ) or str(details.get("subpopulation", "")).strip().lower() in (
                "freight",
            )
            if not excluded and str(details.get("type", "")).casefold() != "truck":
                x = as_number(details.get("home_coordinate_x"))
                y = as_number(details.get("home_coordinate_y"))
                if x is None or y is None:
                    x = as_number(details.get("home_x"))
                    y = as_number(details.get("home_y"))
                age = as_number(details.get("age"))
                if (x is not None and y is not None and 1_000_000 < x < 4_000_000
                        and age is not None and 6 <= age <= 120 and age.is_integer() and region.covers(Point(x, y))):
                    home = home_attributes(person)
                    records.append((
                        x, y, age, 1.0,
                        home.get("municipalityId") or details.get("municipalityId", ""),
                        home.get("municipalityType") or details.get("municipalityType", ""),
                    ))
            root.clear()

    return pd.DataFrame(
        records, columns=["x", "y", "age", "weight", "municipality_id", "municipality_type"]
    )


def grid_summary(data, x0, y0, label):
    frame = data.copy()
    frame["ix"] = np.floor((frame.x - x0) / GRID_SIZE).astype(int)
    frame["iy"] = np.floor((frame.y - y0) / GRID_SIZE).astype(int)
    frame["older_weight"] = frame.weight * (frame.age >= 65)
    grouped = frame.groupby(["ix", "iy"], as_index=False).agg(
        n=("weight", "size"), total=("weight", "sum"), older=("older_weight", "sum")
    )
    grouped[f"{label}_n"] = grouped.pop("n")
    weight = grouped.pop("total")
    grouped[f"{label}_share_pct"] = 100 * weight / frame.weight.sum()
    grouped[f"{label}_older_share_pct"] = 100 * grouped.pop("older") / weight
    return grouped


def polygon_parts(geom):
    if geom.geom_type == "Polygon":
        yield geom
    elif geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        for part in geom.geoms:
            yield from polygon_parts(part)


def plot_grid(cells, column, title, filename, boundary, x0, y0, coverage):
    fig, ax = plt.subplots(figsize=(11, 7))
    for polygon in polygon_parts(boundary):
        x, y = polygon.exterior.xy
        ax.fill(x, y, facecolor="0.90", edgecolor="none", zorder=0)
        for hole in polygon.interiors:
            x, y = hole.xy
            ax.fill(x, y, facecolor="white", edgecolor="none", zorder=1)

    xmin, ymin, xmax, ymax = boundary.bounds
    grid_patches = []
    for ix in range(int(np.ceil((xmax - x0) / GRID_SIZE)) + 1):
        for iy in range(int(np.ceil((ymax - y0) / GRID_SIZE)) + 1):
            tile = box(x0 + ix * GRID_SIZE, y0 + iy * GRID_SIZE,
                       x0 + (ix + 1) * GRID_SIZE, y0 + (iy + 1) * GRID_SIZE)
            for poly in polygon_parts(boundary.intersection(tile)):
                grid_patches.append(MplPolygon(np.asarray(poly.exterior.coords), closed=True))
    if grid_patches:
        ax.add_collection(PatchCollection(
            grid_patches, facecolor="none", edgecolor="0.7", linewidth=0.45, zorder=2
        ))

    patches, values = [], []
    for cell in cells.itertuples(index=False):
        tile = box(x0 + cell.ix * GRID_SIZE, y0 + cell.iy * GRID_SIZE,
                   x0 + (cell.ix + 1) * GRID_SIZE, y0 + (cell.iy + 1) * GRID_SIZE)
        for poly in polygon_parts(boundary.intersection(tile)):
            patches.append(MplPolygon(np.asarray(poly.exterior.coords), closed=True))
            values.append(float(getattr(cell, column)))
    if patches:
        colored = PatchCollection(
            patches, cmap="coolwarm", alpha=0.92, edgecolor="0.55", linewidth=0.4, zorder=3
        )
        colored.set_array(np.array(values))
        limit = max(1.0, max(abs(value) for value in values))
        colored.set_clim(-limit, limit)
        ax.add_collection(colored)
        fig.colorbar(colored, ax=ax, shrink=0.8,
                     label="MATSim minus weighted Mikrozensus (percentage points)")

    for polygon in polygon_parts(boundary):
        x, y = polygon.exterior.xy
        ax.plot(x, y, color="black", linewidth=1.4, zorder=6)
        for hole in polygon.interiors:
            x, y = hole.xy
            ax.plot(x, y, color="black", linewidth=0.8, zorder=6)
    ax.set_xlim(xmin - 2000, xmax + 2000)
    ax.set_ylim(ymin - 2000, ymax + 2000)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("LV95 Easting (m)")
    ax.set_ylabel("LV95 Northing (m)")
    ax.set_title(title)
    ax.legend(handles=[Patch(facecolor="0.90", edgecolor="0.7",
                             label="No reliable cell-level comparison (<30 MZ or <30 MATSim)")],
              loc="lower left", fontsize=8)
    fig.text(0.5, 0.015, coverage + " | Colors clipped to Neuchatel boundary",
             ha="center", va="bottom", fontsize=9)
    fig.tight_layout(rect=[0, 0.05, 1, 1])
    fig.savefig(OUTPUT / filename, dpi=180)
    plt.close(fig)


def municipality_ids(values):
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric.where(numeric.gt(0) & numeric.eq(numeric.round())).astype("Int64")


def municipality_type(value):
    if pd.isna(value):
        return None
    name = str(value).lower().strip().replace("_", "").replace("-", "").replace(" ", "")
    return {"urbancore": "Urban", "urban": "Urban", "suburban": "Suburban", "rural": "Rural"}.get(name)


def plot_urban_rural(mz, sim):
    mz, sim = mz.copy(), sim.copy()
    mz["municipality_id"] = municipality_ids(mz.municipality_id)
    sim["municipality_id"] = municipality_ids(sim.municipality_id)
    sim["urbanity"] = sim.municipality_type.map(municipality_type)

    types = sim.dropna(subset=["municipality_id", "urbanity"])[["municipality_id", "urbanity"]]
    if types.groupby("municipality_id").urbanity.nunique().gt(1).any():
        raise ValueError("Conflicting municipality type labels for the same municipality.")
    types = types.drop_duplicates()
    if types.empty:
        raise ValueError("Missing municipality types on MATSim home activities.")

    mz = mz.merge(types, on="municipality_id", how="left", validate="many_to_one")
    shared = set(mz.loc[mz.urbanity.notna(), "municipality_id"])
    mz_common = mz.loc[mz.urbanity.notna() & mz.municipality_id.isin(shared)].copy()
    sim_common = sim.loc[sim.urbanity.notna() & sim.municipality_id.isin(shared)].copy()
    if mz_common.empty or sim_common.empty:
        raise ValueError("No comparable municipality types in Neuchatel.")

    def summarize(frame, prefix):
        data = frame.assign(older_weight=frame.weight * (frame.age >= 65))
        table = data.groupby("urbanity", as_index=False).agg(
            n=("weight", "size"), total=("weight", "sum"), older=("older_weight", "sum")
        )
        weight = table.pop("total")
        table[f"{prefix}_n"] = table.pop("n")
        table[f"{prefix}_resident_share_pct"] = 100 * weight / data.weight.sum()
        table[f"{prefix}_older_share_pct"] = 100 * table.pop("older") / weight
        return table

    result = summarize(mz_common, "mz").merge(summarize(sim_common, "sim"), on="urbanity", how="outer")
    result["urbanity"] = pd.Categorical(
        result.urbanity, categories=["Urban", "Suburban", "Rural"], ordered=True
    )
    result = result.sort_values("urbanity")
    result["resident_share_diff_pp"] = result.sim_resident_share_pct - result.mz_resident_share_pct
    result["older_share_diff_pp"] = result.sim_older_share_pct - result.mz_older_share_pct
    result["sufficient_n"] = result.mz_n.ge(MIN_MZ) & result.sim_n.ge(MIN_MATSIM)

    output = OUTPUT / "urban_rural"
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "urban_rural_validation.csv", sep=";", index=False)
    plot_data = result.loc[result.sufficient_n]
    if plot_data.empty:
        return

    mz_coverage = 100 * len(mz_common) / len(mz)
    sim_coverage = 100 * len(sim_common) / len(sim)
    for mz_col, sim_col, title, filename, ylabel in [
        ("mz_resident_share_pct", "sim_resident_share_pct",
         "Residents by municipality type in Neuchatel", "urban_rural_resident_distribution.png",
         "Share of residents in matched municipalities (%)"),
        ("mz_older_share_pct", "sim_older_share_pct",
         "Aged 65+ by municipality type in Neuchatel", "urban_rural_older_share.png",
         "Aged 65+ within each municipality type (%)"),
    ]:
        fig, ax = plt.subplots(figsize=(8.5, 5))
        x = np.arange(len(plot_data))
        ax.bar(x - 0.19, plot_data[mz_col], 0.38, label="Mikrozensus (weighted)")
        ax.bar(x + 0.19, plot_data[sim_col], 0.38, label="MATSim (10% synthetic)")
        ax.set_xticks(x, plot_data.urbanity.astype(str).tolist())
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.set_ylim(bottom=0)
        ax.grid(axis="y", alpha=0.2)
        ax.legend()
        fig.text(0.5, 0.015,
                 f"Matched municipalities only | MZ coverage {mz_coverage:.1f}%; "
                 f"MATSim coverage {sim_coverage:.1f}% | <{MIN_MZ} respondents suppressed",
                 ha="center", va="bottom", fontsize=8)
        fig.tight_layout(rect=[0, 0.055, 1, 1])
        fig.savefig(output / filename, dpi=180)
        plt.close(fig)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    boundary = read_boundary()
    mz = read_microcensus(boundary)
    sim = read_population(boundary)
    if mz.empty or sim.empty:
        raise ValueError("No residents found inside the study boundary.")

    plot_urban_rural(mz, sim)
    x0 = np.floor(boundary.bounds[0] / GRID_SIZE) * GRID_SIZE
    y0 = np.floor(boundary.bounds[1] / GRID_SIZE) * GRID_SIZE
    cells = grid_summary(mz, x0, y0, "mz").merge(
        grid_summary(sim, x0, y0, "sim"), on=["ix", "iy"], how="outer"
    ).fillna(0)
    cells['comparison_shown'] = cells.mz_n.ge(MIN_MZ) & cells.sim_n.ge(MIN_MATSIM)
    cells.to_csv(OUTPUT / 'spatial_validation_grid_10km_all_cells.csv', sep=';', index=False)
    cells = cells.loc[cells.comparison_shown].copy()
    if cells.empty:
        raise ValueError("Not enough observations for a 10 km spatial comparison.")
    cells["resident_distribution_diff_pp"] = cells.sim_share_pct - cells.mz_share_pct
    cells["older_share_diff_pp"] = cells.sim_older_share_pct - cells.mz_older_share_pct
    cells.to_csv(OUTPUT / "spatial_validation_grid_10km.csv", sep=";", index=False)

    mz_coverage = 100 * cells.mz_n.sum() / len(mz)
    sim_coverage = 100 * cells.sim_n.sum() / len(sim)
    coverage = f"Population coverage in colored cells: MZ {mz_coverage:.1f}%, MATSim {sim_coverage:.1f}%"
    plot_grid(cells, "resident_distribution_diff_pp", "Spatial distribution of residents (10 km cells)",
              "spatial_resident_distribution.png", boundary, x0, y0, coverage)
    plot_grid(cells, "older_share_diff_pp", "Difference in share of residents aged 65+ (10 km cells)",
              "spatial_older_residents.png", boundary, x0, y0, coverage)


if __name__ == "__main__":
    main()
    from abmt_common import write_method, output
    write_method(output('spatial_validation_corrected'))
