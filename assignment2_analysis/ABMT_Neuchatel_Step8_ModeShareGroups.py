"""Compare trip mode shares by age and household car ownership."""

import gzip
from pathlib import Path
import xml.etree.ElementTree as ET

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ABMT_Neuchatel_Step6_ModeShare import (
    MODES, find_file, load_boundary, local_tag,
    read_survey_residents, read_sim_resident_ids,
)
from ABMT_Neuchatel_Step7_ModeShareDistance import (
    read_mz_distance_trips, read_matsim_distance_trips, DISPLAY_NAMES,
)

OUT = Path(__file__).resolve().parent / "results" / "mode_share_groups"
AGE_GROUPS = ["6-17", "18-34", "35-64", "65+"]
CAR_GROUPS = ["0 cars", "1+ cars", "unknown"]
MIN_MZ_RESPONDENTS = 30
MIN_MATSIM_PERSONS = 30


def age_category(numbers):
    return pd.cut(
        pd.to_numeric(numbers, errors="coerce"),
        bins=[5, 17, 34, 64, 120], labels=AGE_GROUPS, include_lowest=True,
    ).astype("string")


def car_category(numbers):
    cars = pd.to_numeric(numbers, errors="coerce")
    return pd.Series(
        np.where(cars.isna() | (cars < 0), pd.NA,
                 np.where(cars == 0, "0 cars", "1+ cars")),
        index=numbers.index, dtype="string",
    )


def survey_person_attributes(eligible):
    from abmt_common import person_attributes
    a,b=person_attributes()
    return a.loc[a.HHNR.isin(eligible.HHNR)].copy()



def synthetic_person_attributes(eligible_ids):
    from abmt_common import person_attributes
    a,b=person_attributes()
    return b.loc[b.person.isin(eligible_ids)].copy()



def compare_by(df_mz, df_sim, group_column, categories):
    rows = []
    coverage = []
    for group in categories:
        mz = df_mz.loc[df_mz[group_column].eq(group).fillna(False)]
        sim = df_sim.loc[df_sim[group_column].eq(group).fillna(False)]
        mz_people = mz["HHNR"].nunique()
        sim_people = sim["person"].nunique()
        sufficient = mz_people >= MIN_MZ_RESPONDENTS and sim_people >= MIN_MATSIM_PERSONS
        coverage.append({
            "group_dimension": group_column, "group": group,
            "mz_trip_count": len(mz), "mz_distinct_respondents": mz_people,
            "matsim_trip_count": len(sim), "matsim_distinct_persons": sim_people,
            "comparison_minimum_sample_met": sufficient,
        })
        mz_weights = mz.groupby("mode")["WP"].sum().reindex(MODES, fill_value=0.)
        sim_counts = sim.groupby("mode").size().reindex(MODES, fill_value=0)
        mz_total = float(mz_weights.sum())
        sim_total = int(sim_counts.sum())
        for mode in MODES:
            mz_pct = float(mz_weights[mode] / mz_total * 100) if mz_total else np.nan
            sim_pct = float(sim_counts[mode] / sim_total * 100) if sim_total else np.nan
            rows.append({
                "group_dimension": group_column, "group": group, "mode": mode,
                "microcensus_weighted_pct": mz_pct, "matsim_pct": sim_pct,
                "difference_pp": sim_pct - mz_pct,
                "comparison_minimum_sample_met": sufficient,
                "mz_distinct_respondents": mz_people, "matsim_distinct_persons": sim_people,
            })
    result, support = pd.DataFrame(rows), pd.DataFrame(coverage)
    if int(support.mz_trip_count.sum()) != len(df_mz) or int(support.matsim_trip_count.sum()) != len(df_sim):
        raise AssertionError("A demographic/spatial/time partition lost or duplicated primary trips.")
    return result, support


def plot_pt(table, categories, dimension, title):
    pt = table[table["mode"].eq("pt")].set_index("group").reindex(categories)
    valid = pt["comparison_minimum_sample_met"].fillna(False).astype(bool)
    mz = pt["microcensus_weighted_pct"].where(valid)
    sim = pt["matsim_pct"].where(valid)
    x = np.arange(len(categories))
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar(x - .2, mz, .4, label="Mikrozensus (weighted)")
    ax.bar(x + .2, sim, .4, label="MATSim (10% sample)")
    ax.set_xticks(x, categories)
    ax.set_ylabel("Share of trips using public transport (%)")
    ax.set_title(title)
    ax.legend()
    ax.grid(axis="y", alpha=.2)
    fig.text(.5, .01, "Groups with <30 distinct survey respondents or simulated persons omitted.",
             ha="center", fontsize=9)
    fig.tight_layout(rect=(0, .035, 1, 1))
    fig.savefig(OUT / f"pt_mode_share_by_{dimension}.png", dpi=190)
    plt.close(fig)


