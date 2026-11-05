"""Inside-stop PT activity/delays and inside-link whole-link GEH from existing outputs."""
import argparse
import os
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import _abmt_pt as core
import _abmt_counts_io as io
from abmt_common import data_root, boundary, output, write_method


def get_sim(base): return io.get_sim(base)
def geh(model, observed): return io.geh(model, observed)


def file_in(candidates):
    found = [Path(p) for p in candidates if Path(p).is_file()]
    if not found:
        raise FileNotFoundError('A required existing simulation file is missing; inspect the paths in README.md.')
    return found[0]


def passenger_activity(path, stops, folder):
    if not path.is_file(): return {'status': 'passenger_count_file_not_supplied'}
    frame = io.read_tabular(path)
    if not {'stop_id', 'line_id', 'time_bin', 'boardings', 'alightings'}.issubset(frame):
        return {'status': 'unexpected_passenger_schema_no_unfiltered_plot_created'}
    total = len(frame)
    frame = frame.loc[frame.stop_id.map(lambda s: stops.get(str(s), False))].copy()
    for key in ['boardings', 'alightings']:
        frame[key] = pd.to_numeric(frame[key], errors='coerce')
    good = np.isfinite(frame.boardings) & np.isfinite(frame.alightings) & frame.boardings.ge(0) & frame.alightings.ge(0)
    invalid = int((~good).sum()); frame = frame.loc[good].copy()
    byline = frame.groupby('line_id', as_index=False).agg(boardings=('boardings', 'sum'), alightings=('alightings', 'sum'))
    byline.to_csv(folder/'pt_boardings_by_line.csv', index=False)
    io.plot_bars(byline, 'line_id', 'boardings', 'Simulated boardings at inside stops by line', folder/'pt_boardings_by_line.png')
    def hour(v):
        x = core.seconds(v)
        return int(x//3600) if x is not None and x >= 0 else None
    frame['hour'] = frame.time_bin.map(hour)
    hourly = frame.dropna(subset=['hour']).groupby('hour', as_index=False).agg(boardings=('boardings', 'sum'), alightings=('alightings', 'sum'))
    hourly.to_csv(folder/'pt_boardings_by_hour.csv', index=False)
    if not hourly.empty:
        fig, ax = plt.subplots(figsize=(9, 5))
        ax.plot(hourly.hour, hourly.boardings, marker='o', label='Boardings')
        ax.plot(hourly.hour, hourly.alightings, marker='s', label='Alightings')
        ax.set(xlabel='Hour of simulated day', ylabel='Simulated passenger events', title='PT activity at inside stops')
        ax.legend(); fig.tight_layout(); fig.savefig(folder/'pt_boardings_by_hour.png', dpi=180); plt.close(fig)
    return {'status': 'inside_stop_aggregate_simulated_activity', 'file_rows': total, 'inside_valid_rows': len(frame),
            'inside_invalid_rows': invalid, 'time_unclassified_rows': int(frame.hour.isna().sum())}


def add_lane_sensitivity(table):
    table = table.copy()
    table['model_average_hourly_per_lane'] = table.model_average_hourly_link_vehicles/table.comparison_lanes
    table['observed_average_hourly_per_lane'] = table.observed_average_hourly_link_vehicles/table.comparison_lanes
    table['geh_average_hourly_per_lane'] = [core.geh(float(a), float(b)) if pd.notna(a) and pd.notna(b) else np.nan
        for a, b in zip(table.model_average_hourly_per_lane, table.observed_average_hourly_per_lane)]
    return table


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=None)
    parser.add_argument('--simulation-directory', type=Path)
    parser.add_argument('--observed-counts', type=Path)
    parser.add_argument('--sample-size', type=float)
    args = parser.parse_args()
    base = (args.base or data_root()).expanduser().resolve()
    os.environ['ABMT_DATA_DIR'] = str(base)
    sim = args.simulation_directory or get_sim(base)
    folder = output('pt_geh'); region = boundary()
    cfg, mods = io.read_config_modules(sim)
    if cfg is None: raise ValueError('No simulation config found; sample and time-window conventions must be verified.')
    eq = mods.get('eqasim', {}); flow = mods.get('eqasim:flow', {})
    sample = args.sample_size if args.sample_size is not None else core.number(eq.get('sampleSize'))
    start = core.seconds(flow.get('startTime', '0')); end = core.seconds(flow.get('endTime', '86400'))
    if sample is None or not 0 < sample <= 1 or start is None or end is None or not 0 <= start < end:
        raise ValueError('Invalid or missing sample size/count window in configuration.')
    target = args.observed_counts; strategy = 'explicit_cli' if target else None
    if target is None: target, strategy = io.find_reference_counts_csv(sim, cfg, mods)
    if target is None: raise ValueError('Reference counts are missing or ambiguous; use --observed-counts with the configured file.')
    targets, target_schema = core.read_targets(target)
    if not targets: raise ValueError('No valid reference counts.')
    network = file_in(io.network_candidates(sim))
    schedule = file_in([sim/'output_transitSchedule.xml.gz', sim/'output_transitSchedule.xml',
                       sim.parent/'neuchatel_transitSchedule.xml.gz', sim.parent/'neuchatel_transitSchedule.xml'])
    events = file_in([sim/'output_events.xml.gz', sim/'output_events.xml'])
    config = core.read_config(cfg, sample, start, end)
    links = core.read_network(network, targets, region)
    stops, routes, vehicle_modes, departures = core.read_schedule(schedule, region)
    daily, daily_schema = core.read_daily(sim/'output_traffic_flow_daily_counts.csv')
    eligible = {k for k, v in links.items() if v['status'] == 'eligible'}
    print('Streaming existing events for PT and road-count checks...', flush=True)
    counts, delays, road, event_audit = core.event_analysis(events, stops, routes, vehicle_modes, departures, eligible, sample, start, end)
    if event_audit['audit'].get('vehicle_specific_events_out_of_time_order', 0):
        raise ValueError('Vehicle-specific event order is inconsistent; state-based PT results withheld.')
    pt = core.pt_summary(counts, delays)
    table, summary = core.road_summary(targets, links, road, daily, sample, start, end, 'hourly_per_lane', config['count_convention_disagreements'])
    table = add_lane_sensitivity(table)
    pt.to_csv(folder/'pt_performance_summary.csv', index=False, float_format='%.8f')
    table.to_csv(folder/'road_count_comparison.csv', index=False, float_format='%.10f')
    pd.DataFrame([{k: v for k, v in summary.items() if not isinstance(v, dict)}]).to_csv(folder/'road_count_summary.csv', index=False)
    usable = table.loc[table.status.eq('eligible') & table.geh_average_hourly_link.notna()]
    primary = usable[['network_link_id', 'observed_average_hourly_link_vehicles', 'model_average_hourly_link_vehicles', 'geh_average_hourly_link']].rename(
        columns={'network_link_id': 'link_id', 'observed_average_hourly_link_vehicles': 'observed', 'model_average_hourly_link_vehicles': 'simulated', 'geh_average_hourly_link': 'geh'})
    primary.to_csv(folder/'geh_mean_hourly_from_24h_calibration_by_station.csv', index=False, float_format='%.10f')
    lane = usable[['network_link_id', 'observed_average_hourly_per_lane', 'model_average_hourly_per_lane', 'geh_average_hourly_per_lane']].rename(
        columns={'network_link_id': 'link_id', 'observed_average_hourly_per_lane': 'observed', 'model_average_hourly_per_lane': 'simulated', 'geh_average_hourly_per_lane': 'geh'})
    lane.to_csv(folder/'geh_hourly_per_lane_sensitivity.csv', index=False, float_format='%.10f')
    core.plot_pt(pt, folder/'pt_performance.png'); core.plot_road(table, summary, folder/'road_count_comparison.png')
    activity = passenger_activity(sim/'pt_passenger_counts.csv.gz', stops, folder)
    cross = core.passenger_file_check(sim/'pt_passenger_counts.csv.gz', stops, counts)
    supplementary = {'matched_links_n': len(lane), 'median_geh': float(lane.geh.median()) if len(lane) else None,
        'pct_geh_below_5': float(100*lane.geh.lt(5).mean()) if len(lane) else None}
    write_method(folder, {'sample_size': sample, 'count_start_s': start, 'count_end_s': end,
        'road_summary': summary, 'lane_sensitivity_summary': supplementary, 'config': config,
        'target_schema': target_schema, 'target_selection': strategy, 'daily_count_schema': daily_schema,
        'events': event_audit, 'passenger_activity': activity, 'passenger_crosscheck': cross,
        'timetable_scope': {'stops_total': len(stops), 'stops_inside': sum(stops.values())},
        'reference_unit_assumption': 'Targets interpreted as vehicles/hour/lane following cmdp FlowProcessor. Original dates/day types and exact run source version are not established.'})
    (folder/'geh_summary.txt').write_text(
        'Primary: full-scale mean-hourly whole-LINK GEH, inside link midpoints.\n'
        f'Configured interval: {start:g} to {end:g} seconds; sample fraction {sample:g}.\n'
        f'Eligible links: {len(primary)}; median GEH: {summary["median_geh"]}; GEH<5: {summary["pct_geh_below_5"]}%.\n'
        'Per-lane GEH is supplementary and uses a different scale.\n'
        'Targets may be calibration inputs; no independent holdout or hourly-profile validation established.\n', encoding='utf8')
    print('Saved PT and whole-link/per-lane GEH aggregate results.', flush=True)


if __name__ == '__main__': main()
