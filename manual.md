# PQC Drone–GCS Proxy • Test Manual

**Scope.** This manual describes how to validate all supported cryptographic suites, confirm encrypted data flow end-to-end, and exercise **runtime** algorithm changes (live rekey/suite switch) for the PQC Drone–GCS Secure Communication Proxy.

**Platforms under test**

* **GCS (Windows)** PowerShell with `gcs-env`
* **Drone (Linux/RPi)** bash with `cenv`

---

## 0) Pre-flight

1. **Dependencies**

* `oqs` Python bindings present in both envs.
* Keys present: `secrets/gcs_signing.key` (private on GCS), `secrets/gcs_signing.pub` (public on both).

2. **Configuration sanity**

* Open `core/config.py` and set hosts/ports for your LAN. Required keys include:

  * `GCS_HOST`, `DRONE_HOST`
  * `GCS_PLAINTEXT_RX`, `DRONE_PLAINTEXT_RX` (local plaintext)
  * `GCS_ENC_RX`, `DRONE_ENC_RX` (encrypted UDP)
  * `TCP_HANDSHAKE_PORT`
* Optional runtime overrides:

  * Windows: `$Env:UDP_GCS_RX="56011"`, `$Env:UDP_GCS_TX="56012"`, etc.
  * Linux: `export UDP_DRONE_RX=56012`, etc.

3. **Static guardrail (no hardcoded IPs/ports)**

```bash
# Either side (from repo root)
python tools/check_no_hardcoded_ips.py
```

> Expected: “OK” or only whitelisted test files reported.

4. **Firewall**

* Allow inbound/outbound UDP on the encrypted RX ports.
* If taps/forwarders are used, make sure those bind ports are free.

---

## 1) Quick health check (plaintext & handshake)

**A. UDP plaintext sanity (optional but fast)**

```bash
# On GCS (Windows)
python tools\diag_udp.py --role gcs --auto
# On Drone (Linux)
python tools/diag_udp.py --role drone --auto
```

Expected: `Auto test PASSED` on both.

**B. Single suite encrypted smoke**

* Pick any suite listed in §3 (or list programmatically in §2).

```powershell
# GCS (PowerShell)
conda activate gcs-env
python -m core.run_proxy gcs --suite cs-mlkem768-aesgcm-mldsa65 --stop-seconds 120 --json-out gcs_debug.json
```

```bash
# Drone (bash)
source ~/cenv/bin/activate
python -m core.run_proxy drone --suite cs-mlkem768-aesgcm-mldsa65 --stop-seconds 120 --json-out drone_debug.json
```

Expected on both logs:

* `PQC handshake completed successfully`
* `counters` show `enc_in/out > 0`, `drops == 0`

---

## 2) Enumerating supported suites (ground truth)

To avoid guessing suite IDs, print the canonical strings directly from the repo.

```bash
# Either side
python - <<'PY'
from core import test_suites_config as t
for s in t.ALL_SUITES:
    print(s)
PY
```

> This prints **all** suite IDs accepted by `--suite`.
> If legacy aliases are supported, they appear in `t.ALIASES` (print similarly).

---

## 3) Individual algorithm tests (all suites)

You can test each suite **manually**, or use the batch runners.

### 3.1 One suite (manual, fully transparent)

**Start proxies**

```powershell
# GCS
python -m core.run_proxy gcs --suite <SUITE_ID> --stop-seconds 90 --json-out gcs_<SUITE_ID>.json
```

```bash
# Drone
python -m core.run_proxy drone --suite <SUITE_ID> --stop-seconds 90 --json-out drone_<SUITE_ID>.json
```

**Drive traffic (automated)**
In separate terminals:

```powershell
# GCS ? Drone plaintext generator
python tools\traffic_gcs.py --pps 20 --seconds 60 --pattern "seq:{n} time:{ts}"
```

```bash
# Drone sink + ack loopback (if implemented) or passive receive
python tools/traffic_drone.py --expect 1200 --log
```

**(Optional) Ciphertext taps for proof**

```powershell
# Tap packets from Drone?GCS (encrypted)
python tools\udp_forward_log.py --listen 0.0.0.0:46011 --forward 127.0.0.1:$Env:UDP_GCS_RX --label enc_drone_to_GCS
```

