# Tools Directory Analysis - October 2, 2025

## Overview
The `tools/` directory contains a comprehensive suite of utilities for testing, debugging, automation, and validation of the PQC drone-GCS secure proxy. Tools are organized into several categories: automation (`auto/`), manual testing (`manual_4term/`), traffic generation, network diagnostics, and static analysis.

---

## 📁 Directory Structure

```
tools/
├── auto/                          # Automated test orchestration
│   ├── gcs_scheduler_simple.py    # GCS-side rekey automation
│   ├── drone_follower_simple.py   # Drone-side echo server with control API
│   ├── gcs_scheduler.py           # Advanced scheduler with matrix support
│   ├── drone_follower.py          # Advanced follower
│   └── consolidate_json_logs.py   # Log aggregation utility
│
├── manual_4term/                  # Manual testing harness
│   ├── launch_manual_test.py      # Launch 4-terminal setup
│   ├── gcs_tty.py                 # Interactive GCS plaintext console
│   ├── drone_tty.py               # Interactive Drone plaintext console
│   ├── gcs_ground_station_sim.py  # GCS simulator (command generator)
│   ├── drone_autopilot_sim.py     # Drone simulator (telemetry generator)
│   ├── encrypted_bridge_logger.py # Ciphertext tap/logger
│   └── README.md                  # Manual testing documentation
│
├── Traffic Generation
│   ├── traffic_runner.py          # Shared traffic generator engine
│   ├── traffic_gcs.py             # GCS traffic generator CLI
│   ├── traffic_drone.py           # Drone traffic generator CLI
│   └── traffic_common.py          # Shared helpers (ports, sockets, logging)
│
├── Network Diagnostics
│   ├── encrypted_sniffer.py       # UDP packet sniffer (port-based)
│   ├── udp_forward_log.py         # UDP forwarder with PQC header parsing
│   ├── udp_echo.py                # Simple UDP echo server
│   ├── udp_echo_server.py         # Standalone echo server
│   ├── udp_dual_probe.py          # Bidirectional UDP probe
│   ├── diag_udp.py                # UDP diagnostics helper
│   └── packet_interceptor.py      # Packet inspection tool
│
├── Static Analysis & Validation
│   ├── check_no_hardcoded_ips.py  # Enforce config.py discipline
│   ├── check_suites.py            # Validate 21-suite catalog
│   ├── check_ports.py             # Port conflict detection
│   ├── check_matrix_keys.py       # Matrix key validation
│   └── audit_endpoints.py         # Endpoint audit utility
│
├── Utilities & Helpers
│   ├── counter_utils.py           # Parse proxy/traffic JSON counters
│   ├── socket_utils.py            # Socket helpers
│   ├── markers.py                 # Test markers utility
│   └── power_hooks.py             # Power measurement hooks
│
├── Infrastructure & Automation
│   ├── scaffold_repo.py           # Repo scaffolding tool
│   ├── generate_identity.py       # Key generation wrapper
│   ├── generate_env_report.py     # Environment report generator
│   ├── print_oqs_info.py          # oqs-python runtime info
│   ├── prepare_matrix_keys.py     # Generate keys for suite matrix
│   ├── copy_pubs_to_pi.py         # Deploy keys to Raspberry Pi
│   └── pi_check_env.sh            # Raspberry Pi environment check
│
├── Results & Reporting
│   ├── aggregate_lan_results.py   # LAN test aggregation
│   ├── report_saturation_summary.py # Saturation report generator
│   ├── merge_power_csv.py         # Power measurement CSV merger
│   └── full_comm_check.py         # End-to-end communication check
│
└── Scripts (Platform-specific)
    ├── matrix_runner_gcs.ps1      # PowerShell matrix runner (Windows)
    ├── matrix_runner_drone.sh     # Bash matrix runner (Linux)
    └── cleanup_bound_ports.py     # Release stuck ports
```

---

## 🤖 Automated Testing System (`auto/`)

### **`gcs_scheduler_simple.py`** - GCS Side Automation
**Purpose:** Orchestrates GCS proxy with automated rekey testing across all 21 suites.

