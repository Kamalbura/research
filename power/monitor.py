#!/usr/bin/env python3
"""Live power sampling helper for Raspberry Pi INA219 setups.

This script reuses :mod:`core.power_monitor` to capture high-rate samples
(typically 1 kHz) and provides two operation modes:

- ``stream`` (default) prints rolling statistics to stdout while optionally
  logging every sample to CSV.
- ``capture`` performs a fixed window capture using the library helper and
  emits a summary report on completion.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Optional


def _ensure_core_on_path() -> None:
    """Ensure the project root is importable when run as a script."""
    repo_root = Path(__file__).resolve().parent.parent
    repo_str = str(repo_root)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)


_ensure_core_on_path()

from core.power_monitor import (
    Ina219PowerMonitor,
    PowerMonitorUnavailable,
    PowerSample,
)


def _safe_label(value: str) -> str:
    value = value.strip() or "session"
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)[:64]


def _write_sample(writer: Optional[csv.writer], sample: PowerSample, sign_factor: int) -> None:
    if writer is None:
        return
    writer.writerow([
        sample.timestamp_ns,
        f"{sample.current_a:.6f}",
        f"{sample.voltage_v:.6f}",
        f"{sample.power_w:.6f}",
        sign_factor,
    ])


def _stream_mode(monitor: Ina219PowerMonitor, args: argparse.Namespace) -> int:
    duration = None if args.duration <= 0 else float(args.duration)
    label = _safe_label(args.label)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    csv_path = output_dir / f"live_{label}_{timestamp}.csv"

    csv_handle = None
    writer: Optional[csv.writer] = None
    try:
        if not args.no_csv:
            csv_handle = open(csv_path, "w", newline="", encoding="utf-8")
            writer = csv.writer(csv_handle)
            writer.writerow(["timestamp_ns", "current_a", "voltage_v", "power_w", "sign_factor"])
    except OSError as exc:
        print(f"[monitor] failed to open CSV for writing: {exc}", file=sys.stderr)

    print(f"[monitor] streaming samples at ~{monitor.sample_hz} Hz (duration={'∞' if duration is None else f'{duration:.1f}s'})")
    if writer:
        print(f"[monitor] CSV logging enabled -> {csv_path}")

    total_samples = 0
    total_current = 0.0
    total_voltage = 0.0
    total_power = 0.0
    last_report = time.perf_counter()
    start_perf = last_report
    start_ns = time.time_ns()

    try:
        for sample in monitor.iter_samples(duration):
            total_samples += 1
            total_current += sample.current_a
            total_voltage += sample.voltage_v
            total_power += sample.power_w

            _write_sample(writer, sample, monitor.sign_factor)
            if writer and (total_samples % 250) == 0:
                csv_handle.flush()  # type: ignore[union-attr]

            now_perf = time.perf_counter()
            if now_perf - last_report >= args.report_period:
                elapsed = now_perf - start_perf
                avg_rate = total_samples / elapsed if elapsed > 0 else 0.0
                print(
                    f"[monitor] +{elapsed:6.2f}s samples={total_samples:7d} rate={avg_rate:7.1f} Hz "
                    f"avg_power={total_power / max(total_samples, 1):5.3f} W"
                )
                last_report = now_perf
    except KeyboardInterrupt:
        print("\n[monitor] interrupted by user")
    finally:
        if csv_handle:
            csv_handle.flush()
            csv_handle.close()

    elapsed_s = max(time.perf_counter() - start_perf, 1e-9)
    avg_current = total_current / max(total_samples, 1)
    avg_voltage = total_voltage / max(total_samples, 1)
    avg_power = total_power / max(total_samples, 1)
    print(
        "[monitor] summary: samples={:,} duration={:.2f}s rate={:.1f} Hz avg_current={:.3f} A avg_voltage={:.3f} V avg_power={:.3f} W".format(
            total_samples,
            elapsed_s,
            total_samples / elapsed_s,
            avg_current,
            avg_voltage,
            avg_power,
        )
    )
    if not args.no_csv:
        print(f"[monitor] CSV path: {csv_path}")
    return 0


def _capture_mode(monitor: Ina219PowerMonitor, args: argparse.Namespace) -> int:
    label = _safe_label(args.label)
    start_ns = None
    if args.start_delay > 0:
        start_ns = time.time_ns() + int(args.start_delay * 1_000_000_000)
    summary = monitor.capture(label=label, duration_s=args.duration, start_ns=start_ns)
    print("[monitor] capture summary")
    for key, value in asdict(summary).items():
        print(f"  {key}: {value}")
    return 0


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="INA219 power monitor utility")
    parser.add_argument("--mode", choices=["stream", "capture"], default="stream")
    parser.add_argument("--duration", type=float, default=10.0, help="Capture duration seconds (<=0 for continuous stream)")
    parser.add_argument("--label", default="live", help="Label used for file naming")
    parser.add_argument("--output-dir", default="output/power", help="Directory for CSV outputs")
    parser.add_argument("--sample-hz", type=int, default=1000, help="Sampling frequency in Hz")
    parser.add_argument("--shunt-ohm", type=float, default=0.1, help="Shunt resistor value in ohms")
    parser.add_argument("--sign-mode", default="auto", choices=["auto", "positive", "negative"], help="Sign correction mode")
    parser.add_argument("--report-period", type=float, default=1.0, help="Seconds between console reports (stream mode)")
    parser.add_argument("--no-csv", action="store_true", help="Disable CSV logging in stream mode")
    parser.add_argument("--start-delay", type=float, default=0.0, help="Delay before capture start (seconds, capture mode)")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    output_dir = Path(args.output_dir).expanduser().resolve()
    try:
        monitor = Ina219PowerMonitor(
            output_dir,
            sample_hz=args.sample_hz,
            shunt_ohm=args.shunt_ohm,
            sign_mode=args.sign_mode,
        )
    except PowerMonitorUnavailable as exc:
        print(f"[monitor] power monitor unavailable: {exc}", file=sys.stderr)
        return 2

    if args.mode == "capture":
        return _capture_mode(monitor, args)
    return _stream_mode(monitor, args)


if __name__ == "__main__":
    raise SystemExit(main())
