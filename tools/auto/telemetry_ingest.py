#!/usr/bin/env python3
"""Simple telemetry ingest for line-delimited JSON telemetry produced by the follower.

This MVP supports reading a file of newline-delimited JSON messages and
writing per-session CSV outputs (telemetry_events.csv) plus a few flattened
timeseries CSVs for common kinds. Designed to be offline-friendly for tests.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Dict, Any, Optional

OUT_BASE = Path("output/gcs")


class CsvWriter:
    def __init__(self, path: Path, fieldnames):
        self.path = path
        self.fieldnames = list(fieldnames)
        self._ensure_dir()
        self._handle = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._handle, fieldnames=self.fieldnames)
        if os.stat(self.path).st_size == 0:
            self._writer.writeheader()

    def _ensure_dir(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def write(self, row: Dict[str, Any]):
        out = {k: row.get(k, "") for k in self.fieldnames}
        self._writer.writerow(out)
        self._handle.flush()

    def close(self):
        try:
            self._handle.close()
        except Exception:
            pass


def flatten_and_write(session_dir: Path, kind: str, payload: Dict[str, Any], writers: Dict[str, CsvWriter]):
    # Write the raw event to telemetry_events.csv
    events_writer = writers.get("telemetry_events")
    if events_writer:
        events_writer.write({
            "session_id": payload.get("session_id", ""),
            "kind": kind,
            "timestamp_ns": payload.get("timestamp_ns", ""),
            "suite": payload.get("suite", ""),
            "payload_json": json.dumps(payload, ensure_ascii=False),
        })

    # Flatten selected kinds
    if kind == "system_sample":
        w = writers.get("system_samples")
        if w:
            w.write({
                "timestamp_ns": payload.get("timestamp_ns", ""),
                "timestamp_iso": payload.get("timestamp_iso", ""),
                "suite": payload.get("suite", ""),
                "proxy_pid": payload.get("proxy_pid", ""),
                "cpu_percent": payload.get("cpu_percent", ""),
                "cpu_freq_mhz": payload.get("cpu_freq_mhz", ""),
                "cpu_temp_c": payload.get("cpu_temp_c", ""),
                "mem_used_mb": payload.get("mem_used_mb", ""),
                "mem_percent": payload.get("mem_percent", ""),
            })
    elif kind == "psutil_sample":
        w = writers.get("psutil_samples")
        if w:
            w.write({
                "timestamp_ns": payload.get("timestamp_ns", ""),
                "suite": payload.get("suite", ""),
                "cpu_percent": payload.get("cpu_percent", ""),
                "rss_bytes": payload.get("rss_bytes", ""),
                "num_threads": payload.get("num_threads", ""),
            })
    elif kind == "perf_sample":
        w = writers.get("perf_samples")
        if w:
            row = {k: payload.get(k, "") for k in w.fieldnames}
            w.write(row)
    elif kind == "thermal_sample":
        w = writers.get("thermal_samples")
        if w:
            w.write({
                "ts_unix_ns": payload.get("ts_unix_ns", ""),
                "suite": payload.get("suite", ""),
                "temp_c": payload.get("temp_c", ""),
                "freq_hz": payload.get("freq_hz", ""),
                "throttled_hex": payload.get("throttled_hex", ""),
            })
    elif kind == "kinematics":
        w = writers.get("kinematics")
        if w:
            row = {k: payload.get(k, "") for k in w.fieldnames}
            w.write(row)
    elif kind == "udp_echo_sample":
        w = writers.get("udp_echo")
        if w:
            w.write({
                "recv_timestamp_ns": payload.get("recv_timestamp_ns", ""),
                "send_timestamp_ns": payload.get("send_timestamp_ns", ""),
                "processing_ns": payload.get("processing_ns", ""),
                "sequence": payload.get("sequence", ""),
                "suite": payload.get("suite", ""),
            })
    elif kind == "power_summary":
        w = writers.get("power_summaries")
        if w:
            w.write({
                "timestamp_ns": payload.get("timestamp_ns", ""),
                "suite": payload.get("suite", ""),
                "label": payload.get("label", ""),
                "duration_s": payload.get("duration_s", ""),
                "samples": payload.get("samples", ""),
                "avg_current_a": payload.get("avg_current_a", ""),
                "avg_voltage_v": payload.get("avg_voltage_v", ""),
                "avg_power_w": payload.get("avg_power_w", ""),
                "energy_j": payload.get("energy_j", ""),
                "sample_rate_hz": payload.get("sample_rate_hz", ""),
                "csv_path": payload.get("csv_path", ""),
            })
    elif kind in ("capabilities_snapshot", "capabilities_response"):
        w = writers.get("capabilities")
        if w:
            cap = payload.get("capabilities") or payload.get("capabilities") or {}
            w.write({
                "timestamp_ns": payload.get("timestamp_ns", ""),
                "session_id": payload.get("session_id", ""),
                "supported_suites": ";".join(cap.get("supported_suites", [])) if isinstance(cap, dict) else "",
                "missing_kems": ";".join(cap.get("missing_kems", [])) if isinstance(cap, dict) else "",
                "missing_sigs": ";".join(cap.get("missing_sigs", [])) if isinstance(cap, dict) else "",
                "suite_registry_size": cap.get("suite_registry_size", "") if isinstance(cap, dict) else "",
                "raw": json.dumps(cap, ensure_ascii=False) if isinstance(cap, dict) else json.dumps({}),
            })


def process_input_file(path: Path, dry_run: bool = False) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    writers: Dict[str, CsvWriter] = {}

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                session_id = msg.get("session_id") or msg.get("session") or "unknown"
                session_dir = OUT_BASE / str(session_id)
                # instantiate writers for this session lazily
                if "telemetry_events" not in writers:
                    writers["telemetry_events"] = CsvWriter(session_dir / "telemetry_events.csv", ["session_id", "kind", "timestamp_ns", "suite", "payload_json"]) 
                    writers["system_samples"] = CsvWriter(session_dir / "system_samples.csv", ["timestamp_ns","timestamp_iso","suite","proxy_pid","cpu_percent","cpu_freq_mhz","cpu_temp_c","mem_used_mb","mem_percent"]) 
                    writers["psutil_samples"] = CsvWriter(session_dir / "psutil_samples.csv", ["timestamp_ns","suite","cpu_percent","rss_bytes","num_threads"]) 
                    writers["perf_samples"] = CsvWriter(session_dir / "perf_samples.csv", ["ts_unix_ns","t_offset_ms","instructions","cycles","cache-misses","branch-misses","task-clock","context-switches","branches","suite"]) 
                    writers["thermal_samples"] = CsvWriter(session_dir / "thermal_samples.csv", ["ts_unix_ns","suite","temp_c","freq_hz","throttled_hex"]) 
                    writers["kinematics"] = CsvWriter(session_dir / "kinematics.csv", ["timestamp_ns","sequence","suite","velocity_horizontal_mps","velocity_vertical_mps","speed_mps","horizontal_accel_mps2","vertical_accel_mps2","yaw_rate_dps","heading_deg","altitude_m","tilt_deg","predicted_flight_constraint_w","weight_n","mass_kg"]) 
                    writers["udp_echo"] = CsvWriter(session_dir / "udp_echo.csv", ["recv_timestamp_ns","send_timestamp_ns","processing_ns","sequence","suite"]) 
                    writers["power_summaries"] = CsvWriter(session_dir / "power_summaries.csv", ["timestamp_ns","suite","label","duration_s","samples","avg_current_a","avg_voltage_v","avg_power_w","energy_j","sample_rate_hz","csv_path"]) 
                    writers["capabilities"] = CsvWriter(session_dir / "capabilities.csv", ["timestamp_ns","session_id","supported_suites","missing_kems","missing_sigs","suite_registry_size","raw"]) 

                kind = msg.get("kind") or msg.get("event") or "unknown"
                # payload is msg minus session_id and kind/component
                payload = dict(msg)
                # ensure session_id present in payload for downstream
                payload.setdefault("session_id", session_id)
                if dry_run:
                    print(f"DRY {kind} -> session {session_id}")
                    continue
                flatten_and_write(session_dir, kind, payload, writers)
    finally:
        for w in writers.values():
            try:
                w.close()
            except Exception:
                pass


def _parse_args():
    p = argparse.ArgumentParser(description="Telemetry ingest (file mode)")
    p.add_argument("--input-file", type=Path, required=True, help="Line-delimited JSON telemetry file to ingest")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main(argv=None):
    args = _parse_args() if argv is None else _parse_args()
    process_input_file(args.input_file, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
