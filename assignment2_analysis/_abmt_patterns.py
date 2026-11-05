#!/usr/bin/env python3
'Internal reconciled reader/utility derived from the reviewed analysis scripts. See README.md and abmt_common.METHOD for the active definitions.'

import argparse
from collections import Counter
import csv
import gzip
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import xml.etree.ElementTree as ET

import _abmt_modes as core
import _abmt_car as car
import _abmt_spatial as spatial
baseline, trips = core.baseline, core.trips
Audit, code_number = core.Audit, core.code_number
survey_mode, safe_survey_code, model_mode = core.survey_mode, core.safe_survey_code, core.model_mode
import matplotlib.pyplot as plt
from matplotlib.colors import Normalize
from matplotlib.patches import PathPatch
from matplotlib.ticker import FuncFormatter
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import Point


MODES, MODE_LABELS = core.MODES, core.LABELS
SURVEY_COLUMNS = core.SURVEY_COLUMNS + ["f51100"]
OUTPUT_COLUMNS = core.OUTPUT_COLUMNS + ["dep_time"]
RECORD_COLUMNS = core.RECORD_COLUMNS + [
    "person_key", "age_group", "personal", "distance_m", "distance_group",
    "home_x", "home_y", "start_x", "start_y", "end_x", "end_y", "departure_seconds", "time_group"]
AGE_GROUPS = ["6-17", "18-34", "35-64", "65+"]
CAR_GROUPS = ["available", "unavailable", "unknown"]
DISTANCE_GROUPS = ["under_1km", "1_2km", "2_5km", "5_10km", "10plus_km",
                   "loop_or_coincident", "unknown_distance"]
TIME_GROUPS = ["00_06", "06_09", "09_12", "12_15", "15_18", "18_24", "24plus", "unknown_time"]
STRATUM_LABELS = {key: key for key in AGE_GROUPS}
STRATUM_LABELS.update({"available": "Car available", "unavailable": "Car unavailable / inapplicable",
    "unknown": "Car availability unknown", "under_1km": "<1 km", "1_2km": "1-<2 km",
    "2_5km": "2-<5 km", "5_10km": "5-<10 km", "10plus_km": "10 km or more",
    "loop_or_coincident": "Loop / coincident endpoints", "unknown_distance": "Unknown distance",
    "00_06": "00:00-06:00", "06_09": "06:00-09:00", "09_12": "09:00-12:00",
    "12_15": "12:00-15:00", "15_18": "15:00-18:00", "18_24": "18:00-24:00",
    "24plus": "After 24:00", "unknown_time": "Unknown departure time"})
SHORT_MODES = ["Walk", "Bike", "Driver", "Passenger", "PT", "Other", "Unknown"]


def age_group(age):
    value = baseline.number(age)
    if value is None or not value.is_integer() or not 6 <= value <= 120:
        raise ValueError("An eligible person has an invalid age.")
    return AGE_GROUPS[int(np.searchsorted([18, 35, 65], value, side="right"))]


def distance_group(distance, loop):
    if loop:
        return "loop_or_coincident"
    if distance is None or not math.isfinite(distance) or distance < 0:
        return "unknown_distance"
    if distance < 1.0:
        return "loop_or_coincident"
    return DISTANCE_GROUPS[int(np.searchsorted([1000, 2000, 5000, 10000], distance, side="right"))]


def departure_seconds(value, survey=False):
    if survey:
        minutes = baseline.number(value)
        return minutes * 60 if minutes is not None and minutes >= 0 else None
    text = str("" if value is None else value).strip()
    if ":" not in text:
        seconds = baseline.number(text)
        return seconds if seconds is not None and seconds >= 0 else None
    parts = text.split(":")
    if len(parts) not in {2, 3}:
        return None
    values = [baseline.number(item) for item in parts]
    if any(item is None for item in values):
        return None
    hours, minutes = values[:2]
    seconds = values[2] if len(values) == 3 else 0.
    if hours < 0 or not hours.is_integer() or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def time_group(seconds):
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "unknown_time"
    return TIME_GROUPS[int(np.searchsorted(np.asarray([6, 9, 12, 15, 18, 24]) * 3600, seconds, side="right"))]


