#!/usr/bin/env python3
'Internal reconciled reader/utility derived from the reviewed analysis scripts. See README.md and abmt_common.METHOD for the active definitions.'

import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

import _abmt_trips as trips
baseline, car = trips.baseline, trips.car
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import Point


OUTSIDE, UNKNOWN, OTHER = "outside_chain", "unknown_chain", "other_internal_chains"
PURPOSES = {1: "T", 2: "W", 3: "E", 4: "S", 5: "O", 6: "W", 7: "W",
            8: "L", 9: "O", 10: "O", 11: "H", 13: "X"}
MODEL_PURPOSES = {"home": "H", "work": "W", "work_secondary": "W", "education": "E",
                  "shop": "S", "leisure": "L", "other": "O", "outside": "X", "border": "X"}
EXTRA_PERSON = ["HHNR", "f50200", "T_WEGE", "A_X_CH1903", "A_Y_CH1903",
                "AU_X_CH1903", "AU_Y_CH1903"]
STRUCTURES = ["home_only", "home_home_loop", "one_tour_one_stop", "one_tour_multiple_stops",
              "multiple_home_tours", "open_or_nonhome_chain", OUTSIDE, UNKNOWN]
STRUCTURE_LABELS = {
    "home_only": "Home only (H)", "home_home_loop": "Home-to-home trip (H-H)",
    "one_tour_one_stop": "One home tour, one non-home stop",
    "one_tour_multiple_stops": "One home tour, multiple non-home stops",
    "multiple_home_tours": "Multiple departures from home",
    "open_or_nonhome_chain": "Open chain or non-home start/end",
    OUTSIDE: "Chain involves outside travel", UNKNOWN: "Unknown / unresolved chain"}


def numeric_pair(x, y):
    x, y = baseline.number(x), baseline.number(y)
    return (x, y) if x is not None and y is not None else None


def same_site(a, b, tolerance):
    return a is not None and b is not None and np.hypot(a[0] - b[0], a[1] - b[1]) <= tolerance


def initial_survey_purpose(person, first_trip, origin_answers, tolerance):
    """Match known sites first; never silently assume the day starts at home."""
    origin = numeric_pair(first_trip.S_X_CH1903, first_trip.S_Y_CH1903)
    for label, x_name, y_name in [("H", "W_X_CH1903", "W_Y_CH1903"),
                                  ("W", "A_X_CH1903", "A_Y_CH1903"),
                                  ("E", "AU_X_CH1903", "AU_Y_CH1903")]:
        site = numeric_pair(getattr(person, x_name), getattr(person, y_name))
        if site is not None and not (400_000 <= site[0] <= 900_000 and 0 <= site[1] <= 400_000):
            site = None
        if same_site(origin, site, tolerance):
            return label, "initial_origin_matched_" + label
    answers = {PURPOSES.get(baseline.number(value), "U") for value in origin_answers}
    answers -= {"U", "T"}
    if len(answers) == 1:
        return next(iter(answers)), "initial_origin_from_f52950"
    return "U", "initial_origin_conflicting_answers" if len(answers) > 1 else "initial_origin_unresolved"


def finish_chain(tokens, outside, unresolved):
    if outside:
        return OUTSIDE
    if unresolved or not tokens or any(token in {"U", "T", "X"} for token in tokens):
        return UNKNOWN
    return "-".join(tokens)


