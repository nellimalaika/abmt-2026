"""Check whether the saved GEH values can be reproduced from local inputs."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

import ABMT_Neuchatel_Step10_PT_GEH as step10

RESULT_FILE = "geh_mean_hourly_from_24h_calibration_by_station.csv"


def audit(base: Path) -> str:
    try:
        sim = step10.get_sim(base)
        config_path, modules = step10.read_config_modules(sim)
        if config_path is None:
            return "GEH_METHOD_REVIEW_NO_CONFIG"

        eqasim = modules.get("eqasim", {})
        flow = modules.get("eqasim:flow", {})
        declared = modules.get("eqasim:networkCalibration", {}).get("countsFile", "").strip()
        try:
            sample = float(eqasim["sampleSize"])
            start = float(flow.get("startTime", "0"))
            end = float(flow.get("endTime", "86400"))
        except (KeyError, ValueError, TypeError):
            return "GEH_METHOD_REVIEW_CONFIG_VALUES"
        if not all(map(math.isfinite, (sample, start, end))) or abs(sample - 0.1) > 1e-8:
            return "GEH_METHOD_REVIEW_SAMPLE_SIZE"
        if abs(start) > 1e-8 or abs(end - 86400) > 1e-8:
            return "GEH_METHOD_REVIEW_TIME_WINDOW"

        reference_path, source = step10.find_reference_counts_csv(sim, config_path, modules)
        if reference_path is None:
            return "GEH_METHOD_REVIEW_REFERENCE_MISSING_OR_AMBIGUOUS"
        if not declared or source != "configured":
            return "GEH_METHOD_REVIEW_REFERENCE_NOT_CONFIGURED"

        flows = step10.daily_flow_from_csv(sim / "output_traffic_flow_daily_counts.csv")
        if flows is None:
            return "GEH_METHOD_REVIEW_SIMULATED_COUNTS_MISSING"
        reference = step10.reference_counts_from_csv(reference_path)
        pairs = reference.merge(flows, on="link_id", validate="one_to_one")
        pairs = pairs.loc[
            pairs.reference_hourly_per_lane.gt(0) & pairs.raw_sample_volume.ge(0) &
            np.isfinite(pairs.reference_hourly_per_lane) & np.isfinite(pairs.raw_sample_volume)
        ].copy()
        if pairs.empty:
            return "GEH_METHOD_REVIEW_NO_PAIRED_LINKS"

        lanes = step10.read_network_lanes(sim, set(pairs.link_id))
        if lanes is None:
            return "GEH_METHOD_REVIEW_NETWORK_LANES_MISSING"
        expected = pairs.merge(lanes, on="link_id", validate="one_to_one")
        expected = expected.loc[expected.lanes.gt(0) & np.isfinite(expected.lanes)].copy()
        if expected.empty:
            return "GEH_METHOD_REVIEW_NO_LINKS_WITH_LANES"
        expected["observed_expected"] = expected.reference_hourly_per_lane * expected.lanes
        expected["simulated_expected"] = expected.raw_sample_volume / (24 * sample)
        expected["geh_expected"] = step10.geh(expected.simulated_expected, expected.observed_expected)

        result_path = Path(__file__).resolve().parent / "results" / "pt_geh" / RESULT_FILE
        if not result_path.is_file():
            return "GEH_METHOD_REVIEW_STEP10_CALIBRATION_OUTPUT_MISSING"
        saved = pd.read_csv(result_path, dtype={"link_id": str})
        required = {"link_id", "observed", "simulated", "geh"}
        if not required.issubset(saved.columns):
            return "GEH_METHOD_REVIEW_SAVED_COLUMNS"
        saved = saved[["link_id", "observed", "simulated", "geh"]].copy()
        for field in ("observed", "simulated", "geh"):
            saved[field] = pd.to_numeric(saved[field], errors="coerce")
        values = saved[["observed", "simulated", "geh"]]
        if values.isna().any().any() or (values < 0).any().any() or saved.link_id.duplicated().any():
            return "GEH_METHOD_REVIEW_SAVED_VALUES"

        joined = expected.merge(saved, on="link_id", how="outer", indicator=True,
                                validate="one_to_one")
        if len(joined) != len(expected) or not joined._merge.eq("both").all():
            return "GEH_METHOD_REVIEW_LINK_SET_MISMATCH"
        for calculated, recorded in (("observed_expected", "observed"),
                                     ("simulated_expected", "simulated"),
                                     ("geh_expected", "geh")):
            if not np.allclose(joined[calculated], joined[recorded],
                               rtol=1e-8, atol=1e-5, equal_nan=False):
                return "GEH_METHOD_REVIEW_VALUES_DIFFER"

        summary_path = result_path.parent / "geh_summary.txt"
        if not summary_path.exists():
            return "GEH_METHOD_REVIEW_SUMMARY_MISSING"
        summary = summary_path.read_text(encoding="utf-8")
        if "mean_hourly_from_24h_calibration" not in summary or "calibration" not in summary.lower():
            return "GEH_METHOD_REVIEW_SUMMARY_SOURCE_MISMATCH"
        if len(expected) < 10:
            return "GEH_METHOD_CONSISTENT_BUT_FEW_MATCHED_LINKS"

        canonical = (reference_path.name.lower() == "target_flow.csv" and
                     Path(declared).name.lower() == "target_flow.csv")
        return ("GEH_METHOD_AUDIT_PASS_CALIBRATION_ONLY" if canonical else
                "GEH_METHOD_NUMERICALLY_CONSISTENT_REFERENCE_UNITS_ASSUMED")
    except Exception:
        return "GEH_METHOD_REVIEW_TECHNICAL_ERROR"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=step10.DEFAULT_BASE)
    base = parser.parse_args().base.expanduser().resolve()
    result = Path(__file__).resolve().parent / "results" / "pt_geh" / "geh_method_audit.txt"
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text(
        f"STATUS: {audit(base)}\n"
        "Uses calibration counts, not an independent validation sample.\n"
        "GEH compares mean hourly link volumes derived from daily totals.\n"
        "Reference units are assumed to be vehicles per hour per lane.\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