def enrich_record(record, person, origin, destination, transformer=None, departure=None):
    # Called only after BOTH substantive endpoints have passed the existing
    # boundary and coordinate checks. Intermediate transfer points are not used.
    ox, oy = map(float, origin)
    dx, dy = map(float, destination)
    if transformer is not None:
        ox, oy = transformer.transform(ox, oy)
        dx, dy = transformer.transform(dx, dy)
    distance = float(np.hypot(dx - ox, dy - oy))
    record.update(person_key=person.person_key, age_group=person.age_group, personal=person.personal,
                  distance_m=distance, distance_group=distance_group(distance, record["loop"]),
                  home_x=float(person.home_x), home_y=float(person.home_y),
                  start_x=ox, start_y=oy, end_x=dx, end_y=dy,
                  departure_seconds=departure, time_group=time_group(departure))
    return record


def survey_person(person, rows, stage_lookup, boundary, transformer, audit):
    """Retain identifiable trips in partial diaries; never bridge sequence gaps."""
    records = []
    weight = float(person.weight)
    if rows.empty:
        audit.add("residents_without_trip_rows", weight)
        if code_number(person.f50200) != 2 and code_number(person.T_WEGE) != 0:
            audit.add("residents_without_confirmed_zero_or_trip_rows", weight)
        return records
    audit.add("residents_with_trip_rows", weight)
    audit.add("resident_survey_rows", weight * len(rows), len(rows))
    if code_number(person.f50200) == 2:
        audit.add("rows_excluded_nontravel_response_conflict", weight * len(rows), len(rows))
        return records
    order = pd.to_numeric(rows.WEGNR, errors="coerce")
    if not (np.isfinite(order) & order.gt(0) & order.mod(1).eq(0)).all() or order.duplicated().any():
        audit.add("rows_excluded_invalid_or_duplicate_trip_order", weight * len(rows), len(rows))
        return records
    rows = rows.assign(WEGNR=order).sort_values("WEGNR")
    domestic = baseline.number(person.T_WEGE)
    if domestic == 0:
        audit.add("rows_excluded_zero_domestic_count_conflict", weight * len(rows), len(rows))
        return records
    if domestic is not None and domestic > len(rows):
        audit.add("residents_with_fewer_rows_than_domestic_count", weight)
    pending, prior_number, previous_kind = [], None, None

    def finish(block, dangling=False):
        if not block:
            return
        audit.add("candidate_trips", weight)
        first, last = block[0], block[-1]
        origin, destination = first[1], last[2]
        uncertain = dangling or any(item[3] for item in block)
        # A transfer block must describe a connected journey. Do not fill gaps.
        for a, b in zip(block, block[1:]):
            xy_a = [baseline.number(a[0].Z_X_CH1903), baseline.number(a[0].Z_Y_CH1903)]
            xy_b = [baseline.number(b[0].S_X_CH1903), baseline.number(b[0].S_Y_CH1903)]
            if any(v is None for v in xy_a + xy_b) or np.hypot(xy_a[0]-xy_b[0], xy_a[1]-xy_b[1]) > 1:
                uncertain = True
                audit.add("discontinuous_transfer_blocks", weight)
        status = trips.endpoint_trip(origin, destination, uncertain)
        audit.add(status + "_trips", weight)
        if status != "internal":
            return
        categories, raw_codes = [], []
        for item in block:
            row = item[0]
            stages = stage_lookup.get((person.HHNR, int(row.WEGNR)), set())
            categories.append(survey_mode(row.wmittel, stages))
            raw_codes.append(safe_survey_code(row.wmittel))
            if code_number(row.wmittel) == 9 and categories[-1] == "unknown":
                audit.add("internal_car_main_rows_with_missing_or_mixed_role", weight)
        # No invented priority rule for mixed modes across separately recorded
        # Wege rows. Within an ordinary single row, wmittel already is main mode.
        category = categories[0] if len(set(categories)) == 1 else "unknown"
        if len(set(categories)) > 1:
            audit.add("internal_transfer_blocks_with_ambiguous_main_mode", weight)
        loop = any(code_number(item[0].wzweck2) == 3 for item in block)
        raw = raw_codes[0] if len(set(raw_codes)) == 1 else "MULTIROW_TRANSFER_BLOCK"
        records.append(enrich_record(dict(mode=category, weight=weight, loop=loop, code=raw), person,
                                     (first[0].S_X_CH1903, first[0].S_Y_CH1903),
                                     (last[0].Z_X_CH1903, last[0].Z_Y_CH1903), transformer,
                                     departure_seconds(first[0].f51100, survey=True)))

    for row in rows.itertuples(index=False):
        number = int(row.WEGNR)
        if prior_number is not None and number != prior_number + 1:
            finish(pending, dangling=True)
            pending, previous_kind = [], None
            audit.add("sequence_gaps_no_bridging", weight)
        purpose, return_code = code_number(row.wzweck1), code_number(row.wzweck2)
        if return_code in {2, 3}:
            purpose = 11
        kind = ("stage" if purpose == 1 else "outside" if purpose == 13
                else "main" if purpose in range(2, 13) else "unknown")
        origin = trips.point_status(row.S_X_CH1903, row.S_Y_CH1903, boundary, True, transformer)
        destination = trips.point_status(row.Z_X_CH1903, row.Z_Y_CH1903, boundary, True, transformer)
        if previous_kind == "outside":
            origin = "outside"
        if kind == "outside":
            destination = "outside"
        pending.append((row, origin, destination, kind == "unknown" or code_number(row.pseudo) != 1))
        if kind == "stage":
            audit.add("transfer_destinations_collapsed", weight)
        else:
            finish(pending)
            pending = []
        prior_number, previous_kind = number, kind
    finish(pending, dangling=True)
    return records

