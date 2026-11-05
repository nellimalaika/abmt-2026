#!/usr/bin/env python3
'Internal reconciled reader/utility derived from the reviewed analysis scripts. See README.md and abmt_common.METHOD for the active definitions.'

import argparse
from collections import Counter, defaultdict
import csv
import gzip
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import _abmt_age as baseline
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from shapely.geometry import Point


WINDOWS = ["00_06", "06_09", "09_12", "12_15", "15_18", "18_24", "24plus", "unknown_time"]
WINDOW_LABELS = ["00-06", "06-09", "09-12", "12-15", "15-18", "18-24", "24+"]
PT_MODES = ["bus", "rail", "tram", "other_pt", "unknown"]
EVENT_TYPES = {"TransitDriverStarts", "VehicleArrivesAtFacility", "VehicleDepartsAtFacility",
               "PersonEntersVehicle", "PersonLeavesVehicle", "vehicle enters traffic", "vehicle leaves traffic",
               "entered link", "left link", "stuckAndAbort", "departure", "arrival", "actstart", "actend"}
SOURCE_ROOT = "https://github.com/eqasim-org/eqasim-java/blob/cmdp/"
SOURCES = [
    {"url": SOURCE_ROOT + "core/src/main/java/org/eqasim/core/components/network_calibration/Processors/FlowProcessor.java",
     "blob_sha": "8e1af890b4da1d504572ee922e70badfc781a628", "use": "Target convention: full-scale vehicles/hour/lane"},
    {"url": SOURCE_ROOT + "core/src/main/java/org/eqasim/core/components/flow/FlowUtils.java",
     "blob_sha": "0e8afbe5092554a3aa6441fada180dd891b09af2", "use": "Bus sample-equivalent weighting; daily count versus PCU distinction"},
    {"url": SOURCE_ROOT + "core/src/main/java/org/eqasim/core/components/flow/FlowBinManager.java",
     "blob_sha": "037ca6ca70ace02eaa2bdd200c38ac6ba276d6cd", "use": "Counting interval includes both endpoints"},
    {"url": "https://github.com/matsim-org/matsim-libs/blob/2025.0/matsim/src/main/java/org/matsim/core/api/experimental/events/VehicleArrivesAtFacilityEvent.java",
     "blob_sha": "62f4a3d764a5c6445a7338641ee2c256da73efd6", "use": "Vehicle-stop event attributes vehicle/facility/delay"},
]


class ValidationError(ValueError):
    """A safe, hand-written schema/convention diagnostic with no raw record."""


def name(element):
    return element.tag.rsplit("}", 1)[-1]


def number(value):
    return baseline.number(value)


def seconds(value):
    value = str("" if value is None else value).strip()
    if ":" not in value:
        return number(value)
    parts = value.split(":")
    if len(parts) not in {2, 3}:
        return None
    values = [number(item) for item in parts]
    if any(item is None for item in values):
        return None
    hours, minutes = values[:2]
    sec = values[2] if len(values) == 3 else 0.
    if hours < 0 or not hours.is_integer() or not 0 <= minutes < 60 or not 0 <= sec < 60:
        return None
    return 3600 * hours + 60 * minutes + sec


def window(time):
    if time is None or time < 0 or not math.isfinite(time):
        return "unknown_time"
    return WINDOWS[int(np.searchsorted(np.asarray([6, 9, 12, 15, 18, 24]) * 3600, time, side="right"))]


def pt_mode(value):
    text = str(value or "").strip().casefold()
    if text in {"bus", "coach"}:
        return "bus"
    if text in {"rail", "train", "subway", "metro"}:
        return "rail"
    if text == "tram":
        return "tram"
    if text in {"pt", "ferry", "ship", "gondola", "cable_car", "funicular"}:
        return "other_pt"
    return "unknown"


def open_text(path):
    return gzip.open(path, "rt", encoding="utf-8-sig", newline="") if path.name.endswith(".gz") else path.open("rt", encoding="utf-8-sig", newline="")


def xml_root(path):
    with open_text(path) as stream:
        return ET.parse(stream).getroot()


