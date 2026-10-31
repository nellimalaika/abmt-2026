"""Compare recorded standard-mode trips per eligible resident, including zero trips."""
from __future__ import annotations

import gzip
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ABMT_Neuchatel_Step6_ModeShare import (
    MODES, find_file, read_csv_header, load_boundary,
    read_survey_residents, read_survey_trips, read_sim_resident_ids,
    classify_sim_mode,
)
from ABMT_Neuchatel_Step8_ModeShareGroups import synthetic_person_attributes

OUT = Path(__file__).resolve().parent / "results" / "trips_per_person"
CATEGORIES = [str(i) for i in range(8)] + ["8+"]

def read_matsim_trip_classes(resident_ids: set[str]) -> pd.DataFrame:

    filename = find_file("output_trips.csv.gz", "simulation_output")
    fields, sep = read_csv_header(filename)
    person_col = next((v for v in ("person", "person_id", "personId") if v in fields), None)
    mode_col = next((v for v in ("main_mode", "mainMode", "mode") if v in fields), None)
    if not (person_col and mode_col):
        raise ValueError("Missing person/main_mode in MATSim output_trips header.")
    purpose_cols = [v for v in ("start_activity_type", "end_activity_type") if v in fields]
    data = pd.read_csv(filename, sep=sep, compression="gzip", low_memory=False,
                       usecols=[person_col, mode_col] + purpose_cols,
                       dtype={person_col: "string"})
    data = data.loc[data[person_col].isin(resident_ids)].copy()
    for column in purpose_cols:
        artificial = data[column].fillna("").astype("string").str.lower().str.contains(
            r"outside|crossborder|freight|^border", regex=True
        ).fillna(False)
        data = data.loc[~artificial].copy()
    data["class"] = data[mode_col].map(classify_sim_mode)
    if data["class"].eq("unknown").any():
        raise ValueError("Unrecognized MATSim trip modes")
    counts = data.loc[data["class"].isin(MODES)].groupby(person_col).size()
    return counts.rename("ordinary").rename_axis("person").reset_index()

def per_person_survey(people: pd.DataFrame, trips: pd.DataFrame) -> pd.DataFrame:
    counts = trips.groupby("HHNR").size().rename("trips")
    result = people[["HHNR", "alter", "WP"]].merge(
        counts, left_on="HHNR", right_index=True, how="left", validate="one_to_one")
    result["trips"] = result["trips"].fillna(0).astype(int)
    result["weight"] = result["WP"].astype(float)
    result["age65"] = result["alter"].ge(65)
    return result

def per_person_sim(people_ids: set[str], sim_classes: pd.DataFrame) -> pd.DataFrame:
    people = synthetic_person_attributes(people_ids)[["person", "age_group"]].copy()
    if len(people) != len(people_ids) or people["person"].duplicated().any():
        raise ValueError("Synthetic person counts do not match eligible resident IDs.")
    result = people.merge(sim_classes, on="person", how="left", validate="one_to_one")
    result["trips"] = result["ordinary"].fillna(0).astype(int)
    result["weight"] = 1.0
    result["age65"] = result["age_group"].eq("65+").fillna(False)
    return result

def weighted_median(x, w):
    x = np.asarray(x, dtype=float)
    w = np.asarray(w, dtype=float)
    order = np.argsort(x)
    cs = np.cumsum(w[order])
    if len(cs) == 0 or cs[-1] <= 0:
        return float("nan")
    return float(x[order][np.searchsorted(cs, cs[-1] / 2.0)])

def category(counts: pd.Series) -> pd.Series:
    return counts.map(lambda n: str(int(n)) if int(n) < 8 else "8+")

def group_metrics(data: pd.DataFrame, source: str, cohort: str,
                  count_col: str = "trips") -> tuple[pd.DataFrame, dict]:
    if data.empty:
        return pd.DataFrame({"trips_per_person": CATEGORIES,
                             "share_pct": [float("nan")] * len(CATEGORIES)}), {
                                 "source": source, "cohort": cohort,
                                 "persons": 0, "mean_trips": float("nan"),
                                 "median_trips": float("nan"), "zero_trip_share_pct": float("nan")}
    weights = data["weight"].to_numpy(dtype=float)
    counts = data[count_col].to_numpy(dtype=float)
    if not np.all(np.isfinite(weights)) or np.any(weights <= 0):
        raise ValueError("Person weights must be strictly positive and finite.")
    freq = data.assign(bin=category(data[count_col])).groupby("bin")["weight"].sum()
    freq = freq.reindex(CATEGORIES, fill_value=0.0) / weights.sum() * 100
    metric = {
        "source": source,
        "cohort": cohort,
        "persons": len(data),
        "mean_trips": float(np.average(counts, weights=weights)),
        "median_trips": weighted_median(counts, weights),
        "zero_trip_share_pct": float(weights[counts == 0].sum() / weights.sum() * 100),
        "share_with_one_plus_trips_pct": float(weights[counts > 0].sum() / weights.sum() * 100),
    }
    return pd.DataFrame({"trips_per_person": CATEGORIES,
                         "share_pct": freq.to_numpy()}), metric

