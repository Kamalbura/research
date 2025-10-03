# Deep Analysis: `tools/auto/` - Automated Testing Infrastructure
## October 2, 2025

---

## 📋 **Executive Summary**

The `tools/auto/` directory contains a sophisticated **two-host orchestration system** for automated PQC proxy testing. The architecture follows a **scheduler-follower pattern** where the GCS (ground station) acts as the **orchestrator** and the Drone (Pi/embedded device) acts as the **responder**.

**Evolution Path:** The directory shows clear progression from simple to advanced:
1. **`*_simple.py`** - Minimal implementations for quick testing
2. **`*_quickpass.py`** - Fast validation sweep across all suites
3. **Full implementations** - Production-grade with telemetry, monitoring, saturation testing

---

## 🏗️ **Architecture Overview**

### **Communication Model**

```
┌────────────────────────────────────────────────────────────────────┐
│                     GCS Host (192.168.0.103)                       │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │         GCS Scheduler (gcs_scheduler.py)                │     │
│  │                                                          │     │
│  │  1. Launches GCS proxy with --control-manual           │     │
│  │  2. TCP Control API → Drone (JSON over port 48080)     │     │
│  │  3. UDP Traffic Generator (Blaster)                    │     │
│  │     - Sends: 127.0.0.1:47001 (GCS_PLAINTEXT_TX)       │     │
│  │     - Receives: 127.0.0.1:47002 (GCS_PLAINTEXT_RX)    │     │
│  │  4. Telemetry Collector (TCP server port 52080)       │     │
│  │  5. Clock Sync (NTP-like protocol)                    │     │
│  │  6. Excel Export (openpyxl)                           │     │
│  └─────────────────────────────────────────────────────────┘     │
│                            │                                       │
│                            │ TCP Control (48080)                   │
│                            │ ┌──────────────────────────┐          │
│                            └─│ {"cmd": "ping"}          │          │
│                              │ {"cmd": "mark", "suite"} │          │
│                              │ {"cmd": "timesync"}      │          │
│                              │ {"cmd": "stop"}          │          │
│                              └──────────────────────────┘          │
└────────────────────────────────────────────────────────────────────┘
                                       ↕
                        Encrypted UDP (46011 ↔ 46012)
                                       ↕
┌────────────────────────────────────────────────────────────────────┐
│                    Drone Host (192.168.0.102)                      │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │        Drone Follower (drone_follower.py)               │     │
│  │                                                          │     │
│  │  1. Launches Drone proxy (passive, GCS initiates rekey)│     │
│  │  2. Control Server (TCP port 48080)                    │     │
│  │     - Responds to: ping, mark, timesync, stop          │     │
│  │  3. UDP Echo Server                                    │     │
│  │     - Receives: 127.0.0.1:47004 (DRONE_PLAINTEXT_RX)  │     │
│  │     - Sends: 127.0.0.1:47003 (DRONE_PLAINTEXT_TX)     │     │
│  │  4. Performance Monitors (if enabled)                  │     │
│  │     - perf stat (HW counters)                          │     │
│  │     - pidstat (CPU/memory)                             │     │
│  │     - psutil (Python process metrics)                  │     │
│  │     - Thermal monitoring (vcgencmd, Raspberry Pi)      │     │
│  │  5. High-speed system monitor (100ms sampling)         │     │
│  │  6. Telemetry Publisher (TCP to GCS:52080)            │     │
│  └─────────────────────────────────────────────────────────┘     │
└────────────────────────────────────────────────────────────────────┘
```

---

## 📂 **File Inventory**

| File | Lines | Type | Purpose |
|------|-------|------|---------|
| `gcs_scheduler.py` | 1050 | Production | Full-featured GCS orchestrator with saturation testing |
| `gcs_scheduler_simple.py` | 230 | Minimal | Quick validation testing |
| `gcs_scheduler_quickpass.py` | 165 | Fast | Rapid suite sweep |
| `drone_follower.py` | 1180 | Production | Full monitoring + telemetry |
| `drone_follower_simple.py` | 88 | Minimal | Basic echo + control API |
| `consolidate_json_logs.py` | 95 | Utility | Merge JSON logs into single file |

**Duplicate files (marked with `copy.py`):** Likely backup versions or work-in-progress variants.

---

## 🎯 **GCS Scheduler Deep Dive**

### **`gcs_scheduler.py`** - Production Orchestrator

**Key Features:**
1. **Three Traffic Modes:**
   - `blast` - High-rate UDP blaster with RTT sampling
   - `mavproxy` - Reserved for MAVLink integration (placeholder)
   - `saturation` - Automated capacity testing (ramp rates until RTT spike)

2. **Blaster Class** (lines 129-258):
   ```python
   class Blaster:
       """High-rate UDP blaster with RTT sampling and throughput accounting."""
       
       Features:
       - Token bucket rate limiting OR burst mode (rate_pps=0)
       - Selective event logging (sample_every=N to reduce overhead)
       - Clock offset adjustment (for cross-host time sync)
       - Socket buffer tuning (env: GCS_SOCK_SNDBUF, GCS_SOCK_RCVBUF)
       - Dual-threaded: main sends, background receives
       - Pending RTT tracking (seq → t_send mapping)
   ```

   **Packet Format:**
   ```
   [seq:4B][t_send_ns:8B][padding...]
   - seq: uint32 big-endian
   - t_send_ns: int64 big-endian (sender timestamp)
   - padding: zeros to reach payload_bytes
   ```

   **RTT Calculation:**
   ```python
   t_recv = time.time_ns() + offset_ns
   t_send = pending.pop(seq)
   rtt = t_recv - t_send  # nanoseconds
   ```

