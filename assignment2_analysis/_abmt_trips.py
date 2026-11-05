#!/usr/bin/env python3
'Internal reconciled reader/utility derived from the reviewed analysis scripts. See README.md and abmt_common.METHOD for the active definitions.'

import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import _abmt_age as baseline
import _abmt_car as car
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import Point


GROUPS = ["all", "available", "unavailable", "unknown"]
GROUP_LABELS = {"all": "All residents", "available": "Car available",
                "unavailable": "Car unavailable / inapplicable",
                "unknown": "Car availability unknown"}
BINS = ["0", "1", "2", "3", "4", "5", "6+", "unknown"]
LOOP_FLAGS = ["hasWalkLoopTrip", "hasCarLoopTrip", "hasCarPassengerLoopTrip",
              "hasPtLoopTrip", "hasBikeLoopTrip"]
TRIP_COLUMNS = ["HHNR", "WEGNR", "wzweck1", "wzweck2", "pseudo",
                "S_X_CH1903", "S_Y_CH1903", "Z_X_CH1903", "Z_Y_CH1903"]
SOURCES = [
    {"url": "https://github.com/eqasim-org/eqasim-switzerland/blob/cmdp_new_version/data/microcensus/trips.py",
     "blob_sha": "cfe088774cb109260d566e33b69585be281d8211"},
    {"url": "https://github.com/eqasim-org/eqasim-java/blob/cmdp/core/src/main/java/org/eqasim/core/scenario/cutter/population/PlanCutter.java",
     "blob_sha": "56ff405b5bc176aebb3abecd298ac9b81ff10d64"},
    {"url": "https://github.com/eqasim-org/eqasim-java/blob/cmdp/core/src/main/java/org/eqasim/core/misc/Constants.java",
     "blob_sha": "22fd4d332fbd634b4173ba338d2afaeca6656896"},
]


def local_name(element):
    return element.tag.rsplit("}", 1)[-1]


def point_status(x, y, boundary, survey=False, transformer=None):
    x, y = baseline.number(x), baseline.number(y)
    if x is None or y is None:
        return "unknown"
    if survey:
        if not (400_000 <= x <= 900_000 and 0 <= y <= 400_000):
            return "unknown"
        x, y = transformer.transform(x, y)
    elif not (2_000_000 <= x <= 3_000_000 and 1_000_000 <= y <= 1_400_000):
        return "unknown"
    return "inside" if boundary.covers(Point(x, y)) else "outside"


def endpoint_trip(origin, destination, artificial_mode=False):
    # One known outside endpoint is enough to rule out an internal trip.
    if "outside" in {origin, destination}:
        return "excluded_outside"
    if artificial_mode or "unknown" in {origin, destination}:
        return "unresolved"
    return "internal"


def count_survey_person(rows, travel_code, domestic_count, boundary, transformer):
    """Return a count only when the internal count can be determined."""
    audit = Counter()
    travel_code, domestic_count = baseline.number(travel_code), baseline.number(domestic_count)
    if travel_code == 2 and domestic_count is not None and domestic_count > 0:
        audit["nontravel_response_conflicts_with_domestic_count"] += 1
        return None, "unknown", audit
    if rows.empty:
        if travel_code == 2 or domestic_count == 0:
            audit["zero_count_supported_by_person_response"] += 1
            return 0, "no_travel" if travel_code == 2 else "travel" if travel_code == 1 else "unknown", audit
        audit["missing_trip_rows_without_zero_confirmation"] += 1
        return None, "travel" if travel_code == 1 else "unknown", audit

    mobility = "travel"
    unresolved = False
    if travel_code == 2:
        audit["nontravel_response_conflicts_with_trip_rows"] += 1
        mobility, unresolved = "unknown", True
    if domestic_count is not None and domestic_count > len(rows):
        audit["fewer_trip_rows_than_reported_domestic_count"] += 1
        unresolved = True

    sequence = pd.to_numeric(rows.WEGNR, errors="coerce")
    valid_sequence = np.isfinite(sequence) & sequence.gt(0) & sequence.mod(1).eq(0)
    if not valid_sequence.all() or sequence.duplicated().any():
        audit["invalid_or_duplicate_trip_order"] += 1
        return None, mobility, audit
    rows = rows.assign(_order=sequence).sort_values("_order")
    order = rows._order.to_numpy(dtype=int)
    if order[0] != 1 or (np.diff(order) != 1).any():
        audit["nonconsecutive_trip_order"] += 1
        unresolved = True

    count, pending_origin, previous_kind = 0, None, None
    pending_uncertain = False
    for row in rows.itertuples(index=False):
        purpose, return_code = baseline.number(row.wzweck1), baseline.number(row.wzweck2)
        if return_code in {2, 3}:
            purpose = 11
        kind = ("stage" if purpose == 1 else "outside" if purpose == 13
                else "main" if purpose in set(range(2, 13)) else "unknown")
        origin = point_status(row.S_X_CH1903, row.S_Y_CH1903, boundary, True, transformer)
        destination = point_status(row.Z_X_CH1903, row.Z_Y_CH1903, boundary, True, transformer)
        if previous_kind == "outside":
            origin = "outside"
        if kind == "outside":
            destination = "outside"
        if pending_origin is None:
            pending_origin = origin
        pseudo_code = baseline.number(row.pseudo)
        pending_uncertain |= kind == "unknown" or pseudo_code != 1
        audit["survey_rows"] += 1
        if return_code == 3:
            audit["home_to_home_survey_rows"] += 1
        if kind == "stage":
            audit["transfer_rows_collapsed"] += 1
        else:
            status = endpoint_trip(pending_origin, destination, pending_uncertain)
            audit[status + "_trips"] += 1
            count += status == "internal"
            unresolved |= status == "unresolved"
            pending_origin, pending_uncertain = None, False
        previous_kind = kind
    if pending_origin is not None:
        audit["unfinished_transfer_chain"] += 1
        unresolved = True
    if domestic_count == 0 and count > 0:
        audit["zero_domestic_count_conflicts_with_internal_trips"] += 1
        unresolved = True
    return None if unresolved else count, mobility, audit


