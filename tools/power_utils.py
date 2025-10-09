"""Utility helpers for power trace analysis."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Iterator, Optional, Tuple


def _parse_power_row(row: Iterable[str]) -> Optional[Tuple[int, float]]:
    try:
        timestamp_ns = int(row[0])
    except (IndexError, ValueError):
        return None
    try:
        power_w = float(row[3])
    except (IndexError, ValueError):
        return None
    sign = 1.0
    try:
        sign = float(row[4])
    except (IndexError, ValueError):
        pass
    return timestamp_ns, power_w * sign


def calculate_transient_energy(power_csv_path: str, start_ns: int, end_ns: int) -> float:
    """Integrate power samples over ``[start_ns, end_ns]`` and return energy in mJ.

    The CSV is expected to follow the format produced by :mod:`core.power_monitor`
    (timestamp, current, voltage, power, sign_factor). Samples are assumed to be
    chronological; when gaps exist the computation performs linear
    interpolation between adjacent samples.

    Parameters
    ----------
    power_csv_path: str
        Path to the power monitor CSV file.
    start_ns: int
        Inclusive integration start timestamp (nanoseconds).
    end_ns: int
        Exclusive integration end timestamp (nanoseconds). Must be greater than
        ``start_ns``.

    Returns
    -------
    float
        Energy for the window in millijoules (mJ).
    """

    if end_ns <= start_ns:
        raise ValueError("end_ns must be greater than start_ns")

    path = Path(power_csv_path)
    if not path.exists():
        raise FileNotFoundError(power_csv_path)

    energy_j = 0.0
    prev_sample: Optional[Tuple[int, float]] = None

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header_skipped = False
        for row in reader:
            if not header_skipped:
                header_skipped = True
                # Basic validation: header contains "power"
                if row and row[0].lower() == "timestamp_ns":
                    continue
            parsed = _parse_power_row(row)
            if parsed is None:
                continue
            current_ts, current_power = parsed
            if prev_sample is None:
                prev_sample = (current_ts, current_power)
                continue

            prev_ts, prev_power = prev_sample
            if current_ts <= prev_ts:
                prev_sample = (current_ts, current_power)
                continue

            segment_start = max(start_ns, prev_ts)
            segment_end = min(end_ns, current_ts)
            if segment_end > segment_start:
                span = current_ts - prev_ts
                offset_start = segment_start - prev_ts
                offset_end = segment_end - prev_ts
                ratio_start = offset_start / span if span else 0.0
                ratio_end = offset_end / span if span else 0.0
                p_start = prev_power + (current_power - prev_power) * ratio_start
                p_end = prev_power + (current_power - prev_power) * ratio_end
                dt = (segment_end - segment_start) / 1_000_000_000.0
                energy_j += 0.5 * (p_start + p_end) * dt

            if current_ts >= end_ns:
                break
            prev_sample = (current_ts, current_power)

    return energy_j * 1000.0