def read_survey(base, boundary):
    people, selection = baseline.read_survey(base, boundary)
    extra = pd.read_csv(baseline.unique_file(base, "zielpersonen.csv"), encoding="latin1",
                        dtype={"HHNR": "string"}, usecols=["HHNR", "f50200", "T_WEGE"])
    if extra.HHNR.isna().any() or extra.HHNR.duplicated().any():
        raise ValueError("Survey person linkage is missing or duplicated; inspect locally.")
    people = people.merge(extra, on="HHNR", how="left", validate="one_to_one")
    diary = pd.read_csv(baseline.unique_file(base, "wege.csv"), encoding="latin1",
                        dtype={"HHNR": "string"}, usecols=SURVEY_COLUMNS)
    if diary.HHNR.isna().any():
        raise ValueError("Survey trip person linkage is missing; inspect locally.")
    diary = diary.loc[diary.HHNR.isin(people.HHNR)].copy()
    stage_frame = pd.read_csv(baseline.unique_file(base, "etappen.csv"), encoding="latin1",
                              dtype={"HHNR": "string"}, usecols=["HHNR", "WEGNR", "f51300"])
    stage_frame = stage_frame.loc[stage_frame.HHNR.isin(people.HHNR)].copy()
    stage_frame["order"] = pd.to_numeric(stage_frame.WEGNR, errors="coerce")
    stage_frame = stage_frame.loc[np.isfinite(stage_frame.order) & stage_frame.order.gt(0)
                                  & stage_frame.order.mod(1).eq(0)]
    stage_frame["mode_code"] = stage_frame.f51300.map(code_number)
    lookup = stage_frame.groupby(["HHNR", "order"]).mode_code.agg(set).to_dict()
    grouped = dict(tuple(diary.groupby("HHNR", sort=False)))
    empty = diary.iloc[:0]
    audit = Audit()
    audit.add("eligible_residents", float(people.weight.sum()), len(people))
    transformer = Transformer.from_crs(21781, 2056, always_xy=True)
    people["person_key"] = people.HHNR
    people["age_group"] = people.age.map(age_group)
    people["personal"] = people.f42100e.map(lambda value: car.survey_access(value, "any_access"))
    people["home_x"], people["home_y"] = transformer.transform(people.W_X_CH1903.to_numpy(), people.W_Y_CH1903.to_numpy())
    records = []
    for person in people.itertuples(index=False):
        records.extend(survey_person(person, grouped.get(person.HHNR, empty), lookup,
                                     boundary, transformer, audit))
    return pd.DataFrame(records, columns=RECORD_COLUMNS), audit, selection

