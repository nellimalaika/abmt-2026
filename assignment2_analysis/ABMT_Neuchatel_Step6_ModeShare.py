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
    from abmt_paths import find_input_file
    return find_input_file(BASE, name, preferred)


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
    from abmt_common import survey_people
    return survey_people(boundary)



def read_survey_trips(eligible):
    from abmt_common import mode_data
    mz,sim,report = mode_data()
    mz = mz.loc[mz.HHNR.isin(eligible.HHNR)].copy()
    return mz,report["survey_internal_n"],report["survey_excluded_primary_n"]



def to_float(value):
    try:
        number = float(value)
        return number if np.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def bool_flag(value):
    return str(value or "").strip().lower() in {"true", "yes", "1"}


def read_sim_resident_ids(boundary):
    from abmt_common import resident_ids
    return resident_ids(boundary)



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
    from abmt_common import mode_data
    mz,sim,report = mode_data()
    sim = sim.loc[sim.person.isin(resident_ids)].copy()
    return sim,report["simulation_internal_n"],report["simulation_excluded_primary_n"],report["simulation_loop_n"]



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
    from abmt_common import mode_data, write_method
    OUT.mkdir(parents=True,exist_ok=True)
    mz,sim,report = mode_data()
    survey = weighted_mode_share(mz,"WP")
    model = weighted_mode_share(sim.assign(weight=1.),"weight")
    table = pd.DataFrame({"mode": MODES,"microcensus_weighted_pct":survey.reindex(MODES).to_numpy(),
                          "matsim_pct":model.reindex(MODES).to_numpy()})
    table["difference_pp"] = table.matsim_pct-table.microcensus_weighted_pct
    table.to_csv(OUT/"mode_share_comparison.csv",index=False)
    plot_comparison(table)
    for definition in ["eqasim","strict"]:
        a,b,_ = mode_data(definition)
        x=weighted_mode_share(a,"WP"); y=weighted_mode_share(b.assign(weight=1.),"weight")
        pd.DataFrame({"mode":MODES,"microcensus_weighted_pct":x.reindex(MODES).to_numpy(),
                      "matsim_pct":y.reindex(MODES).to_numpy()}).to_csv(OUT/f"mode_share_{definition}.csv",index=False)
    write_method(OUT,report)



if __name__ == "__main__":
    main()