def read_survey_counts(base, boundary):
    frame, selection = baseline.read_survey(base, boundary)
    extra = pd.read_csv(baseline.unique_file(base, "zielpersonen.csv"), encoding="latin1",
                        dtype={"HHNR": "string"}, usecols=["HHNR", "f50200", "T_WEGE"])
    if extra.HHNR.isna().any() or extra.HHNR.duplicated().any():
        raise ValueError("Survey person linkage is missing or duplicated; inspect locally.")
    frame = frame.merge(extra, on="HHNR", validate="one_to_one", how="left")
    trips = pd.read_csv(baseline.unique_file(base, "wege.csv"), encoding="latin1",
                        dtype={"HHNR": "string"}, usecols=TRIP_COLUMNS)
    if trips.HHNR.isna().any():
        raise ValueError("Survey trip linkage is missing; inspect locally.")
    trips = trips.loc[trips.HHNR.isin(frame.HHNR)].copy()
    groups = dict(tuple(trips.groupby("HHNR", sort=False)))
    empty = trips.iloc[:0]
    transformer = Transformer.from_crs(21781, 2056, always_xy=True)
    audit, counts, mobility = Counter(), [], []
    for row in frame.itertuples(index=False):
        count, state, profile = count_survey_person(groups.get(row.HHNR, empty), row.f50200,
                                                   row.T_WEGE, boundary, transformer)
        counts.append(count)
        mobility.append(state)
        audit.update(profile)
    frame["trip_count"], frame["mobility"] = counts, mobility
    frame["personal"] = frame.f42100e.map(lambda value: car.survey_access(value, "any_access"))
    return frame[["age", "weight", "personal", "trip_count", "mobility"]], {"selection": selection, "trip_audit": dict(audit)}


def choose_plan(person):
    plans = [item for item in person if local_name(item) == "plan"]
    selected = [item for item in plans if str(item.get("selected", "")).lower() in {"yes", "true", "1"}]
    if len(selected) == 1:
        return selected[0], "selected_plan"
    if not selected and len(plans) == 1 and not str(plans[0].get("selected", "")).strip():
        return plans[0], "sole_plan_without_selected_marker"
    return None, "missing_or_ambiguous_selected_plan"


def activity_kind(activity):
    name = str(activity.get("type", "")).strip().casefold()
    if name in {"outside", "border"}:
        return "outside"
    if name == "interaction" or name.endswith(" interaction"):
        return "stage"
    return "main" if name else "unknown"