3. **Clock Synchronization** (lines 332-341):
   ```python
   def timesync() -> dict:
       """NTP-like protocol for clock offset calculation."""
       t1 = time.time_ns()  # GCS sends
       resp = ctl_send({"cmd": "timesync", "t1_ns": t1})
       t4 = time.time_ns()  # GCS receives
       t2 = resp.get("t2_ns")  # Drone receives
       t3 = resp.get("t3_ns")  # Drone sends
       
       delay_ns = (t4 - t1) - (t3 - t2)  # Network RTT
       offset_ns = ((t2 - t1) + (t3 - t4)) // 2  # Clock offset
   ```

   **Why This Matters:**
   - Accurate RTT measurements require synchronized clocks
   - Offset applied to all timestamps in Blaster
   - Enables sub-millisecond timing correlation across hosts

4. **Saturation Testing** (lines 691-798):
   ```python
   class SaturationTester:
       """Automated capacity testing via binary search."""
       
       Algorithm:
       1. Start at SATURATION_TEST_RATES = [5, 10, 15, ..., 200] Mbps
       2. For each rate, run fixed-duration test (default 45s)
       3. Measure: throughput_mbps, loss_pct, avg_rtt_ms, max_rtt_ms
       4. Detect saturation:
          - If avg_rtt > baseline_rtt * 1.8 → saturated
          - If achieved < rate * 0.8 → saturated
       5. Export to Excel (one sheet per suite)
   ```

   **Saturation Point Detection:**
   - **Baseline RTT:** First successful rate establishes baseline
   - **Spike Threshold:** `SATURATION_RTT_SPIKE = 1.8` (80% increase)
   - **Throughput Gap:** If achieved is <80% of target rate → link saturated

5. **Telemetry Collection** (lines 800-918):
   ```python
   class TelemetryCollector:
       """TCP server that aggregates telemetry from drone follower."""
       
       Protocol:
       - Line-delimited JSON over TCP
       - Drone publishes: system_sample, perf_sample, udp_echo_sample
       - GCS stores in memory, exports to Excel at end
       
       Threading:
       - Main thread: accept() loop
       - Per-client thread: readline() loop
       - Lock-protected samples list (aggregates all clients)
   ```

6. **Excel Export** (lines 920-1020):
   ```python
   def export_combined_excel(session_id, ...):
       """Master workbook with multiple sheets."""
       
       Sheets:
       - run_info: metadata (timestamp, session_id, drone_session_dir)
       - gcs_summary: main results table
       - saturation_overview: saturation test summary
       - saturation_samples: all rate samples
       - telemetry_samples: system/perf metrics from drone
       - gcs_summary_csv: CSV import
       - [drone CSVs]: auto-discovered from output/drone/{session_id}/
   ```

   **Smart Drone Session Discovery:**
   ```python
   candidates = [
       DRONE_MONITOR_BASE / session_id,          # Config path
       "/home/dev/research/output/drone/{sid}",  # Pi default
       ROOT / "output/drone/{sid}",              # Repo default
   ]
   # First existing path wins
   ```

7. **Control Protocol** (lines 104-122):
   ```python
   def ctl_send(obj: dict, timeout=2.0, retries=4, backoff=0.5):
       """Resilient JSON-over-TCP control client."""
       
       Retry Strategy:
       - 4 attempts with exponential backoff (0.5s, 1.0s, 1.5s, 2.0s)
       - Timeout per attempt: 2.0s
       - Socket lifetime: create → send → shutdown(WR) → read → close
   ```

   **Message Types:**
   ```json
   // Health check
   {"cmd": "ping"} → {"ok": true, "ts": "2025-10-02T12:00:00Z"}
   
   // Suite transition marker
   {"cmd": "mark", "suite": "cs-mlkem768-aesgcm-mldsa65"}
   → {"ok": true, "marked": "cs-mlkem768-aesgcm-mldsa65"}
   
   // Clock sync
   {"cmd": "timesync", "t1_ns": 1696248000000000000}
   → {"ok": true, "t1_ns": ..., "t2_ns": ..., "t3_ns": ...}
   
   // Schedule future marker (for power measurements)
   {"cmd": "schedule_mark", "suite": "...", "t0_ns": 1696248010000000000}
   → {"ok": true, "scheduled": "...", "t0_ns": ...}
   
   // Rekey completion notification
   {"cmd": "rekey_complete", "suite": "...", "status": "ok"}
   → {"ok": true}
   
   // Shutdown
   {"cmd": "stop"} → {"ok": true, "stopping": true}
   ```

8. **Main Workflow** (lines 1020-1050):
   ```python
   1. Parse CLI args (--traffic, --duration, --rate, --suites, --passes)
   2. Resolve suite list (from args or full catalog)
   3. Reorder to start with preferred initial suite (from CONFIG)
   4. Ping follower (8 retries over 4 seconds)
   5. Timesync for clock offset
   6. Start telemetry collector (TCP server)
   7. Launch GCS proxy with --control-manual
   8. Wait for handshake ready (poll status file)
   9. For each pass:
      For each suite:
          - activate_suite() → rekey via stdin
          - run_suite() → send traffic, measure RTT
          - snapshot_proxy_artifacts() → copy status/summary JSONs
          - inter_gap sleep (default 15s)
   10. Write summary CSV
   11. Export combined Excel workbook
   12. Stop follower via control API
   13. Terminate GCS proxy (stdin "quit\n" or kill)
   ```

---

## 🤖 **Drone Follower Deep Dive**

### **`drone_follower.py`** - Production Responder

