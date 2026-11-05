#!/usr/bin/env python3
'Internal reconciled reader/utility derived from the reviewed analysis scripts. See README.md and abmt_common.METHOD for the active definitions.'

import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import _abmt_chains as chains
import _abmt_spatial as spatial
trips, baseline, car = chains.trips, chains.baseline, chains.car
import matplotlib.pyplot as plt
from matplotlib.colors import Normalize
from matplotlib.patches import PathPatch
from matplotlib.ticker import FuncFormatter
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import Point


PURPOSES = ["W", "B", "E", "S", "L", "O"]
PURPOSE_LABELS = {"W": "Work", "B": "Business / service work", "E": "Education",
                  "S": "Shopping", "L": "Leisure", "O": "Errands / escort"}
VISIT_FIELDS = ["person_key", "personal", "weight", "x", "y", "purpose"]


def record_visit(key, personal, weight, purpose, x, y, boundary, audit, survey=False, transformer=None):
    if purpose == "H":
        audit["home_destination"] += 1
    elif purpose == "T":
        audit["transfer"] += 1
    elif purpose == "X":
        audit["outside_destination"] += 1
    elif purpose not in PURPOSES:
        audit["unresolved_purpose"] += 1
    else:
        location = trips.point_status(x, y, boundary, survey, transformer)
        if location == "outside":
            audit["outside_destination"] += 1
        elif location == "unknown":
            audit["unresolved_location"] += 1
        else:
            if survey:
                x, y = transformer.transform(float(x), float(y))
            audit["mapped_nonhome_inside"] += 1
            return dict(person_key=key, personal=personal, weight=weight, x=float(x), y=float(y), purpose=purpose)
    return None


def survey_visits(person, rows, boundary, transformer):
    audit, visits = Counter(), []
    travelled, domestic = baseline.number(person.f50200), baseline.number(person.T_WEGE)
    if rows.empty:
        known_zero = travelled == 2 or domestic == 0
        conflict = travelled == 2 and domestic is not None and domestic > 0
        return visits, "unresolved_diary" if conflict or not known_zero else "no_recorded_trips", audit
    order = pd.to_numeric(rows.WEGNR, errors="coerce")
    valid_order = np.isfinite(order) & order.gt(0) & order.mod(1).eq(0)
    if not valid_order.all() or order.duplicated().any() or travelled == 2:
        audit["discarded_invalid_diary_or_plan"] += len(rows)
        return visits, "unresolved_diary", audit
    sequence = order.sort_values().to_numpy(dtype=int)
    partial = sequence[0] != 1 or (np.diff(sequence) != 1).any() or (domestic is not None and domestic > len(rows))
    personal = car.survey_access(person.f42100e, "any_access")
    for row in rows.itertuples(index=False):
        purpose = ("H" if baseline.number(row.wzweck2) in {2, 3}
                   else chains.PURPOSES.get(baseline.number(row.wzweck1), "U"))
        if purpose in PURPOSES and baseline.number(row.pseudo) != 1:
            audit["pseudo_or_uncertain"] += 1
            continue
        visit = record_visit(person.HHNR, personal, person.weight, purpose,
                             row.Z_X_CH1903, row.Z_Y_CH1903, boundary, audit, True, transformer)
        if visit is not None:
            visits.append(visit)
    if domestic == 0 and visits:
        audit = Counter({"discarded_invalid_diary_or_plan": len(rows)})
        return [], "unresolved_diary", audit
    # A known visit is retained even if another destination or diary portion
    # is unresolved. Coverage diagnostics expose this partial observation.
    partial |= any(audit[r] for r in ["unresolved_purpose", "unresolved_location", "pseudo_or_uncertain"])
    return visits, "partially_resolved_diary" if partial else "resolved_diary", audit