```bash
# Tap packets from GCS?Drone (encrypted)
python tools/udp_forward_log.py --listen 0.0.0.0:46012 --forward 127.0.0.1:$UDP_DRONE_RX --label enc_GCS_to_drone
```

**Pass criteria**

* `traffic_*` prints sent/received counts that **match** across ends (allowing a small UDP loss tolerance on lossy LAN/Wi-Fi).
* Proxy JSON and logs show:

  * `enc_out` and `enc_in` matching packet volumes,
  * `drops == 0` (or explainable minimal `drop_other` if you stop mid-stream).
* Taps display header parse lines with **increasing `seq`**, stable **`session_id`**, correct **`kem`/`sig`** tuple.

### 3.2 Full matrix (automated, all suites)

Run the matrix scripts **in parallel** (one on each side). They iterate the same ordered suite list so each pair lines up.

```powershell
# GCS (PowerShell, from repo root)
.\matrix_runner_gcs.ps1 -DurationSec 40 -Pkts 600 -OutDir ".\artifacts\gcs"  # use -Suites to subset if needed
```

```bash
# Drone (bash)
bash ./matrix_runner_drone.sh --duration 40 --pkts 600 --outdir ./artifacts/drone  # add --suites to subset
```

**What they do**

* For each `<SUITE_ID>` from `test_suites_config.py`, they:

  * launch `core.run_proxy` (gcs/drone),
  * run `tools/traffic_*` generator/receiver,
  * collect `*_debug.json`, stdout logs, and a minimal CSV of counts,
  * advance to the next suite.

**Batch pass criteria**

* No suite reports `Handshake failed`.
* For each suite CSV: `sent ~= received` in both directions; proxy counters have `drops == 0`.
* Aggregate summary printed at the end with per-suite status and totals.

---

## 4) Runtime rekey & live **suite switch**

The proxy implements an **in-band encrypted control channel** (type `0x02`) for rekeying. You can test two things:

* **(A) Rekey within the same suite** (epoch++ / fresh keys)
* **(B) Live **suite** switch** (e.g., ML-KEM-768 + ML-DSA-65 ? ML-KEM-1024 + Falcon-1024)

> Both operations are **online**: traffic continues, and only a short window sees the old epoch.

### 4.1 Manual trigger (explicit control inject)

Keep both proxies running for a longer window (e.g., `--stop-seconds 300`), then:

```powershell
# GCS injects a control message to rekey to a new suite at runtime
python tools\traffic_gcs.py --control "rekey suite=cs-mlkem1024-aesgcm-falcon1024" --at 30s
```

```bash
# Drone monitors for rekey; optionally assert new suite
python tools/traffic_drone.py --assert-suite cs-mlkem1024-aesgcm-falcon1024 --timeout 120
```

**Expected telemetry**

* **Both** proxies increment `rekeys_ok` by **1** in their final counters.
* Taps show `epoch` increments (e.g., `0 ? 1`) and, after the switch point, header `kem`/`sig` fields reflect the **new** suite.
* Sequence typically restarts from 0 at the new epoch; no GCM nonce reuse occurs.
* `drops` remain 0; traffic counters keep rising across the transition.

> If your traffic scripts support time-based or packet-count based triggers, `--after-pkts N` can be used instead of `--at`.

### 4.2 Scheduler-driven rekey (policy test)

If the **scheduler** is enabled to decide rekeys (e.g., based on policy/RL), start proxies with the scheduler flags (consult the scheduler README/config). Then generate steady traffic:

```powershell
# GCS with scheduler enabled (example flags; adjust to your config)
python -m core.run_proxy gcs --suite cs-mlkem768-aesgcm-mldsa65 --policy policy\default.yml --stop-seconds 240 --json-out gcs_sched.json
```

```bash
python tools\traffic_gcs.py --pps 50 --seconds 200
```

```bash
# Drone side stays as in §3.1; just run long enough
python -m core.run_proxy drone --suite cs-mlkem768-aesgcm-mldsa65 --stop-seconds 240 --json-out drone_sched.json
```

**Validate**