def read_residents(path, boundary):
    """Same XML cohort as validate_age; IDs stay in local memory only."""
    identifiers, seen, funnel, flags = {}, set(), Counter(), Counter()
    with gzip.open(path, "rb") as stream:
        root = None
        for event, person in ET.iterparse(stream, events=("start", "end")):
            if root is None:
                root = person
            if event != "end" or trips.local_name(person) != "person":
                continue
            funnel["persons_total"] += 1
            identifier = person.get("id")
            if not identifier or identifier in seen:
                raise ValueError("Synthetic person linkage is missing or duplicated; inspect locally.")
            seen.add(identifier)
            attrs = {item.get("name"): (item.text or "").strip()
                     for section in person if trips.local_name(section) == "attributes"
                     for item in section if trips.local_name(item) == "attribute"}
            home = [(baseline.number(attrs.get(a)), baseline.number(attrs.get(b))) for a, b in
                    [("home_coordinate_x", "home_coordinate_y"), ("home_x", "home_y")]]
            x, y = home[0 if all(v is not None for v in home[0]) else 1]
            age = baseline.number(attrs.get("age"))
            if (baseline.flag(attrs.get("isFreight")) or attrs.get("subpopulation", "").casefold() == "freight"
                    or attrs.get("type", "").casefold() == "truck"):
                funnel["excluded_freight"] += 1
            elif x is None or y is None or not (2_000_000 <= x <= 3_000_000 and 1_000_000 <= y <= 1_400_000):
                funnel["excluded_missing_or_invalid_lv95_home"] += 1
            elif not boundary.covers(Point(x, y)):
                funnel["excluded_home_outside_boundary"] += 1
            elif age is None or not 0 <= age <= 120 or not age.is_integer():
                funnel["excluded_invalid_age"] += 1
            elif age < 6:
                funnel["excluded_under_6"] += 1
            else:
                identifiers[identifier] = dict(person_key=identifier, age_group=age_group(age),
                                               personal=car.synthetic_access(attrs.get("carAvail", "").casefold()),
                                               home_x=x, home_y=y)
                for name in baseline.MODEL_FLAGS:
                    flags[name] += int(baseline.flag(attrs.get(name)))
            root.clear()
    funnel["eligible_residents"] = len(identifiers)
    if not identifiers:
        raise ValueError("No eligible synthetic residents.")
    if len(identifiers) + sum(v for k, v in funnel.items() if k.startswith("excluded_")) != funnel["persons_total"]:
        raise AssertionError("Synthetic resident selection does not reconcile.")
    return identifiers, {"selection": dict(funnel), "flags_among_eligible": dict(flags)}

