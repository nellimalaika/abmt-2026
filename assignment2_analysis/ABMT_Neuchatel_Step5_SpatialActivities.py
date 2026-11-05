"""Compare the locations and purposes of non-home activities in Neuchatel."""

from pathlib import Path
from abmt_paths import data_root
import gzip
import xml.etree.ElementTree as ET

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import colors, patches, ticker
import numpy as np
import pandas as pd
import shapefile
from pyproj import CRS, Transformer
from shapely.geometry import Point, box, shape
from shapely.ops import transform, unary_union
from shapely.prepared import prep


BASE = data_root()
OUTPUT = Path(__file__).resolve().parent / "results" / "spatial_activities"
GRID_SIZE = 10_000
MIN_MZ = 30
MIN_MATSIM = 30
PURPOSES = ("work", "education", "shop", "leisure", "other")
MZ_PURPOSES = {
    1: "interaction", 2: "work", 3: "education", 4: "shop",
    5: "other", 6: "work", 7: "work", 8: "leisure",
    9: "other", 10: "other", 11: "home", 12: "unknown", 13: "outside",
}


def find_file(filename, folder):
    from abmt_paths import find_input_file
    return find_input_file(BASE, filename, folder)


def read_boundary():
    shp = find_file("neuchatel.shp", "Boundary")
    with shapefile.Reader(str(shp)) as reader:
        boundary = unary_union([shape(s.__geo_interface__) for s in reader.shapes()])
    crs = CRS.from_wkt(shp.with_suffix(".prj").read_text(encoding="utf-8")).to_2d()
    if crs != CRS.from_epsg(2056):
        converter = Transformer.from_crs(crs, 2056, always_xy=True)
        boundary = transform(converter.transform, boundary)
    if boundary.is_empty or not boundary.is_valid:
        raise ValueError("Invalid Neuchatel boundary")
    return boundary, prep(boundary)


def survey_residents(region):
    households = pd.read_csv(
        find_file("haushalte.csv", "Mikrozensus"), encoding="latin1",
        dtype={"HHNR": "string"}, usecols=["HHNR", "W_X_CH1903", "W_Y_CH1903"]
    )
    people = pd.read_csv(
        find_file("zielpersonen.csv", "Mikrozensus"), encoding="latin1",
        dtype={"HHNR": "string"}, usecols=["HHNR", "alter", "tag", "WP"]
    )
    people = people.merge(households, on="HHNR", validate="one_to_one")
    fields = ["alter", "tag", "WP", "W_X_CH1903", "W_Y_CH1903"]
    people[fields] = people[fields].apply(pd.to_numeric, errors="coerce")
    people = people.loc[
        people.alter.between(6, 120) & people.tag.between(1, 5) & people.WP.gt(0)
        & people.W_X_CH1903.gt(0) & people.W_Y_CH1903.gt(0)
    ].copy()
    if people.empty or not 400_000 <= people.W_X_CH1903.median() <= 900_000:
        raise ValueError("Unexpected Mikrozensus home coordinates")
    converter = Transformer.from_crs(21781, 2056, always_xy=True)
    xx, yy = converter.transform(people.W_X_CH1903.to_numpy(), people.W_Y_CH1903.to_numpy())
    inside = [region.covers(Point(x, y)) for x, y in zip(xx, yy)]
    people = people.loc[inside, ["HHNR", "alter", "WP"]]
    return people.rename(columns={"alter": "age", "WP": "weight"})


def survey_destinations(people,region):
    from abmt_common import visit_data
    a,b=visit_data()
    return a



def number(value):
    try:
        result = float(value)
        return result if np.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def activity_type(value):
    name = (value or "").strip().lower()
    if name.startswith("home"):
        return "home"
    if name.startswith("work") or name in ("business", "business_trip", "business trip"):
        return "work"
    if name.startswith("education") or name.startswith("school"):
        return "education"
    if name.startswith("shop"):
        return "shop"
    if name.startswith("leisure"):
        return "leisure"
    if name.startswith("other") or name in ("escort", "errands"):
        return "other"
    return "exclude"


def simulation_destinations(region):
    from abmt_common import visit_data
    a,b=visit_data()
    return b