**Key Features:**
- Launches GCS proxy with `--control-manual` (interactive rekey control)
- For each suite: triggers rekey → sends UDP probe → waits for echo → records result
- Exposes interactive mode for manual testing: `list`, `next`, `all`, `<suite-id>`, `quit`
- Auto mode: `--auto` runs full matrix sweep with configurable passes
- Control plane: Communicates with `drone_follower` via TCP JSON API (port 48080)

**Configuration (from `core.config`):**
```python
CONTROL_HOST = CONFIG["DRONE_HOST"]              # Drone control API host
CONTROL_PORT = 48080                             # Drone control API port
APP_SEND_PORT = CONFIG["GCS_PLAINTEXT_TX"]       # 47001
APP_RECV_PORT = CONFIG["GCS_PLAINTEXT_RX"]       # 47002
VERIFY_TIMEOUT_S = 5.0                           # Echo timeout
PACKETS_PER_SUITE = 1                            # Probes per suite
SUITE_DWELL_S = 0.0                              # Dwell time between suites
DEFAULT_PASSES = 1                               # Number of full sweeps
```

**Output Artifacts:**
- `logs/auto/{suite}/gcs_status.json` - Real-time proxy status (handshake, rekey)
- `logs/auto/{suite}/gcs_summary.json` - Final counters (ptx_in/out, enc_in/out, drops, rekeys)
- `logs/auto/quickpass_summary.csv` - Consolidated test results (ts, suite, packets, success, rtt, bytes)

**Control API Messages (to Drone):**
```json
{"cmd": "ping"}                      // Health check
{"cmd": "mark", "suite": "cs-..."}   // Mark suite transition
{"cmd": "stop"}                      // Shutdown signal
```

**Usage:**
```powershell
# Interactive mode (manual suite selection)
python tools/auto/gcs_scheduler_simple.py

# Automated sweep (all 21 suites, 1 pass)
python tools/auto/gcs_scheduler_simple.py --auto

# Automated with multiple passes (for reliability testing)
$Env:SIMPLE_AUTO_PASSES = "3"
python tools/auto/gcs_scheduler_simple.py --auto
```

**Error Handling:**
- **Proxy crash detection:** If GCS proxy dies (poll returns non-None), automatically restarts with last suite
- **Silent exception swallowing (BUG):** 4 bare `except: pass` clauses suppress errors (identified in bug scan)
  - Lines: stdin write failure, ctl() exceptions, proxy restart failure, introspection errors
  - **Recommendation:** Replace with explicit exception types + logging

---

### **`drone_follower_simple.py`** - Drone Side Automation
**Purpose:** Passive drone-side component that echoes UDP traffic and responds to control commands.

**Key Features:**
- Launches Drone proxy once with initial suite (GCS drives rekey)
- UDP echo server: `APP_RECV_PORT → APP_SEND_PORT` (47004 → 47003)
- TCP control API: Port 48080, accepts JSON commands
- Marker files: Creates `logs/auto/drone/marks/{timestamp}_{suite}.json` on `mark` command

**Control API (TCP JSON):**
```json
// Requests
{"cmd": "ping"}                      // Health check
{"cmd": "mark", "suite": "cs-..."}   // Log suite transition
{"cmd": "stop"}                      // Shutdown signal

// Responses
{"ok": true, "ts": "2025-10-02T..."}  // ping response
{"ok": true, "marked": "cs-..."}      // mark response
{"ok": true, "stopping": true}        // stop response
{"ok": false, "error": "unknown_cmd"} // error
```

**Architecture:**
- **Main thread:** Monitors stop event, manages proxy lifecycle
- **UDP echo thread (daemon):** Bidirectional forwarder (recv → send)
- **Control server thread (daemon):** TCP listener (0.0.0.0:48080)

**Usage:**
```bash
# Start drone follower (usually on Raspberry Pi or remote host)
python tools/auto/drone_follower_simple.py
```

**Output Artifacts:**
- `logs/auto/drone/status.json` - Real-time proxy status
- `logs/auto/drone/summary.json` - Final counters
- `logs/auto/drone/marks/{ts}_{suite}.json` - Suite transition markers
- `logs/auto/drone/drone_{timestamp}.log` - Proxy logs