def read_simulation(path, identifiers, boundary):
    records, seen_numbers, seen_ids, contributors = [], set(), set(), set()
    audit = Audit()
    audit.add("eligible_residents", float(len(identifiers)), len(identifiers))
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter=";")
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("Simulation trip header is missing or duplicated.")
        if not set(OUTPUT_COLUMNS).issubset(reader.fieldnames):
            raise ValueError("Simulation trip schema differs from the inspected schema; rerun the audit.")
        for row in reader:
            audit.add("all_output_rows", 1.)
            if None in row or any(row.get(key) is None for key in OUTPUT_COLUMNS):
                raise ValueError("Malformed simulation trip rows; no comparison saved. Inspect locally.")
            identifier = row["person"]
            if not identifier:
                raise ValueError("Simulation trip person linkage is missing; inspect locally.")
            if identifier not in identifiers:
                audit.add("rows_excluded_noncohort_person", 1.)
                continue
            audit.add("resident_output_rows", 1.)
            contributors.add(identifier)
            number = code_number(row["trip_number"])
            trip_id = row["trip_id"].strip()
            if number is None or number < 1 or not trip_id:
                raise ValueError("Resident simulation trip identifiers are invalid; inspect locally.")
            key, id_key = (identifier, number), (identifier, trip_id)
            if key in seen_numbers or id_key in seen_ids:
                raise ValueError("Duplicate resident simulation trips; no comparison saved. Inspect locally.")
            seen_numbers.add(key)
            seen_ids.add(id_key)
            start_type, end_type = [row[key].strip().casefold() for key in
                                    ("start_activity_type", "end_activity_type")]
            if "outside" in {start_type, end_type} or "border" in {start_type, end_type}:
                audit.add("excluded_outside_trips", 1.)
                continue
            if any(not kind or kind == "interaction" or kind.endswith(" interaction") for kind in (start_type, end_type)):
                audit.add("unresolved_trips", 1.)
                continue
            origin = trips.point_status(row["start_x"], row["start_y"], boundary)
            destination = trips.point_status(row["end_x"], row["end_y"], boundary)
            mode, loop, code = model_mode(row["main_mode"])
            status = trips.endpoint_trip(origin, destination, code == "outside")
            audit.add(status + "_trips", 1.)
            if status != "internal":
                continue
            records.append(enrich_record(dict(mode=mode, weight=1., loop=loop, code=code),
                                         SimpleNamespace(**identifiers[identifier]),
                                         (row["start_x"], row["start_y"]), (row["end_x"], row["end_y"]),
                                         departure=departure_seconds(row["dep_time"])))
    audit.add("residents_with_output_rows", float(len(contributors)), len(contributors))
    missing = len(identifiers) - len(contributors)
    audit.add("residents_without_output_rows_not_confirmed_nontravellers", float(missing), missing)
    return pd.DataFrame(records, columns=RECORD_COLUMNS), audit


def stratum_rows(survey, simulation, column, levels, min_contributors):
    result = []
    for stratum in levels:
        rows = [{"stratum": stratum, "stratum_label": STRATUM_LABELS.get(stratum, stratum),
                 "mode": mode, "label": MODE_LABELS[mode]} for mode in MODES]
        for source, original in [("survey", survey), ("simulation", simulation)]:
            frame = original if column is None else original.loc[original[column].eq(stratum)]
            total, contributors = float(frame.weight.sum()), int(frame.person_key.nunique())
            for row in rows:
                sub = frame.loc[frame['mode'].eq(row["mode"])]
                row[source + "_trips_n"] = len(sub)
                row[source + "_trip_weight_sum"] = float(sub.weight.sum())
                row[source + "_pct"] = 100 * float(sub.weight.sum()) / total if total else np.nan
                row[source + "_contributors_n"] = int(sub.person_key.nunique())
                row[source + "_group_trips_n"] = len(frame)
                row[source + "_group_trip_weight_sum"] = total
                row[source + "_group_contributors_n"] = contributors
            if total and not np.isclose(sum(row[source + "_pct"] for row in rows), 100):
                raise AssertionError("Stratum mode shares do not sum to its complete trip denominator.")
        for row in rows:
            row["difference_pp"] = row["simulation_pct"] - row["survey_pct"]
            row["comparison_shown"] = (row["survey_group_contributors_n"] >= min_contributors
                                       and row["simulation_group_contributors_n"] >= min_contributors)
            row["difference_pp_displayed"] = row["difference_pp"] if row["comparison_shown"] else np.nan
            result.append(row)
    return pd.DataFrame(result)


def check_partition(table, survey, simulation):
    first = table.drop_duplicates("stratum")
    for source, frame in [("survey", survey), ("simulation", simulation)]:
        if first[source + "_group_trips_n"].sum() != len(frame):
            raise AssertionError("A trip was lost or duplicated in a stratum partition.")
        if not np.isclose(first[source + "_group_trip_weight_sum"].sum(), frame.weight.sum()):
            raise AssertionError("Partition weights do not reconcile with all included trips.")


