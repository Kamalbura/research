Drone follower telemetry schema and usage

Overview
--------
This repository's `drone_follower` publishes line-delimited JSON telemetry messages to a GCS collector and writes local CSV/JSON artifacts. The machine-readable schema is in `docs/telemetry_schema.json`. Use the schema to build consistent ingestion, feature engineering, and experiment reproducibility.

Primary telemetry kinds
-----------------------
- `hardware_context` — startup diagnostics (one-shot)
- `system_sample` — high-rate system monitor (CPU, memory, optional temperature)
- `psutil_sample` — lower-rate process metrics (rss, threads)
- `perf_sample` — perf stat aggregates (instructions, cycles, cache-misses)
- `thermal_sample` — vcgencmd thermal telemetry
- `kinematics` — synthetic/real flight kinematics and PFC (predicted flight constraint)
- `udp_echo_sample` — per-packet echo timing (one-way / processing)
- `power_summary` — aggregated power capture (avg_power_w, energy_j, CSV path)
- `rekey_transition_*` — rekey start/end events and durations
- lifecycle events: `monitors_started`, `monitors_stopped`, `stop`

How the GCS consumes telemetry
------------------------------
- Live ingestion: `tools/auto/gcs_scheduler.py` accepts live telemetry connections and expects JSON lines; it also requests actions via the follower control server (timesync, power_capture, mark, schedule_mark).
- Artifacts: GCS will attempt SCP/SFTP to retrieve `monitor_manifest.json`, individual CSVs (perf, psutil, packet_timing, power), and `telemetry_status.json` for session health.
- Alignment: GCS aligns packet timing and power traces using timestamps in nanoseconds and uses `tools/` helpers to integrate power and compute energy over windows.

Canonical adapter
-----------------
`src/schedulers/common/telemetry_adapter.py` (created) normalizes raw messages into a canonical dict with keys like `timestamp_ns`, `suite`, `cpu_percent`, `avg_power_w`, `pfc_last_w`, `udp_processing_ns`, `udp_sequence`, etc. Use this adapter as the single ingestion point for all schedulers.

Examples
--------
Example `system_sample` (JSON line):
{
  "kind": "system_sample",
  "timestamp_ns": 1697260000000000000,
  "suite": "cs-mlkem768-aesgcm-mldsa65",
  "cpu_percent": 12.5,
  "cpu_freq_mhz": 1500.0,
  "cpu_temp_c": 45.0,
  "mem_used_mb": 120.5,
  "mem_percent": 10.2
}

Example `power_summary` (JSON):
{
  "kind": "power_summary",
  "timestamp_ns": 1697260002000000000,
  "suite": "cs-mlkem768-aesgcm-mldsa65",
  "duration_s": 45.0,
  "samples": 45000,
  "avg_power_w": 21.5,
  "energy_j": 967.5,
  "csv_path": "/home/dev/output/drone/session/power/power_capture.csv"
}

Next steps
----------
- I can produce a sliding-window aggregator that consumes the adapter output and builds feature vectors for the three scheduler families (expert, RL, hybrid). Say: `implement aggregator + smoke-runner` and I'll create `schedulers/common/aggregator.py` and a CLI `schedulers/smoke_run.py` to run offline evaluations on recorded telemetry.
- Or I can finish the core audit (nonce/epoch/replay invariants) if you prefer to lock safety constraints first.

Contact
-------
Tell me which of the "Next steps" you want and I'll implement it next.