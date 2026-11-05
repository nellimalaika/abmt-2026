#!/usr/bin/env python3
'Internal reconciled reader/utility derived from the reviewed analysis scripts. See README.md and abmt_common.METHOD for the active definitions.'

import argparse
from collections import Counter
import gzip
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import _abmt_age as baseline
import matplotlib.pyplot as plt
from matplotlib.colors import Normalize
from matplotlib.path import Path as PlotPath
from matplotlib.patches import PathPatch
from matplotlib.ticker import FuncFormatter
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import Point, box
from shapely.geometry.polygon import orient


def read_population_homes(path, boundary, extra_attributes=()):
    # validate_age.py discards home coordinates after filtering. Retain them
    # here using the same selection and coordinate priority; the old script
    # and its age bins do not need to be changed.
    if set(extra_attributes) & {"x", "y", "age", "weight"}:
        raise ValueError("Extra XML attributes must not replace resident coordinates, age or weight.")
    records, seen = [], set()
    funnel, selected_flags = Counter(), Counter()
    with gzip.open(path, "rb") as stream:
        root = None
        for event, person in ET.iterparse(stream, events=("start", "end")):
            if root is None:
                root = person
            if event != "end" or person.tag.rsplit("}", 1)[-1] != "person":
                continue
            funnel["persons_total"] += 1
            attrs = {item.get("name"): (item.text or "").strip()
                     for section in person if section.tag.rsplit("}", 1)[-1] == "attributes"
                     for item in section if item.tag.rsplit("}", 1)[-1] == "attribute"}
            identifier = person.get("id")
            if not identifier or identifier in seen:
                raise ValueError("Synthetic person IDs are missing or duplicated; inspect locally.")
            seen.add(identifier)
            is_freight = (baseline.flag(attrs.get("isFreight"))
                          or attrs.get("subpopulation", "").casefold() == "freight"
                          or attrs.get("type", "").casefold() == "truck")
            if is_freight:
                funnel["excluded_freight"] += 1
                root.clear()
                continue
            home = [(baseline.number(attrs.get(a)), baseline.number(attrs.get(b))) for a, b in
                    [("home_coordinate_x", "home_coordinate_y"), ("home_x", "home_y")]]
            first_valid = all(value is not None for value in home[0])
            x, y = home[0 if first_valid else 1]
            if x is None or y is None or not (2_000_000 <= x <= 3_000_000 and 1_000_000 <= y <= 1_400_000):
                funnel["excluded_missing_or_invalid_lv95_home"] += 1
            elif not boundary.covers(Point(x, y)):
                funnel["excluded_home_outside_boundary"] += 1
            else:
                age = baseline.number(attrs.get("age"))
                if age is None or not 0 <= age <= 120 or not age.is_integer():
                    funnel["excluded_invalid_age"] += 1
                elif age < 6:
                    funnel["excluded_under_6"] += 1
                else:
                    record = {"x": x, "y": y, "age": age, "weight": 1.0}
                    # Preserve None (attribute absent) separately from XML
                    # text such as the literal string 'None'.
                    record.update({name: attrs.get(name) for name in extra_attributes})
                    records.append(record)
                    for name in baseline.MODEL_FLAGS:
                        selected_flags[name] += int(baseline.flag(attrs.get(name)))
            root.clear()
    funnel["eligible_residents"] = len(records)
    excluded = sum(value for key, value in funnel.items() if key.startswith("excluded_"))
    if excluded + len(records) != funnel["persons_total"]:
        raise AssertionError("Synthetic population selection does not reconcile.")
    if not records:
        raise ValueError("No eligible synthetic residents after filtering.")
    return pd.DataFrame(records), {"funnel": dict(funnel),
                                  "flags_among_eligible_residents": dict(selected_flags)}