def table_rows(path, required):
    """Only recognised header schemas are returned; arbitrary values not echoed."""
    with open_text(path) as stream:
        first = stream.readline(131073)
        if len(first) > 131072:
            raise ValidationError("A count table has an unexpectedly long header.")
        delimiter = max([";", ",", "\t"], key=first.count)
        stream.seek(0)
        reader = csv.DictReader(stream, delimiter=delimiter)
        headers = reader.fieldnames or []
        normal = [h.strip().casefold().replace("_", "") for h in headers]
        if len(set(normal)) != len(normal):
            raise ValidationError("A count table has duplicate header fields.")
        lookup = dict(zip(normal, headers))
        selected = {}
        for key, aliases in required.items():
            matches = [lookup[a] for a in aliases if a in lookup]
            if len(matches) != 1:
                raise ValidationError("A count table differs from the documented header schema; no rows exported.")
            selected[key] = matches[0]
        rows = []
        for row in reader:
            if None in row or any(row.get(h) is None for h in headers):
                raise ValidationError("A count table contains malformed rows; inspect locally.")
            rows.append({key: row[column] for key, column in selected.items()})
    return rows, {"columns": headers, "delimiter": delimiter, "rows": len(rows)}


def read_targets(path):
    rows, schema = table_rows(path, {"link": ["linkid", "link"], "count": ["count", "counts"]})
    targets, invalid, seen = {}, 0, set()
    for row in rows:
        link, value = row["link"].strip(), number(row["count"])
        if not link or link in seen:
            raise ValidationError("Target link IDs are missing or duplicated; time intervals cannot be guessed.")
        seen.add(link)
        if value is None or value < 0:
            invalid += 1
        else:
            targets[link] = value
    return targets, {**schema, "invalid_count_rows": invalid}


def read_daily(path):
    if not path.exists():
        return {}, {"status": "not_supplied"}
    rows, schema = table_rows(path, {"link": ["linkid", "link"], "count": ["dailycount"]})
    values = {}
    for row in rows:
        link, value = row["link"].strip(), number(row["count"])
        if not link or link in values or value is None or value < 0:
            raise ValidationError("The supplied daily-count file has invalid or duplicate entries.")
        values[link] = value
    return values, schema


def read_config(path, sample_size, start, end):
    root = xml_root(path)
    selected, disagreements = {}, []
    allowed = {"eqasim": {"sampleSize"}, "eqasim:flow": {"activate", "startTime", "endTime", "binSize"},
               "qsim": {"flowCapacityFactor", "storageCapacityFactor", "endTime", "startTime"},
               "controller": {"mobsim", "lastIteration"}, "controler": {"mobsim", "lastIteration"},
               "transit": {"useTransit"}, "eqasim:networkCalibration": {"activate", "calibrate", "countsFile", "objective"}}
    for module in root.iter():
        if name(module) != "module" or module.get("name") not in allowed:
            continue
        module_name = module.get("name")
        params = {}
        for item in module:
            if name(item) != "param" or item.get("name") not in allowed[module_name]:
                continue
            key, raw = item.get("name"), item.get("value", "")
            if key == "countsFile":
                params[key] = Path(raw).name
            elif key in {"activate", "useTransit"}:
                params[key] = "true" if baseline.flag(raw) else "false"
            elif key == "mobsim":
                params[key] = raw if raw in {"qsim", "dsim", "JDEQSim"} else "OTHER_ENGINE"
            elif key in {"calibrate", "objective"}:
                # Do not export arbitrary configuration strings.
                params[key] = [x for x in ["penalty", "freespeed", "agent", "subpopulations"] if x in raw.split(",")]
            else:
                params[key] = seconds(raw)
        selected[module_name] = params
    checks = [(selected.get("eqasim", {}).get("sampleSize"), sample_size, "sample_size_differs_from_output_config"),
              (selected.get("eqasim:flow", {}).get("startTime"), start, "count_start_differs_from_output_config"),
              (selected.get("eqasim:flow", {}).get("endTime"), end, "count_end_differs_from_output_config")]
    for configured, supplied, reason in checks:
        if configured is not None and not np.isclose(configured, supplied, rtol=0, atol=1e-8):
            disagreements.append(reason)
    if selected.get("eqasim:flow", {}).get("activate") == "false":
        disagreements.append("supplied_daily_flow_output_may_not_be_active")
    return {"selected_parameters": selected, "count_convention_disagreements": disagreements}