def plot_purposes(mz, sim, cohort):
    def shares(df):
        counts = df.groupby("purpose").weight.sum().reindex(PURPOSES, fill_value=0)
        return counts / counts.sum() * 100

    left, right = shares(mz), shares(sim)
    table = pd.DataFrame({
        "purpose": PURPOSES, "microcensus_weighted_pct": left.to_numpy(),
        "matsim_pct": right.to_numpy(),
    })
    table["difference_pp"] = table.matsim_pct - table.microcensus_weighted_pct
    table.to_csv(OUTPUT / f"activity_purpose_comparison_{cohort}.csv", index=False)

    xx = np.arange(len(PURPOSES))
    fig, ax = plt.subplots(figsize=(9.5, 4.5))
    ax.bar(xx - .19, table.microcensus_weighted_pct, width=.38, label="Mikrozensus (weighted)")
    ax.bar(xx + .19, table.matsim_pct, width=.38, label="MATSim 10% sample")
    ax.set_xticks(xx, table.purpose.str.title())
    ax.set_ylabel("Share of in-bound nonhome activity destinations (%)")
    ax.set_title("Activity purposes in Neuchatel – " + cohort)
    ax.legend()
    ax.grid(axis="y", alpha=.15)
    fig.tight_layout()
    fig.savefig(OUTPUT / f"activity_purpose_comparison_{cohort}.png", dpi=180)
    plt.close(fig)


def add_grid(df, xmin, ymin, size):
    result = df.copy()
    result["ix"] = np.floor((result.x - xmin) / size).astype(int)
    result["iy"] = np.floor((result.y - ymin) / size).astype(int)
    return result


def grid_table(mz, sim, boundary, size):
    xmin, ymin, _, _ = boundary.bounds
    xmin = np.floor(xmin/size)*size
    ymin = np.floor(ymin/size)*size
    mz_grid = add_grid(mz, xmin, ymin, size)
    sim_grid = add_grid(sim, xmin, ymin, size)
    mz_counts = mz_grid.groupby(["ix", "iy"]).agg(
        mz_activity_count=("person_id", "size"),
        mz_respondents=("person_id", "nunique"),
        mz_activity_weight=("weight", "sum"),
    ).reset_index()
    sim_counts = sim_grid.groupby(["ix", "iy"]).agg(
        sim_activity_count=("person_id", "size"),
        sim_people=("person_id", "nunique"),
    ).reset_index()
    table = mz_counts.merge(sim_counts, on=["ix", "iy"], how="outer").fillna(0)
    table["mz_share_pct"] = 100 * table.mz_activity_weight / mz.weight.sum()
    table["sim_share_pct"] = 100 * table.sim_activity_count / len(sim)
    table["difference_pp"] = table.sim_share_pct - table.mz_share_pct
    table["comparable"] = (table.mz_respondents >= MIN_MZ) & (table.sim_people >= MIN_MATSIM)
    public = table[["ix", "iy", "comparable", "mz_share_pct", "sim_share_pct", "difference_pp"]].copy()
    public.loc[~public.comparable, ["mz_share_pct", "sim_share_pct", "difference_pp"]] = np.nan
    return table, public


def polygon_parts(geometry):
    if geometry.geom_type == "Polygon":
        yield geometry
    elif geometry.geom_type in ("MultiPolygon", "GeometryCollection"):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def draw_polygon(ax, geometry, color):
    for part in polygon_parts(geometry):
        ax.add_patch(patches.Polygon(
            np.asarray(part.exterior.coords), facecolor=color,
            edgecolor="#E4E7EB", linewidth=.25,
        ))
        for hole in part.interiors:
            ax.add_patch(patches.Polygon(
                np.asarray(hole.coords), facecolor="white", edgecolor="white", linewidth=0,
            ))