def count_model_plan(plan, boundary, loop_flag=False):
    audit = Counter()
    if plan is None:
        return None, "unknown", audit
    elements = [item for item in plan if local_name(item) in {"act", "activity", "leg"}]
    if not elements or local_name(elements[0]) == "leg" or local_name(elements[-1]) == "leg":
        audit["malformed_plan"] += 1
        return None, "unknown", audit
    if any((local_name(item) == "leg") != (i % 2 == 1) for i, item in enumerate(elements)):
        audit["malformed_plan"] += 1
        return None, "unknown", audit
    legs_total = sum(local_name(item) == "leg" for item in elements)
    substantive = [item for item in elements if local_name(item) != "leg" and activity_kind(item) != "stage"]
    home_only = (len(substantive) == 1 and str(substantive[0].get("type", "")).casefold().startswith("home")
                 and legs_total == 0 and not loop_flag)
    mobility = "travel" if legs_total or loop_flag else "no_travel" if home_only else "unknown"
    if not substantive or activity_kind(elements[0]) == "stage" or activity_kind(elements[-1]) == "stage":
        audit["incomplete_stage_chain"] += 1
        return None, mobility, audit
    if not legs_total and not home_only:
        audit["single_nonhome_or_loop_flag_without_trip"] += 1
        return None, mobility, audit

    count, origin, leg_modes, unresolved = 0, None, [], False
    for item in elements:
        if local_name(item) == "leg":
            leg_modes.append(str(item.get("mode", "")).strip().casefold())
            continue
        kind = activity_kind(item)
        if kind == "stage":
            audit["interaction_activities_collapsed"] += 1
            continue
        location = "outside" if kind == "outside" else point_status(item.get("x"), item.get("y"), boundary)
        if kind == "unknown":
            location = "unknown"
        if origin is not None:
            status = endpoint_trip(origin, location, "outside" in leg_modes or not all(leg_modes))
            audit[status + "_trips"] += 1
            if "remote_walk" in leg_modes:
                audit["excluded_remote_walk_trips"] += 1
            else:
                count += status == "internal"
                unresolved |= status == "unresolved"
        origin, leg_modes = location, []
    audit["persons_with_loop_flag"] += int(loop_flag)
    audit["home_only_selected_plans"] += int(home_only)
    if any(str(e.get("mode", "")).casefold() == "remote_walk" for e in elements if local_name(e) == "leg"):
        audit["remote_walk_plan_physical_count_unresolved"] += 1
        return None, "unknown", audit
    return None if unresolved else count, mobility, audit


def read_population_counts(path, boundary):
    records, seen, audit = [], set(), Counter()
    funnel, flags = Counter(), Counter()
    with gzip.open(path, "rb") as stream:
        root = None
        for event, person in ET.iterparse(stream, events=("start", "end")):
            if root is None:
                root = person
            if event != "end" or local_name(person) != "person":
                continue
            funnel["persons_total"] += 1
            attrs = {item.get("name"): (item.text or "").strip()
                     for section in person if local_name(section) == "attributes"
                     for item in section if local_name(item) == "attribute"}
            identifier = person.get("id")
            if not identifier or identifier in seen:
                raise ValueError("Synthetic person IDs are missing or duplicated; inspect locally.")
            seen.add(identifier)
            home = [(baseline.number(attrs.get(a)), baseline.number(attrs.get(b))) for a, b in
                    [("home_coordinate_x", "home_coordinate_y"), ("home_x", "home_y")]]
            x, y = home[0 if all(value is not None for value in home[0]) else 1]
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
                plan, chosen = choose_plan(person)
                audit[chosen] += 1
                count, mobility, profile = count_model_plan(plan, boundary, any(baseline.flag(attrs.get(n)) for n in LOOP_FLAGS))
                audit.update(profile)
                records.append({"age": age, "weight": 1.0, "personal": car.synthetic_access(attrs.get("carAvail", "").casefold()),
                                "trip_count": count, "mobility": mobility})
                for name in baseline.MODEL_FLAGS:
                    flags[name] += int(baseline.flag(attrs.get(name)))
            root.clear()
    funnel["eligible_residents"] = len(records)
    if sum(value for key, value in funnel.items() if key.startswith("excluded_")) + len(records) != funnel["persons_total"]:
        raise AssertionError("Synthetic resident selection does not reconcile.")
    if not records:
        raise ValueError("No eligible synthetic residents.")
    return pd.DataFrame(records), {"selection": dict(funnel), "flags_among_eligible": dict(flags), "trip_audit": dict(audit)}


