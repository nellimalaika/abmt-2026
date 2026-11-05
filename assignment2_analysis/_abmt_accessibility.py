"""Common-reference residential PT contexts, adapted from Hatim's scripts.

Primary snapshot: validate_pt_accessibility_common.py,
SHA-256 d057b4d49b9223a0199d6e2717efafc7e75abcdd2d0da8228c1bbe6711da06f2.
Supporting class/grid logic: validate_pt_accessibility.py; see PT_PROVENANCE.md.
No independent eligibility definitions or person-level exports are introduced.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import urllib.request
import zipfile

import matplotlib.pyplot as plt
from matplotlib.colors import Normalize
from matplotlib.patches import PathPatch
from matplotlib.ticker import FuncFormatter
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import Point, box
from shapely.strtree import STRtree
from shapely import wkb

import _abmt_age as baseline
import _abmt_spatial as spatial
import abmt_common as common

CLASSES = ["A", "B", "C", "D", "no_class", "unknown"]

LABELS = ["A: very good", "B: good", "C: moderate", "D: low",
          "No class: marginal or no service", "Unknown / missing"]

LOW_SERVICE = {"D", "no_class"}

SOURCE_ROOT = "https://github.com/eqasim-org/eqasim-switzerland/blob/cmdp_new_version/"

SOURCES = [
    {"url": SOURCE_ROOT + "data/spatial/ovgk.py",
     "content_blob_sha": "e0c2d93ead3cd7fd318653219ff0f3b4361596c7"},
    {"url": SOURCE_ROOT + "data/microcensus/households.py",
     "content_blob_sha": "9b927fc0c1d0741ae57897efccda2752db82195a"},
    {"url": "https://www.are.admin.ch/dam/de/sd-web/AZsFQRE7tiOY/oev-gueteklassen-berechnungsmethodikare.pdf"},
]

def survey_class(value):
    return {1: "A", 2: "B", 3: "C", 4: "D", 5: "no_class"}.get(baseline.number(value), "unknown")

def model_class(value):
    if value is None or pd.isna(value):
        return "unknown"
    text = str(value).strip()
    if text.upper() in {"A", "B", "C", "D"}:
        return text.upper()
    # The string is an explicit outside-class code in the inspected imputer.
    # Python None / absent XML attributes were already handled above.
    return "no_class" if text.casefold() == "none" else "unknown"

def audit_label(value, source):
    if value is None or pd.isna(value) or not str(value).strip():
        return "MISSING_OR_BLANK"
    if source == "synthetic" and str(value).strip().casefold() == "none":
        return "literal_None"
    text = str(value).strip()
    if re.fullmatch(r"[-+]?\d+(?:\.0+)?", text):
        return "numeric_" + text
    if re.fullmatch(r"[A-Za-z]", text):
        return "letter_" + text.upper()
    return "OTHER_UNRECOGNISED_TEXT"

def code_profile(frame, raw_column, source):
    result = frame.assign(code=frame[raw_column].map(lambda value: audit_label(value, source))).groupby(
        "code", as_index=False).agg(persons=("weight", "size"), weight_sum=("weight", "sum"))
    result["weighted_pct"] = 100 * result.weight_sum / frame.weight.sum()
    return result.to_dict(orient="records")

def compare_classes(survey, population):
    table = pd.DataFrame({"pt_class": CLASSES, "label": LABELS})
    for prefix, frame in [("survey", survey), ("synthetic", population)]:
        grouped = frame.groupby("pt_class").agg(n=("weight", "size"), weight_sum=("weight", "sum"))
        grouped = grouped.reindex(CLASSES, fill_value=0)
        table[prefix + "_n"] = grouped.n.to_numpy(dtype=int)
        table[prefix + "_weight_sum"] = grouped.weight_sum.to_numpy()
        table[prefix + "_pct"] = 100 * grouped.weight_sum.to_numpy() / frame.weight.sum()
        if grouped.n.sum() != len(frame) or not np.isclose(table[prefix + "_pct"].sum(), 100):
            raise AssertionError("PT accessibility classes do not reconcile with the full cohort.")
    table = table.rename(columns={"survey_pct": "survey_weighted_pct"})
    table["difference_pp"] = table.synthetic_pct - table.survey_weighted_pct
    return table

def aggregate_quality_grid(survey, population, grid, geometry, size, min_survey_n, min_synthetic_n):
    if size != common.GRID_M or min(min_survey_n, min_synthetic_n) < common.MIN_CONTRIBUTORS:
        raise ValueError('Use the agreed 10 km grid and at least 30 distinct residents per source.')
    table = grid.copy()
    for prefix, frame in [("survey", survey), ("synthetic", population)]:
        identifier = 'HHNR' if prefix == 'survey' else 'person'
        if frame[identifier].isna().any() or frame[identifier].duplicated().any():
            raise ValueError('Grid support requires one row per distinct resident.')
        weight = frame.weight.to_numpy(dtype=float)
        if not len(weight) or not (np.isfinite(weight) & (weight > 0)).all():
            raise ValueError("Expected nonempty cohorts with positive finite weights.")
        keys = spatial.assign_cells(frame, geometry, size)
        known, low = frame.pt_class.ne("unknown").to_numpy(), frame.pt_class.isin(LOW_SERVICE).to_numpy()
        items = pd.DataFrame({"ix": [k[0] for k in keys], "iy": [k[1] for k in keys],
                              "weight": weight, "known": known.astype(int),
                              "known_weight": np.where(known, weight, 0), "low_weight": np.where(low, weight, 0)})
        grouped = items.groupby(["ix", "iy"], as_index=False).agg(
            n=("weight", "size"), known_n=("known", "sum"), weight_sum=("weight", "sum"),
            known_weight=("known_weight", "sum"), low_weight=("low_weight", "sum"))
        metrics = ["n", "known_n", "weight_sum", "known_weight", "low_weight"]
        grouped = grouped.rename(columns={name: prefix + "_" + name for name in metrics})
        table = table.merge(grouped, on=["ix", "iy"], how="left", validate="one_to_one")
        for name in metrics:
            table[prefix + "_" + name] = table[prefix + "_" + name].fillna(0)
        for name in ["n", "known_n"]:
            table[prefix + "_" + name] = table[prefix + "_" + name].astype(int)
        total, known_weight = table[prefix + "_weight_sum"], table[prefix + "_known_weight"]
        table[prefix + "_unknown_pct"] = 100 * (total - known_weight) / total.where(total.gt(0))
        table[prefix + "_low_service_pct"] = 100 * table[prefix + "_low_weight"] / known_weight.where(known_weight.gt(0))
        if table[prefix + "_n"].sum() != len(frame) or not np.isclose(total.sum(), weight.sum()):
            raise AssertionError("Spatial accessibility totals do not reconcile with the full cohort.")
    table["survey_rate_shown"] = table.survey_known_n.ge(min_survey_n)
    table["synthetic_rate_shown"] = table.synthetic_known_n.ge(min_synthetic_n)
    table["difference_shown"] = table.survey_rate_shown & table.synthetic_rate_shown
    table["difference_pp"] = table.synthetic_low_service_pct - table.survey_low_service_pct
    table["difference_pp_displayed"] = table.difference_pp.where(table.difference_shown)
    return table

YEAR = 2015

CATALOG_DATETIME = "2015-03-17T00:00:00Z"

CATALOG_URL = "https://data.geo.admin.ch/api/stac/v0.9/collections/ch.are.gueteklassen_oev/items/gueteklassen_oev_2015"

ARCHIVE_URL = "https://data.geo.admin.ch/ch.are.gueteklassen_oev/gueteklassen_oev_2015/gueteklassen_oev_2015_2056.gpkg.zip"

ZIP_SHA256 = "dfede4a7a2e2c66536c476d958f9daafb9a2dd292ccadbb3fc8d8d419bdaa0b1"

GPKG_SHA256 = "ef7ad0511c68a410c50caa41cfa63a8b24b6cb2d4c069227a302806569428129"

GPKG_MEMBER = "OeV_Gueteklassen_ARE.gpkg"

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def ensure_reference(directory):
    directory.mkdir(parents=True, exist_ok=True)
    gpkg = directory / "OeV_Gueteklassen_ARE_2015.gpkg"
    archive = directory / "gueteklassen_oev_2015_2056.gpkg.zip"
    if gpkg.is_file() and sha256(gpkg) == GPKG_SHA256:
        return gpkg
    if not archive.is_file() or sha256(archive) != ZIP_SHA256:
        print("Downloading the official ARE 2015 reference archive (about 5.7 MB)...", flush=True)
        temporary = archive.with_suffix(archive.suffix + ".part")
        try:
            with urllib.request.urlopen(ARCHIVE_URL, timeout=45) as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target)
            if sha256(temporary) != ZIP_SHA256:
                raise ValueError("The ARE archive checksum differs from the verified reference. Do not use it without checking its metadata.")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    temporary = gpkg.with_suffix(".part")
    try:
        with zipfile.ZipFile(archive) as source:
            if source.namelist().count(GPKG_MEMBER) != 1:
                raise ValueError("The reference archive does not contain the expected unique GeoPackage.")
            # Extract only this named member, without trusting archive paths.
            with source.open(GPKG_MEMBER) as member, temporary.open("wb") as target:
                shutil.copyfileobj(member, target)
        if sha256(temporary) != GPKG_SHA256:
            raise ValueError("Extracted reference GeoPackage checksum differs from the verified file.")
        temporary.replace(gpkg)
    finally:
        temporary.unlink(missing_ok=True)
    return gpkg

def decode_gpkg_geometry(blob):
    # Standard GeoPackage binary header: OGC GeoPackage, section 2.1.3.
    if blob is None or len(blob) < 8 or blob[:2] != b"GP" or blob[2] != 0:
        raise ValueError("Unsupported or missing GeoPackage geometry header.")
    flags = blob[3]
    if flags & 0xE0:
        raise ValueError("Extended/reserved GeoPackage geometry flags are unsupported.")
    srs = struct.unpack("<i" if flags & 1 else ">i", blob[4:8])[0]
    if srs != 2056:
        raise ValueError("Reference geometry is not encoded in EPSG:2056.")
    envelope = (flags >> 1) & 7
    lengths = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    if envelope not in lengths:
        raise ValueError("Invalid GeoPackage envelope indicator.")
    offset = 8 + lengths[envelope]
    if len(blob) <= offset:
        raise ValueError("GeoPackage geometry has no WKB payload.")
    geometry = wkb.loads(blob[offset:])
    if flags & 0x10 or geometry.is_empty or not geometry.is_valid:
        raise ValueError("The reference contains empty or invalid geometry; inspect it before assigning classes.")
    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("The accessibility layer must contain polygons.")
    return geometry

def read_reference_polygons(path, bounds):
    envelope = box(*bounds)
    geometries, classes = [], []
    total = 0
    # Read-only SQLite: the pinned public file has two layers, including stops.
    # Select the polygon layer explicitly, never the point layer.
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as database:
        metadata = database.execute("SELECT column_name, geometry_type_name, srs_id FROM gpkg_geometry_columns "
                                    "WHERE table_name='OeV_Gueteklassen_ARE'").fetchall()
        if metadata != [("geom", "POLYGON", 2056)]:
            raise ValueError("Unexpected ARE polygon-layer schema or CRS.")
        for label, blob in database.execute('SELECT "Klasse", "geom" FROM "OeV_Gueteklassen_ARE"'):
            total += 1
            if label not in {"A", "B", "C", "D"}:
                raise ValueError("Unexpected class in the official ARE reference.")
            geometry = decode_gpkg_geometry(blob)
            if geometry.intersects(envelope):
                geometries.append(geometry)
                classes.append(label)
    if not geometries:
        raise ValueError("No ARE A-D polygons intersect the study envelope; verify the layer and CRS.")
    return geometries, classes, {"national_polygon_count": total,
                                 "polygons_intersecting_study_envelope": len(geometries), "crs": "EPSG:2056"}

def assign_common_classes(frame, geometries, classes):
    tree = STRtree(geometries)
    assigned, ambiguous = [], 0
    for x, y in zip(frame.x, frame.y):
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("An eligible resident has nonfinite home coordinates.")
        point = Point(x, y)
        hits = {classes[int(i)] for i in tree.query(point) if geometries[int(i)].covers(point)}
        ambiguous += int(len(hits) > 1)
        # Boundary-inclusive predicate; take the best class on shared borders
        # or overlapping graded areas. Same rule for both population sources.
        assigned.append(min(hits) if hits else "no_class")
    result = frame.copy()
    result["supplied_class"] = result.pt_class
    result["pt_class"] = assigned
    if (not result.index.equals(frame.index)
            or not np.array_equal(result.weight.to_numpy(), frame.weight.to_numpy())
            or len(result) != len(frame) or not np.isclose(result.weight.sum(), frame.weight.sum())):
        raise AssertionError("Reclassification changed the cohort or its total weight.")
    return result, {"homes_covered_by_multiple_class_labels": ambiguous}

def transitions(frame):
    result = frame.groupby(["supplied_class", "pt_class"], as_index=False).agg(
        persons=("weight", "size"), weight_sum=("weight", "sum"))
    result = result.rename(columns={"pt_class": "common_class"})
    result["weighted_pct"] = 100 * result.weight_sum / frame.weight.sum()
    if result.persons.sum() != len(frame) or not np.isclose(result.weighted_pct.sum(), 100):
        raise AssertionError("Accessibility transition totals do not reconcile.")
    return result

def plot_comparison(supplied, common, survey_n, synthetic_n, path):
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.2), sharex=True, sharey=True)
    maximum = 1.08 * max(supplied.survey_weighted_pct.max(), supplied.synthetic_pct.max(),
                         common.survey_weighted_pct.max(), common.synthetic_pct.max())
    y = np.arange(len(CLASSES))
    for ax, table, title in zip(axes, [supplied, common], ['Supplied attributes', 'Common ARE 2015 reference']):
        ax.barh(y - .19, table.survey_weighted_pct, .36, color='#1f77b4',
                label='Mikrozensus (weighted)')
        ax.barh(y + .19, table.synthetic_pct, .36, color='#ff7f0e',
                label='MATSim (10% sample)')
        ax.set(xlim=(0, maximum), xlabel='Share of eligible residents (%)', title=title)
        ax.grid(axis='x', alpha=.18)
        ax.set_axisbelow(True)
    axes[0].set_yticks(y, ['A: very good', 'B: good', 'C: moderate', 'D: low', 'No class', 'Unknown'])
    axes[0].invert_yaxis()
    axes[1].tick_params(labelleft=False)
    fig.suptitle('Neuchâtel: residential PT classes', fontsize=15, y=.98)
    fig.legend(*axes[0].get_legend_handles_labels(), loc='upper center',
               bbox_to_anchor=(.5, .925), ncol=2, frameon=False, fontsize=10)
    fig.text(.5, .025, 'All eligible residents · Unknown supplied attributes retained', ha='center', fontsize=8)
    fig.subplots_adjust(left=.14, right=.985, top=.79, bottom=.14, wspace=.08)
    fig.savefig(path, dpi=180, bbox_inches='tight', pad_inches=.12)
    plt.close(fig)


def plot_common_maps(table, geometry, polygon, size, min_survey_n, min_synthetic_n, path):
    shown = table.loc[table.difference_shown, 'difference_pp']
    limit = max(float(shown.abs().max()), 1) if len(shown) else 1
    norms = [Normalize(0, 100), Normalize(0, 100), Normalize(-limit, limit)]
    # Preserve the original map palettes; bar-chart colours remain blue/orange.
    cmaps = [plt.get_cmap('YlOrRd'), plt.get_cmap('YlOrRd'), plt.get_cmap('RdBu_r')]
    paths = {key: spatial.polygon_path(value) for key, value in geometry.items()}
    outline = spatial.polygon_path(polygon)
    fig = plt.figure(figsize=(14, 5.1))
    spec = fig.add_gridspec(2, 3, height_ratios=[1, .06])
    xmin, ymin, xmax, ymax = polygon.bounds
    margin = max(xmax - xmin, ymax - ymin) * .04
    columns = ['survey_low_service_pct', 'synthetic_low_service_pct', 'difference_pp']
    masks = ['survey_rate_shown', 'synthetic_rate_shown', 'difference_shown']
    for i, title in enumerate(['Mikrozensus', 'MATSim', 'MATSim − Mikrozensus']):
        ax = fig.add_subplot(spec[0, i])
        for row in table.itertuples(index=False):
            masked = not getattr(row, masks[i])
            ax.add_patch(PathPatch(paths[(row.ix, row.iy)],
                facecolor='#ECECEC' if masked else cmaps[i](norms[i](getattr(row, columns[i]))),
                edgecolor='#AAAAAA' if masked else 'white', linewidth=.4,
                hatch='///' if masked else None))
        ax.add_patch(PathPatch(outline, facecolor='none', edgecolor='#222222', linewidth=.8))
        ax.set(xlim=(xmin-margin, xmax+margin), ylim=(ymin-margin, ymax+margin),
               title=title, xlabel='LV95 easting (km)')
        ax.set_aspect('equal', adjustable='box')
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, pos: f'{value/1000:.0f}'))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, pos: f'{value/1000:.0f}'))
        ax.tick_params(labelsize=8, labelleft=i == 0)
        if i == 0:
            ax.set_ylabel('LV95 northing (km)')
        color_ax = fig.add_subplot(spec[1, i])
        bar = fig.colorbar(plt.cm.ScalarMappable(norm=norms[i], cmap=cmaps[i]),
            cax=color_ax, orientation='horizontal')
        bar.set_label('Difference (percentage points)' if i == 2 else
                      'D / no class among classified residents (%)', fontsize=9)
        bar.ax.tick_params(labelsize=8)
    support = (f'n < {min_survey_n}' if min_survey_n == min_synthetic_n else
               f'MZ n < {min_survey_n} / MATSim n < {min_synthetic_n}')
    fig.suptitle('Neuchâtel: residential PT coverage (ARE 2015)', fontsize=15, y=.98)
    fig.text(.5, .025, f'{size/1000:g} km grid · Hatched: {support}; differences require both sources · Residential reference',
             ha='center', fontsize=8)
    fig.subplots_adjust(left=.055, right=.99, top=.84, bottom=.22, wspace=.13, hspace=.30)
    fig.savefig(path, dpi=180, bbox_inches='tight', pad_inches=.12)
    plt.close(fig)



def add_survey_class(survey, data):
    result = common.link_survey_fields(survey, 'haushalte.csv', ['W_OeV_KLASSE'], base=data)
    result['pt_class'] = result.W_OeV_KLASSE.map(survey_class)
    return common.survey_home_coordinates(result)


def coverage_summary(cells, survey, population):
    result = {'cells_total': len(cells), 'cells_with_displayed_difference': int(cells.difference_shown.sum())}
    for prefix, frame in [('survey', survey), ('synthetic', population)]:
        known = frame.pt_class.ne('unknown')
        shown = cells.difference_shown
        result[prefix] = {
            'residents_n': len(frame), 'weight_sum': float(frame.weight.sum()),
            'classified_n': int(known.sum()),
            'classified_weight_pct': float(100 * frame.loc[known, 'weight'].sum() / frame.weight.sum()),
            'residents_n_in_displayed_difference_cells': int(cells.loc[shown, prefix + '_n'].sum()),
            'resident_weight_pct_in_displayed_difference_cells': float(
                100 * cells.loc[shown, prefix + '_weight_sum'].sum() / frame.weight.sum())}
    return result


def run(reference_dir=None):
    if int(shapely.__version__.split('.')[0]) < 2:
        raise ValueError('Common-reference assignment requires Shapely 2 or newer.')
    data, out = common.data_root(), common.output('pt_accessibility_common')
    cache = Path(reference_dir or os.environ.get('ABMT_ARE_REFERENCE_DIR') or
                 Path(__file__).resolve().parent / 'reference_data' / 'are_2015').expanduser().resolve()
    if any(path == data or data in path.parents for path in [out.resolve(), cache]):
        raise ValueError('Keep outputs and the public reference cache outside the input-data directory.')
    gpkg = ensure_reference(cache)
    region = common.boundary()
    geometries, classes, layer_summary = read_reference_polygons(gpkg, region.context.bounds)
    survey, survey_funnel = common._survey()
    survey = add_survey_class(survey.copy(), data)
    population, population_summary = common.resident_home_attributes(('ovgk',))
    population['pt_class'] = population.ovgk.map(model_class)
    supplied = compare_classes(survey, population)
    supplied_codes = {'survey': code_profile(survey, 'W_OeV_KLASSE', 'survey'),
                      'synthetic': code_profile(population, 'ovgk', 'synthetic')}
    survey, survey_assignment = assign_common_classes(survey, geometries, classes)
    population, population_assignment = assign_common_classes(population, geometries, classes)
    assigned = compare_classes(survey, population)
    comparison = supplied.merge(assigned.drop(columns='label'), on='pt_class',
        suffixes=('_supplied', '_common'), validate='one_to_one')
    comparison['change_in_gap_pp'] = comparison.difference_pp_common - comparison.difference_pp_supplied
    grid, grid_geometry = spatial.make_grid(region.context, common.GRID_M)
    cells = aggregate_quality_grid(survey, population, grid, grid_geometry, common.GRID_M,
                                  common.MIN_CONTRIBUTORS, common.MIN_CONTRIBUTORS)
    crs = {'survey_home': 'EPSG:21781 transformed to EPSG:2056',
           'synthetic_home': 'EPSG:2056', 'reference': 'EPSG:2056'}
    notes = {
        "purpose": "Controlled location comparison under one common official PT accessibility layer",
        "survey_edition": "MZMV 2015", "synthetic_population_reference_year": "unverified",
        "reference": {"provider": "ARE (data.geo.admin.ch)", "archive_year": YEAR, "catalogue_datetime": CATALOG_DATETIME,
                      "catalogue_url": CATALOG_URL, "archive_url": ARCHIVE_URL, "zip_sha256": ZIP_SHA256,
                      "gpkg_sha256": GPKG_SHA256, "schema": "OeV_Gueteklassen_ARE: Klasse, geom; EPSG:2056"},
        "class_assignment": "Identical local point-in-polygon operation with covers, retaining polygon-boundary points. Best grade on shared borders/overlaps. No polygon hit becomes no_class within the nationally covered study area.",
        "cohort_and_weights": "Unchanged eligible residents and weights from earlier scripts: weekday/positive-WP survey, age 6-120, actual home within boundary, synthetic freight excluded, non-travellers retained.",
        "class_chart_denominator": "All eligible residents in each dataset; supplied unknown classes remain in the supplied comparison. Common classification is available for all valid retained homes.",
        "map_denominator": "Classified residents within each cell; D and no_class form the preselected low-service group.",
        "interpretation": ["Archive year 2015 was selected to match the survey edition, without asserting the original survey geodata used this exact layer.",
                           "This removes differing supplied grade surfaces/calculations from the comparison. Differences can still reflect fine-scale residential allocation, home geocoding, weights and sampling.",
                           "A change in the gap helps identify attribute comparability effects; it does not establish which supplied source was wrong or prove service changes caused the gap.",
                           "This does not reconstruct the simulation's service network or validate PT travel time, reliability or shuttle demand.",
                           "Unknown supplied attributes can receive a common grade because valid home coordinates are available; no resident is dropped or reweighted.",
                           "Thresholds are display heuristics, not significance tests."],
        "coordinate_systems": crs, "reference_layer": layer_summary,
        "survey_assignment": survey_assignment, "synthetic_assignment": population_assignment,
        "survey_funnel": survey_funnel, "synthetic_cohort": population_summary,
        "software": {"shapely": shapely.__version__}, "grid_size_metres": common.GRID_M,
        "minimum_known_counts": {"survey": common.MIN_CONTRIBUTORS, "synthetic": common.MIN_CONTRIBUTORS},
    }
    notes['cohort_and_weights'] = common.METHOD['residents'] + ' ' + common.METHOD['survey'] + ' Nontravellers retained; synthetic weight 1.'
    notes['reference']['gpkg_checksum_verified'] = sha256(gpkg) == GPKG_SHA256
    notes['reference']['zip_checksum_verified_in_this_run'] = (
        (cache / 'gueteklassen_oev_2015_2056.gpkg.zip').is_file() and
        sha256(cache / 'gueteklassen_oev_2015_2056.gpkg.zip') == ZIP_SHA256)
    notes['grid'] = {'crs': 'EPSG:2056', 'cell_size_metres': common.GRID_M, 'origin_metres': [0, 0],
        'assignment': 'Floor coordinates / cell size; deterministic adjacent covering cell for boundary-only cells',
        'display': 'Clipped to study boundary; at least 30 distinct classified residents per source for differences'}
    notes['coverage'] = coverage_summary(cells, survey, population)
    notes['supplied_code_audit'] = supplied_codes
    notes['supplied_attribute_definition_sources'] = SOURCES
    notes['class_mapping'] = {'survey': '1=A, 2=B, 3=C, 4=D, 5=no_class; all other answers unknown',
        'synthetic': 'A/B/C/D and literal None=no_class; absent, blank, null/NaN, numeric or other text unknown'}
    notes['provenance'] = {'adapted_from': 'Hatim: validate_pt_accessibility_common.py and dependencies',
        'primary_sha256': 'd057b4d49b9223a0199d6e2717efafc7e75abcdd2d0da8228c1bbe6711da06f2',
        'integration': 'Shared cohorts and output/configuration; agreed global 10 km lattice; see PT_PROVENANCE.md'}
    notes['interpretation'].append('This does not establish current PT service quality, simulated accessibility or punctuality.')
    comparison.to_csv(out / 'pt_accessibility_common_comparison.csv', index=False, float_format='%.6f')
    supplied.to_csv(out / 'pt_accessibility_supplied_comparison.csv', index=False, float_format='%.6f')
    assigned.to_csv(out / 'pt_accessibility_common_classes.csv', index=False, float_format='%.6f')
    transitions(survey).to_csv(out / 'survey_class_transitions.csv', index=False, float_format='%.6f')
    transitions(population).to_csv(out / 'synthetic_class_transitions.csv', index=False, float_format='%.6f')
    cells.to_csv(out / 'pt_accessibility_common_grid.csv', index=False, float_format='%.6f')
    (out / 'pt_accessibility_common_method.json').write_text(json.dumps(notes, indent=2, allow_nan=False) + '\n', encoding='utf8')
    common.write_method(out, {'pt_accessibility_common_method': notes})
    plot_comparison(supplied, assigned, len(survey), len(population), out / 'pt_accessibility_common_comparison.png')
    plot_common_maps(cells, grid_geometry, region.context, common.GRID_M,
        common.MIN_CONTRIBUTORS, common.MIN_CONTRIBUTORS, out / 'pt_accessibility_common_maps.png')
    print('Common-reference residential-location aggregates saved to:', out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-dir', type=Path, help='Local cache for the pinned public ARE layer')
    run(parser.parse_args().reference_dir)