def verify_overall(current, reference_path):
    reference = pd.read_csv(reference_path)
    keys = ["survey_trips_n", "survey_trip_weight_sum", "survey_pct", "simulation_trips_n",
            "simulation_trip_weight_sum", "simulation_pct", "difference_pp"]
    if not {"mode", *keys}.issubset(reference.columns) or reference['mode'].duplicated().any():
        raise ValueError("The aggregate check file has an unexpected schema.")
    if set(reference['mode']) != set(MODES):
        raise ValueError("The aggregate check file has different mode groups.")
    expected, actual = reference.set_index("mode").loc[MODES], current.set_index("mode").loc[MODES]
    for key in keys:
        if not np.allclose(expected[key], actual[key], rtol=0, atol=0.000002, equal_nan=True):
            raise ValueError("Aggregate parity check failed. Input files or mode/selection rules differ; no new results saved.")


def next_batch_inventory(base):
    """Filenames/sizes only: prepare the PT/count checks without reading records."""
    result = {name: [] for name in ["events", "traffic_counts", "network", "transit_schedule", "config"]}
    matches = Counter()
    for directory, subdirectories, filenames in os.walk(base):
        subdirectories[:] = sorted(d for d in subdirectories
                                   if d not in {".git", "results", "__pycache__", "node_modules", "ITERS"})
        for filename in sorted(filenames):
            path = Path(directory) / filename
            name = filename.casefold()
            is_xml = name.endswith((".xml", ".xml.gz"))
            is_table = name.endswith((".csv", ".csv.gz", ".tsv", ".tsv.gz"))
            role = ("events" if is_xml and "events" in name else
                    "traffic_counts" if (is_xml or is_table) and "count" in name else
                    "network" if is_xml and "network" in name else
                    "transit_schedule" if is_xml and ("transitschedule" in name or "transit_schedule" in name) else
                    "config" if is_xml and "config" in name else None)
            if role:
                matches[role] += 1
                if len(result[role]) < 25:
                    result[role].append({"relative_file": str(path.relative_to(base)), "size_bytes": path.stat().st_size})
    return {"scope": "Filenames and sizes only; no records read. At most 25 files listed per role; per-role total counts supplied.",
            "candidate_counts": dict(matches), "files": result}


def spatial_table(survey, simulation, polygon, cell_size, min_contributors):
    grid, geometry = spatial.make_grid(polygon, cell_size)
    assigned = []
    for frame in [survey, simulation]:
        positions = frame[["home_x", "home_y"]].rename(columns={"home_x": "x", "home_y": "y"})
        keys = spatial.assign_cells(positions, geometry, cell_size)
        assigned.append(frame.assign(cell_id=[f"{key[0]}_{key[1]}" for key in keys]))
    table = stratum_rows(*assigned, "cell_id", list(grid.cell_id), min_contributors)
    table = table.rename(columns={"stratum": "cell_id"}).drop(columns="stratum_label")
    table = table.merge(grid, on="cell_id", how="left", validate="many_to_one")
    check_partition(table.rename(columns={"cell_id": "stratum"}), survey, simulation)
    pt = table.loc[table['mode'].eq("pt")].copy()
    pt["survey_rate_shown"] = pt.survey_group_contributors_n.ge(min_contributors)
    pt["simulation_rate_shown"] = pt.simulation_group_contributors_n.ge(min_contributors)
    summary = {"grid_cells": len(grid), "supported_difference_cells": int(pt.comparison_shown.sum())}
    for source, frame in [("survey", survey), ("simulation", simulation)]:
        denominator = float(frame.weight.sum())
        summary[source + "_trip_weight_pct_in_supported_cells"] = (
            100 * float(pt.loc[pt.comparison_shown, source + "_group_trip_weight_sum"].sum()) / denominator if denominator else None)
    return table, pt, geometry, summary


