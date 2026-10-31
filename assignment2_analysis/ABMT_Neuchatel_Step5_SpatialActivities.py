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
MIN_MZ = 10
MIN_MATSIM = 15
PURPOSES = ("work", "education", "shop", "leisure", "other")
MZ_PURPOSES = {
    1: "interaction", 2: "work", 3: "education", 4: "shop",
    5: "other", 6: "work", 7: "work", 8: "leisure",
    9: "other", 10: "other", 11: "home", 12: "unknown", 13: "outside",
}


def find_file(filename, folder):
    matches = sorted(BASE.rglob(filename))
    preferred = [p for p in matches if folder.lower() in str(p.parent).lower()]
    files = preferred or matches
    if not files:
        raise FileNotFoundError(f"Missing {filename} in {BASE}")
    return files[0]


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


def survey_destinations(people, region):
    ways = pd.read_csv(
        find_file("wege.csv", "Mikrozensus"), encoding="latin1", dtype={"HHNR": "string"},
        usecols=["HHNR", "WEGNR", "wzweck1", "wzweck2", "Z_X_CH1903", "Z_Y_CH1903"]
    )
    ways = ways.loc[ways.HHNR.isin(people.HHNR)].drop_duplicates(["HHNR", "WEGNR"])
    ways = ways.merge(people, on="HHNR", validate="many_to_one")
    columns = ["wzweck1", "wzweck2", "Z_X_CH1903", "Z_Y_CH1903"]
    ways[columns] = ways[columns].apply(pd.to_numeric, errors="coerce")
    ways["purpose"] = ways.wzweck1.map(MZ_PURPOSES).fillna("unknown")
    ways.loc[ways.wzweck2 > 1, "purpose"] = "home"
    ways = ways.loc[
        ways.purpose.isin(PURPOSES)
        & ways.Z_X_CH1903.between(400_000, 900_000)
        & ways.Z_Y_CH1903.between(0, 400_000)
    ].copy()
    converter = Transformer.from_crs(21781, 2056, always_xy=True)
    xx, yy = converter.transform(ways.Z_X_CH1903.to_numpy(), ways.Z_Y_CH1903.to_numpy())
    ways["x"], ways["y"] = xx, yy
    inside = [region.covers(Point(x, y)) for x, y in zip(xx, yy)]
    return ways.loc[inside, ["HHNR", "age", "weight", "purpose", "x", "y"]].rename(
        columns={"HHNR": "person_id"}
    )


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
    records = []
    path = find_file("neuchatel_population.xml.gz", "Neuchatel_10pct")
    with gzip.open(path, "rb") as stream:
        root = None
        for event, person in ET.iterparse(stream, events=("start", "end")):
            if root is None:
                root = person
            if event != "end" or person.tag.rsplit("}", 1)[-1] != "person":
                continue

            attrs = {}
            for child in person:
                if child.tag.rsplit("}", 1)[-1] == "attributes":
                    attrs.update({a.get("name"): (a.text or "").strip() for a in child})
            excluded = any(
                str(attrs.get(key, "")).lower() in ("true", "yes", "1")
                for key in ("isOutside", "isFreight", "isCrossBorder", "isExternalFR")
            ) or str(attrs.get("subpopulation", "")).lower() in (
                "freight", "crossborder", "outside"
            )
            age = number(attrs.get("age"))
            x_home = number(attrs.get("home_x") or attrs.get("home_coordinate_x"))
            y_home = number(attrs.get("home_y") or attrs.get("home_coordinate_y"))
            if excluded or age is None or not 6 <= age <= 120 or x_home is None or y_home is None:
                root.clear()
                continue
            if not region.covers(Point(x_home, y_home)):
                root.clear()
                continue

            plans = [p for p in person if p.tag.rsplit("}", 1)[-1] == "plan"]
            selected = next(
                (p for p in plans if p.get("selected", "").lower() in ("yes", "true", "1")),
                plans[0] if plans else None,
            )
            if selected is not None:
                for activity in selected.iter():
                    if activity.tag.rsplit("}", 1)[-1] not in ("act", "activity"):
                        continue
                    purpose = activity_type(activity.get("type"))
                    if purpose not in PURPOSES:
                        continue
                    x, y = number(activity.get("x")), number(activity.get("y"))
                    if x is not None and y is not None and region.covers(Point(x, y)):
                        records.append((person.get("id"), int(age), 1.0, purpose, x, y))
            root.clear()

    if not records:
        raise ValueError("No matching MATSim activity destinations")
    return pd.DataFrame(records, columns=["person_id", "age", "weight", "purpose", "x", "y"])


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
        ax.xaxis.set_major_formatter(ticker.FuncFormatter(lambda value, _: f"{value/1e6:.2f}"))
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda value, _: f"{value/1e6:.2f}"))
        ax.tick_params(labelsize=8)
        ax.set_title(title, fontsize=12)
        ax.set_xlabel("LV95 Easting (million m)", fontsize=9)
        if ax is axes[0]:
            ax.set_ylabel("LV95 Northing (million m)", fontsize=9)
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
        if age_min >= 65 and size == GRID_SIZE:
            comparable = table.loc[table.comparable, ["ix", "iy"]]
            selected = add_grid(mz, *boundary.bounds[:2], size).merge(
                comparable, on=["ix", "iy"]
            )
            coverage = 100 * selected.weight.sum() / mz.weight.sum()
            if len(comparable) < 3 or coverage < 70:
                grid_sizes.append(20_000)


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