**Key Features:**

1. **CPU Optimization** (lines 50-70):
   ```python
   def optimize_cpu_performance(target_khz=1800000):
       """Force CPU governor to 'performance' mode."""
       
       For each CPU core:
       - Set /sys/.../scaling_governor → "performance"
       - Set /sys/.../scaling_min_freq → 1800 MHz
       - Set /sys/.../scaling_max_freq → max(current, 1800 MHz)
       
       Why: Prevents CPU throttling during crypto operations
       Caveat: Requires sudo/root privileges
   ```

2. **Telemetry Publisher** (lines 100-170):
   ```python
   class TelemetryPublisher(threading.Thread):
       """Best-effort TCP stream to GCS telemetry collector."""
       
       Features:
       - Queue-based (maxlen=5000, drops oldest on overflow)
       - Auto-reconnect with exponential backoff (1s → 5s max)
       - Persistent connection (write-only makefile)
       - JSON message format:
         {
           "session_id": "...",
           "kind": "system_sample"|"perf_sample"|"udp_echo_sample"|...,
           "timestamp_ns": ...,
           ...payload...
         }
   ```

3. **High-Speed System Monitor** (lines 230-350):
   ```python
   class HighSpeedMonitor(threading.Thread):
       """100ms sampling for CPU/memory/temp/freq."""
       
       CSV Columns:
       - timestamp_iso, timestamp_ns
       - suite (current cryptographic suite)
       - proxy_pid
       - cpu_percent (psutil)
       - cpu_freq_mhz (read from /sys/.../scaling_cur_freq)
       - cpu_temp_c (vcgencmd measure_temp, Pi-specific)
       - mem_used_mb, mem_percent (psutil)
       - rekey_duration_ms (if rekey in progress)
       
       Rekey Tracking:
       - start_rekey(old, new) → records rekey_start_ns
       - end_rekey() → calculates duration, publishes telemetry
       - Scheduler calls via control API: mark → start, rekey_complete → end
   ```

4. **UDP Echo with Packet Annotation** (lines 350-450):
   ```python
   class UdpEcho(threading.Thread):
       """Echoes UDP packets with timestamp annotation."""
       
       Packet Processing:
       1. recvfrom() → recv_ns = time.time_ns()
       2. Annotate: append recv_ns as 8-byte big-endian suffix
       3. sendto() → send_ns = time.time_ns()
       4. Record: CSV with (recv_ns, send_ns, processing_ns, seq)
       
       Sampling: Every 100th packet logged (seq % 100 == 0)
       
       Socket Tuning:
       - DRONE_SOCK_SNDBUF, DRONE_SOCK_RCVBUF (default 16 MB)
       - Non-blocking with 0.001s timeout
   ```

5. **Performance Monitors** (lines 450-750):
   ```python
   class Monitors:
       """Aggregates multiple profiling tools."""
       
       Tools:
       1. perf stat:
          - Events: task-clock, cycles, instructions, cache-misses,
                    branch-misses, context-switches, branches
          - Interval: 1000ms (-I 1000)
          - CSV output (-x ,)
          - Background thread parses stdout, aggregates per-second samples
       
       2. pidstat:
          - Flags: -hlur (human-readable, threads, CPU, memory)
          - Interval: 1s
          - Raw output to .txt file (legacy parity)
       
       3. psutil:
          - cpu_percent(interval=None) → non-blocking
          - memory_info().rss → resident set size
          - num_threads()
          - 1s loop, CSV output
       
       4. Thermal/Frequency (Pi-specific):
          - vcgencmd measure_temp → CPU temp (°C)
          - vcgencmd measure_clock arm → CPU freq (Hz)
          - vcgencmd get_throttled → throttling flags (hex)
          - 1s loop, CSV output
       
       Lifecycle:
       - start(pid, outdir, suite) → spawn all tools
       - rotate(pid, outdir, suite) → stop + start with new suite
       - stop() → kill all tools, join threads, close CSVs
   ```

   **perf Parsing Logic:**
   ```python
   # perf output format: time,value,unit,event_name,run_time,pct
   # Example: 1.000123456,12345678,,cycles,1000000000,100.00
   
   Algorithm:
   - Parse CSV line-by-line
   - Group by time offset (±0.5ms tolerance)
   - Aggregate all events for that second into one CSV row
   - Flush row when next timestamp arrives
   ```

6. **Control Server** (lines 750-950):
   ```python
   class ControlServer(threading.Thread):
       """TCP JSON-RPC server for GCS scheduler."""
       
       Handlers:
       
       ping:
           Returns: {"ok": true, "ts": "2025-10-02T..."}
       
       timesync:
           t1 = request["t1_ns"]  # GCS send time
           t2 = time.time_ns()    # Drone receive time
           t3 = time.time_ns()    # Drone send time
           Returns: {"ok": true, "t1_ns": t1, "t2_ns": t2, "t3_ns": t3}
       
       status:
           proxy = state["proxy"]
           running = bool(proxy and proxy.poll() is None)
           Returns: {
               "ok": true,
               "suite": current_suite,
               "proxy_pid": proxy.pid,
               "running": running,
               "control_host": ...,
               "control_port": ...,
               "udp_recv_port": 47004,
               "udp_send_port": 47003,
               "monitors_enabled": true|false
           }
       
       mark:
           old_suite = state["suite"]
           new_suite = request["suite"]
           state["suite"] = new_suite
           monitors.rotate(proxy.pid, outdir, new_suite)
           high_speed_monitor.start_rekey(old_suite, new_suite)
           write_marker(new_suite)  # Filesystem timestamp marker
           Returns: {"ok": true, "marked": new_suite}
       
       rekey_complete:
           high_speed_monitor.end_rekey()  # Stop rekey timer
           Returns: {"ok": true}
       
       schedule_mark:
           suite = request["suite"]
           t0_ns = request["t0_ns"]  # Future timestamp
           # Launch background thread:
           delay = (t0_ns - time.time_ns()) / 1e9
           sleep(delay)
           monitors.rotate(proxy.pid, outdir, suite)
           Returns: {"ok": true, "scheduled": suite, "t0_ns": t0_ns}
       
       stop:
           monitors.stop()
           state["stop_event"].set()
           Returns: {"ok": true, "stopping": true}
   ```

