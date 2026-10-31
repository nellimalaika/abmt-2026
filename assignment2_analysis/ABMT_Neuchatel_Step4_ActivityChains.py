"""Compare activity chains of Neuchatel residents in Mikrozensus and MATSim."""

from contextlib import redirect_stdout
from io import StringIO
import gzip
import xml.etree.ElementTree as ET
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

from ABMT_Neuchatel_Step6_ModeShare import (
    find_file, load_boundary, read_survey_residents,
    read_sim_resident_ids, to_float,
)

OUT = Path(__file__).resolve().parent / "results" / "activity_chains"

MZ_PURPOSE = {
    1: "interaction", 2: "work", 3: "education", 4: "shop", 5: "other",
    6: "work", 7: "work", 8: "leisure", 9: "other", 10: "other",
    11: "home", 12: "unknown", 13: "border",
}



def survey_people(boundary):
    people = read_survey_residents(boundary)
    return people.rename(columns={"alter": "age", "WP": "weight"})


def survey_chains(people):
    ways = pd.read_csv(
        find_file("wege.csv", "Mikrozensus"), encoding="latin1",
        dtype={"HHNR": "string"},
        usecols=["HHNR", "WEGNR", "wzweck1", "wzweck2"],
    )
    ways = ways.loc[ways.HHNR.isin(people.HHNR)].copy()
    for column in ("WEGNR", "wzweck1", "wzweck2"):
        ways[column] = pd.to_numeric(ways[column], errors="coerce")
    if ways.duplicated(["HHNR", "WEGNR"]).any():
        raise ValueError("Duplicate Mikrozensus trip identifiers")
    ways = ways.sort_values(["HHNR", "WEGNR"], kind="stable")
    groups = dict(tuple(ways.groupby("HHNR", sort=False)))

    rows = []
    for person in people.itertuples(index=False):
        group = groups.get(person.HHNR)
        if group is None or group.empty:
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

        if valid and len(activities) >= 2 and activities[-1] == "home":
            rows.append((int(person.age), float(person.weight), " > ".join(activities)))

    return pd.DataFrame(rows, columns=["age", "weight", "chain"])



def canon(name):
    purpose = (name or "").strip().lower()
    if not purpose:
        return "unknown"
    if "outside" in purpose or "border" in purpose:
        return "outside"
    if "interaction" in purpose or purpose in ("transfer", "transit"):
        return "interaction"
    if purpose.startswith("home"):
        return "home"
    if purpose.startswith("work") or purpose in ("business", "business_trip", "business trip"):
        return "work"
    if purpose.startswith("education") or purpose.startswith("school"):
        return "education"
    if purpose.startswith("shop"):
        return "shop"
    if purpose.startswith("leisure"):
        return "leisure"
    if purpose in ("other", "errands", "escort") or purpose.startswith("other"):
        return "other"
    return "unknown"


def synthetic_chains(boundary):
    resident_ids = read_sim_resident_ids(boundary)
    rows = []
    seen = set()

    with gzip.open(find_file("neuchatel_population.xml.gz", "Neuchatel_10pct"), "rb") as stream:
        root = None
        for event, person in ET.iterparse(stream, events=("start", "end")):
            if root is None:
                root = person
            if event != "end" or person.tag.rsplit("}", 1)[-1] != "person":
                continue

            person_id = str(person.get("id", ""))
            if person_id not in resident_ids:
                root.clear()
                continue
            if person_id in seen:
                raise ValueError("Duplicate simulated resident ID")
            seen.add(person_id)

            attributes = {}
            for child in person:
                if child.tag.rsplit("}", 1)[-1] == "attributes":
                    for attribute in child:
                        if attribute.tag.rsplit("}", 1)[-1] == "attribute":
                            attributes[attribute.get("name", "")] = (attribute.text or "").strip()
            age = to_float(attributes.get("age"))
            if age is None:
                raise ValueError("Missing age for eligible MATSim resident")

            plans = [p for p in person if p.tag.rsplit("}", 1)[-1] == "plan"]
            selected = [p for p in plans if str(p.get("selected", "")).lower() in ("yes", "true", "1")]
            if len(selected) != 1 and len(plans) != 1:
                root.clear()
                continue
            plan = selected[0] if len(selected) == 1 else plans[0]

            activities, legs = [], []
            for element in plan.iter():
                tag = element.tag.rsplit("}", 1)[-1]
                if tag in ("act", "activity"):
                    activities.append(canon(element.get("type", "")))
                elif tag == "leg":
                    legs.append(str(element.get("mode", "")).strip().lower())

            if not activities or any(p in ("unknown", "outside") for p in activities):
                root.clear()
                continue
            if any(any(word in mode for word in ("outside", "crossborder", "freight")) for mode in legs):
                root.clear()
                continue
            if not legs:
                root.clear()
                continue

            chain_activities = [p for p in activities if p != "interaction"]
            if len(chain_activities) >= 2 and chain_activities[0] == "home" and chain_activities[-1] == "home":
                rows.append((int(age), 1.0, " > ".join(chain_activities)))
            root.clear()

    if seen != resident_ids:
        raise AssertionError("Not all eligible MATSim residents were found")
    return pd.DataFrame(rows, columns=["age", "weight", "chain"])


def compare(mz, sim, cohort, filename):
    a = mz if cohort == "all" else mz.loc[mz.age >= 65]
    b = sim if cohort == "all" else sim.loc[sim.age >= 65]
    if a.empty or b.empty:
        raise ValueError(f"No complete activity chains for {cohort}")

    top = a.groupby("chain").weight.sum().sort_values(ascending=False).head(10).index.tolist()
    categories = top + ["other chains"]

    def shares(df):
        labels = df.chain.where(df.chain.isin(top), "other chains")
        counts = df.assign(label=labels).groupby("label").weight.sum().reindex(categories, fill_value=0)
        return counts / counts.sum() * 100

    left, right = shares(a), shares(b)
    result = pd.DataFrame({"chain": categories, "mz_pct": left.values, "matsim_pct": right.values})
    result["difference_pp"] = result.matsim_pct - result.mz_pct
    result.to_csv(OUT / f"{filename}.csv", sep=";", index=False)

    labels = result.copy()
    labels["chain"] = labels.chain.replace({"home > home": "home > home (round trip)"})
    ax = labels.set_index("chain")[["mz_pct", "matsim_pct"]].plot.barh(figsize=(10, 6.5))
    ax.invert_yaxis()
    ax.set_xlabel("Share among people with classifiable mobile activity chains (%)")
    ax.set_ylabel("Daily activity chain")
    ax.set_title("Neuchatel mobile activity chains: " + (
        "residents age 6+" if cohort == "all" else "residents age 65+"
    ))
    ax.legend(["Mikrozensus (weighted)", "MATSim (10% sample)"])
    plt.tight_layout()
    plt.savefig(OUT / f"{filename}.png", dpi=170)
    plt.close()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with redirect_stdout(StringIO()):
        boundary = load_boundary()
        people = survey_people(boundary)
        mz = survey_chains(people)
        sim = synthetic_chains(boundary)
    compare(mz, sim, "all", "activity_chains_all")
    compare(mz, sim, "65+", "activity_chains_age65plus")


if __name__ == "__main__":
    main()
