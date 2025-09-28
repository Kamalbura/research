Auto Test Playbook for PQC Drone↔GCS Proxy

Goal
----
Provide a concise, copy-pasteable playbook for running automated smoke and matrix tests between the GCS (Windows) and Drone (RPi) using the repository's helper scripts.

Key components
--------------
- GCS controller: `tools/auto_test_gcs.py` — listens on a TCP control port and sends a JSON command to the drone on connect: {"suite": "<suite>", "count": N, "udp_dest": [host,port]}.
- Drone runner: `tools/auto_test_drone.py` — connects to the GCS controller, receives the command, runs N UDP messages to udp_dest and sends results JSON back.
- UDP echo helper: `tools/udp_echo.py` — simple UDP echo server used as a local plaintext endpoint for tests.
- Proxy entrypoint: `python -m core.run_proxy <gcs|drone> --suite <suite>` — starts the proxy role and performs handshake + encrypted UDP handling.

Ports and addresses (defaults)
------------------------------
- GCS host: 192.168.0.101 (CONFIG["GCS_HOST"])  
- Drone host: 192.168.0.102 (CONFIG["DRONE_HOST"])  
- Plaintext ports (local loopback by default):  
  - GCS app→proxy: 47001 (GCS_PLAINTEXT_TX)  
  - GCS proxy→app: 47002 (GCS_PLAINTEXT_RX)  
  - Drone app→proxy: 47003 (DRONE_PLAINTEXT_TX)  
  - Drone proxy→app: 47004 (DRONE_PLAINTEXT_RX)  
- Encrypted UDP: 46011/46012 (GCS/DRONE encrypted RXs)  
- Example automation control port: 47010 (used by auto_test_gcs and auto_test_drone in examples)

JSON control contract
---------------------
- Command sent by GCS to Drone (UTF-8 newline-terminated JSON):
  {
    "suite": "cs-mlkem512-aesgcm-mldsa44",
    "count": 8,
    "udp_dest": ["127.0.0.1", 47001]
  }

- Drone replies with results JSON (newline-terminated):
  {
    "suite": "...",
    "count": 8,
    "results": [{"seq":0,"rtt_ms":12.3}, ...]
  }

Recommended quick smoke (GCS-first)
-----------------------------------
1) On GCS — start a local UDP echo (plaintext endpoint):

```powershell
conda activate gcs-env
cd C:\Users\burak\Desktop\research
python tools/udp_echo.py --host 127.0.0.1 --port 47001
```

Leave this running in Terminal A.

2) On GCS — start the controller (Terminal B):

```powershell
conda activate gcs-env
cd C:\Users\burak\Desktop\research
python tools/auto_test_gcs.py --listen-port 47010 --suite cs-mlkem512-aesgcm-mldsa44 --count 8 --udp-host 127.0.0.1 --udp-port 47001
```

This will print "Listening for drone control on port 47010..." and wait.

3) On Drone — connect and run (SSH/Bash):

```bash
cd ~/research
source ~/cenv/bin/activate
python3 tools/auto_test_drone.py --gcs-host 192.168.0.101 --gcs-port 47010
```

When the drone finishes, the GCS controller prints the results JSON.

Drone-first alternative (retry loop)
------------------------------------
If you prefer to start the drone first, run this on the Pi — it will retry until GCS is reachable:

```bash
until python3 tools/auto_test_drone.py --gcs-host 192.168.0.101 --gcs-port 47010; do
  echo "GCS not ready — retrying in 3s"
  sleep 3
done
```

Batching / matrix note
----------------------
- For full matrix runs use the `matrix_runner_gcs.ps1` and `matrix_runner_drone.sh` wrappers in `scripts/` which orchestrate `core.run_proxy` + `tools/traffic_*` per-suite and collect artifacts.

Troubleshooting quick checklist
-------------------------------
- If the drone cannot connect to the GCS controller: check firewall, ensure GCS is listening on the expected port, and verify `GCS_HOST` is set to the GCS LAN IP.
- If proxies show `enc_in/out == 0` but handshake succeeded: verify plaintext generators are sending to `*_PLAINTEXT_TX` and that plaintext hosts are loopback unless ALLOW_NON_LOOPBACK_PLAINTEXT=1 is set.
- If `oqs` imports fail: ensure `gcs-env` has the `oqs` binding installed. On Windows you might need a conda wheel or prebuilt `liboqs`.

Artifacts to collect
--------------------
- GCS and drone `*_debug.json` files (proxy counters)
- auto_test logs (stdout from the controller)
- Any `udp_forward_log.py` tap output if used

Contact points in code
----------------------
- `tools/auto_test_gcs.py` — controller
- `tools/auto_test_drone.py` — runner
- `tools/udp_echo.py` — plaintext echo helper
- `core/run_proxy.py` — proxy entrypoint
- `core/config.py` — ports/hosts default and validation

Change log
----------
- 2025-09-28: Playbook created and defaults updated to GCS=192.168.0.101, DRONE=192.168.0.102.


---

EOF