def read_network(path, target_links, boundary):
    root = xml_root(path)
    nodes = {}
    for item in root.iter():
        if name(item) == "node":
            nodes[item.get("id")] = (number(item.get("x")), number(item.get("y")))
    links = {}
    for item in root.iter():
        if name(item) != "link" or item.get("id") not in target_links:
            continue
        source, destination = nodes.get(item.get("from")), nodes.get(item.get("to"))
        lanes = number(item.get("permlanes"))
        if source is None or destination is None or any(v is None for v in source + destination):
            status = "invalid_link_geometry"
        elif not all(2_000_000 <= p[0] <= 3_000_000 and 1_000_000 <= p[1] <= 1_400_000 for p in [source, destination]):
            status = "invalid_or_unverified_lv95_coordinates"
        elif lanes is None or lanes <= 0:
            status = "invalid_lane_count"
        elif not boundary.covers(Point((source[0]+destination[0])/2, (source[1]+destination[1])/2)):
            status = "link_midpoint_outside_boundary"
        elif not {"car", "bus", "truck"} & set(item.get("modes", "car").split(",")):
            status = "not_a_road_link"
        else:
            status = "eligible"
        links[item.get("id")] = {"status": status, "network_lanes": lanes,
                                  "comparison_lanes": max(1., lanes) if lanes is not None and lanes > 0 else None}
    return links


def read_schedule(path, boundary):
    root = xml_root(path)
    stops, routes, vehicle_modes = {}, {}, defaultdict(set)
    departures = {}
    for item in root.iter():
        if name(item) == "stopFacility":
            x, y = number(item.get("x")), number(item.get("y"))
            valid = x is not None and y is not None and 2_000_000 <= x <= 3_000_000 and 1_000_000 <= y <= 1_400_000
            stops[item.get("id")] = bool(valid and boundary.covers(Point(x, y)))
    for line in root:
        if name(line) != "transitLine":
            continue
        for route in line:
            if name(route) != "transitRoute":
                continue
            transport = next((item.text for item in route if name(item) == "transportMode"), None)
            mode = pt_mode(transport)
            key = (line.get("id"), route.get("id"))
            inside = any(stops.get(item.get("refId"), False) for profile in route if name(profile) == "routeProfile"
                         for item in profile if name(item) == "stop")
            routes[key] = {"mode": mode, "has_inside_stop": inside}
            for container in route:
                if name(container) != "departures":
                    continue
                for dep in container:
                    if name(dep) != "departure":
                        continue
                    dep_key = key + (dep.get("id"),)
                    if dep_key in departures:
                        raise ValidationError("The transit schedule contains duplicate departure keys.")
                    departures[dep_key] = {"mode": mode, "inside": inside, "time": seconds(dep.get("departureTime"))}
                    if dep.get("vehicleRefId"):
                        vehicle_modes[dep.get("vehicleRefId")].add(mode)
    return stops, routes, vehicle_modes, departures


