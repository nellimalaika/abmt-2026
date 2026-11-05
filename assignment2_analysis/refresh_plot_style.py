"""Redraw eight comparison figures from saved aggregate CSVs.

No survey/person records, events or accessibility polygons are loaded. Only the
public study boundary is needed for the map. Existing CSVs and method JSONs are
preserved. The four updated helper modules are also used by the regular Steps.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

import pandas as pd


def refresh(results=None, base=None):
    if base is not None:
        os.environ['ABMT_DATA_DIR'] = str(Path(base).expanduser().resolve())
    import abmt_common as common
    import _abmt_accessibility as accessibility
    import _abmt_car as car
    import _abmt_pt as pt
    import _abmt_spatial as spatial
    import _abmt_subscriptions as subscriptions

    root = Path(results).expanduser().resolve() if results else common.OUT
    paths = [
        'pt_subscriptions/pt_subscriptions_comparison.csv',
        'pt_subscriptions/pt_subscriptions_sensitivity.csv',
        'pt_accessibility_common/pt_accessibility_supplied_comparison.csv',
        'pt_accessibility_common/pt_accessibility_common_classes.csv',
        'pt_accessibility_common/pt_accessibility_common_grid.csv',
        'car_access/household_ownership_x_personal_access_any_access.csv',
        'car_access/household_ownership_x_personal_access_always_only.csv',
        'pt_geh/pt_performance_summary.csv',
        'pt_geh/road_count_comparison.csv',
    ]
    missing = [name for name in paths if not (root/name).is_file()]
    if missing:
        raise FileNotFoundError('Saved aggregate results are missing: ' + ', '.join(missing) +
                                '. Run Steps2,10,14,15 once before redrawing.')
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    hashes = {name: digest(root/name) for name in paths}
    tables = {name: pd.read_csv(root/name) for name in paths}

    # Resolve and validate the map geometry before overwriting any figure.
    region = common.boundary()
    grid, geometry = spatial.make_grid(region.context, common.GRID_M)
    cells = tables[paths[4]]
    if set(zip(cells.ix, cells.iy)) != set(geometry):
        raise ValueError('The saved grid and current study boundary differ; use the original boundary.')

    for name, definition in zip(paths[:2], ['generator', 'three_items']):
        table = tables[name]
        subscriptions.plot_comparison(table, definition, int(table.survey_n.sum()),
                                       int(table.synthetic_n.sum()), (root/name).with_suffix('.png'))

    supplied, assigned = tables[paths[2]], tables[paths[3]]
    folder = root/'pt_accessibility_common'
    accessibility.plot_comparison(supplied, assigned, int(assigned.survey_n.sum()),
        int(assigned.synthetic_n.sum()), folder/'pt_accessibility_common_comparison.png')
    accessibility.plot_common_maps(cells, geometry, region.context, common.GRID_M,
        common.MIN_CONTRIBUTORS, common.MIN_CONTRIBUTORS, folder/'pt_accessibility_common_maps.png')

    for name, definition in zip(paths[5:7], ['any_access', 'always_only']):
        table = tables[name]
        car.plot_comparison(table, definition, int(table.survey_n.sum()), int(table.synthetic_n.sum()),
                            (root/name).with_suffix('.png'))
    pt.plot_pt(tables[paths[7]], root/'pt_geh/pt_performance.png')
    pt.plot_road(tables[paths[8]], {}, root/'pt_geh/road_count_comparison.png')

    if any(digest(root/name) != hashes[name] for name in paths):
        raise AssertionError('Redrawing changed an aggregate input table.')
    # Retain the longer figure explanations outside the compact figures.
    notes = {
        'purpose': 'Display-only update from existing aggregate results; no analysis rerun.',
        'palette': {'survey': '#1f77b4', 'model': '#ff7f0e',
                    'maps': 'Original YlOrRd absolute scales and RdBu_r for model minus survey.'},
        'source_csv_sha256': hashes,
        'subscription_notes': [
            'All eligible residents, including nontravellers and unknowns, remain in the denominator.',
            'Groups represent model codes, not identical fare rights; none concerns represented products only.',
            'Generator-compatible groups and three-item sensitivity remain separate; exact scenario generator version is unverified.'],
        'common_accessibility_notes': [
            'The same pinned ARE2015 polygons, EPSG2056 coordinates and boundary-inclusive best-grade rule apply to both populations.',
            'No class and unknown supplied attributes remain distinct. Reclassification preserves cohorts and weights.',
            'D/no-class shares use classified residents within each cell. Differences require at least30 distinct residents per source.',
            'Support thresholds are display heuristics, not significance tests.',
            'This compares residential locations, without establishing current service quality, simulated accessibility or punctuality.'],
        'car_access_notes': [
            'Personal access concerns driving, not riding as a passenger. Unknowns remain in totals.',
            'Main survey access is always or after agreement; always-only sensitivity is separate.',
            'Synthetic always/never categories are unchanged; exact scenario generation version is unverified.'],
        'pt_notes': [
            'Stop calls inside the study boundary receive equal weight; these are calls, not passengers.',
            'Internal simulated performance on one average day is not validation against observed punctuality.',
            'Zero delays can reflect timetable assumptions; missing delay attributes remain missing.',
            'Five-minute lateness is a descriptive threshold.'],
        'road_notes': [
            'All traffic at eligible target links; no resident filter. Event counts are scaled by the configured population sample.',
            'Per-lane targets are converted to whole-link volumes; average-hourly whole-link GEH remains primary.',
            'Targets may be calibration inputs. Original observation dates/day types and hourly-profile fit remain unverified.'],
        'existing_methods': 'The existing analysis method JSON files are preserved unchanged.',
    }
    (root/'plot_style_method.json').write_text(json.dumps(notes, indent=2, allow_nan=False)+'\n', encoding='utf8')
    print('Redrew 8 figures. Aggregate CSVs and existing method JSONs are unchanged.')
    print('Results:', root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, help='Existing private data directory, for resolving the public boundary')
    parser.add_argument('--results', type=Path, help='Existing aggregate results directory; default: results beside the code')
    args = parser.parse_args()
    refresh(args.results, args.base)


if __name__ == '__main__':
    main()