def read_survey(base, boundary):
    people, selection = baseline.read_survey(base, boundary)
    extra = pd.read_csv(baseline.unique_file(base, "zielpersonen.csv"), encoding="latin1",
                        dtype={"HHNR": "string"}, usecols=["HHNR", "f50200", "T_WEGE"])
    if extra.HHNR.isna().any() or extra.HHNR.duplicated().any():
        raise ValueError("Survey person identifiers are missing or duplicated; inspect locally.")
    people = people.merge(extra, on="HHNR", how="left", validate="one_to_one")
    diary = pd.read_csv(baseline.unique_file(base, "wege.csv"), encoding="latin1",
                        dtype={"HHNR": "string"}, usecols=trips.TRIP_COLUMNS)
    if diary.HHNR.isna().any():
        raise ValueError("Survey trip identifiers are missing; inspect locally.")
    diary = diary.loc[diary.HHNR.isin(people.HHNR)].copy()
    groups = dict(tuple(diary.groupby("HHNR", sort=False)))
    transformer = Transformer.from_crs(21781, 2056, always_xy=True)
    visits, resident_rows, audit = [], [], Counter()
    for person in people.itertuples(index=False):
        items, state, profile = survey_visits(person, groups.get(person.HHNR, diary.iloc[:0]), boundary, transformer)
        visits.extend(items)
        audit.update(profile)
        resident_rows.append(dict(person_key=person.HHNR, weight=person.weight,
                                  personal=car.survey_access(person.f42100e, "any_access"), record_status=state))
    return pd.DataFrame(visits, columns=VISIT_FIELDS), pd.DataFrame(resident_rows), {"selection": selection, "destination_audit": dict(audit)}


def model_visits(person, key, personal, boundary):
    audit, visits = Counter(), []
    plan, chosen = trips.choose_plan(person)
    audit[chosen] += 1
    if plan is None:
        return visits, "unresolved_diary", audit
    elements = [item for item in plan if trips.local_name(item) in {"act", "activity", "leg"}]
    malformed = (not elements or trips.local_name(elements[0]) == "leg" or trips.local_name(elements[-1]) == "leg"
                 or any((trips.local_name(item) == "leg") != (i % 2 == 1) for i, item in enumerate(elements)))
    if malformed or chains.model_purpose(elements[0]) == "T" or chains.model_purpose(elements[-1]) == "T":
        audit["discarded_invalid_diary_or_plan"] += sum(trips.local_name(item) != "leg" for item in elements)
        return visits, "unresolved_diary", audit
    seen_main = False
    for item in elements:
        if trips.local_name(item) == "leg":
            continue
        purpose = chains.model_purpose(item)
        if purpose == "T":
            audit["transfer"] += 1
            continue
        if not seen_main:
            audit["initial_activity_excluded"] += 1
            seen_main = True
            continue
        visit = record_visit(key, personal, 1.0, purpose, item.get("x"), item.get("y"), boundary, audit)
        if visit is not None:
            visits.append(visit)
    partial = audit["unresolved_purpose"] or audit["unresolved_location"]
    return visits, "partially_resolved_diary" if partial else "resolved_diary", audit


def read_population(path, boundary):
    resident_rows, visits, seen, funnel, flags, audit = [], [], set(), Counter(), Counter(), Counter()
    with gzip.open(path, "rb") as stream:
        root = None
        for event, person in ET.iterparse(stream, events=("start", "end")):
            if root is None:
                root = person
            if event != "end" or trips.local_name(person) != "person":
                continue
            funnel["persons_total"] += 1
            key = person.get("id")
            if not key or key in seen:
                raise ValueError("Synthetic person identifiers are missing or duplicated; inspect locally.")
            seen.add(key)
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
                personal = car.synthetic_access(attrs.get("carAvail", "").casefold())
                items, state, profile = model_visits(person, key, personal, boundary)
                visits.extend(items)
                audit.update(profile)
                resident_rows.append(dict(person_key=key, personal=personal, weight=1.0, record_status=state))
                for name in baseline.MODEL_FLAGS:
                    flags[name] += int(baseline.flag(attrs.get(name)))
            root.clear()
    funnel["eligible_residents"] = len(resident_rows)
    if sum(n for key, n in funnel.items() if key.startswith("excluded_")) + len(resident_rows) != funnel["persons_total"]:
        raise AssertionError("Synthetic resident selection does not reconcile.")
    if not resident_rows:
        raise ValueError("No eligible synthetic residents.")
    return pd.DataFrame(visits, columns=VISIT_FIELDS), pd.DataFrame(resident_rows), {"selection": dict(funnel), "flags_among_eligible": dict(flags), "destination_audit": dict(audit)}


