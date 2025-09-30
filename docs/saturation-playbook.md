# Saturation Sweep Quickstart

This note captures the bare minimum needed to drive a full saturation sweep across all 21 post-quantum suites, while collecting the rich telemetry (RTT, throughput, CPU, memory, perf counters, temperature, and clock frequency) on both GCS and the drone Pi.

## 1. Prerequisites
- **Code state**: Latest `main` with the updated `tools/auto/drone_follower.py` and `tools/auto/gcs_scheduler.py`.
- **Python deps** (both hosts): install `psutil`, `openpyxl`, and anything listed in `environment.yml`.
  ```powershell
  # Windows / GCS host
  pip install -r environment.yml
  pip install openpyxl
  ```
  ```bash
  # Pi
  pip install psutil openpyxl
  sudo apt-get install linux-perf sysstat vcgencmd
  ```
- **Kernel tools** on Pi: `perf`, `pidstat` (from `sysstat`), and `vcgencmd` must be available without sudo.
- **Permissions**: the Pi user needs rights to switch CPU governor to `performance` (script handles this via `sudo sh -c 'echo performance > /sys/devices/system/cpu/.../scaling_governor'`). Run once manually if password-less sudo is required.

## 2. Prepare Output Folders
Both scripts default to `/home/dev/research/output/drone` for telemetry. Confirm the path exists on the Pi and is writable. On the GCS side, the scheduler mirrors detailed run files under `logs/auto/gcs` plus per-suite folders in the same tree.

## 3. Start the Drone Follower (Pi)
SSH to the Pi and launch the follower with a session identifier. The follower will:

```bash
cd /home/dev/research
python tools/auto/drone_follower.py \
  --session-id satrun_$(date +%Y%m%d-%H%M%S)
```

> Override the telemetry destination by setting `DRONE_MONITOR_OUTPUT_BASE` before launch (default already points at `/home/dev/research/output/drone`).

Key files created on the Pi:

- `system_stats.csv`: CPU %, memory, context switches, interrupts sampled ~10 Hz.
- `perf_sample_*.txt`: perf-stat windows capturing IPC, cycles.
- `pidstat.csv`: per-process CPU splits.
- `thermal_freq.csv`: vcgencmd temperature and clock snapshots.
- `udp_echo_events.jsonl`: ingress/egress timestamps for the echo responder (early RTT hints).

## 4. Run the GCS Scheduler (control host)
On the GCS workstation, run the scheduler in saturation mode. This will iterate across all suites, measuring each rate tier until RTT spikes or throughput collapses. Provide the same session ID (purely for naming consistency).

```powershell
cd C:\Users\burak\Desktop\research
python tools\auto\gcs_scheduler.py `
  --traffic saturation `
  --duration 30 `
  --payload-bytes 256 `
  --event-sample 10 `
  --max-rate 200 `
  --pre-gap 1 `
  --inter-gap 10 `
  --session-id satrun_20250930
```

What happens:
- The scheduler reorders suites based on `CONFIG` (ensuring the preferred starter first) and drives all 21 algorithms sequentially.
- Each suite run records per-rate metrics (`throughput_mbps`, `loss_pct`, `avg/min/max RTT`) into JSONL logs under `logs/auto/gcs/suites/<suite>/`.
- Rekey time is measured via the control plane handshake (`rekey_ms`).
- Overall saturation findings land in `logs/auto/gcs/saturation_summary_<SESSION>.json`.
- If `openpyxl` is installed, an Excel workbook is emitted to `/home/dev/research/output/drone/saturation_<suite>_<SESSION>.xlsx` for each suite (easy charting).

## 5. Interpreting the Results
- **Saturation point**: For each suite, the JSON/Excel output lists the Mbps tier where RTT jumped above 1.8× the baseline or achieved throughput fell below 80% of the requested rate.
- **RTT & loss**: The blaster events JSONL (`saturation_<rate>.jsonl`) holds raw send/receive timestamps; aggregate values are in the summary JSON/Excel.
- **CPU & Memory**: Pi logs (`system_stats.csv`, `pidstat.csv`) reveal core saturation. The scheduler also reads proxy counters (`enc_out`, `enc_in`, `drops`, `rekeys_*`) from `gcs_status.json` snapshots.
- **Thermals & frequency**: `thermal_freq.csv` lines expose the temperature (°C) and current frequency (MHz) during each sweep segment.
- **Perf counters**: `perf_sample_*.txt` give IPC and stall summaries every 5 seconds (configurable in `drone_follower.py`).

## 6. Cleanup & Verification
- Stop the scheduler (`Ctrl+C`), then let the follower exit gracefully (`ctl_send stop` happens automatically when the scheduler finishes).
- Confirm the CPU governor on Pi is back to its original setting if required (`ondemand` or `powersave`).
- Copy the `/home/dev/research/output/drone/<SESSION>` directory and `logs/auto/gcs` tree for archival.
- Optional: rerun `python tools/check_no_hardcoded_ips.py` and `pytest tests/test_suites_config.py` to ensure configuration integrity post-changes.

That’s all that’s needed to run and capture a full saturation characterization with aggressive telemetry on both ends.