def compare(survey, population):
    distributions, summaries = [], []
    for group in GROUPS:
        row = {"group": group}
        bins = {name: {"group": group, "trips": name} for name in BINS}
        for source, original in [("survey", survey), ("synthetic", population)]:
            frame = original if group == "all" else original.loc[original.personal.eq(group)]
            weight_sum = float(frame.weight.sum())
            known = frame.trip_count.notna()
            if not frame.empty and (not (np.isfinite(frame.weight) & frame.weight.gt(0)).all()
                                    or not (frame.loc[known, "trip_count"].ge(0)
                                            & frame.loc[known, "trip_count"].mod(1).eq(0)).all()):
                raise AssertionError("Invalid person weights or trip counts.")
            categories = frame.trip_count.map(lambda value: "unknown" if pd.isna(value) else str(int(value)) if value < 6 else "6+")
            for name in BINS:
                mask = categories.eq(name)
                bins[name][source + "_n"] = int(mask.sum())
                bins[name][source + "_weight_sum"] = float(frame.loc[mask, "weight"].sum())
                bins[name][source + "_pct"] = 100 * bins[name][source + "_weight_sum"] / weight_sum if weight_sum else np.nan
            row[source + "_n"] = len(frame)
            row[source + "_weight_sum"] = weight_sum
            row[source + "_known_count_n"] = int(known.sum())
            known_weight = frame.loc[known, "weight"].sum()
            row[source + "_mean_trips_known_counts"] = (float((frame.loc[known, "weight"] * frame.loc[known, "trip_count"]).sum() / known_weight)
                                                        if known_weight else np.nan)
            row[source + "_zero_internal_pct_all"] = bins["0"][source + "_pct"]
            row[source + "_unknown_count_pct_all"] = bins["unknown"][source + "_pct"]
            for state in ["no_travel", "travel", "unknown"]:
                row[source + "_mobility_" + state + "_pct_all"] = (100 * frame.loc[frame.mobility.eq(state), "weight"].sum() / weight_sum
                                                                  if weight_sum else np.nan)
            if weight_sum and not np.isclose(sum(bins[name][source + "_pct"] for name in BINS), 100):
                raise AssertionError("Trip-count bins do not sum to the complete group.")
        for item in bins.values():
            item["difference_pp"] = item["synthetic_pct"] - item["survey_pct"]
            distributions.append(item)
        row["difference_mean_trips_known_counts"] = row["synthetic_mean_trips_known_counts"] - row["survey_mean_trips_known_counts"]
        summaries.append(row)
    return pd.DataFrame(distributions), pd.DataFrame(summaries)


def plot_comparison(table, summary, path):
    fig, axes = plt.subplots(1, 3, figsize=(15, 5.6), sharey=True)
    x = np.arange(len(BINS))
    for ax, group in zip(axes, GROUPS[:3]):
        data = table.loc[table.group.eq(group)]
        row = summary.loc[summary.group.eq(group)].iloc[0]
        ax.bar(x - .19, data.survey_pct, .38, color="#237A8A", label="MZMV 2015 (WP weighted)")
        ax.bar(x + .19, data.synthetic_pct, .38, color="#DF8D39", label="Synthetic plans")
        ax.set(xticks=x, xticklabels=["Unknown" if b == "unknown" else b for b in BINS], xlabel="Trips with both endpoints inside the boundary")
        ax.tick_params(axis="x", labelsize=8)
        ax.set_title(GROUP_LABELS[group], fontsize=12, pad=40)
        ax.text(.5, 1.015, f"Mean among known counts: {row.survey_mean_trips_known_counts:.2f} / {row.synthetic_mean_trips_known_counts:.2f}\n"
                f"Survey n={int(row.survey_n):,}; synthetic n={int(row.synthetic_n):,}",
                transform=ax.transAxes, ha="center", va="bottom", fontsize=8)
        ax.grid(axis="y", alpha=.2)
        ax.set_axisbelow(True)
    axes[0].set_ylabel("Share of all residents in the group (%)")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(.5, .91), ncol=2, frameon=False)
    fig.suptitle("Neuchatel: weekday trips within the study area per resident", fontsize=16, y=.99)
    fig.text(.06, .055, "Same resident selection and weights. Zero includes residents travelling only outside the eligible trip scope; it does not mean stayed home.", fontsize=9)
    fig.text(.06, .025, "Transfers are collapsed; artificial outside segments are excluded. Unresolved counts stay in denominators as Unknown. Car-access unknowns are retained in CSVs.", fontsize=9)
    fig.tight_layout(rect=(.02, .1, 1, .86))
    fig.savefig(path, dpi=180)
    plt.close(fig)




