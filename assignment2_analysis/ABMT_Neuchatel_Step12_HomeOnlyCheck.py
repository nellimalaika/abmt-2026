"""Compare recorded home-only plans and activity-chain sensitivity in Neuchatel."""

from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ABMT_Neuchatel_Step6_ModeShare import (
    find_file, load_boundary, read_survey_residents,
    read_sim_resident_ids, local_tag, to_float,
)
from ABMT_Neuchatel_Step4_ActivityChains import MZ_PURPOSE, canon

VERSION = "ABMT Neuchatel Step 12 – home-only sensitivity v2"
OUT = Path(__file__).resolve().parent / "results" / "home_only_check"
HOME = "home only"
OTHER = "other chains"


def survey_status(people: pd.DataFrame) -> pd.DataFrame:
    ways = pd.read_csv(
        find_file("wege.csv", "Mikrozensus"), encoding="latin1",
        dtype={"HHNR": "string", "WEGNR": "string"},
        usecols=["HHNR", "WEGNR", "wzweck1", "wzweck2"], low_memory=False,
    )
    ways = ways.loc[ways.HHNR.isin(people.HHNR)].copy()
    if ways.duplicated(["HHNR", "WEGNR"]).any():
        raise ValueError("Duplicate survey trip identifiers")
    for field in ("WEGNR", "wzweck1", "wzweck2"):
        ways[field] = pd.to_numeric(ways[field], errors="coerce")
    groups = dict(tuple(ways.sort_values(["HHNR", "WEGNR"], kind="stable")
                        .groupby("HHNR", sort=False)))

    rows = []
    for person in people.itertuples(index=False):
        group = groups.get(str(person.HHNR))
        age, weight = float(person.alter), float(person.WP)
        if group is None or group.empty:
            rows.append((age, weight, "no_way_record", ""))
            continue

        activities = ["home"]
        valid = True
        for trip in group.itertuples(index=False):
            if pd.isna(trip.WEGNR) or pd.isna(trip.wzweck1):
                valid = False
                break
            purpose = MZ_PURPOSE.get(int(trip.wzweck1), "unknown")
            if pd.notna(trip.wzweck2) and trip.wzweck2 > 1:
                purpose = "home"
            if purpose == "interaction":
                continue
            if purpose in ("unknown", "border"):
                valid = False
                break
            activities.append(purpose)

        complete = valid and len(activities) >= 2 and activities[-1] == "home"
        rows.append((age, weight, "complete_chain" if complete else "incomplete",
                     " > ".join(activities) if complete else ""))

    result = pd.DataFrame(rows, columns=["age", "weight", "status", "chain"])
    if len(result) != len(people):
        raise ValueError("Survey denominator mismatch")
    if (result.weight <= 0).any() or not np.isfinite(result.weight).all():
        raise ValueError("Invalid survey weights")
    return result


def sim_status(resident_ids: set[str]) -> tuple[pd.DataFrame, dict]:
    path = find_file("neuchatel_population.xml.gz", "Neuchatel_10pct")
    rows, visited = [], set()
    with gzip.open(path, "rb") as stream:
        root = None
        for event, person in ET.iterparse(stream, events=("start", "end")):
            if root is None:
                root = person
            if event != "end" or local_tag(person.tag) != "person":
                continue
            pid = str(person.get("id", ""))
            if pid not in resident_ids:
                root.clear()
                continue
            if pid in visited:
                raise ValueError("Duplicate simulated person")
            visited.add(pid)

            attributes = {
                item.get("name", ""): (item.text or "").strip()
                for section in person if local_tag(section.tag) == "attributes"
                for item in section if local_tag(item.tag) == "attribute"
            }
            age = to_float(attributes.get("age"))
            if age is None:
                raise ValueError("Simulated resident without age")
            plans = [item for item in person if local_tag(item.tag) == "plan"]
            selected = [item for item in plans if str(item.get("selected", "")).lower() in
                        ("true", "yes", "1")]
            plan = (selected[0] if len(selected) == 1 else
                    plans[0] if not selected and len(plans) == 1 else None)

            status, chain = "incomplete", ""
            if plan is not None:
                activities = [canon(item.get("type", "")) for item in plan.iter()
                              if local_tag(item.tag) in ("act", "activity")]
                legs = [str(item.get("mode", "")).strip().lower() for item in plan.iter()
                        if local_tag(item.tag) == "leg"]
                invalid = (not activities or
                           any(activity in ("outside", "unknown") for activity in activities) or
                           any(any(flag in leg for flag in ("outside", "crossborder", "freight"))
                               for leg in legs))
                if not invalid:
                    purposes = [activity for activity in activities if activity != "interaction"]
                    if len(activities) == 1 and purposes == ["home"] and not legs:
                        status, chain = "home_only_plan", HOME
                    elif len(purposes) >= 2 and purposes[0] == purposes[-1] == "home" and legs:
                        status, chain = "complete_chain", " > ".join(purposes)
            rows.append((age, 1.0, status, chain))
            root.clear()

    if visited != resident_ids or len(rows) != len(resident_ids):
        raise ValueError("Simulated resident denominator mismatch")
    return pd.DataFrame(rows, columns=["age", "weight", "status", "chain"]), {}


