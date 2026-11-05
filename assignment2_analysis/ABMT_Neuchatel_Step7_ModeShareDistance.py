"""Compare transport mode shares by straight-line trip distance."""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pyproj import Transformer

from ABMT_Neuchatel_Step6_ModeShare import (
    MODES, MZ_MODE_MAP, find_file, read_csv_header, classify_sim_mode,
    load_boundary, read_survey_residents, read_sim_resident_ids,
)

OUT = Path(__file__).resolve().parent / "results" / "mode_share_distance"
EDGES_KM = [0, 1, 2, 5, 10, 20, 50, np.inf]
LABELS = ["<1 km", "1-2 km", "2-5 km", "5-10 km", "10-20 km", "20-50 km", "50+ km"]
MIN_MZ_UNIQUE_RESPONDENTS = 30
MIN_SIM_UNIQUE_PERSONS = 30
DISPLAY_NAMES = {
    "car": "Car (driver)", "car_passenger": "Car passenger",
    "pt": "Public transport", "bike": "Bike", "walk": "Walk",
}


def read_mz_distance_trips(eligible):
    from abmt_common import mode_data
    mz,sim,report=mode_data()
    return mz.loc[mz.HHNR.isin(eligible.HHNR)].copy(),report



def read_matsim_distance_trips(ids):
    from abmt_common import mode_data
    mz,sim,report=mode_data()
    return sim.loc[sim.person.isin(ids)].copy(),report



def make_bins(trips):
    trips = trips.copy()
    trips["distance_bin"] = pd.cut(
        trips["distance_m"] / 1000.0, bins=EDGES_KM,
        labels=LABELS, include_lowest=True, right=False,
    )
    if trips["distance_bin"].isna().any():
        raise ValueError("Trip distance outside defined classes")
    return trips


def aggregate(mz, sim):
    mz = make_bins(mz)
    sim = make_bins(sim)
    bins = pd.Index(LABELS, name="distance_bin")
    combinations = pd.MultiIndex.from_product([LABELS, MODES], names=["distance_bin", "mode"])
    mz_mode = mz.groupby(["distance_bin", "mode"], observed=True)["WP"].sum().reindex(combinations, fill_value=0.0)
    sim_mode = sim.groupby(["distance_bin", "mode"], observed=True).size().reindex(combinations, fill_value=0)
    mz_weight = mz.groupby("distance_bin", observed=True)["WP"].sum().reindex(bins, fill_value=0.0)
    sim_count = sim.groupby("distance_bin", observed=True).size().reindex(bins, fill_value=0)
    sizes = pd.DataFrame({
        "distance_bin": LABELS,
        "mz_trip_count": mz.groupby("distance_bin", observed=True).size().reindex(bins, fill_value=0).to_numpy().astype(int),
        "mz_distinct_respondents": mz.groupby("distance_bin", observed=True)["HHNR"].nunique().reindex(bins, fill_value=0).to_numpy().astype(int),
        "mz_weight_sum": mz_weight.to_numpy(),
        "matsim_trip_count": sim_count.to_numpy().astype(int),
        "matsim_distinct_persons": sim.groupby("distance_bin", observed=True)["person"].nunique().reindex(bins, fill_value=0).to_numpy().astype(int),
    })
    sizes["comparison_minimum_sample_met"] = (
        sizes["mz_distinct_respondents"].ge(MIN_MZ_UNIQUE_RESPONDENTS)
        & sizes["matsim_distinct_persons"].ge(MIN_SIM_UNIQUE_PERSONS)
    )
    mz_denominator = np.repeat(mz_weight.to_numpy(), len(MODES))
    sim_denominator = np.repeat(sim_count.to_numpy(), len(MODES))
    mz_pct = np.divide(
        mz_mode.to_numpy() * 100.0, mz_denominator,
        out=np.full(len(combinations), np.nan), where=mz_denominator > 0,
    )
    sim_pct = np.divide(
        sim_mode.to_numpy() * 100.0, sim_denominator,
        out=np.full(len(combinations), np.nan), where=sim_denominator > 0,
    )
    result = pd.DataFrame({
        "distance_bin": combinations.get_level_values(0),
        "mode": combinations.get_level_values(1),
        "microcensus_weighted_pct": mz_pct,
        "matsim_pct": sim_pct,
        "difference_pp": sim_pct - mz_pct,
    })
    return result.merge(sizes, on="distance_bin", validate="many_to_one"), sizes


