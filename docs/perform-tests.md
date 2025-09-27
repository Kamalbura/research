# PQC Drone↔GCS Bi-Directional Test Playbook

This playbook describes how to exercise every registered KEM×SIG suite between the drone proxy (Raspberry Pi 4) and the GCS proxy (Windows host), measure runtime characteristics, and capture logs suitable for research reporting.

## 1. Test objectives

- Validate encrypted bi-directional transport for the 21 suites listed below.
- Measure handshake/rekey timing, CPU, RAM, and throughput during sustained traffic.
- Support on-the-fly suite changes using the in-band control plane.
- Automate matrix sweeps that emit structured logs ready for post-processing.

## 2. Suite catalogue

| Suite ID | KEM | Signature |
| --- | --- | --- |
| `cs-mlkem512-aesgcm-mldsa44` | ML-KEM-512 | ML-DSA-44 |
| `cs-mlkem512-aesgcm-mldsa65` | ML-KEM-512 | ML-DSA-65 |
| `cs-mlkem512-aesgcm-mldsa87` | ML-KEM-512 | ML-DSA-87 |
| `cs-mlkem512-aesgcm-falcon512` | ML-KEM-512 | Falcon-512 |
| `cs-mlkem512-aesgcm-falcon1024` | ML-KEM-512 | Falcon-1024 |
| `cs-mlkem512-aesgcm-sphincs128fsha2` | ML-KEM-512 | SLH-DSA-SHA2-128f |
| `cs-mlkem512-aesgcm-sphincs256fsha2` | ML-KEM-512 | SLH-DSA-SHA2-256f |
| `cs-mlkem768-aesgcm-mldsa44` | ML-KEM-768 | ML-DSA-44 |
| `cs-mlkem768-aesgcm-mldsa65` | ML-KEM-768 | ML-DSA-65 |
| `cs-mlkem768-aesgcm-mldsa87` | ML-KEM-768 | ML-DSA-87 |
| `cs-mlkem768-aesgcm-falcon512` | ML-KEM-768 | Falcon-512 |
| `cs-mlkem768-aesgcm-falcon1024` | ML-KEM-768 | Falcon-1024 |
| `cs-mlkem768-aesgcm-sphincs128fsha2` | ML-KEM-768 | SLH-DSA-SHA2-128f |
| `cs-mlkem768-aesgcm-sphincs256fsha2` | ML-KEM-768 | SLH-DSA-SHA2-256f |
| `cs-mlkem1024-aesgcm-mldsa44` | ML-KEM-1024 | ML-DSA-44 |
| `cs-mlkem1024-aesgcm-mldsa65` | ML-KEM-1024 | ML-DSA-65 |
| `cs-mlkem1024-aesgcm-mldsa87` | ML-KEM-1024 | ML-DSA-87 |
| `cs-mlkem1024-aesgcm-falcon512` | ML-KEM-1024 | Falcon-512 |
| `cs-mlkem1024-aesgcm-falcon1024` | ML-KEM-1024 | Falcon-1024 |
| `cs-mlkem1024-aesgcm-sphincs128fsha2` | ML-KEM-1024 | SLH-DSA-SHA2-128f |
| `cs-mlkem1024-aesgcm-sphincs256fsha2` | ML-KEM-1024 | SLH-DSA-SHA2-256f |

## 3. Prerequisites

### Hardware & OS

- **GCS host**: Windows 11/10 with PowerShell, Python 3.10+, `perfmon` optional.
- **Drone host**: Raspberry Pi 4 (64-bit Linux) with Python 3.10+. Enable profiling tools via `sudo apt install linux-perf sysstat` (provides `perf`, `pidstat`, and friends).
- Both hosts must resolve each other according to `core/config.py` (defaults to `192.168.0.103` for GCS and `192.168.0.102` for drone). Adjust environment variables if the LAN layout differs.

### Repository setup