**Deployment Pattern:**
```bash
# On Drone host (192.168.0.102)
python tools/auto/drone_follower_simple.py &

# On GCS host (192.168.0.103)
python tools/auto/gcs_scheduler_simple.py --auto
```

---

## 🎮 Manual Testing Harness (`manual_4term/`)

### **Architecture Overview**
The manual testing harness provides a **4-terminal setup** for interactive proxy testing:
1. **GCS Proxy** - Server role, handles handshakes, encrypts/decrypts
2. **Drone Proxy** - Client role, connects to GCS, encrypts/decrypts
3. **GCS Ground Station Sim** - Sends commands, receives telemetry
4. **Drone Autopilot Sim** - Sends telemetry, receives commands

Optional 5th terminal: **Encrypted Bridge Logger** - Intercepts ciphertext for inspection

**Port Map:**
| Purpose | Port | Description |
|---------|------|-------------|
| TCP Handshake | 46000 | PQC key exchange |
| GCS Encrypted UDP | 46011 | GCS receives encrypted packets |
| Drone Encrypted UDP | 46012 | Drone receives encrypted packets |
| Intercept D→G | 46001 | Logger listens (Drone→GCS) |
| Intercept G→D | 46002 | Logger listens (GCS→Drone) |
| GCS Plaintext TX | 47001 | App sends commands to GCS proxy |
| GCS Plaintext RX | 47002 | App receives telemetry from GCS proxy |
| Drone Plaintext TX | 47003 | App sends telemetry to Drone proxy |
| Drone Plaintext RX | 47004 | App receives commands from Drone proxy |

---

### **`launch_manual_test.py`** - Orchestrator
**Purpose:** One-command launcher for all 4 (or 5) components.

**Features:**
- Auto-generates GCS signing keys if missing (`secrets/gcs_signing.{key,pub}`)
- Launches all processes with environment overrides (port remapping)
- Optional `--with-intercept` for ciphertext logging
- `--new-windows` on Windows spawns separate console windows
- `Ctrl+C` terminates all child processes

**Usage:**
```powershell
# Basic 4-terminal setup
python tools/manual_4term/launch_manual_test.py --suite cs-mlkem768-aesgcm-mldsa65

# With encrypted traffic logger
python tools/manual_4term/launch_manual_test.py --with-intercept

# New Windows console windows (easier debugging)
python tools/manual_4term/launch_manual_test.py --new-windows
```

---

### **`gcs_tty.py` / `drone_tty.py`** - Interactive Plaintext Consoles
**Purpose:** Manual UDP input/output for driving proxies interactively.

**Features:**
- **gcs_tty:** Sends to 47001, receives from 47002 (commands ↔ telemetry)
- **drone_tty:** Sends to 47003, receives from 47004 (telemetry ↔ commands)
- Line-based input (4096 byte max per datagram)
- `--expect N` exits after N received lines (scripting support)
- `--verbose` prints debug info to stderr
- Dual-threaded: stdin sender + UDP receiver in parallel

**Usage:**
```powershell
# GCS terminal (type commands, see telemetry)
python tools/manual_4term/gcs_tty.py --expect 0

# Drone terminal (type telemetry, see commands)
python tools/manual_4term/drone_tty.py --expect 0
```

**Interactive Example:**
```
# In gcs_tty.py terminal:
> SET_MODE LOITER
> ARM_MOTORS true
> REQUEST_TELEMETRY all

# In drone_tty.py terminal (you'll see decrypted commands)
< {"cmd": "SET_MODE", "mode": "LOITER"}
< {"cmd": "ARM_MOTORS", "state": true}
< {"cmd": "REQUEST_TELEMETRY", "scope": "all"}

# Type telemetry response:
> {"lat": 37.7749, "lon": -122.4194, "alt": 100.5, "batt": 85}

# In gcs_tty.py terminal (you'll see decrypted telemetry)
< {"lat": 37.7749, "lon": -122.4194, "alt": 100.5, "batt": 85}
```

---

