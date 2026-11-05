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
    visible = table.loc[(table.index < 4) | (table.survey_n + table.synthetic_n > 0)]
    household_labels = {'0_cars': '0 household cars', '1plus_cars': '1+ household cars',
                        'unknown': 'Ownership unknown'}
    access_labels = {'available': 'Driver access', 'unavailable': 'No driver access / inapplicable',
                    'unknown': 'Access unknown'}
    if definition == 'always_only':
        access_labels.update(available='Always available', unavailable='Not always available / inapplicable')
    labels = [household_labels[h] + '\n' + access_labels[p]
              for h, p in zip(visible.household, visible.personal)]
    y = np.arange(len(visible))
    fig, ax = plt.subplots(figsize=(10.5, max(4.8, .65 * len(visible) + 1.6)))
    ax.barh(y - .19, visible.survey_weighted_pct, .36, color='#1f77b4', label='Mikrozensus (weighted)')
    ax.barh(y + .19, visible.synthetic_pct, .36, color='#ff7f0e', label='MATSim (10% sample)')
    ax.set(yticks=y, yticklabels=labels, xlabel='Share of eligible residents (%)')
    ax.invert_yaxis()
    ax.tick_params(axis='y', labelsize=9)
    ax.grid(axis='x', alpha=.18)
    ax.set_axisbelow(True)
    if len(visible) > 4:
        ax.axhline(3.5, color='.65', linewidth=.6)
    title = 'Neuchâtel: car ownership and driver access'
    if definition == 'always_only':
        title += ' (always only)'
    fig.suptitle(title, y=.98, fontsize=14)
    ax.legend(loc='lower center', bbox_to_anchor=(.5, 1.005), ncol=2, frameon=False, fontsize=9)
    note = ('Access = always or by agreement' if definition == 'any_access' else 'Access = always only')
    fig.text(.5, .025, note + ' · All eligible residents; unknowns retained', ha='center', fontsize=8)
    fig.tight_layout(rect=(0, .06, 1, .92))
    fig.savefig(path, dpi=180, bbox_inches='tight', pad_inches=.12)
    plt.close(fig)



def unknown_shares(table):
    return {source: {"household_ownership_unknown_pct": float(table.loc[
                        table.household.eq("unknown"), column].sum()),
                     "personal_availability_unknown_pct": float(table.loc[
                        table.personal.eq("unknown"), column].sum())}
            for source, column in [("survey", "survey_weighted_pct"), ("synthetic", "synthetic_pct")]}




