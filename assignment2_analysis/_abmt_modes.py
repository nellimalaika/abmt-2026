#!/usr/bin/env python3
'Internal reconciled reader/utility derived from the reviewed analysis scripts. See README.md and abmt_common.METHOD for the active definitions.'

import argparse
from collections import Counter
import csv
import gzip
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import _abmt_trips as trips
baseline = trips.baseline
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import Point


MODES = ["walk", "bike", "car", "car_passenger", "pt", "other", "unknown"]
LABELS = {"walk": "Walk", "bike": "Bicycle / e-bike", "car": "Car driver",
          "car_passenger": "Car passenger", "pt": "PT / coach",
          "other": "Other / auxiliary mode", "unknown": "Unknown mode / car role"}
LOOPS = {mode + "_loop": mode for mode in MODES[:5]}
SURVEY_COLUMNS = trips.TRIP_COLUMNS + ["wmittel"]
OUTPUT_COLUMNS = ["person", "trip_number", "trip_id", "main_mode", "start_x", "start_y",
                  "end_x", "end_y", "start_activity_type", "end_activity_type"]
RECORD_COLUMNS = ["mode", "weight", "loop", "code"]


class Audit:
    def __init__(self):
        self.n = Counter()
        self.weight = Counter()

    def add(self, metric, weight=0., n=1):
        self.n[metric] += n
        self.weight[metric] += weight

    def table(self, source):
        return [{"source": source, "metric": key, "n": self.n[key],
                 "weight_sum": self.weight[key]} for key in sorted(self.n)]


def code_number(value):
    value = baseline.number(value)
    return int(value) if value is not None and value.is_integer() else None


def survey_mode(value, stage_codes):
    code = code_number(value)
    if 8 in stage_codes:
        return "car_passenger"
    return {**dict.fromkeys(range(1, 9), "pt"), 9:"car", 10:"car", 11:"pt",
            12:"car", 13:"car", 14:"bike", 15:"walk", 16:"bike"}.get(code,"unknown")

def survey_mode_strict(value, stage_codes):
    code = code_number(value)
    if code == 9:
        roles = set(stage_codes) & {7,8}
        return "car" if roles == {7} else "car_passenger" if roles == {8} else "unknown"
    return {**dict.fromkeys(range(1,9),"pt"),10:"car",11:"pt",12:"car",13:"car",
            14:"bike",15:"walk",16:"bike"}.get(code,"unknown")



def safe_survey_code(value):
    code = code_number(value)
    return str(code) if code in set(range(1, 18)) | {-99, -98, -97} else "MISSING_OR_UNRECOGNISED"


def model_mode(value):
    code = str(value or "").strip().casefold()
    if code in LOOPS:
        return LOOPS[code], True, code
    if code in MODES[:5]:
        return code, False, code
    if code in {"remote_walk", "truck"}:
        return "other", False, code
    return "unknown", False, "outside" if code == "outside" else "MISSING_OR_UNRECOGNISED"


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
        records.append(dict(mode=category, weight=weight, loop=loop, code=raw))

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
    records = []
    for person in people.itertuples(index=False):
        records.extend(survey_person(person, grouped.get(person.HHNR, empty), lookup,
                                     boundary, transformer, audit))
    return pd.DataFrame(records, columns=RECORD_COLUMNS), audit, selection


def read_resident_ids(path, boundary):
    """Same XML cohort as validate_age; IDs stay in local memory only."""
    identifiers, seen, funnel, flags = set(), set(), Counter(), Counter()
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
                identifiers.add(identifier)
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
            records.append(dict(mode=mode, weight=1., loop=loop, code=code))
    audit.add("residents_with_output_rows", float(len(contributors)), len(contributors))
    missing = len(identifiers) - len(contributors)
    audit.add("residents_without_output_rows_not_confirmed_nontravellers", float(missing), missing)
    return pd.DataFrame(records, columns=RECORD_COLUMNS), audit