### **`encrypted_bridge_logger.py`** - Ciphertext Tap
**Purpose:** Passive inspection of encrypted UDP traffic while maintaining connectivity.

**Architecture:**
```
Drone:46012 ──┐
              ├──> Logger:46001 ──> GCS:46011 (Drone→GCS path)
GCS:46011  ──┘

GCS:46011  ──┐
              ├──> Logger:46002 ──> Drone:46012 (GCS→Drone path)
Drone:46012 ──┘
```

**Output Format:**
```
[12:34:56][tap] 87B from 192.168.0.102:46012 hdr={'version': 1, 'kem': (1, 1), 'sig': (2, 2), 'session_id': '3a7f...', 'seq': 42, 'epoch': 0}
[12:34:57][tap] 104B from 192.168.0.103:46011 hdr={'version': 1, 'kem': (1, 1), 'sig': (2, 2), 'session_id': '3a7f...', 'seq': 43, 'epoch': 0}
```

**Usage:**
```powershell
python tools/manual_4term/encrypted_bridge_logger.py `
  --d2g-listen 46001 --d2g-forward 127.0.0.1:46011 `
  --g2d-listen 46002 --g2d-forward 127.0.0.1:46012
```

**Debugging Value:**
- Validates encrypted traffic is flowing (non-zero bytes)
- Inspects sequence numbers (detects gaps/replays)
- Monitors epoch transitions during rekey
- Identifies session_id mismatches (stale packets post-rekey)

---

## 🚦 Traffic Generation

### **`traffic_runner.py`** - Core Traffic Engine
**Purpose:** Shared engine for automated UDP plaintext traffic generation with JSON payloads.

**Features:**
- Sends `--count` packets at `--rate` packets/sec (token bucket rate limiting)
- Each payload: `{"role": "gcs"|"drone", "seq": N, "t_send_ns": timestamp}`
- Optional `--payload-bytes` for throughput testing (appends padding bytes)
- Receives echoed packets and validates sequence ordering
- Tracks out-of-order packets, unique senders, RTT, throughput

**Output Artifacts:**
- **NDJSON event log:** `logs/{role}_traffic_{timestamp}.jsonl` (per-packet events)
- **JSON summary:** `logs/{role}_traffic_summary_{timestamp}.json` (aggregate stats)

**Summary Schema:**
```json
{
  "role": "gcs",
  "peer_role": "drone",
  "sent_total": 200,
  "recv_total": 198,
  "tx_bytes_total": 45600,
  "rx_bytes_total": 45200,
  "first_send_ts": "2025-10-02T12:00:00.123456Z",
  "last_send_ts": "2025-10-02T12:00:04.987654Z",
  "first_recv_ts": "2025-10-02T12:00:00.234567Z",
  "last_recv_ts": "2025-10-02T12:00:05.123456Z",
  "out_of_order": 2,
  "unique_senders": 1
}
```

---

### **`traffic_gcs.py` / `traffic_drone.py`** - CLI Wrappers
**Purpose:** Thin wrappers around `traffic_runner.py` with role-specific defaults.

**Usage:**
```powershell
# GCS side: send 1000 packets at 100 pps for 10 seconds
python tools/traffic_gcs.py --count 1000 --rate 100 --duration 10 --out logs/gcs_test.jsonl

# Drone side: echo traffic, 200 packets at 50 pps
python tools/traffic_drone.py --count 200 --rate 50 --out logs/drone_test.jsonl

# High-throughput test with 1KB payloads
python tools/traffic_gcs.py --count 10000 --rate 500 --payload-bytes 1024
```

---

### **`traffic_common.py`** - Shared Helpers
**Purpose:** Utility functions for traffic generators.

**Key Exports:**
- `load_ports_and_hosts(role)` - Resolve ports from `core.config.CONFIG`
- `open_udp_socket(bind_addr)` - Create non-blocking UDP socket
- `ndjson_logger(path)` - NDJSON file logger with flush
- `TokenBucket(rate_per_sec)` - Rate limiter
- `configured_selector(sock)` - Selectors setup