def summarize_coverage(mz: pd.DataFrame, sim: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for cohort in ("all", "65+"):
        for source, data in (("Mikrozensus", mz), ("MATSim", sim)):
            group = data if cohort == "all" else data.loc[data.age >= 65]
            total = group.weight.sum()
            if group.empty or total <= 0:
                raise ValueError("Empty eligible cohort")
            for status in ("complete_chain", "no_way_record", "home_only_plan", "incomplete"):
                selected = group.loc[group.status.eq(status)]
                rows.append({
                    "cohort": cohort, "source": source, "status": status,
                    "n_persons": len(selected),
                    "weighted_share_pct": float(100 * selected.weight.sum() / total),
                })
    return pd.DataFrame(rows)


def sensitivity_chains(mz: pd.DataFrame, sim: pd.DataFrame, cohort: str) -> pd.DataFrame:
    a = mz if cohort == "all" else mz.loc[mz.age >= 65]
    b = sim if cohort == "all" else sim.loc[sim.age >= 65]
    a = a.loc[a.status.isin(("complete_chain", "no_way_record"))].copy()
    b = b.loc[b.status.isin(("complete_chain", "home_only_plan"))].copy()
    if a.empty or b.empty:
        raise ValueError("Missing comparable chains")
    a.loc[a.status.eq("no_way_record"), "chain"] = HOME
    b.loc[b.status.eq("home_only_plan"), "chain"] = HOME

    top = (a.loc[a.chain.ne(HOME)].groupby("chain").weight.sum()
           .sort_values(ascending=False).head(9).index.tolist())
    categories = [HOME] + top + [OTHER]

    def shares(df):
        cat = df.chain.where(df.chain.isin(categories[:-1]), OTHER)
        weights = df.assign(category=cat).groupby("category").weight.sum()
        return weights.reindex(categories, fill_value=0) / df.weight.sum() * 100

    m, s = shares(a), shares(b)
    table = pd.DataFrame({"chain": categories,
                          "mz_pct_IF_no_way_means_home": m.values,
                          "matsim_pct": s.values})
    table["difference_pp_under_assumption"] = (
        table.matsim_pct - table.mz_pct_IF_no_way_means_home)
    if abs(m.sum() - 100) > 1e-7 or abs(s.sum() - 100) > 1e-7:
        raise ValueError("Chain shares do not sum to 100%")

    fig, ax = plt.subplots(figsize=(11, max(5.2, 0.46 * len(categories) + 1.3)))
    y = np.arange(len(categories))
    ax.barh(y - 0.2, table.mz_pct_IF_no_way_means_home, 0.38,
            label="Mikrozensus (weighted; no-way=home assumed)")
    ax.barh(y + 0.2, table.matsim_pct, 0.38, label="MATSim (10% population)")
    ax.set_yticks(y, categories)
    ax.invert_yaxis()
    ax.set_xlabel("Share of classifiable residents in sensitivity scenario (%)")
    ax.set_title("Neuchatel activity chains incl. tentative home only – " +
                 ("classifiable residents age 6+" if cohort == "all" else "classifiable residents age 65+"))
    ax.legend(loc="lower right", fontsize=9)
    ax.grid(axis="x", alpha=0.2)
    fig.text(0.5, 0.009,
             "SENSITIVITY ONLY: MZ no recorded ways is NOT verified home-only. "
             "Incomplete cases excluded in both datasets.", ha="center", fontsize=8)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    name = "home_only_sensitivity_all" if cohort == "all" else "home_only_sensitivity_age65plus"
    fig.savefig(OUT / f"{name}.png", dpi=185)
    plt.close(fig)
    table.insert(0, "cohort", cohort)
    return table


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    boundary = load_boundary()
    mz = survey_status(read_survey_residents(boundary))
    sim, _ = sim_status(read_sim_resident_ids(boundary))
    summarize_coverage(mz, sim).to_csv(
        OUT / "home_only_coverage.csv", index=False, float_format="%.5f")
    tables = [sensitivity_chains(mz, sim, cohort) for cohort in ("all", "65+")]
    pd.concat(tables, ignore_index=True).to_csv(
        OUT / "home_only_sensitivity.csv", index=False, float_format="%.5f")
    notes = [
        "Mikrozensus: missing recorded ways are not independently confirmed home-only.",
        "Plots assume missing MZ ways mean home-only solely as a sensitivity case.",
        "MATSim: one selected home activity without legs is classified as home-only.",
        "Home-to-home trips with legs remain mobile; incomplete chains are excluded.",
        "Only eligible Neuchatel weekday residents aged 6+ are included.",
        "Mikrozensus WP weights are used; MATSim 10% sample is unweighted.",
        "Step 4 presents the separate mobile-chain comparison.",
    ]
    (OUT / "home_only_methodology.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