def make_grid(polygon, cell_size):
    xmin, ymin, xmax, ymax = polygon.bounds
    geometry = {}
    rows = []
    for ix in range(math.floor(xmin / cell_size), math.floor(xmax / cell_size) + 1):
        for iy in range(math.floor(ymin / cell_size), math.floor(ymax / cell_size) + 1):
            clipped = polygon.intersection(box(ix * cell_size, iy * cell_size,
                                               (ix + 1) * cell_size, (iy + 1) * cell_size))
            if clipped.is_empty or clipped.area <= 0:
                continue
            geometry[(ix, iy)] = clipped
            rows.append({"cell_id": f"{ix}_{iy}", "ix": ix, "iy": iy,
                         "grid_xmin_m": ix * cell_size, "grid_ymin_m": iy * cell_size,
                         "area_inside_boundary_km2": clipped.area / 1_000_000})
    if not rows:
        raise ValueError("The study boundary produced no grid cells.")
    return pd.DataFrame(rows), geometry


def assign_cells(frame, geometry, cell_size):
    keys = []
    for x, y in zip(frame.x, frame.y):
        key = (math.floor(x / cell_size), math.floor(y / cell_size))
        if key not in geometry:
            # A point on the outer boundary can floor into a cell whose
            # intersection has zero area. Give such points a deterministic
            # adjacent cell that covers them, consistently in both datasets.
            point = Point(x, y)
            candidates = sorted((key[0] + dx, key[1] + dy)
                                for dx in (-1, 0, 1) for dy in (-1, 0, 1))
            candidates = [candidate for candidate in candidates
                          if candidate in geometry and geometry[candidate].covers(point)]
            if not candidates:
                raise ValueError("An eligible resident could not be assigned to a grid cell.")
            key = candidates[0]
        keys.append(key)
    return keys


def aggregate_grid(survey, population, grid, geometry, cell_size, min_survey_n):
    table = grid.copy()
    for name, frame in [("survey", survey), ("synthetic", population)]:
        weights = frame.weight.to_numpy(dtype=float)
        if not len(weights) or not (np.isfinite(weights) & (weights > 0)).all():
            raise ValueError("Expected nonempty cohorts with positive finite weights.")
        keys = assign_cells(frame, geometry, cell_size)
        grouped = pd.DataFrame({"ix": [key[0] for key in keys], "iy": [key[1] for key in keys],
                                "weight": weights}).groupby(["ix", "iy"], as_index=False).agg(
                                    n=("weight", "size"), weight_sum=("weight", "sum"))
        grouped = grouped.rename(columns={"n": name + "_n", "weight_sum": name + "_weight_sum"})
        table = table.merge(grouped, on=["ix", "iy"], how="left", validate="one_to_one")
        for column in [name + "_n", name + "_weight_sum"]:
            table[column] = table[column].fillna(0)
        table[name + "_n"] = table[name + "_n"].astype(int)
        table[name + "_pct"] = 100 * table[name + "_weight_sum"] / weights.sum()
        if table[name + "_n"].sum() != len(frame) or not np.isclose(table[name + "_pct"].sum(), 100):
            raise AssertionError("Grid totals do not reconcile with the complete resident cohort.")
    table = table.rename(columns={"survey_pct": "survey_weighted_pct"})
    table["difference_pp"] = table.synthetic_pct - table.survey_weighted_pct
    table["difference_shown"] = table.survey_n.ge(min_survey_n)
    table["difference_pp_displayed"] = table.difference_pp.where(table.difference_shown)
    if not np.isclose(table.difference_pp.sum(), 0, atol=1e-8):
        raise AssertionError("Unmasked spatial differences do not sum to zero.")
    return table


def polygon_parts(geometry):
    if geometry.geom_type == "Polygon":
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from polygon_parts(part)