def plot_mode_panels(result):
    fig, axes = plt.subplots(3, 2, figsize=(13.5, 12), sharex=False, sharey=False)
    x = np.arange(len(LABELS))
    for idx, mode in enumerate(MODES):
        ax = axes.flat[idx]
        subset = result[result["mode"].eq(mode)].set_index("distance_bin").loc[LABELS]
        trusted = subset["comparison_minimum_sample_met"].to_numpy(dtype=bool)
        mz = np.where(trusted, subset["microcensus_weighted_pct"].to_numpy(), np.nan)
        sim = np.where(trusted, subset["matsim_pct"].to_numpy(), np.nan)
        ax.plot(x, mz, "o-", label="Mikrozensus (weighted)")
        ax.plot(x, sim, "s-", label="MATSim (10% sample)")
        ax.set_title(DISPLAY_NAMES[mode])
        ax.set_xticks(x, LABELS, rotation=32, ha="right")
        ax.grid(axis="y", alpha=.25)
        if idx in (0, 2, 4):
            ax.set_ylabel("Share of trips in bin (%)")
        if idx == 0:
            ax.legend(fontsize=9)
    axes.flat[-1].axis("off")
    fig.suptitle("Neuchatel – trip-based mode share by straight-line distance", fontsize=15)
    fig.text(.5, .015, "Bins with fewer than 30 distinct survey respondents or 30 simulated people are omitted from plots.",
             ha="center", fontsize=9)
    fig.tight_layout(rect=(0, .035, 1, .975))
    fig.savefig(OUT / "mode_share_by_distance.png", dpi=180)
    plt.close(fig)


def plot_pt(result):
    subset = result[result["mode"].eq("pt")].set_index("distance_bin").loc[LABELS]
    trusted = subset["comparison_minimum_sample_met"].to_numpy(dtype=bool)
    mz = np.where(trusted, subset["microcensus_weighted_pct"].to_numpy(), np.nan)
    sim = np.where(trusted, subset["matsim_pct"].to_numpy(), np.nan)
    x = np.arange(len(LABELS))
    fig, ax = plt.subplots(figsize=(10, 5.4))
    ax.plot(x, mz, "o-", label="Mikrozensus (weighted)")
    ax.plot(x, sim, "s-", label="MATSim (10% sample)")
    ax.set_xticks(x, LABELS, rotation=20)
    ax.set_xlabel("Straight-line trip distance")
    ax.set_ylabel("Public transport share of trips in bin (%)")
    ax.set_title("Neuchatel – Public transport share by trip distance")
    ax.legend()
    ax.grid(axis="y", alpha=.25)
    fig.text(.5, .01, "Only bins with >=30 unique survey respondents and >=30 simulated persons shown.",
             ha="center", fontsize=9)
    fig.tight_layout(rect=(0, .03, 1, 1))
    fig.savefig(OUT / "pt_mode_share_by_distance.png", dpi=180)
    plt.close(fig)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    boundary = load_boundary()
    mz, mz_info = read_mz_distance_trips(read_survey_residents(boundary))
    sim, sim_info = read_matsim_distance_trips(read_sim_resident_ids(boundary))
    result, sizes = aggregate(mz, sim)
    result.to_csv(OUT / "mode_share_by_distance.csv", index=False, float_format="%.6f")
    sizes.to_csv(OUT / "distance_bin_coverage.csv", index=False, float_format="%.6f")
    plot_mode_panels(result)
    plot_pt(result)
    notes = [
        "Trip mode share by straight-line distance bin, in km.",
        "Mikrozensus: Neuchatel residents aged 6–120, weekdays only, WP-weighted trips.",
        "MATSim: residents from the 10% sample, unweighted trips.",
        "Trip distances: Euclidean origin-to-destination, >1 m; LV03 coordinates transformed to LV95 for survey trips.",
        "Synthetic outside, freight, cross-border and special loop/remote modes excluded.",
        "Subsamples below 30 distinct respondents/persons are suppressed in figures, not in CSV.",
        "Step 6 may differ because it does not require a valid trip distance.",
        f"Survey filters: {mz_info}", f"MATSim filters: {sim_info}",
    ]
    (OUT / "mode_share_distance_methodology.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
    from abmt_common import write_method, output
    write_method(output('mode_share_distance'))