def event_analysis(path, stops, routes, vehicle_modes, departures, target_links, sample_size, count_start, count_end):
    vehicle_state, network_modes, drivers, active_stop = {}, {}, set(), {}
    vehicle_last_time = {}
    audit, event_types, attrs = Counter(), Counter(), defaultdict(set)
    road = {link: Counter() for link in target_links}
    delays = defaultdict(list)
    counts = Counter()
    started_departures = set()
    maximum_time, previous_time = 0., -math.inf

    def add_count(mode, time, metric):
        period = window(time)
        for key in [(mode, period, metric), (mode, "all", metric), ("all", period, metric), ("all", "all", metric)]:
            counts[key] += 1

    def add_delay(mode, time, kind, value):
        for key in [(mode, window(time), kind), (mode, "all", kind), ("all", window(time), kind), ("all", "all", kind)]:
            delays[key].append(value)

    def vehicle_mode(vehicle):
        if vehicle in vehicle_state:
            return vehicle_state[vehicle]["mode"]
        candidates = vehicle_modes.get(vehicle, set())
        return next(iter(candidates)) if len(candidates) == 1 else "unknown"

    with open_text(path) as stream:
        root = None
        for action, element in ET.iterparse(stream, events=("start", "end")):
            if root is None:
                root = element
            if action != "end" or name(element) != "event":
                continue
            audit["events_scanned"] += 1
            raw_type = element.get("type", "")
            kind = raw_type if raw_type in EVENT_TYPES else "OTHER_EVENT_TYPE"
            event_types[kind] += 1
            if kind != "OTHER_EVENT_TYPE":
                attrs[kind].update(k for k in element.attrib if k in {"time", "type", "person", "vehicle", "vehicleId", "facility", "delay", "link", "networkMode", "driverId", "transitLineId", "transitRouteId", "departureId", "legMode"})
            time = number(element.get("time"))
            if time is None or time < 0:
                audit["events_with_invalid_time"] += 1
                root.clear()
                continue
            if time < previous_time:
                audit["events_out_of_time_order"] += 1
            previous_time, maximum_time = time, max(maximum_time, time)
            vehicle = element.get("vehicle") or element.get("vehicleId")
            if vehicle and kind in {"TransitDriverStarts", "vehicle enters traffic", "entered link",
                                    "VehicleArrivesAtFacility", "VehicleDepartsAtFacility", "PersonEntersVehicle", "PersonLeavesVehicle"}:
                if time < vehicle_last_time.get(vehicle, -math.inf) - 1e-8:
                    audit["vehicle_specific_events_out_of_time_order"] += 1
                vehicle_last_time[vehicle] = time
            if kind == "TransitDriverStarts":
                key = (element.get("transitLineId"), element.get("transitRouteId"))
                route = routes.get(key, {"mode": "unknown", "has_inside_stop": False})
                vehicle_state[vehicle] = route
                if element.get("driverId"):
                    drivers.add(element.get("driverId"))
                dep_key = key + (element.get("departureId"),)
                if dep_key in departures:
                    if dep_key in started_departures:
                        audit["duplicate_scheduled_departure_starts"] += 1
                    started_departures.add(dep_key)
                else:
                    audit["transit_starts_without_schedule_match"] += 1
                if route["has_inside_stop"]:
                    add_count(route["mode"], time, "driver_starts_on_routes_serving_area")
            elif kind == "vehicle enters traffic":
                raw_mode = element.get("networkMode", "").casefold()
                network_modes[vehicle] = raw_mode if raw_mode in {"car", "truck", "bike", "bus", "pt", "car_passenger"} else "unknown"
            elif kind == "entered link":
                link = element.get("link")
                if link in road and count_start <= time <= count_end:
                    network_mode = network_modes.get(vehicle, "unknown")
                    service_mode = vehicle_mode(vehicle)
                    known_transit = vehicle in vehicle_state or vehicle in vehicle_modes
                    mode = ("bus" if service_mode == "bus" or network_mode == "bus" else
                            "pt" if known_transit else network_mode)
                    road[link][mode] += 1
            elif kind in {"VehicleArrivesAtFacility", "VehicleDepartsAtFacility"}:
                facility = element.get("facility")
                mode = vehicle_mode(vehicle)
                if kind == "VehicleArrivesAtFacility":
                    active_stop[vehicle] = (facility, mode)
                else:
                    active_stop.pop(vehicle, None)
                if stops.get(facility, False):
                    which = "arrival" if kind == "VehicleArrivesAtFacility" else "departure"
                    add_count(mode, time, which + "_calls_n")
                    delay = number(element.get("delay"))
                    if delay is not None:
                        add_delay(mode, time, which, delay)
                    else:
                        add_count(mode, time, which + "_missing_delay_n")
            elif kind in {"PersonEntersVehicle", "PersonLeavesVehicle"}:
                stop = active_stop.get(vehicle)
                known_transit = vehicle in vehicle_state or vehicle in vehicle_modes
                if element.get("person") in drivers:
                    audit["driver_boarding_alighting_events_excluded"] += 1
                elif stop and stops.get(stop[0], False):
                    add_count(stop[1], time, "boardings_n" if kind == "PersonEntersVehicle" else "alightings_n")
                elif known_transit and stop is None:
                    audit["pt_boarding_alighting_events_without_active_stop"] += 1
                elif known_transit:
                    audit["pt_boarding_alighting_events_outside_or_unmapped_stop"] += 1
            elif kind == "stuckAndAbort":
                audit["stuck_events_entire_output"] += 1
                if element.get("legMode", "").casefold() == "pt":
                    audit["pt_stuck_events_entire_output"] += 1
            root.clear()
            if audit["events_scanned"] % 500000 == 0:
                print(f"Processed {audit['events_scanned']:,} events...", flush=True)
    scheduled = [value for value in departures.values() if value["inside"] and value["time"] is not None
                 and 0 <= value["time"] <= maximum_time]
    matched = [departures[key] for key in started_departures if departures[key]["inside"]
               and departures[key]["time"] is not None and 0 <= departures[key]["time"] <= maximum_time]
    audit["scheduled_departures_serving_area_starting_within_event_horizon"] = len(scheduled)
    audit["matched_departure_starts_in_that_schedule_set"] = len(matched)
    audit["maximum_event_time_seconds"] = maximum_time
    return counts, delays, road, {"audit": dict(audit), "event_types": dict(event_types),
                                 "known_event_attribute_names": {k: sorted(v) for k, v in attrs.items()}}


