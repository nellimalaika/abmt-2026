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

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import shapefile
from pyproj import CRS, Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform, unary_union
from shapely.prepared import prep


AGE_LABELS = ["6-17", "18-34", "35-64", "65+"]
AGE_EDGES = [5, 17, 34, 64, 120]
SURVEY_FIELDS = ["HHNR", "alter", "WP", "tag", "f42100e",
                 "f41610a", "f41610b", "f41610c"]
HOUSEHOLD_FIELDS = ["HHNR", "W_X_CH1903", "W_Y_CH1903", "f30100"]
MODEL_FLAGS = ["isFreight", "isOutside", "isCrossBorder", "isExternalFR"]
PT_CODES = {"none", "ga", "ht", "va", "va+ht"}


def unique_file(root, name):
    from abmt_paths import find_input_file
    return find_input_file(root, name)


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def flag(value):
    return str(value or "").strip().casefold() in {"true", "yes", "1"}


def car_count_status(value):
    if value is None or str(value).strip() == "":
        return "missing"
    count = number(value)
    if count is None:
        return "non_numeric_or_non_finite"
    if count < 0:
        return f"negative_code_{int(count)}" if count in {-99, -98, -97, -1} else "other_negative"
    if not count.is_integer():
        return "non_integer"
    return "0_cars" if count == 0 else "1plus_cars"


def model_code(value, allowed):
    code = str(value or "").strip().casefold()
    return code if code in allowed else "MISSING" if not code else "UNRECOGNISED"


def read_boundary(path):
    projection = path.with_suffix(".prj")
    source = CRS.from_wkt(projection.read_text(encoding="utf-8-sig")).to_2d()
    with shapefile.Reader(str(path)) as reader:
        polygons = [shape(item.__geo_interface__) for item in reader.shapes()]
    polygon = unary_union(polygons)
    if source != CRS.from_epsg(2056):
        converter = Transformer.from_crs(source, 2056, always_xy=True)
        polygon = transform(converter.transform, polygon)
    if polygon.is_empty or not polygon.is_valid:
        raise ValueError("Study boundary is empty or invalid.")
    return prep(polygon), {"source_crs": source.to_string(), "analysis_crs": "EPSG:2056"}


def read_survey(base, boundary):
    households = pd.read_csv(unique_file(base, "haushalte.csv"), encoding="latin1",
                             dtype={"HHNR": "string"}, usecols=HOUSEHOLD_FIELDS)
    persons = pd.read_csv(unique_file(base, "zielpersonen.csv"), encoding="latin1",
                         dtype={"HHNR": "string"}, usecols=SURVEY_FIELDS)
    if (households.HHNR.isna().any() or persons.HHNR.isna().any()
            or households.HHNR.duplicated().any() or persons.HHNR.duplicated().any()):
        raise ValueError("Survey linkage identifiers are missing or non-unique; inspect locally.")
    frame = persons.merge(households, on="HHNR", how="left", validate="one_to_one", indicator=True)
    funnel = {"target_persons_total": len(frame)}

    def retain(mask, label):
        nonlocal frame
        before = len(frame)
        frame = frame.loc[mask].copy()
        funnel[label] = {"excluded": before - len(frame), "retained": len(frame)}

    retain(frame._merge.eq("both"), "household_link")
    for column in set(SURVEY_FIELDS + HOUSEHOLD_FIELDS) - {"HHNR"}:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    retain(frame.tag.between(1, 5), "weekday")
    retain(np.isfinite(frame.WP) & frame.WP.gt(0), "positive_person_weight")
    retain(frame.alter.between(0, 120) & frame.alter.mod(1).eq(0), "valid_age")
    retain(frame.alter.ge(6), "age_6plus")
    valid_coords = (frame.W_X_CH1903.between(400_000, 900_000)
                    & frame.W_Y_CH1903.between(0, 400_000))
    retain(valid_coords, "valid_lv03_home")
    converter = Transformer.from_crs(21781, 2056, always_xy=True)
    x, y = converter.transform(frame.W_X_CH1903.to_numpy(), frame.W_Y_CH1903.to_numpy())
    inside = [boundary.covers(Point(float(a), float(b))) for a, b in zip(x, y)]
    retain(np.asarray(inside, dtype=bool), "home_inside_boundary")
    if frame.empty:
        raise ValueError("No eligible survey respondents after filtering.")
    frame["age"] = frame.alter
    frame["weight"] = frame.WP
    return frame, funnel