def plot_comparison(mz: pd.DataFrame, sim: pd.DataFrame, cohort: str,
                    filename: str) -> tuple[pd.DataFrame, list]:
    a = mz if cohort == "all" else mz.loc[mz["age65"]]
    b = sim if cohort == "all" else sim.loc[sim["age65"]]
    mz_sh, mz_summary = group_metrics(a, "Mikrozensus (weighted)", cohort)
    sim_sh, sim_summary = group_metrics(b, "MATSim (10% sample)", cohort)
    table = pd.DataFrame({
        "cohort": cohort, "trips_per_person": CATEGORIES,
        "mz_weighted_share_pct": mz_sh.share_pct,
        "matsim_share_pct": sim_sh.share_pct,
    })
    table["difference_pp"] = table.matsim_share_pct - table.mz_weighted_share_pct
    if len(a) and abs(table.mz_weighted_share_pct.sum() - 100) > 1e-6:
        raise AssertionError("Mikrozensus trip-count distribution does not sum to 100%.")
    if len(b) and abs(table.matsim_share_pct.sum() - 100) > 1e-6:
        raise AssertionError("MATSim trip-count distribution does not sum to 100%.")
    fig, ax = plt.subplots(figsize=(10, 5.1))
    xx = np.arange(len(CATEGORIES))
    ax.bar(xx - 0.2, table.mz_weighted_share_pct.to_numpy(), .4,
           label="Mikrozensus (weighted)")
    ax.bar(xx + 0.2, table.matsim_share_pct.to_numpy(), .4,
           label="MATSim (10% sample)")
    ax.set_xticks(xx, CATEGORIES)
    ax.set_xlabel("Number of recorded standard-mode trips per person")
    ax.set_ylabel("Share of eligible residents (%)")
    ax.set_title("Neuchatel – trips per person (" + ("all residents" if cohort == "all" else "age 65+") + ")")
    ax.grid(axis="y", alpha=0.2)
    ax.legend()
    fig.text(.5,.013, "Zero standard trips ≠ confirmed staying home; verify incomplete survey responses.",
             ha="center", fontsize=9)
    fig.tight_layout(rect=(0,.04,1,1))
    fig.savefig(OUT / filename, dpi=190)
    plt.close(fig)
    return table, [mz_summary, sim_summary]

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    boundary = load_boundary()
    people_mz = read_survey_residents(boundary)
    survey_trips, _, _ = read_survey_trips(people_mz)
    ids = read_sim_resident_ids(boundary)
    sim_counts = read_matsim_trip_classes(ids)
    survey = per_person_survey(people_mz, survey_trips)
    sim = per_person_sim(ids, sim_counts)

    all_tab, all_metrics = plot_comparison(survey, sim, "all", "trips_per_person_all.png")
    age_tab, age_metrics = plot_comparison(survey, sim, "65+", "trips_per_person_age65.png")
    pd.concat([all_tab, age_tab], ignore_index=True).to_csv(
        OUT / "trips_per_person_distribution.csv", index=False, float_format="%.5f")
    summaries = pd.DataFrame(all_metrics + age_metrics)
    summaries.to_csv(OUT / "trips_per_person_summary.csv", index=False, float_format="%.5f")

    notes = [
        "Recorded standard-mode trips per eligible resident, including zero-trip residents.",
        "Mikrozensus: Neuchatel residents aged 6–120, weekdays, WP-weighted.",
        "MATSim: matching residents from the 10% population, unweighted.",
        "Five conventional transport modes; artificial outside and special modes excluded.",
        "No distance filter; the main-mode definition is the same as Step 6.",
        "Special loop trips can represent travel, so these counts may underestimate mobility.",
        "Zero recorded standard-mode trips do not establish that somebody stayed at home.",
        "Survey diary omissions may also affect the zero-trip category.",
    ]
    (OUT / "trips_per_person_methodology.txt").write_text("\n".join(notes) + "\n", encoding="utf8")

if __name__ == "__main__":
    main()