```powershell
# Windows (GCS)
# If you use conda on the GCS host, activate the project environment instead of creating a venv:
#
# PowerShell (conda)
# Note: your environment name may differ; you mentioned `gcs-env` so we show that here.
conda activate gcs-env
# After activating, install the repo in editable mode if needed:
pip install -e .

# Or use a native Python virtualenv instead:
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

```bash
# Raspberry Pi (drone)
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
sudo apt install -y linux-perf sysstat
```

Ensure both sides share the same repo revision.

## 4. Prepare key material

1. On the GCS host, generate signing identities for all suites and stage them under `secrets/matrix/<suite>/`:

```powershell
# PowerShell (Windows)
python -m tools.prepare_matrix_keys `
  --suite cs-mlkem512-aesgcm-mldsa44 `
  --suite cs-mlkem512-aesgcm-mldsa65 `
  --suite cs-mlkem512-aesgcm-mldsa87 `
  --suite cs-mlkem512-aesgcm-falcon512 `
  --suite cs-mlkem512-aesgcm-falcon1024 `
  --suite cs-mlkem512-aesgcm-sphincs128fsha2 `
  --suite cs-mlkem512-aesgcm-sphincs256fsha2 `
  --suite cs-mlkem768-aesgcm-mldsa44 `
  --suite cs-mlkem768-aesgcm-mldsa65 `
  --suite cs-mlkem768-aesgcm-mldsa87 `
  --suite cs-mlkem768-aesgcm-falcon512 `
  --suite cs-mlkem768-aesgcm-falcon1024 `
  --suite cs-mlkem768-aesgcm-sphincs128fsha2 `
  --suite cs-mlkem768-aesgcm-sphincs256fsha2 `
  --suite cs-mlkem1024-aesgcm-mldsa44 `
  --suite cs-mlkem1024-aesgcm-mldsa65 `
  --suite cs-mlkem1024-aesgcm-mldsa87 `
  --suite cs-mlkem1024-aesgcm-falcon512 `
  --suite cs-mlkem1024-aesgcm-falcon1024 `
  --suite cs-mlkem1024-aesgcm-sphincs128fsha2 `
  --suite cs-mlkem1024-aesgcm-sphincs256fsha2
```

2. Synchronise `secrets/matrix/` to the Pi (only the public keys are required on the drone):

```bash
# On GCS (PowerShell) – use your preferred copy mechanism
scp -r secrets/matrix pi@192.168.0.102:~/research/secrets/
```

3. Confirm the drone host has `secrets/matrix/<suite>/gcs_signing.pub` for every target suite.

## 5. Manual validation workflow

Perform this sequence for each suite to establish a baseline before automation.

### 5.1 Launch proxies

**GCS side (PowerShell):**

```powershell
$Suite = "cs-mlkem768-aesgcm-mldsa65"
$JsonOut = "logs\manual\gcs_${Suite}.json"
python -m core.run_proxy gcs `
  --suite $Suite `
  --gcs-secret-file secrets\matrix\$Suite\gcs_signing.key `
  --stop-seconds 120 `
  --json-out $JsonOut `
  --status-file logs\manual\gcs_status_${Suite}.json `
  --control-manual
```

**Drone side (Linux):**

```bash
export SUITE=cs-mlkem768-aesgcm-mldsa65
export PUB=secrets/matrix/${SUITE}/gcs_signing.pub
python -m core.run_proxy drone \
  --suite "${SUITE}" \
  --peer-pubkey-file "${PUB}" \
  --stop-seconds 120 \
  --json-out "logs/manual/drone_${SUITE}.json"
```

The GCS console opens an interactive `rekey>` prompt because `--control-manual` is enabled. Leave it running for now.

### 5.2 Drive traffic

Launch plaintext generators on each host to verify both directions:

**GCS traffic (PowerShell):**

```powershell
python -m tools.traffic_gcs --count 500 --rate 75 --duration 45 `
  --out logs\manual\gcs_traffic_${Suite}.jsonl `
  --summary logs\manual\gcs_traffic_${Suite}.json
```

**Drone traffic (Linux):**

```bash
python -m tools.traffic_drone --count 500 --rate 75 --duration 45 \
  --out logs/manual/drone_traffic_${SUITE}.jsonl \
  --summary logs/manual/drone_traffic_${SUITE}.json
```

### 5.3 Dynamic suite switch & timing

From the GCS `rekey>` prompt:

1. List suites: type `list`.
2. Queue a rekey (example switch to ML-KEM-1024 + Falcon-1024): type `cs-mlkem1024-aesgcm-falcon1024` and press Enter.
3. Observe `prepare`/`commit` logs and the `status_file` JSON updates. Timestamps in the status file mark when the new suite became active.
4. Record the time difference between the `prepare_ok` note in `logs/manual/gcs_status_*.json` and the corresponding `Control rekey successful` entry in `logs/gcs-*.log` to measure rekey latency.

Repeat traffic generation for the new suite to confirm continuity without restarting processes.

## 6. Automated matrix sweep

### 6.1 GCS host (PowerShell)

```powershell
$Suites = @(
  "cs-mlkem512-aesgcm-mldsa44",
  "cs-mlkem512-aesgcm-mldsa65",
  "cs-mlkem512-aesgcm-mldsa87",
  "cs-mlkem512-aesgcm-falcon512",
  "cs-mlkem512-aesgcm-falcon1024",
  "cs-mlkem512-aesgcm-sphincs128fsha2",
  "cs-mlkem512-aesgcm-sphincs256fsha2",
  "cs-mlkem768-aesgcm-mldsa44",
  "cs-mlkem768-aesgcm-mldsa65",
  "cs-mlkem768-aesgcm-mldsa87",
  "cs-mlkem768-aesgcm-falcon512",
  "cs-mlkem768-aesgcm-falcon1024",
  "cs-mlkem768-aesgcm-sphincs128fsha2",
  "cs-mlkem768-aesgcm-sphincs256fsha2",
  "cs-mlkem1024-aesgcm-mldsa44",
  "cs-mlkem1024-aesgcm-mldsa65",
  "cs-mlkem1024-aesgcm-mldsa87",
  "cs-mlkem1024-aesgcm-falcon512",
  "cs-mlkem1024-aesgcm-falcon1024",
  "cs-mlkem1024-aesgcm-sphincs128fsha2",
  "cs-mlkem1024-aesgcm-sphincs256fsha2"
)

$SuiteArg = ($Suites -join ",")

powershell -File tools\matrix_runner_gcs.ps1 `
  -Suites $Suites `
  -DurationSec 25 `
  -SlowSecs 90 `
  -Count 400 `
  -Rate 100 `
  -OutDir logs\matrix `
  -SecretsDir secrets
```

### 6.2 Drone host (Linux)

```bash
SUITE_LIST="cs-mlkem512-aesgcm-mldsa44,cs-mlkem512-aesgcm-mldsa65,cs-mlkem512-aesgcm-mldsa87,cs-mlkem512-aesgcm-falcon512,cs-mlkem512-aesgcm-falcon1024,cs-mlkem512-aesgcm-sphincs128fsha2,cs-mlkem512-aesgcm-sphincs256fsha2,cs-mlkem768-aesgcm-mldsa44,cs-mlkem768-aesgcm-mldsa65,cs-mlkem768-aesgcm-mldsa87,cs-mlkem768-aesgcm-falcon512,cs-mlkem768-aesgcm-falcon1024,cs-mlkem768-aesgcm-sphincs128fsha2,cs-mlkem768-aesgcm-sphincs256fsha2,cs-mlkem1024-aesgcm-mldsa44,cs-mlkem1024-aesgcm-mldsa65,cs-mlkem1024-aesgcm-mldsa87,cs-mlkem1024-aesgcm-falcon512,cs-mlkem1024-aesgcm-falcon1024,cs-mlkem1024-aesgcm-sphincs128fsha2,cs-mlkem1024-aesgcm-sphincs256fsha2"

./tools/matrix_runner_drone.sh \
  --suites "${SUITE_LIST}" \
  --duration 25 \
  --slow-duration 90 \
  --pkts 400 \
  --rate 100 \
  --outdir logs/matrix \
  --secrets-dir secrets
```

Both runners emit per-suite JSON counters, NDJSON traffic traces, and CSV summaries under `logs/matrix/`.

### 6.3 Aggregate results

After both hosts finish:

```powershell
python -m tools.aggregate_lan_results --results-dir logs\matrix
```

```bash
python -m tools.aggregate_lan_results --results-dir logs/matrix
```

Combine the GCS and drone summaries to compute pass/fail status per suite. Use `python -m tools.counter_utils` helpers to script additional assertions (e.g., ensuring every suite rekeyed successfully during manual experiments).

## 7. High-resolution performance logging (Raspberry Pi)

Run these commands as `root` or with `sudo` to capture CPU and memory metrics:

```bash
# Identify PIDs once proxies start
PGID=$(pgrep -f "core.run_proxy drone")

