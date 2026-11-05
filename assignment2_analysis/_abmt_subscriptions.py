"""PT subscription comparison adapted from Hatim's validate_pt_subscriptions.py.

Snapshot SHA-256: b2c5c6db866f513167bc4eaaa56d2ee804ec89195e6d221b2d9d4539cff37a1f.
See PT_PROVENANCE.md. Cohorts/configuration/output come from abmt_common.
"""
import json
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import _abmt_age as baseline
import abmt_common as common

CATEGORIES = ["none", "ga", "va", "ht", "va+ht", "unknown"]

DEFINITIONS = ["generator", "three_items"]

EXTRA_FIELDS = ["f41610d", "f41610f"]

LABELS = {
    "generator": ["None of the represented groups",
                  "GA-coded group\n(includes Junior/Enkel below 16)",
                  "Regional / route pass\nwithout Half Fare",
                  "Half Fare\nwithout GA-coded or regional / route pass",
                  "Regional / route pass + Half Fare",
                  "Unknown / insufficient information"],
    "three_items": ["None of GA / regional / Half Fare",
                    "GA (reported)", "Regional / Inter pass\nwithout Half Fare",
                    "Half Fare\nwithout GA or regional pass",
                    "Regional / Inter pass + Half Fare",
                    "Unknown / insufficient information"],
}

SOURCE_ROOT = "https://github.com/eqasim-org/eqasim-switzerland/blob/cmdp_new_version/"

SOURCES = [
    {"url": SOURCE_ROOT + "data/microcensus/persons.py",
     "content_blob_sha": "b774ebf1b1209ae8d886446ba947019eca31dca9"},
    {"url": SOURCE_ROOT + "synthesis/population/models/subscriptions.py",
     "content_blob_sha": "7a4b39150e917b1159fae226f3ad28c1e3d05cbc"},
    {"url": SOURCE_ROOT + "matsim/scenario/population.py",
     "content_blob_sha": "28ee2d507e82b9d693c1b266868af40ddc59ac43"},
]

def yes_no(value, field, age):
    """True / False / None; do not turn item nonresponse into 'no'."""
    code = baseline.number(value)
    if code == 1:
        return True
    if code == 2:
        return False
    if field == "f41610b" and code == -99 and age < 16:
        return False  # Codebook: Half Fare question inapplicable below 16.
    return None

def either(first, second):
    """Three-valued OR: a confirmed 'yes' resolves an unknown other flag."""
    if first is True or second is True:
        return True
    if first is False and second is False:
        return False
    return None

def classify_survey(row, definition):
    if definition not in DEFINITIONS:
        raise ValueError("Unknown subscription definition.")
    age = float(row["age"])
    ga = yes_no(row["f41610a"], "f41610a", age)
    ht = yes_no(row["f41610b"], "f41610b", age)
    regional = yes_no(row["f41610c"], "f41610c", age)
    if definition == "generator":
        if age < 16:
            ga = either(ga, yes_no(row["f41610f"], "f41610f", age))
        regional = either(regional, yes_no(row["f41610d"], "f41610d", age))
    if ga is True:
        return "ga"  # GA-coded group takes priority, regardless of other flags.
    if ga is None or ht is None or regional is None:
        return "unknown"
    if regional and ht:
        return "va+ht"
    if regional:
        return "va"
    return "ht" if ht else "none"

def add_extra_fields(survey, data):
    result = common.link_survey_fields(survey, "zielpersonen.csv", EXTRA_FIELDS, base=data)
    for field in EXTRA_FIELDS:
        result[field] = pd.to_numeric(result[field], errors="coerce")
    return result


def summarize(frame, categories):
    weights = frame.weight.to_numpy(dtype=float)
    if not len(weights) or not (np.isfinite(weights) & (weights > 0)).all():
        raise ValueError("Expected a nonempty eligible cohort with positive finite weights.")
    category_values = np.asarray(categories)
    if not set(category_values).issubset(set(CATEGORIES)):
        raise ValueError("An unrecognised comparison category was produced.")
    data = pd.DataFrame({"category": category_values, "weight": weights})
    grouped = data.groupby("category").agg(n=("weight", "size"), weight_sum=("weight", "sum"))
    grouped = grouped.reindex(CATEGORIES, fill_value=0)
    grouped["pct"] = 100 * grouped.weight_sum / weights.sum()
    if grouped.n.sum() != len(frame) or not np.isclose(grouped.pct.sum(), 100):
        raise AssertionError("PT categories do not reconcile with the full eligible cohort.")
    return grouped