def survey_chain(person, rows, origin_answers, boundary, transformer, tolerance):
    audit = Counter()
    travelled, domestic = baseline.number(person.f50200), baseline.number(person.T_WEGE)
    if travelled == 2 and (not rows.empty or (domestic is not None and domestic > 0)):
        audit["nontravel_response_conflict"] += 1
        return UNKNOWN, audit
    if rows.empty:
        if travelled == 2:
            audit["confirmed_home_only"] += 1
            return "H", audit
        audit["no_trip_rows_without_confirmed_nontravel"] += 1
        return UNKNOWN, audit
    sequence = pd.to_numeric(rows.WEGNR, errors="coerce")
    if not (np.isfinite(sequence) & sequence.gt(0) & sequence.mod(1).eq(0)).all() or sequence.duplicated().any():
        audit["invalid_trip_order"] += 1
        return UNKNOWN, audit
    rows = rows.assign(_order=sequence).sort_values("_order")
    records = list(rows.itertuples(index=False))
    order = sequence.sort_values().to_numpy(dtype=int)
    if order[0] != 1 or (np.diff(order) != 1).any() or (domestic is not None and domestic > len(rows)):
        audit["incomplete_trip_sequence"] += 1
        return UNKNOWN, audit
    first = records[0]
    origin, reason = initial_survey_purpose(person, first, origin_answers, tolerance)
    audit[reason] += 1
    tokens, outside, unresolved = [origin], origin == "X", origin == "U"
    initial_location = trips.point_status(first.S_X_CH1903, first.S_Y_CH1903, boundary, True, transformer)
    outside |= initial_location == "outside"
    unresolved |= initial_location == "unknown"
    substantive_origin = initial_location
    pending_stage, previous_destination = False, None
    internal_candidates = 0
    for row in records:
        start = trips.point_status(row.S_X_CH1903, row.S_Y_CH1903, boundary, True, transformer)
        end = trips.point_status(row.Z_X_CH1903, row.Z_Y_CH1903, boundary, True, transformer)
        # A gap between one recorded trip's destination and the next origin
        # can conceal an activity/trip. Do not manufacture a complete chain.
        coordinate_origin = numeric_pair(row.S_X_CH1903, row.S_Y_CH1903)
        if previous_destination is not None and coordinate_origin is not None and not same_site(previous_destination, coordinate_origin, tolerance):
            unresolved = True
            audit["discontinuous_trip_coordinates"] += 1
        previous_destination = numeric_pair(row.Z_X_CH1903, row.Z_Y_CH1903)
        code, return_code = baseline.number(row.wzweck1), baseline.number(row.wzweck2)
        purpose = "H" if return_code in {2, 3} else PURPOSES.get(code, "U")
        outside |= purpose == "X"
        unresolved |= purpose == "U" or baseline.number(row.pseudo) != 1
        if purpose == "T":
            pending_stage = True
            audit["transfer_destinations_collapsed"] += 1
        else:
            # Intermediate transfer locations are not substantive activities;
            # a transfer outside the boundary does not alone make an outside chain.
            outside |= end == "outside"
            unresolved |= end == "unknown"
            tokens.append(purpose)
            pending_stage = False
            if substantive_origin == end == "inside":
                internal_candidates += 1
            substantive_origin = end
        if return_code == 3:
            audit["home_home_trip_rows"] += 1
    if pending_stage:
        audit["dangling_transfer_chain"] += 1
        unresolved = True
    if domestic == 0 and internal_candidates:
        audit["zero_domestic_count_conflicts_with_internal_trip"] += 1
        return UNKNOWN, audit
    audit["outside_chain_also_has_unresolved_details"] += int(outside and unresolved)
    return finish_chain(tokens, outside, unresolved), audit


def read_survey(base, boundary, tolerance):
    frame, selection = baseline.read_survey(base, boundary)
    extra = pd.read_csv(baseline.unique_file(base, "zielpersonen.csv"), encoding="latin1",
                        dtype={"HHNR": "string"}, usecols=EXTRA_PERSON)
    if extra.HHNR.isna().any() or extra.HHNR.duplicated().any():
        raise ValueError("Survey person identifiers are missing or duplicated; inspect locally.")
    frame = frame.merge(extra, on="HHNR", how="left", validate="one_to_one")
    diary = pd.read_csv(baseline.unique_file(base, "wege.csv"), encoding="latin1",
                        dtype={"HHNR": "string"}, usecols=trips.TRIP_COLUMNS)
    if diary.HHNR.isna().any():
        raise ValueError("Survey trip identifiers are missing; inspect locally.")
    diary = diary.loc[diary.HHNR.isin(frame.HHNR)].copy()
    groups = dict(tuple(diary.groupby("HHNR", sort=False)))
    stages = pd.read_csv(baseline.unique_file(base, "etappen.csv"), encoding="latin1",
                         dtype={"HHNR": "string"}, usecols=["HHNR", "WEGNR", "f52950"])
    # f52950 is only a fallback for the first trip's starting activity; it
    # describes the activity from which a return-home journey originated.
    stages = stages.loc[stages.HHNR.isin(frame.HHNR) & pd.to_numeric(stages.WEGNR, errors="coerce").eq(1)]
    origin_answers = stages.groupby("HHNR", sort=False).f52950.agg(list).to_dict()
    transformer = Transformer.from_crs(21781, 2056, always_xy=True)
    audit, chains = Counter(), []
    for person in frame.itertuples(index=False):
        chain, profile = survey_chain(person, groups.get(person.HHNR, diary.iloc[:0]),
                                      origin_answers.get(person.HHNR, []), boundary, transformer, tolerance)
        chains.append(chain)
        audit.update(profile)
    frame["chain"] = chains
    frame["personal"] = frame.f42100e.map(lambda v: car.survey_access(v, "any_access"))
    return frame[["age", "weight", "personal", "chain"]], {"selection": selection, "chain_audit": dict(audit)}