def plot_panels(table, levels, title, path, min_contributors, extra_note=""):
    cols = min(3, len(levels))
    rows = math.ceil(len(levels) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 3.8 * rows + 1.7), squeeze=False, sharey=True)
    finite = np.concatenate([table.survey_pct.to_numpy(), table.simulation_pct.to_numpy()])
    finite = finite[np.isfinite(finite)]
    ymax = min(105, max(float(finite.max()) + 8 if len(finite) else 10, 20))
    x = np.arange(len(MODES))
    for ax, stratum in zip(axes.flat, levels):
        data = table.loc[table.stratum.eq(stratum)].set_index("mode").loc[MODES]
        row = data.iloc[0]
        supported = bool(row.comparison_shown)
        if supported:
            ax.bar(x - .19, data.survey_pct, .36, color="#237A8A")
            ax.bar(x + .19, data.simulation_pct, .36, color="#E38D35")
        else:
            ax.text(.5, .48, "Limited contributor support\nSee CSV for descriptive values", transform=ax.transAxes,
                    ha="center", va="center", fontsize=10)
        ax.set_title(STRATUM_LABELS.get(stratum, stratum), pad=28)
        ax.text(.5, 1.015, f"Distinct contributors: survey {int(row.survey_group_contributors_n):,}; simulation {int(row.simulation_group_contributors_n):,}",
                transform=ax.transAxes, ha="center", fontsize=8)
        ax.set_xticks(x, SHORT_MODES, rotation=35, ha="right")
        ax.set_ylim(0, ymax)
        ax.set_ylabel("Share of trips within the group (%)")
        ax.grid(axis="y", alpha=.2)
        ax.set_axisbelow(True)
    for ax in list(axes.flat)[len(levels):]:
        ax.axis("off")
    handles = [plt.Rectangle((0, 0), 1, 1, color=color) for color in ["#237A8A", "#E38D35"]]
    fig.legend(handles, ["MZMV 2015 (WP weighted)", "Supplied simulation trips"],
               loc="upper center", bbox_to_anchor=(.5, .95), ncol=2, frameon=False)
    fig.suptitle(title, fontsize=16, y=.99)
    note = ("Same resident cohort and internal-trip scope as the aggregate comparison. Every included mode remains in each group's denominator.\n"
            f"Comparison panels require {min_contributors} distinct contributors in each source; this is a support heuristic, not a significance test.")
    if extra_note:
        note += "\n" + extra_note
    fig.text(.02, .015, note, va="bottom", fontsize=9, linespacing=1.5)
    fig.subplots_adjust(left=.065, right=.985, top=.72 if rows == 1 else .82,
                        bottom=.27 if rows == 1 else .17, wspace=.20, hspace=.65)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def plot_time(table, path, min_contributors):
    data = table.loc[table['mode'].eq("pt")].set_index("stratum").loc[TIME_GROUPS[:-1]]
    fig, ax = plt.subplots(figsize=(11, 5.6))
    x = np.arange(len(data))
    for source, offset, color, label in [("survey", -.19, "#237A8A", "MZMV 2015 (WP weighted)"),
                                        ("simulation", .19, "#E38D35", "Supplied simulation trips")]:
        values = data[source + "_pct"].where(data[source + "_group_contributors_n"].ge(min_contributors))
        ax.bar(x + offset, values, .36, color=color, label=label)
        for position, value in zip(x + offset, values):
            if np.isfinite(value) and value == 0:
                ax.annotate("0", (position, 0), xytext=(0, 3), textcoords="offset points", ha="center", color=color, fontsize=9)
    ax.set_xticks(x, [STRATUM_LABELS[k] for k in data.index], fontsize=9)
    ax.set_ylabel("PT / coach share of trips\nwithin the window (%)")
    ax.set_xlabel("Departure time since the start of the diary / simulation day")
    ax.set_title("Neuchatel: PT mode share by departure window", pad=42, fontsize=16)
    ax.legend(loc="lower center", bbox_to_anchor=(.5, 1.01), ncol=2, frameon=False)
    ax.grid(axis="y", alpha=.2)
    ax.set_axisbelow(True)
    note = ("Same resident/internal-trip scope and complete within-window mode denominators. This shows mode shares, not PT volumes or punctuality.\n"
            f"Bars require {min_contributors} distinct contributors in the relevant source. Blank bars mean limited support or no trips, not zero PT.\n"
            "After-midnight departures retain times >=24:00; they are not folded into the start of the day. Unknown times remain in the CSV.")
    fig.text(.02, .02, note, fontsize=9, linespacing=1.5)
    fig.subplots_adjust(left=.085, right=.98, top=.77, bottom=.34)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def plot_spatial(pt, geometry, polygon, path, min_contributors, cell_size):
    values = np.concatenate([pt.survey_pct.to_numpy(), pt.simulation_pct.to_numpy()])
    values = values[np.isfinite(values)]
    maximum = max(float(values.max()), 1.) if len(values) else 1.
    differences = pt.loc[pt.comparison_shown, "difference_pp"].dropna()
    limit = max(float(differences.abs().max()), .1) if len(differences) else 1.
    paths = {key: spatial.polygon_path(value) for key, value in geometry.items()}
    outline = spatial.polygon_path(polygon)
    fig = plt.figure(figsize=(15.5, 7.2), layout="constrained")
    gs = fig.add_gridspec(3, 3, height_ratios=[1, .045, .20])
    xmin, ymin, xmax, ymax = polygon.bounds
    margin = max(xmax-xmin, ymax-ymin) * .04
    for i, (source, title) in enumerate([( "survey", "MZMV 2015 (WP weighted)"),
                                        ("simulation", "Supplied simulation trips"),
                                        (None, "Simulation minus survey")]):
        ax = fig.add_subplot(gs[0, i])
        cmap = plt.get_cmap("RdBu_r" if source is None else "YlGnBu")
        norm = Normalize(-limit, limit) if source is None else Normalize(0, maximum)
        for row in pt.itertuples(index=False):
            shown = row.comparison_shown if source is None else getattr(row, source + "_rate_shown")
            value = row.difference_pp if source is None else getattr(row, source + "_pct")
            masked = not shown or not math.isfinite(value)
            ax.add_patch(PathPatch(paths[(row.ix, row.iy)], facecolor="#ECECEC" if masked else cmap(norm(value)),
                                   edgecolor="#AAAAAA" if masked else "white", linewidth=.35,
                                   hatch="///" if masked else None))
        ax.add_patch(PathPatch(outline, facecolor="none", edgecolor="#222222", linewidth=.8))
        ax.set(xlim=(xmin-margin, xmax+margin), ylim=(ymin-margin, ymax+margin), title=title, xlabel="Easting (LV95 km)")
        ax.set_aspect("equal", adjustable="box")
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, pos: f"{value/1000:.0f}"))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, pos: f"{value/1000:.0f}"))
        ax.tick_params(labelsize=8, labelleft=i == 0)
        if i == 0:
            ax.set_ylabel("Northing (LV95 km)")
        color_ax = fig.add_subplot(gs[1, i])
        fig.colorbar(plt.cm.ScalarMappable(cmap=cmap, norm=norm), cax=color_ax, orientation="horizontal",
                     label="Difference in percentage points" if source is None else "PT / coach share of residents' internal trips (%)")
    footer = fig.add_subplot(gs[2, :])
    footer.axis("off")
    footer.text(0, .95,
                "Trips are attributed to the resident's HOME cell, regardless of where the trip starts. Denominator: all included trips of residents in that cell.\n"
                "This compares residential patterns of mode choice; it does not measure stop boardings, destination shares or current service quality.\n"
                f"Hatching: fewer than {min_contributors} distinct contributors in the relevant source. Differences require support in BOTH sources.\n"
                "Other/unknown modes remain in denominators. Support thresholds are heuristics, not significance tests.", va="top", fontsize=9, linespacing=1.5)
    fig.suptitle(f"Neuchatel: PT share of residents' internal trips, by home cell ({cell_size/1000:g} km)", fontsize=15)
    fig.savefig(path, dpi=180)
    plt.close(fig)




