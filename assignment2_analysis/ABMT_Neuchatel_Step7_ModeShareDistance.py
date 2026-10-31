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
    columns = ["HHNR", "WEGNR", "wmittel", "S_X_CH1903", "S_Y_CH1903", "Z_X_CH1903", "Z_Y_CH1903"]
    trips = pd.read_csv(
        find_file("wege.csv", "Mikrozensus"), encoding="latin1",
        dtype={"HHNR": "string", "WEGNR": "string"}, usecols=columns, low_memory=False,
    )
    total = len(trips)
    trips = trips.loc[trips["HHNR"].isin(eligible["HHNR"])].copy()
    resident_trips = len(trips)
    if trips.duplicated(["HHNR", "WEGNR"]).any():
        raise ValueError("Duplicate Mikrozensus trip IDs")
    trips = trips.merge(eligible[["HHNR", "WP"]], on="HHNR", validate="many_to_one")
    trips["mode"] = pd.to_numeric(trips["wmittel"], errors="coerce").map(MZ_MODE_MAP)

    stages = pd.read_csv(
        find_file("etappen.csv", "Mikrozensus"), encoding="latin1",
        dtype={"HHNR": "string", "WEGNR": "string"},
        usecols=["HHNR", "WEGNR", "f51300"],
    )
    passengers = stages.loc[
        pd.to_numeric(stages["f51300"], errors="coerce").eq(8), ["HHNR", "WEGNR"]
    ].drop_duplicates()
    passengers["passenger"] = True
    trips = trips.merge(passengers, on=["HHNR", "WEGNR"], how="left", validate="many_to_one")
    trips.loc[trips["passenger"].eq(True), "mode"] = "car_passenger"

    valid_mode = trips["mode"].isin(MODES)
    unknown_modes = int((~valid_mode).sum())
    trips = trips.loc[valid_mode].copy()
    for col in columns[3:]:
        trips[col] = pd.to_numeric(trips[col], errors="coerce")
    valid_coordinates = (
        trips["S_X_CH1903"].between(400_000, 900_000)
        & trips["Z_X_CH1903"].between(400_000, 900_000)
        & trips["S_Y_CH1903"].between(0, 400_000)
        & trips["Z_Y_CH1903"].between(0, 400_000)
    )
    invalid_coordinates = int((~valid_coordinates).sum())
    trips = trips.loc[valid_coordinates].copy()
    if trips.empty:
        raise ValueError("No survey ways with valid coordinates")

    convert = Transformer.from_crs("EPSG:21781", "EPSG:2056", always_xy=True)
    sx, sy = convert.transform(trips["S_X_CH1903"].to_numpy(), trips["S_Y_CH1903"].to_numpy())
    zx, zy = convert.transform(trips["Z_X_CH1903"].to_numpy(), trips["Z_Y_CH1903"].to_numpy())
    trips["distance_m"] = np.hypot(zx - sx, zy - sy)
    valid_distance = np.isfinite(trips["distance_m"]) & trips["distance_m"].gt(1.0)
    invalid_distance = int((~valid_distance).sum())
    trips = trips.loc[valid_distance, ["HHNR", "WP", "mode", "distance_m"]].copy()
    if trips.empty:
        raise ValueError("No survey ways with valid distance")
    info = {
        "all_ways": total, "resident_ways": resident_trips,
        "unknown_modes": unknown_modes, "invalid_origin_destination": invalid_coordinates,
        "zero_or_invalid_distance": invalid_distance, "usable_ways": len(trips),
        "unique_respondents": trips["HHNR"].nunique(),
    }
    return trips, info


def read_matsim_distance_trips(ids):
    path = find_file("output_trips.csv.gz", "simulation_output")
    columns, delimiter = read_csv_header(path)
    person_col = next((c for c in ("person", "person_id", "personId") if c in columns), None)
    mode_col = next((c for c in ("main_mode", "mainMode", "mode") if c in columns), None)
    distance_col = next((c for c in ("euclidean_distance", "euclideanDistance") if c in columns), None)
    if not all((person_col, mode_col, distance_col)):
        raise ValueError("MATSim trips need person, mode and euclidean_distance columns")
    activity_cols = [c for c in ("start_activity_type", "end_activity_type") if c in columns]
    trips = pd.read_csv(
        path, sep=delimiter, compression="gzip", dtype={person_col: "string"},
        usecols=[person_col, mode_col, distance_col] + activity_cols, low_memory=False,
    )
    total = len(trips)
    trips = trips.loc[trips[person_col].isin(ids)].copy()
    resident_trips = len(trips)
    if trips.empty:
        raise ValueError("No trips for eligible MATSim residents")

    outside = pd.Series(False, index=trips.index)
    for col in activity_cols:
        outside |= trips[col].fillna("").astype("string").str.lower().str.contains(
            "outside|crossborder|freight|^border", regex=True,
        ).fillna(False)
    outside_activities = int(outside.sum())
    trips = trips.loc[~outside].copy()
    trips["mode"] = trips[mode_col].map(classify_sim_mode)
    special = int(trips["mode"].isin(["special_loop", "special_remote"]).sum())
    outside_modes = int(trips["mode"].eq("outside").sum())
    unknown = trips.loc[trips["mode"].eq("unknown"), mode_col]
    if not unknown.empty:
        raise ValueError(f"Unrecognized MATSim modes: {unknown.unique().tolist()}")
    trips = trips.loc[trips["mode"].isin(MODES)].copy()
    trips["distance_m"] = pd.to_numeric(trips[distance_col], errors="coerce")
    valid_distance = trips["distance_m"].gt(1.0) & np.isfinite(trips["distance_m"])
    invalid_distance = int((~valid_distance).sum())
    trips = trips.loc[valid_distance, [person_col, "mode", "distance_m"]].copy()
    trips.rename(columns={person_col: "person"}, inplace=True)
    if trips.empty:
        raise ValueError("No MATSim trips with valid distance")
    info = {
        "all_output_trips": total, "resident_trips": resident_trips,
        "excluded_outside_activities": outside_activities,
        "excluded_special_modes": special, "excluded_outside_modes": outside_modes,
        "excluded_nonpositive_distance": invalid_distance,
        "usable_trips": len(trips), "unique_people": trips["person"].nunique(),
    }
    return trips, info


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
