"""Compare aggregated trip-based mode shares in Neuchatel."""

import csv
import gzip
from pathlib import Path
from abmt_paths import data_root
import xml.etree.ElementTree as ET

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import shapefile
from pyproj import CRS, Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform as transform_geometry, unary_union
from shapely.prepared import prep

BASE = data_root()
OUT = Path(__file__).resolve().parent / "results" / "mode_share"
MODES = ["car", "car_passenger", "pt", "bike", "walk"]
SPECIAL_LOOP_MODES = {"walk_loop", "bike_loop", "car_loop", "pt_loop", "car_passenger_loop"}
SPECIAL_REMOTE_MODES = {"remote_walk"}

MZ_MODE_MAP = {
    1: "pt", 2: "pt", 3: "pt", 4: "pt", 5: "pt", 6: "pt", 7: "pt", 8: "pt",
    9: "car", 10: "car", 11: "pt", 12: "car", 13: "car",
    14: "bike", 15: "walk", 16: "bike",
}
SIM_MODE_MAP = {
    "car": "car", "car_driver": "car",
    "car_passenger": "car_passenger", "carpassenger": "car_passenger", "ride": "car_passenger",
    "pt": "pt", "public_transport": "pt", "transit": "pt",
    "train": "pt", "rail": "pt", "bus": "pt", "tram": "pt",
    "bike": "bike", "bicycle": "bike", "ebike": "bike", "e_bike": "bike",
    "walk": "walk", "walking": "walk", "transit_walk": "walk", "non_network_walk": "walk",
}


def local_tag(tag):
    return tag.rsplit("}", 1)[-1].lower()


def find_file(name, preferred):
    files = sorted(path for path in BASE.rglob(name) if path.is_file())
    matching = [path for path in files if preferred.casefold() in str(path.parent).casefold()]
    if matching:
        files = matching
    if not files:
        raise FileNotFoundError(f"Missing {name} in {BASE}")
    return files[0]


def load_boundary():
    path = find_file("neuchatel.shp", "Boundary")
    projection = path.with_suffix(".prj")
    if not projection.exists():
        raise FileNotFoundError(projection)
    with shapefile.Reader(str(path)) as reader:
        polygon = unary_union([shape(item.__geo_interface__) for item in reader.shapes()])
    source = CRS.from_wkt(projection.read_text(encoding="utf-8")).to_2d()
    target = CRS.from_epsg(2056)
    if source != target:
        convert = Transformer.from_crs(source, target, always_xy=True)
        polygon = transform_geometry(convert.transform, polygon)
    if polygon.is_empty or not polygon.is_valid:
        raise ValueError("Invalid Neuchatel boundary")
    return prep(polygon)


def read_survey_residents(boundary):
    households = pd.read_csv(
        find_file("haushalte.csv", "Mikrozensus"), encoding="latin1",
        dtype={"HHNR": "string"}, usecols=["HHNR", "W_X_CH1903", "W_Y_CH1903"],
    )
    respondents = pd.read_csv(
        find_file("zielpersonen.csv", "Mikrozensus"), encoding="latin1",
        dtype={"HHNR": "string"}, usecols=["HHNR", "alter", "tag", "WP"],
    )
    if households["HHNR"].duplicated().any() or respondents["HHNR"].duplicated().any():
        raise ValueError("Duplicate Mikrozensus person IDs")
    people = respondents.merge(households, on="HHNR", validate="one_to_one")
    for column in ("alter", "tag", "WP", "W_X_CH1903", "W_Y_CH1903"):
        people[column] = pd.to_numeric(people[column], errors="coerce")
    people = people.loc[
        people["alter"].between(6, 120)
        & people["tag"].between(1, 5)
        & people["WP"].gt(0)
        & people["W_X_CH1903"].between(400_000, 900_000)
        & people["W_Y_CH1903"].between(0, 400_000)
    ].copy()
    if people.empty:
        raise ValueError("No eligible Mikrozensus respondents")
    transformer = Transformer.from_crs("EPSG:21781", "EPSG:2056", always_xy=True)
    x, y = transformer.transform(people["W_X_CH1903"].to_numpy(), people["W_Y_CH1903"].to_numpy())
    inside = [boundary.covers(Point(float(a), float(b))) for a, b in zip(x, y)]
    people = people.loc[inside, ["HHNR", "alter", "WP"]].copy()
    if people.empty:
        raise ValueError("No eligible respondents inside the study area")
    return people