def model_purpose(activity):
    name = str(activity.get("type", "")).strip().casefold()
    if trips.activity_kind(activity) == "stage":
        return "T"
    if re.fullmatch(r"home_secondary\d+", name):
        return "H"
    return MODEL_PURPOSES.get(name, "U")


def model_chain(person, boundary):
    audit = Counter()
    plan, reason = trips.choose_plan(person)
    audit[reason] += 1
    if plan is None:
        return UNKNOWN, audit
    elements = [item for item in plan if trips.local_name(item) in {"act", "activity", "leg"}]
    if not elements or trips.local_name(elements[0]) == "leg" or trips.local_name(elements[-1]) == "leg":
        audit["malformed_plan"] += 1
        return UNKNOWN, audit
    if any((trips.local_name(item) == "leg") != (i % 2 == 1) for i, item in enumerate(elements)):
        audit["malformed_plan"] += 1
        return UNKNOWN, audit
    attrs = {item.get("name"): (item.text or "").strip()
             for section in person if trips.local_name(section) == "attributes"
             for item in section if trips.local_name(item) == "attribute"}
    loop_flag = any(baseline.flag(attrs.get(name)) for name in trips.LOOP_FLAGS)
    audit["persons_with_loop_flag"] += int(loop_flag)
    outside, unresolved, tokens, legs = False, False, [], 0
    if model_purpose(elements[0]) == "T" or model_purpose(elements[-1]) == "T":
        audit["incomplete_stage_chain"] += 1
        return UNKNOWN, audit
    for item in elements:
        if trips.local_name(item) == "leg":
            legs += 1
            mode = str(item.get("mode", "")).strip().casefold()
            outside |= mode == "outside"
            unresolved |= not mode
            continue
        purpose = model_purpose(item)
        if purpose == "T":
            audit["interaction_activities_collapsed"] += 1
            continue
        location = "outside" if purpose == "X" else trips.point_status(item.get("x"), item.get("y"), boundary)
        outside |= location == "outside"
        unresolved |= location == "unknown" or purpose == "U"
        if purpose == "U":
            audit["unrecognised_activity_types"] += 1
        tokens.append(purpose)
    if not legs and (tokens != ["H"] or loop_flag):
        audit["single_nonhome_or_loop_flag_without_trip"] += 1
        return UNKNOWN, audit
    if tokens == ["H"] and not legs and not unresolved:
        audit["home_only_selected_plan"] += 1
    return finish_chain(tokens, outside, unresolved), audit


def read_population(path, boundary):
    records, seen, funnel, flags, audit = [], set(), Counter(), Counter(), Counter()
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
                raise ValueError("Synthetic person identifiers are missing or duplicated; inspect locally.")
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
                chain, profile = model_chain(person, boundary)
                audit.update(profile)
                records.append({"age": age, "weight": 1.0, "personal": car.synthetic_access(attrs.get("carAvail", "").casefold()), "chain": chain})
                for name in baseline.MODEL_FLAGS:
                    flags[name] += int(baseline.flag(attrs.get(name)))
            root.clear()
    funnel["eligible_residents"] = len(records)
    if sum(n for key, n in funnel.items() if key.startswith("excluded_")) + len(records) != funnel["persons_total"]:
        raise AssertionError("Synthetic resident selection does not reconcile.")
    if not records:
        raise ValueError("No eligible synthetic residents.")
    return pd.DataFrame(records), {"selection": dict(funnel), "flags_among_eligible": dict(flags), "chain_audit": dict(audit)}