def delay_metrics(values, prefix):
    array = np.asarray(values, dtype=float)
    n = len(array)
    result = {prefix + "_known_n": n}
    for key, value in {
        "mean_s": float(array.mean()) if n else None,
        "median_s": float(np.median(array)) if n else None,
        "p90_s": float(np.percentile(array, 90)) if n else None,
        "late_gt_60s_pct": float(100 * np.mean(array > 60)) if n else None,
        "late_gt_300s_pct": float(100 * np.mean(array > 300)) if n else None,
        "early_lt_minus60s_pct": float(100 * np.mean(array < -60)) if n else None,
        "zero_within_1s_pct": float(100 * np.mean(np.abs(array) <= 1)) if n else None,
    }.items():
        result[prefix + "_" + key] = value
    return result


def pt_summary(counts, delays):
    rows = []
    for mode in ["all"] + PT_MODES:
        for period in ["all"] + WINDOWS:
            metrics = {metric: counts[(mode, period, metric)] for metric in
                       ["arrival_calls_n", "departure_calls_n", "boardings_n", "alightings_n",
                        "arrival_missing_delay_n", "departure_missing_delay_n", "driver_starts_on_routes_serving_area"]}
            if not any(metrics.values()):
                continue
            row = {"service_mode": mode, "time_window": period, **metrics}
            row.update(delay_metrics(delays[(mode, period, "arrival")], "arrival_delay"))
            row.update(delay_metrics(delays[(mode, period, "departure")], "departure_delay"))
            rows.append(row)
    if rows:
        return pd.DataFrame(rows)
    columns = ["service_mode", "time_window", "arrival_calls_n", "departure_calls_n", "boardings_n", "alightings_n",
               "arrival_missing_delay_n", "departure_missing_delay_n", "driver_starts_on_routes_serving_area"]
    columns += list(delay_metrics([], "arrival_delay")) + list(delay_metrics([], "departure_delay"))
    return pd.DataFrame(columns=columns)


def passenger_file_check(path, stops, counts):
    if not path.exists():
        return {"status": "not_supplied"}
    try:
        rows, schema = table_rows(path, {"stop": ["stopid"], "boardings": ["boardings"], "alightings": ["alightings"]})
    except ValueError:
        return {"status": "schema_differs_no_values_exported"}
    boarding, alighting, invalid = 0, 0, 0
    for row in rows:
        if not stops.get(row["stop"], False):
            continue
        b, a = number(row["boardings"]), number(row["alightings"])
        if b is None or a is None or b < 0 or a < 0:
            invalid += 1
        else:
            boarding += b
            alighting += a
    return {"status": "compared_simulation_outputs_only", "schema": schema, "invalid_inside_rows": invalid,
            "supplied_boardings_inside": boarding, "supplied_alightings_inside": alighting,
            "event_boardings_inside_driver_excluded": counts[("all", "all", "boardings_n")],
            "event_alightings_inside_driver_excluded": counts[("all", "all", "alightings_n")],
            "interpretation": "Both sources are simulation outputs, not observed passenger counts. Driver handling and event timing can differ."}


