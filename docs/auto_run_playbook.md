# Automated PQC Suite Sweep Playbook

This document explains how to run the automated 21-suite PQC sweep using the new helper scripts:

- `tools/auto/drone_follower.py` — run on the Drone (Raspberry Pi). Starts the drone proxy, a UDP echo loopback on the local app port, exposes a small TCP control API for the GCS scheduler, and rotates perf/pidstat captures per suite.
- `tools/auto/gcs_scheduler.py` — run on the GCS (Windows). Starts the GCS proxy with manual control and drives per-suite rekeys, coordinates the drone follower, pushes traffic, and writes per-suite NDJSON + a CSV summary.

This playbook assumes the repository root is identical on both hosts and `secrets/matrix/` has been generated and copied to the drone (public keys required on the Pi).

## Prerequisites

On GCS (Windows):
- Python 3.10+, the project installed (`pip install -e .` or conda env)
- PowerShell
- Ensure the repo is at the same commit that generated `secrets/matrix/`

On Drone (Raspberry Pi):
- Python 3.10+, project installed in a virtualenv
- `perf` and `sysstat` installed: `sudo apt install -y linux-perf sysstat`
- `perf_event_paranoid` relaxed if needed:

```bash
sudo sh -c 'echo 2 >/proc/sys/kernel/perf_event_paranoid'
```

Both hosts must be able to reach each other at the addresses in `core/config.py` or via overrides below.

## Ports and environment

Default ports used by the scripts (can be overridden via CLI flags):

- Control TCP (drone follower): 48080
- GCS plaintext app send port: 47001
- GCS plaintext app recv port: 47002
- Drone plaintext app send port: 47003
- Drone plaintext app recv port: 47004

Set helpful env vars before starting (example PowerShell):

```powershell
$env:DRONE_HOST="192.168.0.102"
$env:GCS_HOST="192.168.0.101"
$env:ENABLE_PACKET_TYPE="1"
$env:STRICT_UDP_PEER_MATCH="1"
```

## Step-by-step

1. On the Drone (Pi) — start the follower:

```bash
cd ~/research
source .venv/bin/activate
python -m tools.auto.drone_follower \
  --gcs 192.168.0.101 --drone 192.168.0.102 \
  --control-port 48080 \
  --initial-suite cs-mlkem768-aesgcm-mldsa65 \
  --app-recv-port 47004 \
  --outdir logs/auto --secrets-dir secrets/matrix
```

Output you should see on the Pi:

- `UDP echo listening on 0.0.0.0:47004`
- `Control server on 0.0.0.0:48080`
- A `logs/auto/<suite>/drone_summary.json` will be created by the follower's proxy.

2. On the GCS — start the scheduler (from project root in PowerShell):

```powershell
cd C:\Users\burak\Desktop\research
conda activate gcs-env
$env:DRONE_HOST="192.168.0.102"
$env:GCS_HOST="192.168.0.101"
$env:ENABLE_PACKET_TYPE="1"
$env:STRICT_UDP_PEER_MATCH="1"

python -m tools.auto.gcs_scheduler `
  --gcs 192.168.0.101 --drone 192.168.0.102 `
  --control-port 48080 `
  --app-send-port 47001 --app-recv-port 47002 `
  --duration 25 --rate 100 `
  --outdir logs\auto --secrets-dir secrets\matrix
```

Expected scheduler behavior:
- Starts the GCS proxy for the first suite with `--control-manual`.
- Connects to the drone control server and issues a `mark` so perf/pidstat rotate.
- Waits for handshake readiness via the `status_file` written by the proxy.
- For each suite, sends traffic for `duration` seconds and logs NDJSON events under `logs/auto/<suite>/gcs_events.jsonl`.
- Writes `logs/auto/summary.csv` with per-suite summary rows.

## Outputs

Each suite directory under `logs/auto/<suite>/` will contain:

- `gcs_events.jsonl` — NDJSON send/recv events from the GCS side
- `drone_summary.json` — drone-side summary written by the follower
- `perf_<suite>.csv` and `pidstat_<suite>.txt` — perf and pidstat captures from the Pi
- `gcs_summary.json` (best-effort) — proxy summary scraped by the scheduler

Top-level artifacts:
- `logs/auto/summary.csv` — per-suite rollup CSV
- `logs/auto/gcs_YYYYMMDD-HHMMSS.log` and `logs/auto/drone_YYYYMMDD-HHMMSS.log`

## Troubleshooting

- Handshake does not complete:
  - Verify `secrets/matrix/<suite>/gcs_signing.pub` exists on the Pi.
  - Verify `DRONE_HOST`/`GCS_HOST` environment variables point to correct IPs.
  - Tail proxy logs: `Get-Content logs\gcs-*.log -Tail 200 -Wait` (PowerShell) or `tail -f logs/drone-*.log` (Pi).

- `bad signature` or `Connection closed reading ciphertext length`:
  - These indicate mismatched key material (ephemeral vs persistent) — ensure GCS started with the correct `--gcs-secret-file` or use `--ephemeral` on both sides and pass the printed hex to the drone.

- `perf` / `pidstat` not found on Pi:
  - Install: `sudo apt install -y linux-perf sysstat`

- Port collisions on Windows (`WinError 10048`):
  - Run `netstat -ano | Select-String "46000|46011|46012|47001|47002|47003|47004"` and `taskkill /PID <pid> /F` on conflicting PIDs.

## Restart-per-suite variant (optional)
If you prefer each suite to be a fresh `core.run_proxy` process (so each proxy writes its own `--json-out` and `--status-file` per suite), ask and I will provide a variant of `gcs_scheduler.py` to restart the proxy per suite instead of writing to stdin.

## Next steps
- Run a single-suite smoke sweep (duration 20s) to verify logs appear as expected.
- If successful, run the full 21-suite sweep and then collect `logs/auto/summary.csv` and the per-suite artifacts for analysis.

---

If you'd like, I can: add a `--restart-per-suite` flag now, or add basic retries/timeouts to the control client for more robustness. Which would you prefer?