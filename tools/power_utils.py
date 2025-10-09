"""Utility helpers for power trace analysis on the GCS."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple


_TS_FIELDS = ("timestamp_ns", "ts_ns", "time_ns", "timestamp", "ts")
_POWER_FIELDS = ("power_w", "power", "power_watts", "watts")
_CURRENT_FIELDS = ("current_a", "current", "amps", "i_a")
_VOLTAGE_FIELDS = ("voltage_v", "voltage", "volts", "v_v")
_SIGN_FIELDS = ("sign", "sign_factor", "sign_multiplier", "direction")


@dataclass(frozen=True)
class PowerSample:
    """Single power reading expressed in nanoseconds and Watts."""

    ts_ns: int
    power_w: float


def _normalize(field: str) -> str:
    return field.strip().lower().replace("-", "_")


def _detect_header(row: Sequence[str]) -> bool:
    if not row:
        return False
    lowered = [_normalize(cell) for cell in row]
    if any(name in lowered for name in _TS_FIELDS + _POWER_FIELDS):
        return True
    # Heuristic: if any cell contains alphabetic characters, treat as header.
    for cell in row:
        if any(ch.isalpha() for ch in cell):
            return True
    return False


def _row_to_sample(row: Sequence[str], headers: Sequence[str]) -> Optional[PowerSample]:
    mapping = {headers[idx]: value for idx, value in enumerate(row)}
    ts_value: Optional[int] = None
    for field in _TS_FIELDS:
        raw = mapping.get(field)
        if raw is None or raw == "":
            continue
        try:
            ts_value = int(raw)
            break
        except ValueError:
            continue
    if ts_value is None:
        return None

    power_value: Optional[float] = None
    for field in _POWER_FIELDS:
        raw = mapping.get(field)
        if raw is None or raw == "":
            continue
        try:
            power_value = float(raw)
            break
        except ValueError:
            continue

    if power_value is None:
        current = None
        voltage = None
        for field in _CURRENT_FIELDS:
            raw = mapping.get(field)
            if raw is None or raw == "":
                continue
            try:
                current = float(raw)
                break
            except ValueError:
                continue
        for field in _VOLTAGE_FIELDS:
            raw = mapping.get(field)
            if raw is None or raw == "":
                continue
            try:
                voltage = float(raw)
                break
            except ValueError:
                continue
        if current is not None and voltage is not None:
            power_value = current * voltage

    if power_value is None:
        return None

    sign_multiplier = 1.0
    for field in _SIGN_FIELDS:
        raw = mapping.get(field)
        if raw is None or raw == "":
            continue
        try:
            sign_multiplier = float(raw)
            break
        except ValueError:
            continue

    return PowerSample(ts_ns=ts_value, power_w=power_value * sign_multiplier)


def load_power_trace(csv_path: str | Path) -> List[PowerSample]:
    """Load a power CSV and return chronologically sorted samples.

    The loader is tolerant to optional headers and derives ``power_w`` from
    voltage/current columns when an explicit power column is absent.
    """

    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(path)

    samples: List[PowerSample] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            first_row = next(reader)
        except StopIteration:
            return []

        has_header = _detect_header(first_row)
        headers: List[str]
        data_rows: List[Sequence[str]]
        if has_header:
            headers = [_normalize(cell) for cell in first_row]
            data_rows = list(reader)
        else:
            headers = [f"col_{idx}" for idx in range(len(first_row))]
            data_rows = [first_row] + list(reader)

        # Ensure canonical header names exist even for header-less files.
        if not any(name in headers for name in _TS_FIELDS):
            headers = list(headers)
            headers[0] = "timestamp_ns"
        if not any(name in headers for name in _POWER_FIELDS):
            if len(headers) > 3:
                headers[3] = "power_w"

        for row in data_rows:
            if not row:
                continue
            sample = _row_to_sample(row, headers)
            if sample is not None:
                samples.append(sample)

    samples.sort(key=lambda item: item.ts_ns)
    return samples


def slice_window(samples: Sequence[PowerSample], start_ns: int, end_ns: int) -> List[PowerSample]:
    """Return samples that overlap the window ``[start_ns, end_ns]``."""

    if end_ns <= start_ns:
        return []
    window: List[PowerSample] = []
    for sample in samples:
        if sample.ts_ns < start_ns:
            continue
        if sample.ts_ns > end_ns:
            break
        window.append(sample)
    return window


def integrate_energy_mj(samples: Sequence[PowerSample], start_ns: int, end_ns: int) -> Tuple[float, int]:
    """Integrate energy for ``[start_ns, end_ns]`` using trapezoidal rule."""

    if end_ns <= start_ns:
        return 0.0, 0
    if not samples:
        return 0.0, 0

    total_j = 0.0
    used_segments = 0
    prev: Optional[PowerSample] = None

    for sample in samples:
        if prev is None:
            prev = sample
            continue
        if sample.ts_ns <= prev.ts_ns:
            prev = sample
            continue

        segment_start = max(start_ns, prev.ts_ns)
        segment_end = min(end_ns, sample.ts_ns)
        if segment_end > segment_start:
            span = sample.ts_ns - prev.ts_ns
            ratio_start = (segment_start - prev.ts_ns) / span if span else 0.0
            ratio_end = (segment_end - prev.ts_ns) / span if span else 0.0
            p_start = prev.power_w + (sample.power_w - prev.power_w) * ratio_start
            p_end = prev.power_w + (sample.power_w - prev.power_w) * ratio_end
            dt = (segment_end - segment_start) / 1_000_000_000.0
            total_j += 0.5 * (p_start + p_end) * dt
            used_segments += 1
        if sample.ts_ns >= end_ns:
            break
        prev = sample

    return total_j * 1000.0, used_segments


def align_gcs_to_drone(ts_gcs_ns: int, offset_ns: int) -> int:
    """Convert a GCS timestamp into the drone clock domain."""

    return ts_gcs_ns + offset_ns


def calculate_transient_energy(power_csv_path: str, start_ns: int, end_ns: int) -> float:
    """Backward compatible helper used by legacy callers."""

    samples = load_power_trace(power_csv_path)
    energy_mj, _ = integrate_energy_mj(samples, start_ns, end_ns)
    return energy_mj