7. **Main Workflow** (lines 950-1180):
   ```python
   1. Parse CLI args (--initial-suite, --disable-monitors, --session-id)
   2. Discover initial suite (from config or secrets directory)
   3. Create session directory: DEFAULT_MONITOR_BASE / session_id
   4. Start telemetry publisher (TCP to GCS:52080)
   5. Optimize CPU performance (unless --no-cpu-optimization)
   6. Start high-speed system monitor (100ms samples)
   7. Launch drone proxy with initial suite
   8. Start performance monitors (perf, pidstat, psutil, thermal)
   9. Start UDP echo server
   10. Start control server (TCP port 48080)
   11. Main loop: monitor proxy health
   12. On shutdown: stop all monitors, kill proxy, stop telemetry
   ```

---

## 🔄 **Comparison: Simple vs Production**

### **`gcs_scheduler_simple.py`** (230 lines)

**Purpose:** Minimal validation testing.

**Features:**
- Single-packet echo test per suite
- Interactive mode: `list`, `next`, `all`, `<suite-id>`, `quit`
- Auto mode: `--auto` for full sweep
- CSV output: `logs/auto/quickpass_summary.csv`
- No traffic generator (just one probe packet)
- No telemetry collection
- No Excel export

**Usage:**
```powershell
# Interactive
python tools/auto/gcs_scheduler_simple.py

# Auto sweep (all 21 suites, 1 pass)
python tools/auto/gcs_scheduler_simple.py --auto

# Multiple passes
$Env:SIMPLE_AUTO_PASSES = "3"
python tools/auto/gcs_scheduler_simple.py --auto
```

**CSV Schema:**
```csv
ts,suite,packets,success_packets,ok,best_rtt_ns,bytes,note
2025-10-02T12:00:00Z,cs-mlkem768-aesgcm-mldsa65,1,1,True,1234567,64,
2025-10-02T12:00:05Z,cs-mlkem1024-aesgcm-falcon1024,1,0,False,,,timeout
```

---

### **`gcs_scheduler_quickpass.py`** (165 lines)

**Purpose:** Fast validation without full proxy setup.