def read_survey_trips(eligible):
    trips = pd.read_csv(
        find_file("wege.csv", "Mikrozensus"), encoding="latin1",
        dtype={"HHNR": "string", "WEGNR": "string"},
        usecols=["HHNR", "WEGNR", "wmittel"],
    )
    trips = trips.loc[trips["HHNR"].isin(eligible["HHNR"])].copy()
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

    valid = trips.loc[trips["mode"].isin(MODES), ["HHNR", "WP", "mode"]].copy()
    if valid.empty:
        raise ValueError("No valid Mikrozensus trips")
    return valid, len(trips), len(trips) - len(valid)


def to_float(value):
    try:
        number = float(value)
        return number if np.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def bool_flag(value):
    return str(value or "").strip().lower() in {"true", "yes", "1"}


def read_sim_resident_ids(boundary):
    ids = set()
    with gzip.open(find_file("neuchatel_population.xml.gz", "Neuchatel_10pct"), "rb") as stream:
        root = None
        for event, person in ET.iterparse(stream, events=("start", "end")):
            if root is None:
                root = person
            if event != "end" or local_tag(person.tag) != "person":
                continue
            attrs = {
                attribute.get("name", ""): (attribute.text or "").strip()
                for child in person if local_tag(child.tag) == "attributes"
                for attribute in child if local_tag(attribute.tag) == "attribute"
            }
            external = any(bool_flag(attrs.get(flag)) for flag in (
                "isOutside", "isFreight", "isExternalFR", "isCrossBorder"
            )) or attrs.get("subpopulation", "").casefold() in {"outside", "freight", "crossborder"}
            age = to_float(attrs.get("age"))
            x = to_float(attrs.get("home_x") or attrs.get("home_coordinate_x"))
            y = to_float(attrs.get("home_y") or attrs.get("home_coordinate_y"))
            if not external and age is not None and 6 <= age <= 120 and x is not None and y is not None:
                if boundary.covers(Point(x, y)):
                    ids.add(str(person.get("id", "")))
            root.clear()
    if not ids:
        raise ValueError("No eligible MATSim residents")
    return ids


def read_csv_header(path):
    with gzip.open(path, "rt", encoding="utf-8-sig", errors="replace") as stream:
        first_line = stream.readline().strip()
    delimiter = max((";", ",", "\t"), key=first_line.count)
    return next(csv.reader([first_line], delimiter=delimiter)), delimiter


def classify_sim_mode(value):
    mode = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not mode or mode in {"nan", "none"}:
        return "unknown"
    if "outside" in mode or "crossborder" in mode or "freight" in mode or mode.startswith("external"):
        return "outside"
    if mode in SPECIAL_LOOP_MODES:
        return "special_loop"
    if mode in SPECIAL_REMOTE_MODES:
        return "special_remote"
    return SIM_MODE_MAP.get(mode, "unknown")