def plot_all_modes(table, categories, dimension, title):
    x = np.arange(len(categories))
    fig, ax = plt.subplots(figsize=(10.5, 5.6))
    bottom_mz = np.zeros(len(categories))
    bottom_sim = np.zeros(len(categories))
    for mode in MODES:
        part = table[table["mode"].eq(mode)].set_index("group").reindex(categories)
        valid = part["comparison_minimum_sample_met"].fillna(False).astype(bool).to_numpy()
        mz = np.where(valid, part["microcensus_weighted_pct"].to_numpy(), np.nan)
        sim = np.where(valid, part["matsim_pct"].to_numpy(), np.nan)
        bars = ax.bar(x - .2, mz, .4, bottom=bottom_mz, label=DISPLAY_NAMES[mode])
        color = bars.patches[0].get_facecolor()
        ax.bar(x + .2, sim, .4, bottom=bottom_sim, color=color, alpha=.75)
        bottom_mz += np.nan_to_num(mz)
        bottom_sim += np.nan_to_num(sim)
    ax.set_xticks(x, categories)
    ax.set_ylim(0, 110)
    ax.set_ylabel("Share of trips within group (%)")
    ax.set_title(title)
    ax.legend(loc="upper center", ncol=5, fontsize=8)
    ax.grid(axis="y", alpha=.18)
    fig.text(.5, .015, "For each group: left bar = MZ; right bar = MATSim. Small groups omitted.",
             ha="center", fontsize=9)
    fig.tight_layout(rect=(0, .055, 1, 1))
    fig.savefig(OUT / f"mode_share_by_{dimension}.png", dpi=190)
    plt.close(fig)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    boundary = load_boundary()
    survey_people = read_survey_residents(boundary)
    sim_people = read_sim_resident_ids(boundary)
    mz_trips, mz_info = read_mz_distance_trips(survey_people)
    sim_trips, sim_info = read_matsim_distance_trips(sim_people)
    mz = mz_trips.merge(survey_person_attributes(survey_people), on="HHNR", validate="many_to_one")
    sim = sim_trips.merge(synthetic_person_attributes(sim_people), on="person", validate="many_to_one")
    if mz["age_group"].isna().all() or sim["age_group"].isna().all():
        raise ValueError("Age-group data missing after joining trips and people")
    for dimension, categories, title in (
        ("age_group", AGE_GROUPS, "Age group"),
        ("car_group", CAR_GROUPS, "Household car ownership"),
        ("personal", ["available","unavailable","unknown"], "Personal car availability"),
        ("personal_always_only", ["available","unavailable","unknown"], "Always-only car availability sensitivity"),
    ):
        results, coverage = compare_by(mz, sim, dimension, categories)
        results.to_csv(OUT / f"mode_share_by_{dimension}.csv", index=False, float_format="%.6f")
        coverage.to_csv(OUT / f"coverage_by_{dimension}.csv", index=False)
        plot_pt(results, categories, dimension, f"Neuchatel – public transport share by {title.lower()}")
        plot_all_modes(results, categories, dimension, f"Neuchatel – all modes by {title.lower()}")

    notes = [
        "Mode shares within age and household car-ownership groups.",
        "Neuchatel residents aged 6–120; weekdays and WP weights for Mikrozensus.",
        "Trip modes and distance >1 m selection inherited from Step 7.",
        "MATSim 10% sample unweighted. Artificial outside and special modes excluded.",
        "Car ownership: MZ households.f30100; MATSim person attribute numberOfCars.",
        "Groups with fewer than 30 distinct people per dataset omitted from figures, not CSV.",
        "Persons without trips do not contribute to trip-mode denominators.",
        f"Survey filters: {mz_info}", f"MATSim filters: {sim_info}",
    ]
    (OUT / "mode_share_groups_methodology.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
    from abmt_common import write_method, output
    write_method(output('mode_share_groups'))