def compare(survey, population, definition):
    mz_codes = survey.apply(classify_survey, axis=1, definition=definition)
    sim_codes = population.subscriptions.map(
        lambda value: value if value in CATEGORIES[:-1] else "unknown")
    mz, sim = summarize(survey, mz_codes), summarize(population, sim_codes)
    table = pd.DataFrame({"category": CATEGORIES, "label": LABELS[definition]})
    table["survey_n"] = mz.n.to_numpy(dtype=int)
    table["survey_weight_sum"] = mz.weight_sum.to_numpy()
    table["survey_weighted_pct"] = mz.pct.to_numpy()
    table["synthetic_n"] = sim.n.to_numpy(dtype=int)
    table["synthetic_pct"] = sim.pct.to_numpy()
    table["difference_pp"] = table.synthetic_pct - table.survey_weighted_pct
    return table, mz_codes

def plot_comparison(table, definition, survey_n, synthetic_n, path):
    labels = (['None of represented products', 'GA-coded (incl. Junior/Enkel <16)',
               'Regional / route pass', 'Half Fare', 'Regional / route + Half Fare', 'Unknown']
              if definition == 'generator' else
              ['None of the three products', 'GA', 'Regional / Inter pass',
               'Half Fare', 'Regional / Inter + Half Fare', 'Unknown'])
    y = np.arange(len(table))
    fig, ax = plt.subplots(figsize=(10.5, 5.5))
    ax.barh(y - .19, table.survey_weighted_pct, .36, color='#1f77b4',
            label='Mikrozensus (weighted)')
    ax.barh(y + .19, table.synthetic_pct, .36, color='#ff7f0e',
            label='MATSim (10% sample)')
    ax.set(yticks=y, yticklabels=labels, xlabel='Share of eligible residents (%)')
    ax.invert_yaxis()
    ax.tick_params(axis='y', labelsize=10)
    ax.grid(axis='x', alpha=.18)
    ax.set_axisbelow(True)
    ax.axhline(4.5, color='.65', linewidth=.6)
    grouping = 'main groups' if definition == 'generator' else 'three-item sensitivity'
    fig.suptitle(f'Neuchâtel: PT subscriptions — {grouping}', fontsize=14, y=.98)
    ax.legend(loc='lower center', bbox_to_anchor=(.5, 1.005), ncol=2, frameon=False, fontsize=9)
    fig.text(.5, .025, 'Unknowns included · Product groups do not imply identical fare rights.',
             ha='center', fontsize=8)
    fig.tight_layout(rect=(0, .06, 1, .92))
    fig.savefig(path, dpi=180, bbox_inches='tight', pad_inches=.12)
    plt.close(fig)