def structure(chain):
    if chain in {OUTSIDE, UNKNOWN}:
        return chain
    if chain == "H":
        return "home_only"
    if chain == "H-H":
        return "home_home_loop"
    tokens = chain.split("-")
    if tokens[0] != "H" or tokens[-1] != "H":
        return "open_or_nonhome_chain"
    if tokens.count("H") > 2:
        return "multiple_home_tours"
    return "one_tour_one_stop" if len(tokens) == 3 else "one_tour_multiple_stops"


def compare(survey, population, column, categories):
    rows = []
    for group in trips.GROUPS:
        frames = {name: frame if group == "all" else frame.loc[frame.personal.eq(group)]
                  for name, frame in [("survey", survey), ("synthetic", population)]}
        for category in categories:
            row = {"group": group, column: category}
            for name, frame in frames.items():
                total = float(frame.weight.sum())
                selected = frame.loc[frame[column].eq(category)]
                row[name + "_n"] = len(selected)
                row[name + "_weight_sum"] = float(selected.weight.sum())
                row[name + "_pct"] = 100 * row[name + "_weight_sum"] / total if total else np.nan
            row["difference_pp"] = row["synthetic_pct"] - row["survey_pct"]
            rows.append(row)
    table = pd.DataFrame(rows)
    for name, frame in [("survey", survey), ("synthetic", population)]:
        if not (np.isfinite(frame.weight) & frame.weight.gt(0)).all():
            raise ValueError("Expected positive finite person weights.")
        for group in trips.GROUPS:
            subset = frame if group == "all" else frame.loc[frame.personal.eq(group)]
            data = table.loc[table.group.eq(group)]
            if data[name + "_n"].sum() != len(subset) or (len(subset) and not np.isclose(data[name + "_pct"].sum(), 100)):
                raise AssertionError("Activity-chain categories do not reconcile with all residents.")
    return table


def top_categories(full, limit):
    overall = full.loc[full.group.eq("all") & ~full.chain.isin([OUTSIDE, UNKNOWN])].copy()
    overall["score"] = (overall.survey_pct + overall.synthetic_pct) / 2
    ranked = overall.sort_values(["score", "chain"], ascending=[False, True]).chain.tolist()
    if "H" in ranked:
        ranked.remove("H")
        ranked.insert(0, "H")
    return ranked[:limit]


def plot_comparison(table, categories, path, survey_n, synthetic_n):
    height = max(7, .47 * len(categories) + 2.2)
    fig, axes = plt.subplots(1, 3, figsize=(15, height), sharex=True, sharey=True)
    y = np.arange(len(categories))
    labels = ["Home only (H)" if c == "H" else "Other internal chains" if c == OTHER
              else STRUCTURE_LABELS.get(c, c) for c in categories]
    for ax, group in zip(axes, trips.GROUPS[:3]):
        data = table.loc[table.group.eq(group)].set_index("chain").reindex(categories)
        ax.barh(y - .19, data.survey_pct, .36, color="#237A8A", label="MZMV 2015 (WP weighted)")
        ax.barh(y + .19, data.synthetic_pct, .36, color="#DF8D39", label="Synthetic plans")
        ax.set(yticks=y, yticklabels=labels, xlabel="Share of all residents in the group (%)")
        ax.set_title(trips.GROUP_LABELS[group], fontsize=12, pad=12)
        ax.grid(axis="x", alpha=.2)
        ax.set_axisbelow(True)
        ax.axhline(len(categories) - 2.5, color=".65", linewidth=.8)
    axes[0].invert_yaxis()
    handles, legend_labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, legend_labels, loc="upper center", bbox_to_anchor=(.58, .95), ncol=2, frameon=False)
    fig.suptitle("Neuchatel: weekday activity chains of residents", fontsize=16, y=.995)
    fig.text(.06, .085, "H home/accommodation; W work; B business/service work; E education; S shopping; L leisure; O errands/escort.", fontsize=9)
    fig.text(.06, .06, "Transfers collapsed; repeated substantive visits retained. Any chain involving outside travel is shown separately; unknowns stay in totals.", fontsize=9)
    fig.text(.06, .035, f"Same resident cohort: survey n={survey_n:,}; synthetic n={synthetic_n:,}. Most common chains selected using the equally weighted shares of both sources.", fontsize=9)
    fig.tight_layout(rect=(.015, .12, 1, .91))
    fig.savefig(path, dpi=180)
    plt.close(fig)




