#!/usr/bin/env python3
"""Aggregate drone follower metrics on the GCS side.

This helper scans a ``logs/auto/gcs/run_<id>/summary.csv`` file, collects the
per-suite monitor artifacts that the scheduler already fetched from the drone
(companion computer), and emits a compact dataset suitable for downstream
analysis and scheduler research.

Metrics captured today:

* CPU usage, RSS, and thread count derived from ``psutil`` samples.
* Synthetic ``perf`` deltas (instructions, cycles, IPC, context switches).
* Thermal envelope and frequency wander from ``sys_telemetry``.
* Power trace extrema and variance extracted from the high-rate CSV traces.

The output lives in ``output/gcs/<run-id>/drone_metrics.csv`` by default accompanied
by a JSON dump for easier loading in notebooks.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUMMARY = REPO_ROOT / "logs/auto/gcs/summary.csv"
DEFAULT_OUTPUT_BASE = REPO_ROOT / "output/gcs"
SUITES_ROOT = REPO_ROOT / "logs/auto/gcs/suites"


def _strip_quotes(value: str) -> str:
    text = value.strip()
    if text.startswith("\"") and text.endswith("\""):
        return text[1:-1]
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    return text


def _safe_float(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _safe_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


class RunningStats:
    """Welford aggregator to compute mean / variance online."""

    __slots__ = ("count", "mean", "m2", "min", "max")

    def __init__(self) -> None:
        self.count = 0
        self.mean = 0.0
        self.m2 = 0.0
        self.min: Optional[float] = None
        self.max: Optional[float] = None

    def push(self, value: float) -> None:
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)
        if self.min is None or value < self.min:
            self.min = value
        if self.max is None or value > self.max:
            self.max = value

    @property
    def variance(self) -> Optional[float]:
        if self.count <= 1:
            return None
        return self.m2 / (self.count - 1)

    @property
    def stddev(self) -> Optional[float]:
        var = self.variance
        return math.sqrt(var) if var is not None else None


@dataclass
class PsutilMetrics:
    samples: int = 0
    cpu_avg_pct: Optional[float] = None
    cpu_max_pct: Optional[float] = None
    rss_max_bytes: Optional[int] = None
    rss_avg_bytes: Optional[float] = None
    threads_avg: Optional[float] = None
    threads_max: Optional[int] = None


@dataclass
class PerfMetrics:
    samples: int = 0
    duration_s: Optional[float] = None
    instructions: Optional[int] = None
    cycles: Optional[int] = None
    ipc: Optional[float] = None
    context_switches: Optional[int] = None
    task_clock_ms: Optional[float] = None
    cache_misses: Optional[int] = None
    cache_miss_rate: Optional[float] = None
    branch_misses: Optional[int] = None
    branch_miss_rate: Optional[float] = None


@dataclass
class TelemetryMetrics:
    samples: int = 0
    temp_c_avg: Optional[float] = None
    temp_c_min: Optional[float] = None
    temp_c_max: Optional[float] = None
    freq_hz_avg: Optional[float] = None
    freq_hz_min: Optional[float] = None
    freq_hz_max: Optional[float] = None
    throttle_flags: Optional[str] = None


@dataclass
class PowerMetrics:
    samples: int = 0
    power_avg_w: Optional[float] = None
    power_min_w: Optional[float] = None
    power_max_w: Optional[float] = None
    power_std_w: Optional[float] = None
    current_avg_a: Optional[float] = None
    current_std_a: Optional[float] = None
    voltage_avg_v: Optional[float] = None
    voltage_std_v: Optional[float] = None
    power_slope_w_per_s: Optional[float] = None
    duration_s: Optional[float] = None


@dataclass
class SuiteAggregate:
    run_id: str
    suite: str
    traffic_mode: Optional[str]
    throughput_mbps: Optional[float]
    loss_pct: Optional[float]
    avg_power_w: Optional[float]
    energy_j: Optional[float]
    psutil: PsutilMetrics
    perf: PerfMetrics
    telemetry: TelemetryMetrics
    power: PowerMetrics

    def to_flat_dict(self) -> Dict[str, Optional[float]]:
        base = {
            "run_id": self.run_id,
            "suite": self.suite,
            "traffic_mode": self.traffic_mode,
            "throughput_mbps": self.throughput_mbps,
            "loss_pct": self.loss_pct,
            "power_avg_w": self.avg_power_w,
            "power_energy_j": self.energy_j,
        }
        # Flatten nested dataclasses with prefixes for readability.
        for prefix, section in (
            ("psutil", self.psutil),
            ("perf", self.perf),
            ("telemetry", self.telemetry),
            ("power", self.power),
        ):
            payload = asdict(section)
            for key, value in payload.items():
                base[f"{prefix}_{key}"] = value
        return base


def _find_suite_root(power_csv_path: Optional[str], suite: str) -> Path:
    if power_csv_path:
        candidate = Path(_strip_quotes(power_csv_path)).resolve()
        if candidate.exists():
            return candidate.parent.parent
    return SUITES_ROOT / suite


def _resolve_monitor_file(root: Path, prefix: str, suite: str, suffix: str) -> Optional[Path]:
    monitor_dir = root / "monitor"
    if not monitor_dir.exists():
        return None
    exact = monitor_dir / f"{prefix}{suite}{suffix}"
    if exact.exists():
        return exact
    candidates = sorted(
        path for path in monitor_dir.glob(f"{prefix}*{suffix}") if suite in path.name
    )
    if candidates:
        return candidates[-1]
    fallbacks = sorted(monitor_dir.glob(f"{prefix}*{suffix}"))
    return fallbacks[-1] if fallbacks else None


def _analyze_psutil(path: Path) -> PsutilMetrics:
    metrics = PsutilMetrics()
    if not path or not path.exists():
        return metrics
    cpu_stats = RunningStats()
    rss_stats = RunningStats()
    thread_stats = RunningStats()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                cpu = float(row.get("cpu_percent", ""))
                rss = float(row.get("rss_bytes", ""))
                threads = float(row.get("num_threads", ""))
            except ValueError:
                continue
            cpu_stats.push(cpu)
            rss_stats.push(rss)
            thread_stats.push(threads)
    metrics.samples = cpu_stats.count
    if cpu_stats.count:
        metrics.cpu_avg_pct = cpu_stats.mean
        metrics.cpu_max_pct = cpu_stats.max
    if rss_stats.count:
        metrics.rss_avg_bytes = rss_stats.mean
        metrics.rss_max_bytes = int(rss_stats.max or 0)
    if thread_stats.count:
        metrics.threads_avg = thread_stats.mean
        metrics.threads_max = int(thread_stats.max or 0)
    return metrics


def _analyze_perf(path: Path) -> PerfMetrics:
    metrics = PerfMetrics()
    if not path or not path.exists():
        return metrics
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = list(csv.DictReader(handle))
    metrics.samples = len(reader)
    if len(reader) < 2:
        return metrics
    first = reader[0]
    last = reader[-1]

    def _delta(field: str) -> Optional[int]:
        try:
            start = int(float(first.get(field, "0")))
            end = int(float(last.get(field, "0")))
        except ValueError:
            return None
        return max(0, end - start)

    duration_ns = _delta("ts_unix_ns")
    metrics.duration_s = duration_ns / 1_000_000_000.0 if duration_ns else None

    metrics.instructions = _delta("instructions")
    metrics.cycles = _delta("cycles")
    if metrics.instructions is not None and metrics.cycles:
        metrics.ipc = metrics.instructions / metrics.cycles

    metrics.context_switches = _delta("context-switches")
    metrics.cache_misses = _delta("cache-misses")
    metrics.branch_misses = _delta("branch-misses")

    branches = _delta("branches") or 0
    if metrics.branch_misses is not None and branches > 0:
        metrics.branch_miss_rate = metrics.branch_misses / branches
    cache_refs = _delta("cache-references")
    if cache_refs is None or cache_refs <= 0:
        cache_refs = branches
    if metrics.cache_misses is not None and cache_refs and cache_refs > 0:
        metrics.cache_miss_rate = metrics.cache_misses / cache_refs

    try:
        task_clock_last = float(last.get("task-clock", "0"))
        task_clock_first = float(first.get("task-clock", "0"))
        metrics.task_clock_ms = max(0.0, task_clock_last - task_clock_first)
    except ValueError:
        metrics.task_clock_ms = None

    return metrics


def _analyze_telemetry(path: Path) -> TelemetryMetrics:
    metrics = TelemetryMetrics()
    if not path or not path.exists():
        return metrics
    temp_stats = RunningStats()
    freq_stats = RunningStats()
    throttle_values: List[str] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                temp = float(row.get("temp_c", ""))
                freq = float(row.get("freq_hz", ""))
            except ValueError:
                continue
            temp_stats.push(temp)
            freq_stats.push(freq)
            flags = row.get("throttled_hex")
            if flags:
                throttle_values.append(flags.strip())
    metrics.samples = temp_stats.count
    if temp_stats.count:
        metrics.temp_c_avg = temp_stats.mean
        metrics.temp_c_min = temp_stats.min
        metrics.temp_c_max = temp_stats.max
    if freq_stats.count:
        metrics.freq_hz_avg = freq_stats.mean
        metrics.freq_hz_min = freq_stats.min
        metrics.freq_hz_max = freq_stats.max
    if throttle_values:
        metrics.throttle_flags = ",".join(sorted(set(throttle_values)))
    return metrics


def _analyze_power(path: Path) -> PowerMetrics:
    metrics = PowerMetrics()
    if not path or not path.exists():
        return metrics
    power_stats = RunningStats()
    current_stats = RunningStats()
    voltage_stats = RunningStats()
    first_power: Optional[float] = None
    first_ts: Optional[int] = None
    last_power: Optional[float] = None
    last_ts: Optional[int] = None
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                ts = int(row.get("timestamp_ns") or row.get("ts_ns") or "0")
            except ValueError:
                continue
            try:
                power = float(row.get("power_w") or row.get("power") or "")
            except ValueError:
                # Attempt reconstruction from current/voltage.
                try:
                    current = float(row.get("current_a", ""))
                    voltage = float(row.get("voltage_v", ""))
                    power = current * voltage
                except ValueError:
                    continue
            try:
                current = float(row.get("current_a", "")) if row.get("current_a") else None
            except ValueError:
                current = None
            try:
                voltage = float(row.get("voltage_v", "")) if row.get("voltage_v") else None
            except ValueError:
                voltage = None

            power_stats.push(power)
            if current is not None:
                current_stats.push(current)
            if voltage is not None:
                voltage_stats.push(voltage)
            if first_ts is None:
                first_ts = ts
                first_power = power
            last_ts = ts
            last_power = power
    metrics.samples = power_stats.count
    if not power_stats.count:
        return metrics
    metrics.power_avg_w = power_stats.mean
    metrics.power_min_w = power_stats.min
    metrics.power_max_w = power_stats.max
    metrics.power_std_w = power_stats.stddev
    metrics.current_avg_a = current_stats.mean if current_stats.count else None
    metrics.current_std_a = current_stats.stddev
    metrics.voltage_avg_v = voltage_stats.mean if voltage_stats.count else None
    metrics.voltage_std_v = voltage_stats.stddev
    if first_ts is not None and last_ts and last_ts > first_ts and first_power is not None and last_power is not None:
        metrics.duration_s = (last_ts - first_ts) / 1_000_000_000.0
        metrics.power_slope_w_per_s = (last_power - first_power) / metrics.duration_s
    return metrics


def _load_summary(summary_csv: Path) -> List[dict]:
    with summary_csv.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _detect_run_id(rows: Iterable[dict]) -> Optional[str]:
    for row in rows:
        power_path = row.get("power_csv_path")
        if power_path:
            for part in Path(_strip_quotes(power_path)).parts:
                if part.startswith("run_"):
                    return part
    for row in rows:
        start_ns = row.get("start_ns")
        if start_ns:
            return f"run_{start_ns}"
    return None


def aggregate_run(summary_csv: Path, run_id: Optional[str]) -> List[SuiteAggregate]:
    rows = _load_summary(summary_csv)
    if not rows:
        return []
    detected = _detect_run_id(rows)
    run_label = run_id or detected or "run_unknown"

    aggregates: List[SuiteAggregate] = []
    for row in rows:
        suite = row.get("suite") or "unknown"
        suite_root = _find_suite_root(row.get("power_csv_path"), suite)
        psutil_path = _resolve_monitor_file(suite_root, "psutil_proc_", suite, ".csv")
        perf_path = _resolve_monitor_file(suite_root, "perf_samples_", suite, ".csv")
        telemetry_path = _resolve_monitor_file(suite_root, "sys_telemetry_", suite, ".csv")

        power_csv_field = row.get("power_csv_path")
        power_csv_path = Path(_strip_quotes(power_csv_field)).resolve() if power_csv_field else None
        if not power_csv_path or not power_csv_path.exists():
            alt_power_dir = suite_root / "power"
            if alt_power_dir.exists():
                candidates = sorted(p for p in alt_power_dir.glob("power_*.csv") if suite in p.name)
                power_csv_path = candidates[-1] if candidates else None

        aggregate = SuiteAggregate(
            run_id=run_label,
            suite=suite,
            traffic_mode=row.get("traffic_mode"),
            throughput_mbps=_safe_float(row.get("throughput_mbps")),
            loss_pct=_safe_float(row.get("loss_pct")),
            avg_power_w=_safe_float(row.get("power_avg_w")),
            energy_j=_safe_float(row.get("power_energy_j")),
            psutil=_analyze_psutil(psutil_path) if psutil_path else PsutilMetrics(),
            perf=_analyze_perf(perf_path) if perf_path else PerfMetrics(),
            telemetry=_analyze_telemetry(telemetry_path) if telemetry_path else TelemetryMetrics(),
            power=_analyze_power(power_csv_path) if power_csv_path else PowerMetrics(),
        )
        aggregates.append(aggregate)
    return aggregates


def _write_outputs(aggregates: List[SuiteAggregate], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "drone_metrics.csv"
    if aggregates:
        fieldnames = list(aggregates[0].to_flat_dict().keys())
    else:
        fieldnames = ["run_id", "suite"]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for aggregate in aggregates:
            writer.writerow(aggregate.to_flat_dict())

    json_path = output_dir / "drone_metrics.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump([agg.to_flat_dict() for agg in aggregates], handle, indent=2)

    print(f"Wrote {len(aggregates)} suite rows to {csv_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate drone follower metrics for a run")
    parser.add_argument(
        "--summary-csv",
        type=Path,
        default=DEFAULT_SUMMARY,
        help="Path to GCS summary.csv (defaults to logs/auto/gcs/summary.csv)",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Optional run identifier (e.g. run_1760347748) to select per-run summary.csv",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for aggregated dataset (defaults to output/gcs/<run-id>)",
    )
    args = parser.parse_args()

    summary_csv = args.summary_csv
    if args.run_id:
        run_summary = REPO_ROOT / f"logs/auto/gcs/{args.run_id}/summary.csv"
        if run_summary.exists():
            summary_csv = run_summary
    if not summary_csv.exists():
        raise SystemExit(f"Summary CSV not found: {summary_csv}")

    aggregates = aggregate_run(summary_csv, args.run_id)
    if not aggregates:
        print("No suites discovered; exiting")
        return

    run_id = aggregates[0].run_id
    output_dir = args.output_dir or (DEFAULT_OUTPUT_BASE / run_id)
    _write_outputs(aggregates, output_dir)


if __name__ == "__main__":
    main()