**Key Differences from Simple:**
- Standalone (doesn't depend on core.config imports in main logic)
- Explicit CLI args for all network params (`--gcs`, `--drone`, `--control-port`, etc.)
- Useful for CI/CD where config.py may not be available
- Event log: `quickpass_events.jsonl` (JSON lines with timestamps)

**Usage:**
```powershell
python tools/auto/gcs_scheduler_quickpass.py `
  --gcs 192.168.0.103 `
  --drone 192.168.0.102 `
  --control-port 48080 `
  --app-send-port 47001 `
  --app-recv-port 47002 `
  --passes 2
```

---

### **`drone_follower_simple.py`** (88 lines)

**Purpose:** Minimal echo server + control API.

**Features:**
- No performance monitors (no perf, pidstat, psutil)
- No telemetry publisher
- Basic control API: `ping`, `mark`, `stop`
- Marker files only: `logs/auto/drone/marks/{timestamp}_{suite}.json`
- UDP echo without packet annotation (just echo, no timing CSV)

**Usage:**
```bash
# On Drone host
python tools/auto/drone_follower_simple.py
```

---

## 📊 **Output Artifacts**

### **GCS Scheduler Outputs**

**Directory Structure:**
```
logs/auto/gcs/
├── gcs_20251002-120000.log              # Proxy stdout/stderr
├── gcs_status.json                       # Real-time proxy status
├── gcs_summary.json                      # Final counters
├── summary.csv                           # Main results table
└── suites/
    ├── cs-mlkem512-aesgcm-mldsa44/
    │   ├── blaster_events.jsonl          # Per-packet send/recv events
    │   ├── gcs_status.json               # Snapshot at end of suite
    │   └── gcs_summary.json
    ├── cs-mlkem768-aesgcm-mldsa65/
    │   └── ...
    └── ...

output/gcs/
├── session_1696248000_combined.xlsx      # Master workbook
└── saturation_cs-mlkem768-aesgcm-mldsa65_session_1696248000.xlsx
```

**`summary.csv` Schema:**
```csv
pass,suite,duration_s,sent,rcvd,pps,throughput_mbps,rtt_avg_ms,rtt_max_ms,loss_pct,enc_out,enc_in,drops,rekeys_ok,rekeys_fail,start_ns,end_ns,rekey_ms
0,cs-mlkem768-aesgcm-mldsa65,45.123,4512,4510,100.0,2.345,1.234,5.678,0.044,4512,4510,2,1,0,1696248000123456789,1696248045246802135,125.456
```

---

### **Drone Follower Outputs**

**Directory Structure:**
```
logs/auto/drone/
├── drone_20251002-120000.log            # Proxy stdout/stderr
├── marks/
│   ├── 1696248000_cs-mlkem512-aesgcm-mldsa44.json
│   ├── 1696248015_cs-mlkem768-aesgcm-mldsa65.json
│   └── ...
└── suites/
    ├── cs-mlkem512-aesgcm-mldsa44/
    │   ├── drone_status.json
    │   └── drone_summary.json
    └── ...

output/drone/session_1696248000/
├── system_monitoring_session_1696248000.csv    # High-speed monitor
├── packet_timing.csv                            # UDP echo samples
├── perf_samples_cs-mlkem768-aesgcm-mldsa65.csv
├── pidstat_cs-mlkem768-aesgcm-mldsa65.txt
├── psutil_proc_cs-mlkem768-aesgcm-mldsa65.csv
└── sys_telemetry_cs-mlkem768-aesgcm-mldsa65.csv
```

**`system_monitoring_*.csv` Schema (100ms sampling):**
```csv
timestamp_iso,timestamp_ns,suite,proxy_pid,cpu_percent,cpu_freq_mhz,cpu_temp_c,mem_used_mb,mem_percent,rekey_duration_ms
2025-10-02 12:00:00.000,1696248000000000000,cs-mlkem768-aesgcm-mldsa65,12345,85.3,1800.0,62.5,512.3,45.2,
2025-10-02 12:00:00.100,1696248000100000000,cs-mlkem768-aesgcm-mldsa65,12345,87.1,1800.0,62.6,512.5,45.2,
2025-10-02 12:00:00.200,1696248000200000000,cs-mlkem768-aesgcm-mldsa65,12345,88.5,1800.0,62.7,512.8,45.3,125.6
```

**`perf_samples_*.csv` Schema (1s aggregation):**
```csv
ts_unix_ns,t_offset_ms,instructions,cycles,cache-misses,branch-misses,task-clock,context-switches,branches
1696248000000000000,0,123456789,234567890,12345,6789,1000.0,42,45678901
1696248001000000000,1000,125432109,236789012,12567,7012,1000.5,45,47890123
```

---

## 🔬 **Advanced Features**

### **1. Saturation Testing**

**Algorithm:**
```python
rates = [5, 10, 15, 20, 25, 30, 35, 40, 50, 60, 70, 80, 90, 100, 125, 150, 175, 200]
baseline_rtt = None
saturation_point = None

for rate_mbps in rates:
    rate_pps = (rate_mbps * 1_000_000) / (payload_bytes * 8)
    blaster.run(duration_s, rate_pps)
    
    if baseline_rtt is None:
        baseline_rtt = blaster.avg_rtt_ms
    
    # Saturation detection:
    if blaster.avg_rtt_ms > baseline_rtt * 1.8:
        saturation_point = rate_mbps
        break
    if blaster.throughput_mbps < rate_mbps * 0.8:
        saturation_point = rate_mbps
        break
```

**Use Cases:**
- Determine max throughput for each suite
- Compare NIST L1/L3/L5 performance
- Identify signature overhead (ML-DSA vs Falcon vs SPHINCS+)
- Detect Pi throttling under load

---

### **2. Clock Synchronization**

**NTP-like Protocol:**
```
GCS                           Drone
 │                              │
 │  t1 = now()                  │
 │  {"cmd":"timesync","t1":t1}  │
 ├────────────────────────────→ │
 │                          t2 = now() (receive)
 │                          t3 = now() (send)
 │  {"ok":true,"t1":t1,        │
 │   "t2":t2,"t3":t3}           │
 │←──────────────────────────── │
 │  t4 = now()                  │
 │                              │
 
offset = ((t2 - t1) + (t3 - t4)) / 2
rtt = (t4 - t1) - (t3 - t2)
```

**Applied Offset:**
```python
# In Blaster class:
def _now(self):
    return time.time_ns() + self.offset_ns
```

**Typical Results:**
- Offset: ±5-50ms (depends on NTP sync quality)
- RTT: 0.1-2ms (LAN), 10-50ms (WAN)
- Precision: nanosecond timestamps (int64)

---

### **3. Power Measurement Integration**

**Scheduled Markers:**
```python
# GCS sends:
start_mark_ns = time.time_ns() + offset_ns + 150ms + pre_gap
ctl_send({"cmd": "schedule_mark", "suite": suite, "t0_ns": start_mark_ns})

# Drone receives, waits until t0_ns, then marks
# External power meter correlates timestamps with energy readings
```

**CSV Correlation:**
```python
# In system_monitoring_*.csv:
rekey_duration_ms column shows elapsed time during rekey

# Analysis:
import pandas as pd
sys_df = pd.read_csv("system_monitoring.csv")
power_df = pd.read_csv("power_meter.csv")

# Merge on timestamp_ns
merged = pd.merge_asof(sys_df, power_df, on="timestamp_ns")

# Calculate energy per rekey:
rekey_rows = merged[merged["rekey_duration_ms"].notna()]
energy_per_rekey = rekey_rows.groupby("suite")["power_watts"].mean() * rekey_rows["rekey_duration_ms"].mean() / 1000
```

---

### **4. Telemetry Streaming**

**Publisher-Collector Pattern:**
```
Drone (Publisher)                    GCS (Collector)
├─ TelemetryPublisher                ├─ TelemetryCollector
│  ├─ Queue (5000 items)             │  ├─ TCP Server (port 52080)
│  ├─ Background thread              │  ├─ Per-client threads
│  ├─ Auto-reconnect                 │  └─ Aggregated samples list
│  └─ JSON lines over TCP            │
│                                    │
│  {"session_id": "...",             │
│   "kind": "system_sample",         │
│   "timestamp_ns": ...,             │
│   "cpu_percent": 85.3, ...}        │
├────────────────────────────────────→
│                                    │
│  {"kind": "perf_sample",           │
│   "instructions": 123456789, ...}  │
├────────────────────────────────────→
│                                    │
│  {"kind": "udp_echo_sample",       │
│   "processing_ns": 12345, ...}     │
├────────────────────────────────────→
```

**Message Kinds:**
- `telemetry_hello` - Connection handshake
- `system_sample` - CPU/mem/temp/freq (100ms)
- `perf_sample` - Hardware counters (1s)
- `psutil_sample` - Python process metrics (1s)
- `thermal_sample` - Temp/freq/throttle (1s)
- `udp_echo_sample` - Packet processing latency (every 100th)
- `rekey_transition_start` - Rekey initiated
- `rekey_transition_end` - Rekey completed
- `mark` - Suite changed
- `rekey_complete` - Rekey finalized
- `status_reply` - Status query response
- `schedule_mark` - Scheduled marker
- `stop` - Shutdown signal
- `monitors_started` - Monitors launched
- `monitors_stopped` - Monitors terminated

**Backpressure Handling:**
```python
try:
    queue.put_nowait(message)
except queue.Full:
    # Drop oldest message
    queue.get_nowait()
    queue.put_nowait(message)
```

---

## 🐛 **Known Issues & Limitations**

### **1. Silent Exception Swallowing in `gcs_scheduler_simple.py`**

**Lines:** ~82, ~86, ~92, ~99

**Code:**
```python
try:
    gcs.stdin.write(to_suite + "\n"); gcs.stdin.flush()
except Exception as e:
    log_event(event="gcs_write_fail", msg=str(e))
    # NO re-raise! Continues silently

try:
    ctl(args.drone, args.control_port, {"cmd": "mark", "suite": to_suite})
except Exception as e:
    log_event(event="control_warn", msg=f"mark failed: {e}")
    # NO re-raise! Continues silently
```

**Impact:**
- Rekey failures may go unnoticed
- Test results invalid if proxy dies mid-test
- CSV shows "ok=True" even if rekey never happened

**Recommended Fix:**
```python
try:
    gcs.stdin.write(to_suite + "\n")
    gcs.stdin.flush()
except BrokenPipeError:
    log_event(event="gcs_pipe_broken", msg="Proxy died")
    raise RuntimeError("GCS proxy terminated unexpectedly")
except Exception as exc:
    log_event(event="gcs_write_fail", msg=str(exc))
    raise
```

---

### **2. Hardcoded Timeout Values**

**Issue:** Timeout constants scattered throughout code without configurability.

**Examples:**
- `wait_handshake(timeout=20.0)` - Fixed 20s
- `wait_active_suite(timeout=10.0)` - Fixed 10s
- `ctl_send(timeout=2.0, retries=4)` - Fixed 2s × 4 = 8s total

**Impact:**
- May timeout prematurely on slow Pi devices
- Cannot extend for debugging (e.g., attach gdb)
- No env var overrides

**Recommended Fix:**
```python
HANDSHAKE_TIMEOUT_S = float(os.getenv("GCS_HANDSHAKE_TIMEOUT", "20.0"))
REKEY_TIMEOUT_S = float(os.getenv("GCS_REKEY_TIMEOUT", "10.0"))
CONTROL_TIMEOUT_S = float(os.getenv("GCS_CONTROL_TIMEOUT", "2.0"))
```

---

### **3. Race Condition in `drone_follower.py` State Access**

**Issue:** `state` dict accessed from multiple threads without locks.

**Lines:** ~850-900 (ControlServer.handle)

**Code:**
```python
# Multiple threads read/write state dict:
state = {
    "proxy": proxy,              # Modified by main thread (restart)
    "suite": initial_suite,      # Modified by control thread (mark)
    "monitors": monitors,        # Modified by control thread (rotate)
    "stop_event": stop_event,
    ...
}

# In ControlServer.handle (different thread):
proxy = self.state["proxy"]     # Read
if proxy.poll() is not None:    # Race: proxy could be restarting
    ...
```

**Impact:**
- Rare crash if proxy restarts during control message handling
- state["suite"] could be stale if mark and status happen concurrently

**Recommended Fix:**
```python
import threading

state = {
    "proxy": proxy,
    "suite": initial_suite,
    "lock": threading.Lock(),  # Add lock
    ...
}

# In all access points:
with state["lock"]:
    proxy = state["proxy"]
    if proxy and proxy.poll() is None:
        ...
```

---

### **4. Excel Export Fails Silently**

**Issue:** If `openpyxl` not installed, prints warning but continues.

**Code:**
```python
try:
    from openpyxl import Workbook
except ImportError:
    Workbook = None

...

if Workbook is None:
    print("[WARN] openpyxl not available; skipping Excel export")
    return None  # No error raised!
```

**Impact:**
- User expects Excel file, doesn't exist
- CI/CD pipelines may not fail properly

**Recommended Fix:**
```python
try:
    from openpyxl import Workbook
except ImportError:
    if os.getenv("REQUIRE_EXCEL_EXPORT") == "1":
        raise RuntimeError("openpyxl required but not installed; pip install openpyxl")
    Workbook = None
```

---

### **5. Socket Buffer Size Tuning Not Documented**

**Issue:** Env vars `GCS_SOCK_SNDBUF`, `DRONE_SOCK_RCVBUF` used but undocumented.

**Code:**
```python
sndbuf = int(os.getenv("GCS_SOCK_SNDBUF", str(1 << 20)))  # 1 MB default
rcvbuf = int(os.getenv("GCS_SOCK_RCVBUF", str(1 << 20)))
```

**Impact:**
- Users don't know about tuning option
- Pi may drop packets at high rates (16 MB drone default vs 1 MB GCS default asymmetry)

**Recommended Fix:**
Add to `docs/` or `.env.example`:
```bash
# Socket buffer sizes (bytes)
GCS_SOCK_SNDBUF=16777216    # 16 MB
GCS_SOCK_RCVBUF=16777216
DRONE_SOCK_SNDBUF=16777216
DRONE_SOCK_RCVBUF=16777216
```

---

## 🎯 **Usage Patterns**

### **Pattern 1: Quick Validation**

```powershell
# GCS host
python tools/auto/gcs_scheduler_simple.py --auto

# Drone host
python tools/auto/drone_follower_simple.py
```

**Duration:** ~2 minutes (21 suites × 5s each)  
**Output:** `logs/auto/quickpass_summary.csv`  
**Use Case:** Smoke test after code changes

---

### **Pattern 2: Performance Characterization**

```powershell
# Drone host
python tools/auto/drone_follower.py --session-id perf_test_001

# GCS host
python tools/auto/gcs_scheduler.py \
  --traffic blast \
  --duration 60 \
  --rate 100 \
  --passes 3 \
  --session-id perf_test_001
```

**Duration:** ~1 hour (21 suites × 60s × 3 passes + 15s gaps)  
**Output:**
- `logs/auto/gcs/summary.csv` - Aggregate results
- `output/gcs/perf_test_001_combined.xlsx` - Master workbook
- `output/drone/perf_test_001/` - All monitoring CSVs

**Use Case:** Generate data for research paper, compare suite performance

---

### **Pattern 3: Saturation Testing**

```powershell
# Drone host
python tools/auto/drone_follower.py \
  --session-id saturation_001 \
  --no-cpu-optimization  # Let scheduler control rates

# GCS host
python tools/auto/gcs_scheduler.py \
  --traffic saturation \
  --duration 30 \
  --max-rate 200 \
  --suites cs-mlkem768-aesgcm-mldsa65 cs-mlkem1024-aesgcm-falcon1024 \
  --session-id saturation_001
```

**Duration:** ~10 minutes per suite (ramp 5→200 Mbps, 30s each)  
**Output:**
- `output/gcs/saturation_cs-mlkem768-aesgcm-mldsa65_saturation_001.xlsx`
- `logs/auto/gcs/saturation_summary_saturation_001.json`

**Use Case:** Determine max throughput, detect bottlenecks

---

### **Pattern 4: Power Measurement Integration**

```powershell
# Drone host (with power meter attached)
python tools/auto/drone_follower.py --session-id power_001

# Power meter script (parallel)
python tools/power_meter_logger.py --output power_001.csv

# GCS host
python tools/auto/gcs_scheduler.py \
  --traffic blast \
  --duration 45 \
  --pre-gap 5 \
  --inter-gap 20 \
  --session-id power_001
```

**Flow:**
1. GCS sends `schedule_mark` 5s before traffic starts
2. Drone waits, then marks at exact `t0_ns`
3. Power meter records energy readings
4. Correlate timestamps: `system_monitoring.csv` ↔ `power_001.csv`

**Analysis:**
```python
import pandas as pd

sys_df = pd.read_csv("output/drone/power_001/system_monitoring_power_001.csv")
power_df = pd.read_csv("power_001.csv")

merged = pd.merge_asof(
    sys_df.sort_values("timestamp_ns"),
    power_df.sort_values("timestamp_ns"),
    on="timestamp_ns",
    direction="nearest"
)

# Energy per rekey:
rekey_df = merged[merged["rekey_duration_ms"].notna()]
energy_per_suite = rekey_df.groupby("suite").agg({
    "power_watts": "mean",
    "rekey_duration_ms": "mean"
})
energy_per_suite["energy_j"] = (
    energy_per_suite["power_watts"] * 
    energy_per_suite["rekey_duration_ms"] / 1000
)
print(energy_per_suite.sort_values("energy_j"))
```

---

## 🔍 **Debugging Tips**

### **1. GCS Proxy Won't Start**

**Symptom:** `wait_handshake()` times out.

**Check:**
```powershell
# Verify key exists
ls secrets/matrix/cs-mlkem768-aesgcm-mldsa65/gcs_signing.key

# Check proxy log
cat logs/auto/gcs/gcs_20251002-120000.log

# Common errors:
# - Missing key file
# - oqs-python import failure
# - Port already bound (46011 or 46012)
```

---

### **2. Drone Follower Not Reachable**

**Symptom:** `[WARN] follower not reachable at 192.168.0.102:48080`

**Check:**
```bash
# On drone host
netstat -tuln | grep 48080
# Should show: tcp  0.0.0.0:48080  LISTEN

# Test manually
echo '{"cmd":"ping"}' | nc 192.168.0.102 48080
# Should return: {"ok":true,"ts":"2025-10-02T..."}

# Firewall?
sudo ufw status
sudo ufw allow 48080/tcp
```

---

### **3. High Packet Loss**

**Symptom:** `loss_pct > 10%` in summary CSV.

**Check:**
```bash
# On drone host
cat /proc/net/udp
# Look for drops in column 12 (Rx-DROP)

# Increase socket buffers
export DRONE_SOCK_RCVBUF=33554432  # 32 MB
python tools/auto/drone_follower.py

# Check CPU throttling
vcgencmd get_throttled
# 0x0 = OK, non-zero = throttled at some point
```

---

### **4. Excel Export Missing Drone Data**

**Symptom:** Combined workbook has no drone CSV sheets.

**Check:**
```powershell
# Verify session directory exists
ls output/drone/session_1696248000/

# Check GCS discovery logic
python -c "from pathlib import Path; print(Path('/home/dev/research/output/drone/session_1696248000').exists())"

# Workaround: copy manually
Copy-Item output/drone/session_1696248000/*.csv output/gcs/
```

---

### **5. Rekey Not Happening**

**Symptom:** All suites show same `rekeys_ok=1` (initial handshake only).

**Check:**
```powershell
# Verify GCS proxy stdin write
# Look for this in gcs log:
grep "rekey ->" logs/auto/gcs/gcs_20251002-120000.log

# Check drone mark acknowledgement
grep "marked" logs/auto/drone/drone_20251002-120000.log

# Manual test
echo "cs-mlkem1024-aesgcm-falcon1024" | python -m core.run_proxy gcs --suite cs-mlkem768-aesgcm-mldsa65 --control-manual
# Should see rekey happen in logs
```

---

## 📚 **Future Enhancements**

### **1. Multi-Drone Support**

**Current Limitation:** One GCS scheduler per one Drone follower.

**Proposed Design:**
```python
# gcs_scheduler.py --drones drone1:48080,drone2:48081,drone3:48082
drones = [
    {"host": "192.168.0.102", "port": 48080},
    {"host": "192.168.0.103", "port": 48081},
    {"host": "192.168.0.104", "port": 48082},
]

for suite in suites:
    # Parallel mark
    threads = [threading.Thread(target=ctl_send, args=(d["host"], d["port"], {"cmd": "mark", "suite": suite})) for d in drones]
    [t.start() for t in threads]
    [t.join() for t in threads]
    
    # Aggregate results
    results = [read_proxy_stats(d) for d in drones]
```

---

### **2. Real-Time Dashboard**

**Proposed:** Web UI showing live metrics.

**Tech Stack:**
- Backend: Flask/FastAPI
- Frontend: React + Recharts
- Transport: WebSocket (telemetry stream)

**Features:**
- Live RTT graphs
- CPU/memory/temp gauges
- Rekey event timeline
- Packet loss heatmap per suite

---

### **3. Automated Analysis Reports**

**Proposed:** Generate LaTeX/PDF report from combined Excel.

**Sections:**
- Executive summary (best/worst suites)
- RTT distribution histograms
- Throughput vs signature size regression
- Power consumption by NIST level
- Rekey latency CDF

---

### **4. Continuous Integration Integration**

**Proposed:** Jenkins/GitHub Actions pipeline.

**Workflow:**
```yaml
name: PQC Matrix Test
on: [push, pull_request]
jobs:
  matrix-test:
    runs-on: [self-hosted, gcs, drone]
    steps:
      - uses: actions/checkout@v3
      - name: Start Drone Follower
        run: ssh drone "cd ~/research && python tools/auto/drone_follower.py --session-id ci-$GITHUB_RUN_ID &"
      - name: Run GCS Scheduler
        run: python tools/auto/gcs_scheduler.py --traffic blast --duration 30 --session-id ci-$GITHUB_RUN_ID
      - name: Validate Results
        run: python tools/validate_test_results.py --min-success-rate 95
      - uses: actions/upload-artifact@v3
        with:
          name: test-results
          path: output/gcs/ci-$GITHUB_RUN_ID_combined.xlsx
```

---

## ✅ **Best Practices**

### **1. Always Use Session IDs**

```powershell
# Good
--session-id perf_test_20251002_001

# Bad (timestamp collision risk)
--session-id test
```

---

### **2. Monitor Drone Health**

```bash
# Before long test run:
vcgencmd measure_temp  # Should be < 70°C
vcgencmd get_throttled  # Should be 0x0
free -h                 # Should have >500 MB free
```

---

### **3. Tune Socket Buffers for High Rates**

```bash
# For rates >50 Mbps:
export GCS_SOCK_SNDBUF=33554432
export GCS_SOCK_RCVBUF=33554432
export DRONE_SOCK_SNDBUF=33554432
export DRONE_SOCK_RCVBUF=33554432
```

---

### **4. Use Pre-Gap for Power Measurements**

```powershell
# Give power meter time to stabilize
--pre-gap 5  # 5 seconds idle before traffic
```

---

### **5. Verify Results Immediately**

```powershell
# Check summary CSV exists
ls logs/auto/gcs/summary.csv

# Quick sanity check
python -c "import pandas as pd; df = pd.read_csv('logs/auto/gcs/summary.csv'); print(df[['suite', 'loss_pct', 'rtt_avg_ms']].describe())"
```

---

## 🎓 **Conclusion**

The `tools/auto/` infrastructure is a **production-grade orchestration system** for automated PQC testing. Key strengths:

✅ **Robust Control Protocol** - TCP JSON-RPC with retries and timeouts  
✅ **Comprehensive Monitoring** - perf, pidstat, psutil, thermal, packet timing  
✅ **Telemetry Streaming** - Real-time metrics from Drone to GCS  
✅ **Clock Synchronization** - Nanosecond-precision RTT measurements  
✅ **Power Integration** - Scheduled markers for energy correlation  
✅ **Excel Export** - Multi-sheet workbooks for analysis  
✅ **Saturation Testing** - Automated capacity discovery  
✅ **Graceful Degradation** - Simple variants for quick testing  

**Use the simple versions for smoke tests, production versions for serious experiments.**

---

**Analysis Completed By:** AI Coding Agent (GitHub Copilot)  
**Date:** October 2, 2025  
**Status:** ✅ Deep analysis of `tools/auto/` complete with 5 known issues identified