**Port Resolution Logic:**
```python
# For role="gcs":
tx_addr = (CONFIG["GCS_PLAINTEXT_HOST"], CONFIG["GCS_PLAINTEXT_TX"])  # 127.0.0.1:47001
rx_bind = (CONFIG["GCS_PLAINTEXT_HOST"], CONFIG["GCS_PLAINTEXT_RX"])  # 127.0.0.1:47002

# For role="drone":
tx_addr = (CONFIG["DRONE_PLAINTEXT_HOST"], CONFIG["DRONE_PLAINTEXT_TX"])  # 127.0.0.1:47003
rx_bind = (CONFIG["DRONE_PLAINTEXT_HOST"], CONFIG["DRONE_PLAINTEXT_RX"])  # 127.0.0.1:47004
```

---

## 🔍 Network Diagnostics

### **`encrypted_sniffer.py`** - Simple Packet Sniffer
**Purpose:** Quick verification that UDP traffic is flowing on a specific port.

**Usage:**
```powershell
# Sniff GCS encrypted port
python tools/encrypted_sniffer.py 46011

# Output:
# [12:34:56] Packet #1: Received 87 bytes from 192.168.0.102:46012 | Data (hex): 0101010202...
```

**Use Cases:**
- Validate proxies are sending/receiving encrypted traffic
- Debug firewall/NAT issues (traffic reaching correct ports)
- Confirm DSCP marking (use Wireshark to verify TOS byte = 0xB8 for DSCP 46)

---

### **`udp_forward_log.py`** - Header-Parsing Forwarder
**Purpose:** Transparent UDP forwarder that logs PQC header fields without decryption.

**Features:**
- Parses 22-byte header: version, kem_id, kem_param, sig_id, sig_param, session_id, seq, epoch
- Forwards packets unchanged (preserves ciphertext integrity)
- Useful for **drop classification debugging** (pre-decrypt inspection)

**Usage:**
```powershell
# Insert between Drone and GCS encrypted ports
python tools/udp_forward_log.py `
  --listen 0.0.0.0:56012 `
  --forward 127.0.0.1:46012 `
  --label drone-tap
```

**Output Example:**
```
[12:34:56][drone-tap] 87B from 192.168.0.102:12345 hdr={'version': 1, 'kem': (1, 1), 'sig': (2, 2), 'session_id': '3a7f2b4e...', 'seq': 42, 'epoch': 0}
```

**Debugging Scenarios:**
- **Epoch mismatch:** Old packets arrive with stale epoch after rekey
- **Session mismatch:** Packets from previous handshake still in flight
- **Header corruption:** version != 1, kem/sig IDs don't match suite catalog
- **Sequence analysis:** Detect gaps (packet loss) or duplicates (retransmission)

---

### **`udp_echo.py` / `udp_echo_server.py`** - Simple Echo Servers
**Purpose:** Minimal UDP echo for basic connectivity testing.

**Usage:**
```powershell
# Start echo server on port 9999
python tools/udp_echo_server.py 9999

# Send test packet
echo "hello" | nc -u localhost 9999
```

---

## ✅ Static Analysis & Validation

### **`check_no_hardcoded_ips.py`** - Configuration Discipline Enforcer
**Purpose:** Static analysis to prevent hardcoded IPs/ports outside `core/config.py`.

**Rules:**
- **Allowed IPs:** `0.0.0.0`, `127.0.0.1`, `::1` (loopback only)
- **Allowed Ports:** `0` (ephemeral), `53` (DNS)
- **Scans:** All `.py`, `.ps1`, `.sh` files (except `core/config.py`, tests, venv)
- **Violations:** Any non-loopback IP literal or `socket.bind/connect/sendto` with numeric port

**Usage:**
```powershell
# Run static check (CI requirement)
python tools/check_no_hardcoded_ips.py