def read_population(path, boundary):
    records, seen = [], set()
    funnel, selected_flags = Counter(), Counter()
    coordinate_sources = Counter()
    coordinate_disagreements = 0
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
            is_freight = (flag(attrs.get("isFreight"))
                          or attrs.get("subpopulation", "").casefold() == "freight"
                          or attrs.get("type", "").casefold() == "truck")
            if is_freight:
                funnel["excluded_freight"] += 1
                root.clear()
                continue
            home = [(number(attrs.get(a)), number(attrs.get(b))) for a, b in
                    [("home_coordinate_x", "home_coordinate_y"), ("home_x", "home_y")]]
            valid = [all(v is not None for v in pair) for pair in home]
            if all(valid) and max(abs(home[0][k] - home[1][k]) for k in (0, 1)) > 1:
                coordinate_disagreements += 1
            source = 0 if valid[0] else 1
            x, y = home[source]
            if x is None or y is None or not (2_000_000 <= x <= 3_000_000 and 1_000_000 <= y <= 1_400_000):
                funnel["excluded_missing_or_invalid_lv95_home"] += 1
            elif not boundary.covers(Point(x, y)):
                funnel["excluded_home_outside_boundary"] += 1
            else:
                age = number(attrs.get("age"))
                if age is None or not 0 <= age <= 120 or not age.is_integer():
                    funnel["excluded_invalid_age"] += 1
                elif age < 6:
                    funnel["excluded_under_6"] += 1
                else:
                    records.append({"age": age, "weight": 1.0,
                                    "car_count_status": car_count_status(attrs.get("numberOfCars")),
                                    "carAvail": model_code(attrs.get("carAvail"), {"always", "never"}),
                                    "subscriptions": model_code(attrs.get("subscriptions"), PT_CODES)})
                    coordinate_sources[["home_coordinate", "home_xy"][source]] += 1
                    for name in MODEL_FLAGS:
                        selected_flags[name] += int(flag(attrs.get(name)))
            root.clear()
    funnel["eligible_residents"] = len(records)
    exclusions = sum(n for key, n in funnel.items() if key.startswith("excluded_"))
    if exclusions + len(records) != funnel["persons_total"]:
        raise AssertionError("Synthetic population funnel does not reconcile.")
    if not records:
        raise ValueError("No eligible synthetic residents after filtering.")
    return pd.DataFrame(records), {"funnel": dict(funnel),
                                  "flags_among_eligible_residents": dict(selected_flags),
                                  "home_coordinate_sources_among_eligible": dict(coordinate_sources),
                                  "home_coordinate_disagreements_over_1m_nonfreight": coordinate_disagreements}


def compare_ages(survey, population):
    def summarize(frame):
        bins = pd.cut(frame.age, AGE_EDGES, labels=AGE_LABELS)
        if bins.isna().any():
            raise ValueError("An eligible age falls outside the declared bins.")
        grouped = frame.assign(age_group=bins).groupby("age_group", observed=False)
        counts = grouped.size().reindex(AGE_LABELS, fill_value=0)
        weights = grouped.weight.sum().reindex(AGE_LABELS, fill_value=0)
        return counts, weights, 100 * weights / weights.sum()
    mz_n, mz_w, mz_pct = summarize(survey)
    sim_n, _, sim_pct = summarize(population)
    table = pd.DataFrame({"age_group": AGE_LABELS, "survey_n": mz_n.to_numpy(),
                          "survey_weight_sum": mz_w.to_numpy(), "survey_weighted_pct": mz_pct.to_numpy(),
                          "synthetic_n": sim_n.to_numpy(), "synthetic_pct": sim_pct.to_numpy()})
    table["difference_pp"] = table.synthetic_pct - table.survey_weighted_pct
    if not np.allclose([table.survey_weighted_pct.sum(), table.synthetic_pct.sum()], 100):
        raise AssertionError("Age proportions do not sum to 100 percent.")
    return table


def diagnostics(survey, population):
    survey_codes = {}
    for column, allowed in {"f42100e": {-99, -98, -97, 1, 2, 3},
                            "f41610a": {-99, -98, -97, 1, 2},
                            "f41610b": {-99, -98, -97, 1, 2},
                            "f41610c": {-99, -98, -97, 1, 2}}.items():
        labels = survey[column].map(lambda x: str(int(x)) if pd.notna(x) and x in allowed
                                    else "MISSING" if pd.isna(x) else "UNRECOGNISED")
        rows = survey.assign(code=labels).groupby("code", as_index=False).agg(
            persons=("weight", "size"), weight_sum=("weight", "sum"))
        survey_codes[column] = rows.to_dict(orient="records")
    survey_car = survey.f30100.map(car_count_status)
    counts = population.groupby(["car_count_status", "carAvail"], as_index=False).size()
    counts = counts.rename(columns={"size": "persons"})
    return {"scope": "Same eligible residents used for the age comparison; category checks only.",
            "survey_response_codes": survey_codes,
            "survey_household_car_count_status": survey_car.value_counts().to_dict(),
            "synthetic_joint_car_codes": counts.to_dict(orient="records"),
            "synthetic_subscription_codes": population.subscriptions.value_counts().to_dict()}


def plot_comparison(table, survey_n, synthetic_n, path):
    x = np.arange(len(table))
    fig, ax = plt.subplots(figsize=(8.5, 4.8))
    ax.bar(x - 0.19, table.survey_weighted_pct, 0.38, color="#237A8A", label="MZMV 2015 (WP weighted)")
    ax.bar(x + 0.19, table.synthetic_pct, 0.38, color="#DF8D39", label="Synthetic population (10% sample)")
    ax.set(xticks=x, xticklabels=AGE_LABELS, xlabel="Age group", ylabel="Share of eligible residents (%)")
    ax.set_title("Neuchatel: age composition of residents", pad=32)
    ax.grid(axis="y", alpha=0.2)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="lower center", bbox_to_anchor=(0.5, 1.01), ncol=2, fontsize=8)
    fig.text(0.5, 0.015, f"Matched home boundary; weekday survey | Survey n={survey_n:,}; synthetic n={synthetic_n:,}",
             ha="center", fontsize=8)
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    fig.savefig(path, dpi=180)
    plt.close(fig)




