"""Compare the demographic composition of Neuchatel residents in both datasets."""

from pathlib import Path
from abmt_paths import data_root
import gzip
import xml.etree.ElementTree as ET

import matplotlib.pyplot as plt
import pandas as pd
import shapefile
from pyproj import CRS, Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform, unary_union
from shapely.prepared import prep


BASE = data_root()
OUTPUT = Path(__file__).resolve().parent / "results" / "boundary_matched"
AGE_LABELS = ["6-17", "18-34", "35-64", "65+"]


def find_file(filename, folder):
    files = [p for p in BASE.rglob(filename) if p.is_file()]
    preferred = [p for p in files if folder.lower() in str(p.parent).lower()]
    if not files:
        raise FileNotFoundError(f"File not found: {filename}")
    return (preferred or files)[0]


def read_boundary():
    folder = BASE / "Boundary"
    crs = CRS.from_wkt((folder / "neuchatel.prj").read_text()).to_2d()
    with shapefile.Reader(str(folder / "neuchatel.shp")) as reader:
        geometries = [shape(item.__geo_interface__) for item in reader.shapes()]
    if not geometries:
        raise ValueError("The boundary file contains no geometry.")
    boundary = unary_union(geometries)
    if crs != CRS.from_epsg(2056):
        converter = Transformer.from_crs(crs, 2056, always_xy=True)
        boundary = transform(converter.transform, boundary)
    if boundary.is_empty or not boundary.is_valid:
        raise ValueError("Invalid Neuchatel boundary.")
    return prep(boundary)


def read_microcensus(boundary):
    households = pd.read_csv(
        find_file("haushalte.csv", "Mikrozensus"), encoding="latin1",
        usecols=["HHNR", "W_X_CH1903", "W_Y_CH1903", "f30100"],
        dtype={"HHNR": "string"},
    )
    persons = pd.read_csv(
        find_file("zielpersonen.csv", "Mikrozensus"), encoding="latin1",
        usecols=["HHNR", "alter", "gesl", "WP", "tag"],
        dtype={"HHNR": "string"},
    )
    if households.HHNR.duplicated().any() or persons.HHNR.duplicated().any():
        raise ValueError("Duplicate household/person identifiers.")

    mz = persons.merge(households, on="HHNR", validate="one_to_one")
    numeric_columns = ["alter", "gesl", "WP", "tag", "W_X_CH1903", "W_Y_CH1903", "f30100"]
    mz[numeric_columns] = mz[numeric_columns].apply(pd.to_numeric, errors="coerce")
    mz = mz.loc[
        mz.tag.between(1, 5) & mz.alter.between(6, 120) & mz.WP.gt(0)
        & mz.W_X_CH1903.gt(0) & mz.W_Y_CH1903.gt(0)
    ].copy()

    if not 400_000 <= mz.W_X_CH1903.median() <= 900_000:
        raise ValueError("Unexpected Microcensus coordinate system.")
    converter = Transformer.from_crs(21781, 2056, always_xy=True)
    x, y = converter.transform(mz.W_X_CH1903.to_numpy(), mz.W_Y_CH1903.to_numpy())
    mz = mz.loc[[boundary.covers(Point(px, py)) for px, py in zip(x, y)]].copy()
    if mz.empty:
        raise ValueError("No Microcensus residents inside the boundary.")

    mz["age_group"] = pd.cut(mz.alter, [5, 17, 34, 64, 120], labels=AGE_LABELS)
    mz["sex_group"] = mz.gesl.map({1: "male", 2: "female"})
    mz["household_car_ownership"] = mz.f30100.map(car_group)
    return mz


def car_group(value):
    if pd.isna(value) or value < 0:
        return None
    return "1+ cars" if value >= 1 else "0 cars"


def as_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_population(boundary):
    records = []
    population = find_file("neuchatel_population.xml.gz", "Neuchatel_10pct")
    with gzip.open(population, "rb") as infile:
        iterator = ET.iterparse(infile, events=("start", "end"))
        root = None
        for event, person in iterator:
            if root is None:
                root = person
            if event != "end" or person.tag.rsplit("}", 1)[-1] != "person":
                continue

            attrs = {}
            for child in person:
                if child.tag.rsplit("}", 1)[-1] == "attributes":
                    attrs = {
                        attribute.get("name"): (attribute.text or "").strip()
                        for attribute in child
                        if attribute.tag.rsplit("}", 1)[-1] == "attribute"
                    }
                    break

            if attrs.get("isFreight", "false").lower() == "true" or attrs.get("isOutside", "false").lower() == "true":
                root.clear()
                continue

            x = as_number(attrs.get("home_x"))
            y = as_number(attrs.get("home_y"))
            if x is None or y is None:
                x = as_number(attrs.get("home_coordinate_x"))
                y = as_number(attrs.get("home_coordinate_y"))
            age = as_number(attrs.get("age"))
            if (
                x is not None and y is not None and 1_000_000 < x < 4_000_000
                and boundary.covers(Point(x, y)) and age is not None and 6 <= age <= 120
            ):
                sex = {"m": "male", "f": "female", "male": "male", "female": "female"}.get(
                    attrs.get("sex", "").lower()
                )
                records.append((age, sex, car_group(as_number(attrs.get("numberOfCars")))))
            root.clear()

    if not records:
        raise ValueError("No synthetic residents inside the boundary.")
    population = pd.DataFrame(records, columns=["age", "sex_group", "household_car_ownership"])
    population["age_group"] = pd.cut(population.age, [5, 17, 34, 64, 120], labels=AGE_LABELS)
    return population


def save_comparison(mz, population, column, groups, filename):
    survey = mz.loc[mz[column].isin(groups)].groupby(column, observed=True).WP.sum()
    simulated = population.loc[population[column].isin(groups)].groupby(column, observed=True).size()
    if survey.sum() <= 0 or simulated.sum() <= 0:
        raise ValueError(f"No comparable data for {column}.")

    result = pd.DataFrame({
        "category": groups,
        "microcensus_weighted_pct": (survey / survey.sum() * 100).reindex(groups, fill_value=0).to_numpy(),
        "matsim_pct": (simulated / simulated.sum() * 100).reindex(groups, fill_value=0).to_numpy(),
    })
    result["difference_pp"] = result.matsim_pct - result.microcensus_weighted_pct
    result.to_csv(OUTPUT / f"{filename}.csv", index=False, sep=";")

    ax = result.set_index("category")[["microcensus_weighted_pct", "matsim_pct"]].plot.bar()
    ax.set_title(f"Neuchatel: {filename.replace('_', ' ').title()}")
    ax.set_ylabel("Share of persons (%)")
    ax.set_xlabel("")
    ax.legend(["Mikrozensus (weighted)", "MATSim (10% sample)"])
    plt.xticks(rotation=0)
    plt.tight_layout()
    plt.savefig(OUTPUT / f"{filename}.png", dpi=170)
    plt.close()


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    boundary = read_boundary()
    mz = read_microcensus(boundary)
    population = read_population(boundary)
    save_comparison(mz, population, "age_group", AGE_LABELS, "age_validation")
    save_comparison(mz, population, "sex_group", ["male", "female"], "sex_validation")
    save_comparison(mz, population, "household_car_ownership", ["0 cars", "1+ cars"], "car_ownership_validation")


if __name__ == "__main__":
    main()