# Expected output:
# No hard-coded IPs or forbidden port literals detected.
```

**Why This Matters:**
- Prevents accidental commit of dev IP addresses (192.168.x.x, 10.x.x.x)
- Ensures all network config comes from `core.config.CONFIG` (environment-overridable)
- Simplifies deployment across different networks (LAN, cloud, airgapped)

---

### **`check_suites.py`** - Suite Catalog Validator
**Purpose:** Verify all 21 expected suites are registered in `core/suites.py`.

**Expected Suites (3 KEMs × 7 Signatures):**
```python
# 21 total combinations:
# ML-KEM: mlkem512, mlkem768, mlkem1024
# Signatures: mldsa44, mldsa65, mldsa87, falcon512, falcon1024, sphincs128fsha2, sphincs256fsha2

wanted = [
    "cs-mlkem512-aesgcm-mldsa44",    # L1 + L2 sig
    "cs-mlkem512-aesgcm-mldsa65",    # L1 + L3 sig
    "cs-mlkem512-aesgcm-mldsa87",    # L1 + L5 sig
    # ... (18 more)
]
```

**Usage:**
```powershell
python tools/check_suites.py

# Output:
# missing: []
# total registry suites: 21
```

---

### **`check_ports.py` / `check_matrix_keys.py` / `audit_endpoints.py`**
**Purpose:** Additional validation utilities for port conflicts, key integrity, endpoint reachability.

---

## 🛠️ Utilities & Helpers

### **`counter_utils.py`** - JSON Counter Parser
**Purpose:** Type-safe parsing of proxy and traffic counter artifacts.

**Key Classes:**
```python
@dataclass
class ProxyCounters:
    role: str                # "gcs" or "drone"
    suite: str               # Suite ID
    counters: Dict[str, Any] # Raw counters
    ts_stop_ns: Optional[int]
    path: Optional[Path]
    
    @property
    def rekeys_ok(self) -> int
    @property
    def rekeys_fail(self) -> int
    @property
    def last_rekey_suite(self) -> Optional[str]
    
    def ensure_rekey(self, expected_suite: str) -> None
        # Validates successful rekey to expected suite

@dataclass
class TrafficSummary:
    role: str
    sent_total: int
    recv_total: int
    tx_bytes_total: int
    rx_bytes_total: int
    out_of_order: int
    unique_senders: int
    # ... timestamps
```

**Usage:**
```python
from tools.counter_utils import load_proxy_counters, load_traffic_summary

# Load proxy counters
counters = load_proxy_counters("logs/auto/gcs_summary.json")
print(f"Role: {counters.role}, Suite: {counters.suite}")
print(f"Rekeys OK: {counters.rekeys_ok}, Failed: {counters.rekeys_fail}")
counters.ensure_rekey("cs-mlkem1024-aesgcm-falcon1024")  # Validates rekey

# Load traffic summary
traffic = load_traffic_summary("logs/gcs_traffic_summary.json")
print(f"Sent: {traffic.sent_total}, Recv: {traffic.recv_total}")
print(f"Out of order: {traffic.out_of_order}")
```

**Integration Points:**
- `scripts/orchestrate_e2e.py` uses these for validation
- `tools/aggregate_lan_results.py` merges multiple counter files
- Test suite uses `ensure_rekey()` for automated rekey verification

---

## 📊 Results & Reporting

### **`aggregate_lan_results.py`** - Multi-Run Aggregation
**Purpose:** Merge counter files from multiple LAN test runs into unified reports.

### **`report_saturation_summary.py`** - Saturation Analysis
**Purpose:** Analyze high-throughput test results for packet loss, jitter, throughput limits.

### **`merge_power_csv.py`** - Power Measurement Integration
**Purpose:** Merge power measurement CSV files with timing logs for energy-per-packet analysis.

---

## 🚀 Infrastructure & Automation

### **`prepare_matrix_keys.py`** - Matrix Key Generation
**Purpose:** Pre-generate GCS signing keypairs for all 21 suites.

**Output Structure:**
```
secrets/matrix/
├── cs-mlkem512-aesgcm-mldsa44/
│   ├── gcs_signing.key
│   └── gcs_signing.pub
├── cs-mlkem512-aesgcm-mldsa65/
│   ├── gcs_signing.key
│   └── gcs_signing.pub
└── ... (19 more suite directories)
```

**Usage:**
```powershell
# Generate keys for all suites
python tools/prepare_matrix_keys.py