def subset(frame, group):
    return frame if group == "all" else frame.loc[frame.personal.eq(group)]


def make_tables(survey, model, survey_people, model_people, grid, geometry, cell_size, minimum):
    grids, summaries, purposes = [], [], []
    # Assign coordinates once per source, before conditioning on car access.
    for visits in [survey, model]:
        if len(visits) and not (np.isfinite(visits.weight) & visits.weight.gt(0)).all():
            raise ValueError("Expected positive finite visit weights.")
        keys = spatial.assign_cells(visits, geometry, cell_size)
        visits["ix"] = [key[0] for key in keys]
        visits["iy"] = [key[1] for key in keys]
    for group in trips.GROUPS:
        table = grid.copy()
        table.insert(0, "group", group)
        summary = {"group": group}
        purpose_rows = {p: {"group": group, "purpose": p, "label": PURPOSE_LABELS[p]} for p in PURPOSES}
        for name, visits, residents in [("survey", survey, survey_people), ("synthetic", model, model_people)]:
            v, people = subset(visits, group), subset(residents, group)
            total = float(v.weight.sum())
            resident_weight = float(people.weight.sum())
            if not (np.isfinite(people.weight) & people.weight.gt(0)).all():
                raise ValueError("Expected positive finite resident weights.")
            summary.update({name + "_residents_n": len(people), name + "_resident_weight_sum": resident_weight,
                            name + "_mapped_visits_n": len(v), name + "_mapped_visit_weight_sum": total,
                            name + "_residents_with_mapped_visits_n": int(v.person_key.nunique()),
                            name + "_mapped_visits_per_100_residents": 100 * total / resident_weight if resident_weight else np.nan})
            for state in ["unresolved_diary", "partially_resolved_diary"]:
                selected = people.loc[people.record_status.eq(state)]
                summary[name + "_" + state + "_residents_n"] = len(selected)
                summary[name + "_" + state + "_resident_pct"] = 100 * selected.weight.sum() / resident_weight if resident_weight else np.nan
            aggregate = v.groupby(["ix", "iy"]).agg(visits_n=("weight", "size"),
                            contributors_n=("person_key", "nunique"), visit_weight_sum=("weight", "sum")).reset_index()
            aggregate = aggregate.rename(columns={c: name + "_" + c for c in ["visits_n", "contributors_n", "visit_weight_sum"]})
            table = table.merge(aggregate, on=["ix", "iy"], how="left", validate="one_to_one")
            for column in ["visits_n", "contributors_n", "visit_weight_sum"]:
                c = name + "_" + column
                table[c] = pd.to_numeric(table[c], errors="raise").astype(float).fillna(0)
                if column.endswith("_n"):
                    table[c] = table[c].astype(int)
            table[name + "_visit_pct"] = 100 * table[name + "_visit_weight_sum"] / total if total else np.nan
            table[name + "_visits_per_100_residents"] = (100 * table[name + "_visit_weight_sum"] / resident_weight if resident_weight else np.nan)
            if table[name + "_visits_n"].sum() != len(v) or (total and not np.isclose(table[name + "_visit_pct"].sum(), 100)):
                raise AssertionError("Activity visits do not reconcile with grid totals.")
            for p in PURPOSES:
                chosen = v.loc[v.purpose.eq(p)]
                purpose_rows[p][name + "_visits_n"] = len(chosen)
                purpose_rows[p][name + "_visit_weight_sum"] = float(chosen.weight.sum())
                purpose_rows[p][name + "_visit_pct"] = 100 * chosen.weight.sum() / total if total else np.nan
        table["difference_pp"] = table.synthetic_visit_pct - table.survey_visit_pct
        table["difference_visits_per_100_residents"] = table.synthetic_visits_per_100_residents - table.survey_visits_per_100_residents
        table["difference_shown"] = (table.survey_contributors_n.ge(minimum) & table.synthetic_contributors_n.ge(minimum)
                                      & table.difference_pp.notna())
        table["difference_pp_displayed"] = table.difference_pp.where(table.difference_shown)
        for name in ["survey", "synthetic"]:
            summary[name + "_visit_pct_in_supported_cells"] = float(table.loc[table.difference_shown, name + "_visit_pct"].sum())
        summary["supported_cells_n"] = int(table.difference_shown.sum())
        for row in purpose_rows.values():
            row["difference_pp"] = row["synthetic_visit_pct"] - row["survey_visit_pct"]
            purposes.append(row)
        grids.append(table)
        summaries.append(summary)
    return pd.concat(grids, ignore_index=True), pd.DataFrame(summaries), pd.DataFrame(purposes)