def read_sim_trips(resident_ids):
    path = find_file("output_trips.csv.gz", "simulation_output")
    columns, delimiter = read_csv_header(path)
    person_col = next((name for name in ("person", "person_id", "personId") if name in columns), None)
    mode_col = next((name for name in ("main_mode", "mainMode", "mode") if name in columns), None)
    if not person_col or not mode_col:
        raise ValueError("MATSim trips need person and mode columns")
    activity_cols = [name for name in ("start_activity_type", "end_activity_type") if name in columns]
    trips = pd.read_csv(
        path, sep=delimiter, compression="gzip", dtype={person_col: "string"},
        usecols=[person_col, mode_col] + activity_cols, low_memory=False,
    )
    total_rows = len(trips)
    trips = trips.loc[trips[person_col].isin(resident_ids)].copy()
    if trips.empty:
        raise ValueError("No MATSim trips for eligible residents")
    outside_activity = pd.Series(False, index=trips.index)
    for column in activity_cols:
        outside_activity |= trips[column].fillna("").astype(str).str.lower().str.contains(
            r"outside|crossborder|freight|^border", regex=True,
        )
    trips = trips.loc[~outside_activity].copy()
    trips["mode"] = trips[mode_col].map(classify_sim_mode)
    excluded_outside = int(trips["mode"].eq("outside").sum())
    excluded_special = int(trips["mode"].isin({"special_loop", "special_remote"}).sum())
    unknown = trips.loc[trips["mode"].eq("unknown"), mode_col]
    if not unknown.empty:
        raise ValueError(f"Unrecognized MATSim modes: {unknown.unique().tolist()}")
    trips = trips.loc[trips["mode"].isin(MODES), ["mode"]].copy()
    if trips.empty:
        raise ValueError("No valid ordinary MATSim trips")
    return trips, total_rows, excluded_outside, excluded_special


def weighted_mode_share(frame, weight_column):
    weights = frame.groupby("mode")[weight_column].sum().reindex(MODES, fill_value=0)
    return weights / weights.sum() * 100


def plot_comparison(table):
    x = np.arange(len(table))
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(x - .19, table["microcensus_weighted_pct"], width=.38, label="Mikrozensus (weighted)")
    ax.bar(x + .19, table["matsim_pct"], width=.38, label="MATSim (10% sample)")
    ax.set_xticks(x, ["Car (driver)", "Car passenger", "Public transport", "Bike", "Walk"])
    ax.set_ylabel("Share of trips (%)")
    ax.set_title("Neuchatel – aggregated mode share by trip")
    ax.legend()
    ax.grid(axis="y", alpha=.2)
    fig.tight_layout()
    fig.savefig(OUT / "mode_share_aggregated.png", dpi=190)
    plt.close(fig)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    boundary = load_boundary()
    survey_people = read_survey_residents(boundary)
    survey_trips, mz_total, mz_unknown = read_survey_trips(survey_people)
    sim_people = read_sim_resident_ids(boundary)
    sim_trips, sim_total, sim_outside, sim_special = read_sim_trips(sim_people)

    survey = weighted_mode_share(survey_trips.rename(columns={"WP": "weight"}), "weight")
    simulated = weighted_mode_share(sim_trips.assign(weight=1.0), "weight")
    table = pd.DataFrame({
        "mode": MODES,
        "microcensus_weighted_pct": survey.to_numpy(),
        "matsim_pct": simulated.to_numpy(),
    })
    table["difference_pp"] = table["matsim_pct"] - table["microcensus_weighted_pct"]
    table = table.round(3)
    table.to_csv(OUT / "mode_share_aggregated.csv", index=False)
    plot_comparison(table)

    notes = [
        "Trip-based main-mode shares, Neuchatel residents aged 6–120.",
        "Mikrozensus: weekdays, respondent weight WP per trip.",
        "MATSim: 10% population sample, unweighted.",
        "Artificial outside, freight, cross-border and special modes excluded.",
        "Zero-trip residents do not contribute to trip-based shares.",
        "Step 7 additionally filters trips by valid straight-line distance > 1 m.",
        f"Mikrozensus: {len(survey_people)} residents; {len(survey_trips)} valid trips; {mz_unknown}/{mz_total} unknown modes.",
        f"MATSim: {len(sim_people)} residents; {len(sim_trips)} valid trips; {sim_outside} outside modes; {sim_special} special modes; {sim_total} total output rows.",
    ]
    (OUT / "mode_share_methodology.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