# Deploy to Raspberry Pi
python tools/copy_pubs_to_pi.py --host 192.168.0.102
```

---

### **`generate_env_report.py`** - Environment Inspector
**Purpose:** Collect system info for bug reports (OS, Python version, oqs availability, network interfaces).

**Usage:**
```powershell
python tools/generate_env_report.py > docs/env_report.md
```

---

### **`print_oqs_info.py`** - oqs-python Runtime Info
**Purpose:** Display available KEM/Signature algorithms from liboqs.

**Usage:**
```powershell
python tools/print_oqs_info.py

# Output:
# Available KEMs:
#   ML-KEM-512, ML-KEM-768, ML-KEM-1024
# Available Signatures:
#   ML-DSA-44, ML-DSA-65, ML-DSA-87, Falcon-512, Falcon-1024, SPHINCS+-SHA2-128f-simple, ...
```

---

## 🎯 Key Takeaways

### **Testing Workflows**
1. **Quick Validation (5 min):**
   ```powershell
   python tools/manual_4term/launch_manual_test.py --suite cs-mlkem768-aesgcm-mldsa65
   # Observe bidirectional traffic in 4 terminals
   ```

2. **Automated Matrix Test (30 min):**
   ```powershell
   # On Drone host:
   python tools/auto/drone_follower_simple.py &
   
   # On GCS host:
   python tools/auto/gcs_scheduler_simple.py --auto
   ```

3. **LAN Two-Host Test (10 min):**
   ```powershell
   python scripts/orchestrate_e2e.py --drone-host 192.168.0.102 --gcs-host 192.168.0.103 --suite cs-mlkem768-aesgcm-mldsa65 --duration 60
   ```

### **Debugging Patterns**
- **No encrypted traffic?** → `encrypted_sniffer.py` on both ports (46011, 46012)
- **Handshake failing?** → Check `logs/{role}-*.log` for signature verification errors
- **Packets dropping?** → `udp_forward_log.py` to inspect headers pre-decrypt
- **Rekey not working?** → Check `logs/auto/quickpass_summary.csv` for suite transition failures
- **Port conflicts?** → `check_ports.py` or `cleanup_bound_ports.py`

### **Configuration Discipline**
- **Always** source ports/hosts from `core.config.CONFIG`
- **Never** hardcode IPs (except 127.0.0.1, 0.0.0.0) outside `core/config.py`
- **Validate** with `check_no_hardcoded_ips.py` before committing
- **Override** via environment variables for deployment flexibility

### **Counter Forensics**
Use `counter_utils.py` to extract:
- **Rekey success rate:** `counters.rekeys_ok / (counters.rekeys_ok + counters.rekeys_fail)`
- **Packet loss rate:** `counters.drops / counters.enc_in`
- **Drop breakdown:** `drop_auth`, `drop_replay`, `drop_header`, `drop_session_epoch`
- **Throughput:** `traffic.tx_bytes_total / duration_seconds`

---

## 🐛 Known Issues (from Bug Scan)

### **Silent Exception Swallowing in `gcs_scheduler_simple.py`**
**Lines:** ~82, ~86, ~92, ~99  
**Impact:** Errors hidden during proxy restart, control API calls, stdin writes  
**Fix:** Replace bare `except: pass` with explicit exception types + logging

### **Buffer Size in Traffic Generators**
**Current:** `recvfrom(4096)` in `traffic_runner.py`  
**Impact:** Sufficient for typical payloads but may truncate SPHINCS+ signatures  
**Status:** Lower priority (traffic generators use plaintext side, no AEAD overhead)

---

## 📚 References

- **Manual Testing Guide:** `tools/manual_4term/README.md`
- **LAN Test Procedure:** `docs/lan-test.txt`
- **Rekey Protocol:** `docs/RUNTIME_SUITE_SWITCHING.md`
- **Port Mapping:** `docs/portss-and-networking.txt`
- **Counter Schema:** `tools/counter_utils.py` docstrings

---

**Analysis Completed By:** AI Coding Agent (GitHub Copilot)  
**Date:** October 2, 2025  
**Status:** ✅ Comprehensive tools directory scan complete