# 1 ms interval perf counters with wall-clock aligned timestamps
echo "timestamp_ns,event,metric" > logs/perf_drone_${SUITE}.csv
sudo perf stat -I 1000 -e task-clock,cycles,instructions,cache-misses,context-switches -p "$PGID" \
  --log-fd 1 2>&1 | awk '{print strftime("%Y-%m-%dT%H:%M:%S"),",",$0}' >> logs/perf_drone_${SUITE}.csv
```

Optional flamegraph capture for 60 seconds:

```bash
sudo perf record -F 99 -g -p "$PGID" -- sleep 60
sudo perf script > logs/perf_drone_${SUITE}.script
```

Complement `perf` with system-level snapshots:

```bash
# CPU, memory, and process statistics every second
sudo pidstat -hlur -p "$PGID" 1 > logs/pidstat_${SUITE}.txt &
# Thermal and frequency telemetry
watch -n 1 -t 'vcgencmd measure_temp; vcgencmd measure_clock arm' >> logs/thermal_${SUITE}.txt &
```

Use `kill` to stop background monitors after each run.

## 8. Handshake and rekey timing analysis

- The GCS proxy writes `status_file` JSON updates whenever a handshake or rekey completes. Each entry includes millisecond timestamps (`t_ms`).
- Subtract the `prepare_rekey` timestamp from the subsequent `status` entry to measure control-plane duration.
- Compare with drone-side counters `last_rekey_ms` (found in the JSON summary) to validate both perspectives.

For automated runs, enable status capture by adding `--status-file logs\matrix\gcs_status.json` to the GCS runner invocation. The PowerShell script already supports this—extend it if more granularity is required.

## 9. Round-trip latency and throughput

The traffic generators write NDJSON rows containing send timestamps (`t_send_ns`) and sequence numbers. To approximate RTT:

1. Load `logs/matrix/traffic/<suite>/gcs_events.jsonl` and `drone_events.jsonl`.
2. For each sequence number, subtract the send timestamp recorded on one host from the receive event on the peer.
3. Aggregate latencies per suite and correlate with CPU metrics collected via `perf`.

A sample notebook skeleton:

```bash
python - <<'PY'
import json
from pathlib import Path

suite = "cs-mlkem768-aesgcm-mldsa65"
base = Path("logs/matrix/traffic") / suite

sends = {row["seq"]: row["t_send_ns"] for row in map(json.loads, open(base / "gcs_events.jsonl")) if row["event"] == "send"}
recvs = [json.loads(line) for line in open(base / "drone_events.jsonl") if 'seq' in line]

latencies = []
for row in recvs:
    seq = row.get("seq")
    if seq in sends:
        latencies.append((row["ts"], (int(row.get("t_recv_ns", 0)) - sends[seq]) / 1e6))

print(f"Measured {len(latencies)} RTT samples for {suite}")
PY
```

## 10. Troubleshooting checklist

- **Handshake failures**: Verify the drone uses the matching `gcs_signing.pub` for the suite and that `CONFIG["DRONE_HOST"]`/`CONFIG["GCS_HOST"]` match real IPs.
- **Control-plane stalls**: Ensure `CONFIG["ENABLE_PACKET_TYPE"]` remains `True` (default). Without it, rekey messages will be dropped.
- **Perf permission errors**: Confirm the Pi kernel has `perf_event_paranoid <= 2` via `echo 2 | sudo tee /proc/sys/kernel/perf_event_paranoid`.
- **Clock drift**: Synchronise hosts with NTP before collecting RTT stats to avoid skew.
- **Resource saturation**: Use `htop`/`top` alongside `perf` to watch temperature and throttling on the Pi.

## 11. Next steps

- Feed aggregated metrics into `docs/measurement-and-results.txt` or a new analysis notebook.
- Compare CPU cycles per packet across suites to highlight cost gaps for the research paper.
- Consider extending `tools/matrix_runner_*` to emit perf markers automatically (hook into `tools/markers.py`) for even tighter alignment with external power instrumentation.