def plot_grid(table, boundary, cohort, size, suffix=""):
    xmin, ymin, xmax, ymax = boundary.bounds
    xmin = np.floor(xmin/size)*size
    ymin = np.floor(ymin/size)*size
    cells = {(int(row.ix), int(row.iy)): row for row in table.itertuples(index=False)}
    valid = table.loc[table.comparable]
    maximum_share = max(1.0, float(valid[["mz_share_pct", "sim_share_pct"]].to_numpy().max())) if len(valid) else 1.0
    maximum_diff = max(1.0, float(valid.difference_pp.abs().max())) if len(valid) else 1.0
    share_norm = colors.Normalize(0, maximum_share)
    difference_norm = colors.TwoSlopeNorm(vmin=-maximum_diff, vcenter=0, vmax=maximum_diff)
    share_colors = plt.get_cmap("viridis")
    difference_colors = plt.get_cmap("coolwarm")

    fig, axes = plt.subplots(1, 3, figsize=(17, 5.5), constrained_layout=True)
    titles = ("Mikrozensus (weighted)", "MATSim (10% sample)", "MATSim − Mikrozensus")
    variables = ("mz_share_pct", "sim_share_pct", "difference_pp")
    for ax, title, variable in zip(axes, titles, variables):
        for ix in range(int(np.floor((xmax - xmin) / size)) + 1):
            for iy in range(int(np.floor((ymax - ymin) / size)) + 1):
                cell = box(xmin + ix * size, ymin + iy * size,
                           xmin + (ix + 1) * size, ymin + (iy + 1) * size)
                clipped = cell.intersection(boundary)
                if clipped.is_empty or clipped.area == 0:
                    continue
                row = cells.get((ix, iy))
                if row is None:
                    color = "#F0F1F3"
                elif not row.comparable:
                    color = "#CFD4DB"
                elif variable == "difference_pp":
                    color = difference_colors(difference_norm(getattr(row, variable)))
                else:
                    color = share_colors(share_norm(getattr(row, variable)))
                draw_polygon(ax, clipped, color)
        for part in polygon_parts(boundary):
            xx, yy = part.exterior.xy
            ax.plot(xx, yy, linewidth=.9, color="#334155")
        pad = max(xmax - xmin, ymax - ymin) * .035
        ax.set_xlim(xmin - pad, xmax + pad)
        ax.set_ylim(ymin - pad, ymax + pad)
        ax.set_aspect("equal")
        ax.xaxis.set_major_formatter(ticker.FuncFormatter(lambda value, _: f"{value/1000:.1f}"))
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda value, _: f"{value/1000:.1f}"))
        ax.tick_params(labelsize=8)
        ax.set_title(title, fontsize=12)
        ax.set_xlabel("LV95 Easting (km)", fontsize=9)
        if ax is axes[0]:
            ax.set_ylabel("LV95 Northing (km)", fontsize=9)
    fig.colorbar(plt.cm.ScalarMappable(norm=share_norm, cmap=share_colors),
                 ax=axes[:2], shrink=.64, pad=.015,
                 label="Share of all in-bound activity destinations (%)")
    fig.colorbar(plt.cm.ScalarMappable(norm=difference_norm, cmap=difference_colors),
                 ax=axes[2], shrink=.64, pad=.015, label="Difference in percentage points")
    fig.suptitle(f"Nonhome activity destinations – Neuchatel, {cohort} | {size // 1000} km grid", fontsize=13)
    fig.text(.5, .01,
             "Gray: too few distinct Mikrozensus respondents or MATSim persons; light gray: no destinations. "
             "All cells clipped to study boundary.", ha="center", fontsize=9)
    fig.savefig(OUTPUT / f"activity_location_grid_{cohort}{suffix}.png", dpi=180, bbox_inches="tight")
    plt.close(fig)


def analyze(mz, sim, boundary, cohort, age_min=6):
    mz = mz.loc[mz.age >= age_min]
    sim = sim.loc[sim.age >= age_min]
    if mz.empty or sim.empty:
        return
    plot_purposes(mz, sim, cohort)
    grid_sizes = [GRID_SIZE]
    for size in grid_sizes:
        table, public = grid_table(mz, sim, boundary, size)
        suffix = "" if size == GRID_SIZE else f"_{size // 1000}km"
        public.to_csv(OUTPUT / f"activity_location_grid_{cohort}{suffix}.csv", index=False)
        plot_grid(table, boundary, cohort, size, suffix)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    boundary, prepared = read_boundary()
    residents = survey_residents(prepared)
    mz = survey_destinations(residents, prepared)
    sim = simulation_destinations(prepared)
    analyze(mz, sim, boundary, "all")
    analyze(mz, sim, boundary, "age65plus", age_min=65)


if __name__ == "__main__":
    main()