def plot_maps(table, geometry, polygon, group, cell_size, minimum, share_max, diff_limit, summary, path):
    data = table.loc[table.group.eq(group)]
    paths = {key: spatial.polygon_path(value) for key, value in geometry.items()}
    outline = spatial.polygon_path(polygon)
    norms = [Normalize(0, share_max), Normalize(0, share_max), Normalize(-diff_limit, diff_limit)]
    cmaps = [plt.get_cmap("YlGnBu"), plt.get_cmap("YlGnBu"), plt.get_cmap("RdBu_r")]
    fig = plt.figure(figsize=(15.5, 7.6), layout="constrained")
    spec = fig.add_gridspec(3, 3, height_ratios=[1, .045, .23])
    axes = [fig.add_subplot(spec[0, i]) for i in range(3)]
    xmin, ymin, xmax, ymax = polygon.bounds
    margin = max(xmax - xmin, ymax - ymin) * .04
    for i, (ax, column, title) in enumerate(zip(axes, ["survey_visit_pct", "synthetic_visit_pct", "difference_pp"],
                                               ["MZMV 2015 (WP weighted)", "Synthetic plans", "Synthetic minus survey"])):
        for row in data.itertuples(index=False):
            value = getattr(row, column)
            masked = pd.isna(value) or (i == 2 and not row.difference_shown)
            ax.add_patch(PathPatch(paths[(row.ix, row.iy)], facecolor="#ECECEC" if masked else cmaps[i](norms[i](value)),
                                   edgecolor="#AAAAAA" if masked else "white", linewidth=.35, hatch="///" if masked else None))
        ax.add_patch(PathPatch(outline, facecolor="none", edgecolor="#222222", linewidth=.8))
        ax.set(xlim=(xmin - margin, xmax + margin), ylim=(ymin - margin, ymax + margin), title=title, xlabel="Easting (LV95 km)")
        ax.set_aspect("equal", adjustable="box")
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, pos: f"{value / 1000:.0f}"))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, pos: f"{value / 1000:.0f}"))
        ax.tick_params(labelsize=8, labelleft=i == 0)
        if i == 0:
            ax.set_ylabel("Northing (LV95 km)")
        color_ax = fig.add_subplot(spec[1, i])
        fig.colorbar(plt.cm.ScalarMappable(norm=norms[i], cmap=cmaps[i]), cax=color_ax, orientation="horizontal",
                     label="Difference in percentage points" if i == 2 else "Share of mapped non-home destination visits (%)")
    row = summary.loc[summary.group.eq(group)].iloc[0]
    footer = fig.add_subplot(spec[2, :])
    footer.axis("off")
    footer.text(0, .95,
                f"Residents: survey n={int(row.survey_residents_n):,}; synthetic n={int(row.synthetic_residents_n):,}. "
                f"Mapped visits: survey n={int(row.survey_mapped_visits_n):,}; synthetic n={int(row.synthetic_mapped_visits_n):,}.\n"
                "Repeated visits count repeatedly; initial activities, home destinations, transfers and artificial outside destinations are excluded.\n"
                "Share denominators: all mapped inside-area destination visits in each group, including visits in sparse cells. All three figures use common colour scales.\n"
                f"Difference hatching: fewer than {minimum} distinct survey OR synthetic contributors in the cell; heuristic, not a significance test.\n"
                "Visits reached from outside are included. Coverage and mapped visits per 100 residents are reported separately; these maps do not measure total trip frequency.",
                va="top", fontsize=8.5, linespacing=1.55)
    fig.suptitle(f"Neuchatel: non-home destination visits ({cell_size / 1000:g} km grid)\n{trips.GROUP_LABELS[group]}", fontsize=14)
    fig.savefig(path, dpi=180)
    plt.close(fig)