def geh(model, observed):
    if model < 0 or observed < 0 or not math.isfinite(model + observed):
        return None
    return math.sqrt(2 * (model-observed)**2 / (model+observed)) if model + observed else 0.


def road_summary(targets, links, road, daily, sample_size, start, end, target_unit, config_disagreements):
    hours = (end-start) / 3600
    rows = []
    for link, observed in targets.items():
        metadata = links.get(link, {"status": "missing_from_output_network", "network_lanes": None, "comparison_lanes": None})
        row = {"network_link_id": link, **metadata, "target_value_supplied": observed, "target_unit": target_unit}
        counts = road.get(link, Counter())
        for mode in ["car", "truck", "bus", "bike", "car_passenger", "pt", "unknown"]:
            row["link_entries_" + mode] = counts[mode]
        ambiguous = counts["pt"] + counts["unknown"]
        row["ambiguous_road_vehicle_entries_n"] = ambiguous
        sample_equivalent = counts["car"] + counts["truck"] + sample_size * counts["bus"]
        row["event_sample_equivalent_count"] = sample_equivalent
        row["supplied_daily_count"] = daily.get(link)
        row["daily_file_minus_event_sample_count"] = daily.get(link) - sample_equivalent if link in daily else None
        row["daily_file_matches_sample_count"] = abs(daily[link]-sample_equivalent) <= .011 if link in daily else None
        status = metadata["status"]
        if status == "eligible" and config_disagreements:
            status = "count_convention_differs_from_output_config"
        if status == "eligible" and ambiguous:
            status = "unclassified_road_vehicle_entries"
        row["status"] = status
        model_hourly = sample_equivalent / sample_size / hours
        lanes = metadata["comparison_lanes"]
        observed_hourly = (observed * lanes if target_unit == "hourly_per_lane" and lanes is not None else
                           observed if target_unit == "hourly_per_link" else observed / hours if target_unit == "daily_per_link" else None)
        row["model_average_hourly_link_vehicles"] = model_hourly if status == "eligible" else None
        row["observed_average_hourly_link_vehicles"] = observed_hourly if status == "eligible" else None
        row["geh_average_hourly_link"] = geh(model_hourly, observed_hourly) if status == "eligible" and observed_hourly is not None else None
        row["difference_model_minus_observed_hourly"] = model_hourly-observed_hourly if status == "eligible" and observed_hourly is not None else None
        rows.append(row)
    table = pd.DataFrame(rows)
    usable = table.loc[table.status.eq("eligible") & table.geh_average_hourly_link.notna()]
    n = len(usable)
    summary = {"supplied_valid_targets_n": len(targets), "matched_eligible_links_n": n,
               "excluded_by_status": {str(k): int(v) for k, v in table.loc[~table.status.eq("eligible")].status.value_counts().items()},
               "sample_size": sample_size, "count_start_s": start, "count_end_s": end, "count_hours": hours,
               "target_unit_assumption": target_unit,
               "geh_basis": "Full-scale average-hourly LINK volumes; target per-lane values multiplied by comparison lanes before GEH.",
               "median_geh": float(usable.geh_average_hourly_link.median()) if n else None,
               "pct_geh_below_5": float(100 * (usable.geh_average_hourly_link < 5).mean()) if n else None,
               "pct_geh_below_10": float(100 * (usable.geh_average_hourly_link < 10).mean()) if n else None,
               "model_sum_hourly_across_matched_links": float(usable.model_average_hourly_link_vehicles.sum()) if n else None,
               "observed_sum_hourly_across_matched_links": float(usable.observed_average_hourly_link_vehicles.sum()) if n else None,
               "independent_holdout": "Not established: supplied target_counts.csv may have been used for calibration.",
               "observed_time_provenance": "Original measurement year, weekday selection and exact temporal representativeness are not established from the CSV alone.",
               "threshold_caution": "Average-hour rates do not validate individual hourly profiles. GEH thresholds are descriptive benchmarks here, not a formal pass/fail test."}
    if n:
        summary["matched_links_reconciling_supplied_daily_file_n"] = int(usable.daily_file_matches_sample_count.fillna(False).sum())
    return table, summary