def polygon_path(geometry):
    # Opposite ring orientations preserve holes in the boundary/cell geometry.
    vertices, codes = [], []
    for polygon in polygon_parts(geometry):
        polygon = orient(polygon, sign=1.0)
        for ring in [polygon.exterior, *polygon.interiors]:
            points = list(ring.coords)
            vertices.extend(points)
            codes.extend([PlotPath.MOVETO] + [PlotPath.LINETO] * (len(points) - 2) + [PlotPath.CLOSEPOLY])
    return PlotPath(np.asarray(vertices), np.asarray(codes, dtype=np.uint8))


def plot_maps(table, geometry, polygon, cell_size, min_survey_n, survey_n, synthetic_n, path):
    share_max = max(float(table.survey_weighted_pct.max()), float(table.synthetic_pct.max()))
    differences = table.loc[table.difference_shown, "difference_pp"]
    limit = max(float(differences.abs().max()), 0.1) if len(differences) else 1.0
    share_norm, diff_norm = Normalize(0, share_max), Normalize(-limit, limit)
    share_cmap, diff_cmap = plt.get_cmap("YlGnBu"), plt.get_cmap("RdBu_r")
    paths = {key: polygon_path(value) for key, value in geometry.items()}
    outline = polygon_path(polygon)
    fig = plt.figure(figsize=(15.5, 7.0), layout="constrained")
    spec = fig.add_gridspec(3, 3, height_ratios=[1, 0.045, 0.18])
    axes = [fig.add_subplot(spec[0, i]) for i in range(3)]
    titles = ["MZMV 2015 (WP weighted)", "Synthetic population", "Synthetic minus survey"]
    columns = ["survey_weighted_pct", "synthetic_pct", "difference_pp"]
    xmin, ymin, xmax, ymax = polygon.bounds
    margin = max(xmax - xmin, ymax - ymin) * 0.04
    for i, (ax, title, column) in enumerate(zip(axes, titles, columns)):
        cmap, norm = (diff_cmap, diff_norm) if i == 2 else (share_cmap, share_norm)
        for row in table.itertuples(index=False):
            masked = i == 2 and not row.difference_shown
            ax.add_patch(PathPatch(paths[(row.ix, row.iy)],
                                   facecolor="#ECECEC" if masked else cmap(norm(getattr(row, column))),
                                   edgecolor="#AAAAAA" if masked else "white",
                                   linewidth=0.35, hatch="///" if masked else None))
        ax.add_patch(PathPatch(outline, facecolor="none", edgecolor="#222222", linewidth=0.8))
        ax.set(xlim=(xmin - margin, xmax + margin), ylim=(ymin - margin, ymax + margin),
               title=title, xlabel="Easting (LV95 km)")
        ax.set_aspect("equal", adjustable="box")
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, pos: f"{value / 1000:.0f}"))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, pos: f"{value / 1000:.0f}"))
        ax.tick_params(labelsize=8)
        if i == 0:
            ax.set_ylabel("Northing (LV95 km)")
        else:
            ax.tick_params(labelleft=False)
        color_ax = fig.add_subplot(spec[1, i])
        label = "Difference in percentage points" if i == 2 else "Share of all eligible residents (%)"
        fig.colorbar(plt.cm.ScalarMappable(norm=norm, cmap=cmap), cax=color_ax,
                     orientation="horizontal", label=label)
    footer = fig.add_subplot(spec[2, :])
    footer.axis("off")
    footer.text(0, 0.92,
                f"Matched home boundary; ages 6-120; weekday survey. Survey n={survey_n:,}; synthetic n={synthetic_n:,}.\n"
                f"Both share maps use the same colour scale and complete-cohort denominators. "
                f"Hatching: survey n < {min_survey_n}; difference masked.\n"
                "Positive differences indicate a larger synthetic share in that cell. "
                "The support threshold is a heuristic, not a significance test.",
                va="top", fontsize=9, linespacing=1.6)
    fig.suptitle(f"Neuchatel: residential distribution on a {cell_size / 1000:g} km grid", fontsize=15)
    fig.savefig(path, dpi=180)
    plt.close(fig)