def make_tables(survey, simulation, survey_audit, simulation_audit):
    rows = [{"mode": mode, "label": LABELS[mode]} for mode in MODES]
    codes = []
    for source, frame, audit in [("survey", survey, survey_audit), ("simulation", simulation, simulation_audit)]:
        total = float(frame.weight.sum())
        if frame.empty or not (np.isfinite(frame.weight) & frame.weight.gt(0)).all():
            raise ValueError("No eligible internal trips with positive weights in one dataset.")
        if not frame['mode'].isin(MODES).all():
            raise AssertionError("A mode was lost during grouping.")
        if audit.n["internal_trips"] != len(frame) or not np.isclose(audit.weight["internal_trips"], total):
            raise AssertionError("Internal trip totals do not reconcile.")
        audit.add("internal_trips_with_loop_marker", float(frame.loc[frame.loop, "weight"].sum()), int(frame.loop.sum()))
        for row in rows:
            sub = frame.loc[frame['mode'].eq(row["mode"])]
            row[source + "_trips_n"] = len(sub)
            row[source + "_trip_weight_sum"] = float(sub.weight.sum())
            row[source + "_pct"] = 100 * float(sub.weight.sum()) / total
        if not np.isclose(sum(row[source + "_pct"] for row in rows), 100):
            raise AssertionError("Mode shares do not sum to all included internal trips.")
        for (code, mode, loop), sub in frame.groupby(["code", "mode", "loop"], sort=True):
            codes.append(dict(source=source, input_code=code, grouped_mode=mode, loop_marker=bool(loop),
                              trips_n=len(sub), trip_weight_sum=float(sub.weight.sum()),
                              pct_of_all_internal_trips=100 * float(sub.weight.sum()) / total))
    comparison = pd.DataFrame(rows)
    comparison["difference_pp"] = comparison.simulation_pct - comparison.survey_pct
    coverage = pd.DataFrame(survey_audit.table("survey") + simulation_audit.table("simulation"))
    return comparison, coverage, pd.DataFrame(codes)


def plot_comparison(table, survey_audit, simulation_audit, path):
    fig, ax = plt.subplots(figsize=(11, 6.8))
    y = np.arange(len(MODES))
    ax.barh(y - .19, table.survey_pct, .36, color="#237A8A", label="MZMV 2015 (WP weighted)")
    ax.barh(y + .19, table.simulation_pct, .36, color="#E38D35", label="Supplied simulation trips")
    ax.set_yticks(y, [LABELS[m] for m in MODES])
    ax.invert_yaxis()
    ax.set_xlabel("Share of all included internal trips (%)")
    ax.set_title("Neuchatel: weekday trip main modes of residents", pad=42, fontsize=17)
    ax.legend(loc="lower center", bbox_to_anchor=(.5, 1.01), ncol=2, frameon=False)
    ax.grid(axis="x", alpha=.2)
    ax.set_axisbelow(True)
    for i, row in enumerate(table.itertuples(index=False)):
        for offset, value in [(-.19, row.survey_pct), (.19, row.simulation_pct)]:
            if value > 0:
                ax.text(value + .25, i + offset, f"{value:.1f}", va="center", fontsize=9)
    ax.set_xlim(0, max(table.survey_pct.max(), table.simulation_pct.max()) * 1.13 + 1)
    sn, mn = survey_audit.n, simulation_audit.n
    note = (f"Same home-based cohort, ages 6-120; weekday survey. Residents: survey n={sn['eligible_residents']:,}; simulation n={mn['eligible_residents']:,}.\n"
            f"Included trips: survey n={sn['internal_trips']:,}; simulation n={mn['internal_trips']:,}. Both substantive endpoints inside the boundary.\n"
            "Loop codes join their base mode; other and unknown modes remain in the denominator. Survey car role uses car stages only.\n"
            "Other includes survey motorcycle, taxi, truck, air and vehicle-like devices, and simulation remote_walk/truck when eligible.")
    fig.text(.02, .025, note, ha="left", va="bottom", fontsize=9, linespacing=1.5)
    fig.subplots_adjust(left=.25, right=.97, bottom=.25, top=.80)
    fig.savefig(path, dpi=180)
    plt.close(fig)




