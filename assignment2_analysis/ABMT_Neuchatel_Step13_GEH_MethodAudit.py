"""Audit Step10 arithmetic and link sets; does not establish external validity."""
import numpy as np
import pandas as pd
from abmt_common import output
from ABMT_Neuchatel_Step10_PT_GEH import geh


def main():
    folder = output('pt_geh')
    t = pd.read_csv(folder/'road_count_comparison.csv', dtype={'network_link_id': str})
    t = t.loc[t.status.eq('eligible') & t.geh_average_hourly_link.notna()]
    saved = pd.read_csv(folder/'geh_mean_hourly_from_24h_calibration_by_station.csv', dtype={'link_id': str})
    if t.empty: raise ValueError('No eligible comparisons to audit.')
    if saved.link_id.duplicated().any() or set(saved.link_id) != set(t.network_link_id):
        raise AssertionError('Step10 exported link sets do not reconcile.')
    for unit, m, c, g in [
        ('whole_link', 'model_average_hourly_link_vehicles', 'observed_average_hourly_link_vehicles', 'geh_average_hourly_link'),
        ('per_lane', 'model_average_hourly_per_lane', 'observed_average_hourly_per_lane', 'geh_average_hourly_per_lane')]:
        if not np.allclose(geh(t[m], t[c]), t[g], atol=1e-7): raise AssertionError(f'{unit} GEH arithmetic differs.')
    if not np.allclose(t.geh_average_hourly_link, np.sqrt(t.comparison_lanes)*t.geh_average_hourly_per_lane, atol=1e-7):
        raise AssertionError('Whole-link/per-lane scale relation differs.')
    j = t.merge(saved, left_on='network_link_id', right_on='link_id', validate='one_to_one')
    for a, b in [('model_average_hourly_link_vehicles', 'simulated'), ('observed_average_hourly_link_vehicles', 'observed'), ('geh_average_hourly_link', 'geh')]:
        if not np.allclose(j[a], j[b], atol=1e-7): raise AssertionError('Primary exported values differ.')
    known = t.daily_file_matches_sample_count.notna()
    checked = int(known.sum()); matched = int(t.loc[known, 'daily_file_matches_sample_count'].astype(bool).sum())
    (folder/'geh_method_audit.txt').write_text(
        f'ARITHMETIC_PASS: {len(t)} eligible links; whole-link and per-lane exports reconcile.\n'
        f'Event/daily-file matches: {matched}/{checked}; inspect discrepancies before interpreting traffic fit.\n'
        'Checks arithmetic and output consistency, not measurement provenance or independent validity.\n', encoding='utf8')
    print('GEH arithmetic audit passed.')


if __name__ == '__main__': main()