* `rekeys_ok >= 1` on **both** sides.
* `last_rekey_suite` matches the scheduler’s chosen target.
* Taps show `epoch` increments and new `(kem, sig)` fields.
* No stall in `enc_in/out`.

---

## 5) Suites under test (reference)

> Always treat `test_suites_config.py` as the source of truth. The list below is typical; your file may include aliases or additional variants.

**KEM (Kyber / ML-KEM):** `mlkem512`, `mlkem768`, `mlkem1024`
**SIG families and levels:**

* Dilithium / ML-DSA: `mldsa44` (L2), `mldsa65` (L3), `mldsa87` (L5)
* Falcon: `falcon512` (L1), `falcon1024` (L5)
* SPHINCS+ (SHA2 fast): `slhdsasha2-128f` (L1), `slhdsasha2-256f` (L5)

**Canonical suite ID format**

```
cs-<kem>-aesgcm-<sig>
# examples:
cs-mlkem768-aesgcm-mldsa65
cs-mlkem1024-aesgcm-falcon1024
cs-mlkem512-aesgcm-slhdsasha2-128f
```

List programmatically as shown in §2.

---

## 6) Evidence to collect (per run)

* `logs/*.log` for both proxies
* `*_debug.json` (GCS and Drone) with final counters:

  * `enc_in/out`, `ptx_in/out`, `drops`, `rekeys_ok`, `last_rekey_suite`, `last_rekey_ms`
* `tools/traffic_*` stdout: sent/received counts with timestamps
* Optional: `udp_forward_log.py` outputs showing header fields:

  * `session_id`, `seq`, `epoch`, `(kem, sig)`

A minimal CSV is written by the matrix runners into `./artifacts/<host>/summary.csv`.

---

## 7) Troubleshooting

* **WinError 10048 (address in use).** Another process (often a tap/forwarder) is already bound. Stop forwarders or change RX with `$Env:UDP_GCS_RX` / `export UDP_DRONE_RX`.
* **`ModuleNotFoundError: oqs`.** Activate the correct env (`conda activate gcs-env` or `source ~/cenv/bin/activate`) and reinstall `oqs`.
* **Handshake succeeds but `enc_in/out == 0`.** Check plaintext generators; ensure `traffic_gcs.py`/`traffic_drone.py` are running and bound to the config’s plaintext ports.
* **Replay drops.** If you pause one side and resume, old packets may fall outside the sliding window; restart a clean session for deterministic tests.
* **NAT/firewall.** On Windows, allow the Python interpreter for UDP inbound on encrypted RX; on Linux, open the port or test on the same L2 segment.

---

## 8) Clean state & reproducibility

* Avoid mixing taps with production RX ports in long runs. If tapping, **forward to a different local port** and point `UDP_*_RX` to that port.
* Reset env overrides after runs:

  * Windows: `Remove-Item Env:UDP_GCS_RX, Env:UDP_GCS_TX` (as applicable)
  * Linux: `unset UDP_DRONE_RX UDP_DRONE_TX`
* Keep a copy of `test_suites_config.py` and `core/config.py` used for the campaign alongside the artifacts.

---

## 9) One-liners (cheat sheet)

**List suites**

```bash
python - <<'PY'
from core import test_suites_config as t
print("\n".join(t.ALL_SUITES))
PY
```

**Single suite, 60-second run (both sides in parallel)**

```powershell
python -m core.run_proxy gcs --suite cs-mlkem768-aesgcm-mldsa65 --stop-seconds 60 --json-out gcs.json
python tools\traffic_gcs.py --pps 30 --seconds 45
```

```bash
python -m core.run_proxy drone --suite cs-mlkem768-aesgcm-mldsa65 --stop-seconds 60 --json-out drone.json
python tools/traffic_drone.py --expect 1350
```

**Runtime suite switch at t=20s**

```powershell
python tools\traffic_gcs.py --control "rekey suite=cs-mlkem1024-aesgcm-falcon1024" --at 20s
```

**Matrix (all suites)**

```powershell
.\matrix_runner_gcs.ps1 -DurationSec 40 -Pkts 800 -OutDir .\artifacts\gcs
```

```bash
bash ./matrix_runner_drone.sh --duration 40 --pkts 800 --outdir ./artifacts/drone
```

---

**End of manual.**
