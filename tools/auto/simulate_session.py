"""Emit a small line-delimited JSON telemetry trace for testing.

This script is intentionally lightweight and self-contained so tests can run
without external deps.
"""
from __future__ import annotations

import json
from pathlib import Path
from time import time_ns

EXAMPLE_KINDS = [
    "system_sample",
    "psutil_sample",
    "perf_sample",
    "kinematics",
    "udp_echo_sample",
    "power_summary",
    "thermal_sample",
    "capabilities_response",
]


def make_event(kind: str, session_id: str, seq: int) -> dict:
    ts = time_ns()
    base = {
        "session_id": session_id,
        "kind": kind,
        "timestamp_ns": ts,
        "suite": "cs-mlkem512-aesgcm-mldsa44",
    }
    if kind == "system_sample":
        base.update({"cpu_percent": 12.3, "mem_percent": 34.5, "proxy_pid": 1234})
    elif kind == "psutil_sample":
        base.update({"cpu_percent": 11.1, "rss_bytes": 12345678, "num_threads": 7})
    elif kind == "perf_sample":
        base.update({"ts_unix_ns": ts, "t_offset_ms": seq, "instructions": 1000 + seq, "cycles": 500 + seq, "task-clock": 10.5 + seq, "suite": base["suite"]})
    elif kind == "kinematics":
        base.update({"sequence": seq, "speed_mps": 3.14, "velocity_horizontal_mps": 2.0})
    elif kind == "udp_echo_sample":
        base.update({"recv_timestamp_ns": ts, "send_timestamp_ns": ts - 1000000, "processing_ns": 1000000, "sequence": seq})
    elif kind == "power_summary":
        base.update({"label": "ina219_main", "duration_s": 1.0, "samples": 10, "avg_current_a": 0.12, "avg_voltage_v": 5.0, "avg_power_w": 0.6, "energy_j": 0.6})
    elif kind == "thermal_sample":
        base.update({"ts_unix_ns": ts, "temp_c": 45.0, "freq_hz": 700000000})
    elif kind == "capabilities_response":
        base.update({"capabilities": {"supported_suites": [base["suite"]], "suite_registry_size": 1}})
    return base


def write_trace(path: Path, session_id: str = "sim-1", events: int = 20):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        seq = 0
        for i in range(events):
            kind = EXAMPLE_KINDS[i % len(EXAMPLE_KINDS)]
            ev = make_event(kind, session_id, seq)
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            seq += 1


if __name__ == "__main__":
    out = Path("tests/_data/sim_trace.ldjson")
    write_trace(out, "sim-1", events=24)
    print(f"Wrote {out}")