def run():
    data, out = common.data_root(), common.output('pt_subscriptions')
    survey, survey_funnel = common._survey()
    survey = add_extra_fields(survey.copy(), data)
    population, population_summary = common.resident_home_attributes(('subscriptions',))
    population['subscriptions'] = population.subscriptions.map(
        lambda value: baseline.model_code(value, baseline.PT_CODES))
    crs = {'survey_home': 'EPSG:21781', 'analysis_home': 'EPSG:2056'}
    main_table, main_codes = compare(survey, population, 'generator')
    sensitivity, sensitivity_codes = compare(survey, population, 'three_items')
    transitions = pd.DataFrame({'generator_group': main_codes.to_numpy(),
        'three_item_group': sensitivity_codes.to_numpy(), 'weight': survey.weight.to_numpy()})
    transitions = transitions.groupby(['generator_group', 'three_item_group'], as_index=False).agg(
        persons=('weight', 'size'), weight_sum=('weight', 'sum'))
    transitions['survey_weighted_pct'] = 100 * transitions.weight_sum / survey.weight.sum()
    notes = {
        "survey_edition": "MZMV 2015", "synthetic_reference_year": "not yet verified",
        "eligibility": "Same resident selection as validate_age.py: home within supplied boundary; ages 6-120; "
                       "non-travellers retained. Weekday survey with positive WP; synthetic freight excluded.",
        "denominator": "All eligible people, including unknown subscription groups; no complete-case renormalisation.",
        "weights": {"survey": "WP person weight, not household weight WM", "synthetic": "One per eligible agent"},
        "survey_fields": {"f41610a": "GA", "f41610b": "Half Fare", "f41610c": "Regional / Inter pass",
                          "f41610d": "Route pass", "f41610f": "Junior / Enkel card"},
        "generator_mapping": {"ga": "GA OR Junior/Enkel below 16; priority over other products",
                              "va": "Regional/Inter OR route pass, without GA-coded or Half Fare",
                              "ht": "Half Fare, without GA-coded or regional/route pass",
                              "va+ht": "Regional/route AND Half Fare, without GA-coded product",
                              "none": "None of those represented groups; other products may still be held"},
        "three_item_sensitivity": "Uses literal GA, regional/Inter and Half Fare items only, with GA priority. "
                                  "Survey grouping changes; supplied synthetic codes remain unchanged.",
        "missingness": [
            "1=yes, 2=no. -97/-98, missing and unrecognised answers remain unknown.",
            "Half Fare -99 below 16 is structural inapplicability, treated as no Half Fare product.",
            "Other -99 not-asked responses remain unknown; they are not assumed to mean no product.",
            "A confirmed GA-coded product resolves the category even if lower-priority products are unknown.",
            "For combined products, a confirmed yes resolves an OR; otherwise an unresolved relevant flag gives unknown.",
            "The public training pipeline treats non-yes answers as false. This validation preserves uncertainty instead."],
        "interpretation": [
            "GA-coded children with Junior/Enkel cards do not have the same fare rights as GA holders.",
            "Half Fare is a discount product; product ownership is not PT trip frequency or demonstrated shuttle demand.",
            "Gleis 7 and other products are not separate synthetic classes; 'none' is not literal absence of all PT products.",
            "The inspected generator is a definition reference, not verified provenance of the supplied scenario.",
            "Survey/model agreement does not establish independent out-of-sample validation; training-data overlap is possible."],
        "sources_inspected_2026_11_03": SOURCES,
        "survey_mapping_transitions": transitions.to_dict(orient="records"),
        "unknown_pct": {"survey_generator": float(main_table.loc[main_table.category.eq("unknown"), "survey_weighted_pct"].iloc[0]),
                        "survey_three_items": float(sensitivity.loc[sensitivity.category.eq("unknown"), "survey_weighted_pct"].iloc[0]),
                        "synthetic": float(main_table.loc[main_table.category.eq("unknown"), "synthetic_pct"].iloc[0])},
        "survey_funnel": survey_funnel, "synthetic_cohort": population_summary, "coordinate_systems": crs,
    }
    notes['eligibility'] = common.METHOD['residents'] + ' ' + common.METHOD['survey'] + ' Nontravellers retained.'
    notes['provenance'] = {'adapted_from': 'Hatim: validate_pt_subscriptions.py',
        'sha256': 'b2c5c6db866f513167bc4eaaa56d2ee804ec89195e6d221b2d9d4539cff37a1f',
        'integration': 'Nelli step layout and shared resident readers; see PT_PROVENANCE.md'}
    notes['cohort_totals'] = {'survey_n': len(survey), 'survey_weight_sum': float(survey.weight.sum()),
        'synthetic_n': len(population), 'synthetic_weight_sum': float(population.weight.sum())}
    for definition, table, stem in [('generator', main_table, 'pt_subscriptions_comparison'),
                                    ('three_items', sensitivity, 'pt_subscriptions_sensitivity')]:
        table.to_csv(out / (stem + '.csv'), index=False, float_format='%.6f')
        plot_comparison(table, definition, len(survey), len(population), out / (stem + '.png'))
    transitions.to_csv(out / 'survey_subscription_mapping_transitions.csv', index=False, float_format='%.6f')
    (out / 'pt_subscriptions_method.json').write_text(json.dumps(notes, indent=2, allow_nan=False) + '\n', encoding='utf8')
    common.write_method(out, {'pt_subscriptions_method': notes})
    print('PT subscription aggregates saved to:', out)
