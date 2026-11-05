#!/usr/bin/env python3
'Internal reconciled reader/utility derived from the reviewed analysis scripts. See README.md and abmt_common.METHOD for the active definitions.'

import argparse
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import _abmt_age as baseline
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


# The first four are the substantive 2 x 2 categories. The remaining categories
# retain whichever dimension is known, instead of pooling every unknown case.
CATEGORIES = [
    ("0_cars", "unavailable"), ("0_cars", "available"),
    ("1plus_cars", "unavailable"), ("1plus_cars", "available"),
    ("unknown", "unavailable"), ("unknown", "available"),
    ("0_cars", "unknown"), ("1plus_cars", "unknown"),
    ("unknown", "unknown"),
]
HOUSEHOLD_LABELS = {"0_cars": "No household car",
                    "1plus_cars": "At least one household car",
                    "unknown": "Household car ownership unknown"}
DEFINITIONS = {
    "any_access": "Survey access = always OR available after agreement",
    "always_only": "Sensitivity: survey access = always only",
}
SOURCE_ROOT = "https://github.com/eqasim-org/eqasim-switzerland/blob/cmdp_new_version/"
SOURCES = [
    {"url": SOURCE_ROOT + "data/constants.py",
     "content_blob_sha": "dcfa0c7461edb027aadfb5d1b3713b663ba0d1c4"},
    {"url": SOURCE_ROOT + "data/microcensus/21/persons.py",
     "content_blob_sha": "25bef576ed0ce37cfe5b5410e7dd27c12480f8c0"},
    {"url": SOURCE_ROOT + "synthesis/population/models/caravailability.py",
     "content_blob_sha": "f0875aceafcbf92dec878513ae754076e60c3290"},
    {"url": SOURCE_ROOT + "matsim/scenario/population.py",
     "content_blob_sha": "28ee2d507e82b9d693c1b266868af40ddc59ac43"},
]


def ownership(status):
    """Never interpret an unusable car count as zero."""
    return status if status in {"0_cars", "1plus_cars"} else "unknown"


def survey_access(value, definition):
    if definition not in DEFINITIONS:
        raise ValueError("Unknown availability definition.")
    code = baseline.number(value)
    if code == 1:
        return "available"
    if code == 2:
        return "available" if definition == "any_access" else "unavailable"
    if code in {3, -99}:
        # -99 is structural inapplicability (age/licence), not item nonresponse.
        # We compare driver access, so retain these people under no access.
        return "unavailable"
    return "unknown"  # Includes -97, -98, missing and unrecognised codes.


def synthetic_access(value):
    return {"always": "available", "never": "unavailable"}.get(value, "unknown")


def summarize(frame, household, personal):
    weights = frame.weight.to_numpy(dtype=float)
    if not len(weights) or not (np.isfinite(weights) & (weights > 0)).all():
        raise ValueError("Expected a nonempty eligible cohort with positive finite weights.")
    data = pd.DataFrame({"household": np.asarray(household),
                         "personal": np.asarray(personal), "weight": weights})
    if not set(zip(data.household, data.personal)).issubset(set(CATEGORIES)):
        raise ValueError("A person has an unrecognised comparison category.")
    index = pd.MultiIndex.from_tuples(CATEGORIES, names=["household", "personal"])
    grouped = data.groupby(["household", "personal"]).agg(
        n=("weight", "size"), weight_sum=("weight", "sum"))
    grouped = grouped.reindex(index, fill_value=0)
    grouped["pct"] = 100 * grouped.weight_sum / weights.sum()
    if grouped.n.sum() != len(frame) or not np.isclose(grouped.pct.sum(), 100):
        raise AssertionError("Car-access categories do not reconcile with the full cohort.")
    return grouped


def compare(survey, population, definition):
    mz = summarize(survey, survey.f30100.map(baseline.car_count_status).map(ownership),
                   survey.f42100e.map(lambda value: survey_access(value, definition)))
    sim = summarize(population, population.car_count_status.map(ownership),
                    population.carAvail.map(synthetic_access))
    table = mz.index.to_frame(index=False)
    table["survey_n"] = mz.n.to_numpy(dtype=int)
    table["survey_weight_sum"] = mz.weight_sum.to_numpy()
    table["survey_weighted_pct"] = mz.pct.to_numpy()
    table["synthetic_n"] = sim.n.to_numpy(dtype=int)
    table["synthetic_pct"] = sim.pct.to_numpy()
    table["difference_pp"] = table.synthetic_pct - table.survey_weighted_pct
    return table


def plot_comparison(table, definition, survey_n, synthetic_n, path):
    # Always show the four substantive categories. Show additional unknown
    # categories if either cohort contains someone in them, even at tiny shares.
    visible = table.loc[(table.index < 4) | (table.survey_n + table.synthetic_n > 0)]
    access_labels = {"available": "Personal access", "unavailable": "No personal access / inapplicable",
                     "unknown": "Personal availability unknown"}
    if definition == "always_only":
        access_labels.update(available="Always available",
                             unavailable="Not always available / inapplicable")
    labels = [HOUSEHOLD_LABELS[h] + "\n" + access_labels[p]
              for h, p in zip(visible.household, visible.personal)]
    y = np.arange(len(visible))
    fig, ax = plt.subplots(figsize=(11, max(5.5, 0.72 * len(visible) + 2.2)))
    ax.barh(y - 0.19, visible.survey_weighted_pct, height=0.36, color="#237A8A",
            label="MZMV 2015 (WP weighted)")
    ax.barh(y + 0.19, visible.synthetic_pct, height=0.36, color="#DF8D39",
            label="Synthetic population")
    ax.set(yticks=y, yticklabels=labels, xlabel="Share of all eligible residents (%)")
    ax.invert_yaxis()
    ax.tick_params(axis="y", labelsize=9)
    ax.grid(axis="x", alpha=0.2)
    ax.set_axisbelow(True)
    if len(visible) > 4:
        ax.axhline(3.5, color="#777777", linewidth=0.7, alpha=0.6)
    fig.suptitle("Neuchatel: household car ownership and personal availability", y=0.98, fontsize=13)
    fig.text(0.5, 0.93, DEFINITIONS[definition], ha="center", fontsize=10)
    fig.legend(*ax.get_legend_handles_labels(), loc="upper center", bbox_to_anchor=(0.5, 0.91),
               ncol=2, frameon=False, fontsize=9)
    fig.text(0.5, 0.055, f"Matched resident cohort, ages 6-120; weekday survey. "
             f"Survey n={survey_n:,}; synthetic n={synthetic_n:,}.", ha="center", fontsize=8)
    fig.text(0.5, 0.025, "Unknowns remain in totals. Personal access concerns driving, not riding as a passenger.\n"
             "Synthetic always/never is unchanged; exact scenario generation version is unverified.",
             ha="center", fontsize=8)
    fig.tight_layout(rect=(0, 0.09, 1, 0.86))
    fig.savefig(path, dpi=180)
    plt.close(fig)


def unknown_shares(table):
    return {source: {"household_ownership_unknown_pct": float(table.loc[
                        table.household.eq("unknown"), column].sum()),
                     "personal_availability_unknown_pct": float(table.loc[
                        table.personal.eq("unknown"), column].sum())}
            for source, column in [("survey", "survey_weighted_pct"), ("synthetic", "synthetic_pct")]}