def plot_pt(table, path):
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.2))
    if table.empty:
        for ax in axes:
            ax.axis("off")
        axes[0].text(.5, .5, "No suitable PT vehicle-stop events.\nOperational reliability not assessed.", ha="center", transform=axes[0].transAxes)
    else:
        data = table[(table.time_window == "all") & (table.service_mode != "all")]
        x = np.arange(len(data))
        axes[0].bar(x, data.arrival_delay_median_s / 60, color="#237A8A", label="Median")
        axes[0].scatter(x, data.arrival_delay_p90_s / 60, color="#E38D35", marker="D", label="90th percentile", zorder=3)
        axes[0].set_xticks(x, data.service_mode)
        axes[0].axhline(0, color="black", linewidth=.7)
        axes[0].set_ylabel("Arrival delay relative to timetable (minutes)")
        axes[0].set_title("Delay by service mode")
        axes[0].legend(frameon=False)
        temporal = table[(table.service_mode == "all") & table.time_window.isin(WINDOWS[:-1])].set_index("time_window").reindex(WINDOWS[:-1])
        axes[1].bar(np.arange(7), temporal.arrival_delay_late_gt_300s_pct, color="#237A8A")
        axes[1].set_xticks(np.arange(7), WINDOW_LABELS)
        axes[1].set_ylabel("Stop arrivals more than 5 minutes late (%)")
        axes[1].set_title("Late-arrival fraction by clock window")
        for ax in axes:
            ax.grid(axis="y", alpha=.2)
            ax.set_axisbelow(True)
    fig.suptitle("Neuchatel: simulated PT timetable adherence", fontsize=16)
    fig.text(.025, .035, "Stop calls at timetable stops inside the study boundary; all service users. Calls, not passengers, receive equal weight.\n"
             "Internal simulated performance only: one average day, without observed punctuality validation. Zero delays can reflect timetable assumptions.\n"
             "Five-minute lateness is a descriptive threshold chosen for this analysis. Missing delay attributes remain missing, never zero.", fontsize=9, linespacing=1.5)
    fig.subplots_adjust(left=.075, right=.98, top=.82, bottom=.28, wspace=.30)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def plot_road(table, summary, path):
    data = table[table.status.eq("eligible") & table.geh_average_hourly_link.notna()]
    fig, axes = plt.subplots(1, 2, figsize=(11, 5.2))
    if data.empty:
        for ax in axes:
            ax.axis("off")
        axes[0].text(.5, .5, "No eligible GEH comparisons.\nSee status/diagnostics in JSON.", ha="center", transform=axes[0].transAxes)
    else:
        maximum = max(data.model_average_hourly_link_vehicles.max(), data.observed_average_hourly_link_vehicles.max()) * 1.05
        axes[0].scatter(data.observed_average_hourly_link_vehicles, data.model_average_hourly_link_vehicles, color="#237A8A")
        axes[0].plot([0, maximum], [0, maximum], color="#555555", linewidth=1)
        axes[0].set(xlabel="Target average-hourly link vehicles", ylabel="Event-derived model average-hourly link vehicles", title=f"Matched targets: n={len(data)}")
        values = np.sort(data.geh_average_hourly_link.to_numpy())
        axes[1].plot(values, np.arange(1, len(values)+1) * 100/len(values), color="#237A8A", marker=".")
        axes[1].axvline(5, color="#E38D35", linestyle="--", label="GEH = 5")
        axes[1].set(xlabel="GEH from average-hourly link volumes", ylabel="Cumulative share of matched links (%)", title="Distribution of count discrepancies")
        axes[1].set_ylim(0, 100)
        axes[1].legend(frameon=False)
        for ax in axes:
            ax.grid(alpha=.2)
    fig.suptitle("Neuchatel: road-count consistency with supplied targets", fontsize=15)
    fig.text(.025, .035, "All road traffic at eligible target links; no resident filter. Events counted within the stated window and scaled by the population sample.\n"
             "Target units are explicit; per-lane targets are converted to all-lane LINK volumes before GEH. CSV targets may be calibration inputs.\n"
             "This checks period-average rates; original observation dates/day types and individual hourly fit remain unverified.", fontsize=9, linespacing=1.5)
    fig.subplots_adjust(left=.08, right=.98, top=.82, bottom=.28, wspace=.30)
    fig.savefig(path, dpi=180)
    plt.close(fig)




