# PQC Drone↔GCS Secure Proxy — System Diagrams

This note stays high-level on purpose so it can live inside the paper without overwhelming readers. Each diagram is followed by a short list of plain-language takeaways.

## 1. Component Map

```mermaid
graph TD
    subgraph GCS_Side[Ground Control Station]
        SCHED[tools/auto/gcs_scheduler.py]
        GCS_PROXY[core.run_proxy gcs]
        TELEMETRY_GCS[Telemetry Collector]
        DDOS_STACK_GCS["DDoS Analytics: XGBoost & TST"]
    end

    subgraph Drone_Side[Drone]
        FOLLOWER[tools/auto/drone_follower.py]
        DRONE_PROXY[core.run_proxy drone]
        TELEMETRY_DRONE[Telemetry Publisher]
        POWER[Power Monitor]
        DDOS_STACK_DRONE[Token Bucket & Rate Guards]
    end

    subgraph Shared
        SECRETS[secrets/<suite>/ key pairs]
        CONTROL[channel: JSON over TCP]
        ENCRYPTED[Encrypted UDP tunnel]
    end

    SCHED -->|start + rekey commands| GCS_PROXY
    GCS_PROXY -->|HKDF keys| ENCRYPTED
    ENCRYPTED --> DRONE_PROXY
    DRONE_PROXY --> FOLLOWER
    FOLLOWER -->|status + telemetry| CONTROL
    CONTROL --> SCHED
    FOLLOWER -->|metrics| TELEMETRY_DRONE --> TELEMETRY_GCS
    DRONE_PROXY -->|rate stats| DDOS_STACK_GCS
    DRONE_PROXY -->|token bucket| DDOS_STACK_DRONE
    POWER --> FOLLOWER --> TELEMETRY_DRONE
    SECRETS --> GCS_PROXY
    SECRETS --> DRONE_PROXY
```

- GCS and drone proxies read matching public keys and secrets from the `secrets/` tree so they already trust each other before the network comes up.
- The scheduler drives the GCS proxy and talks to the drone follower over a small TCP control channel on loopback or LAN.
- Telemetry runs out-of-band so control or data hiccups do not hide health signals.
- Lightweight rate guards (token bucket) live inside the proxy, while the heavier DDoS detectors (XGBoost + Time Series Transformer) run on the ground station where more compute is available.

### Component Map — Simplified View

```mermaid
flowchart LR
    GCS[Ground Control Station]
    Tunnel[Encrypted UDP Tunnel]
    Drone[Drone + Monitors]
    Telemetry[Telemetry Loop]
    DDoS[DDoS Analytics]

    GCS --> Tunnel --> Drone
    Drone --> Telemetry --> GCS
    GCS --> DDoS
```

- Ground station drives the tunnel and analytics.
- Drone handles mission logic plus local monitors.
- Telemetry forms a feedback loop that keeps operators informed.

## 2. Secure Session Lifecycle (MitM Defense)

```mermaid
sequenceDiagram
    participant Scheduler as Scheduler
    participant GCS as GCS Proxy
    participant DroneCtl as Drone Control Server
    participant Drone as Drone Proxy

    Scheduler->>GCS: Launch with suite ID + secret paths
    GCS->>DroneCtl: server_gcs_handshake() (signed ServerHello)
    DroneCtl->>Drone: client_drone_handshake()
    Drone-->>DroneCtl: Verify signature + suite policy
    DroneCtl-->>GCS: Accept only if keys + suite match policy
    GCS-->>Drone: HKDF derives send/recv 32B keys + nonce seeds
    Drone-->>GCS: Confirms, starts encrypted UDP epoch 0
    Scheduler->>DroneCtl: mark + schedule_mark commands for rekeys
    GCS->>Drone: New handshake on rekey (epoch++ / new keys)
```

Key points:

- Every handshake carries both a post-quantum KEM shared secret and a signature from the GCS keypair. The drone rejects the session if the signature or suite ID is unexpected, blocking man-in-the-middle attempts.
- After the KEM exchange, both sides run HKDF-SHA256 with a fixed salt (`"pq-drone-gcs|hkdf|v1"`) so fresh send/receive keys and nonce seeds exist for each epoch.
- Rekeys reuse the same flow; epochs increment so old traffic can never be decrypted under the new key schedule.

### Session Lifecycle — Simplified View

```mermaid
flowchart LR
    Launch[Scheduler launches proxy]
    Hello[Signed ServerHello]
    Verify[Drone verifies suite]
    HKDF[HKDF derives fresh keys]
    Secure[Encrypted epoch active]
    Rekey[Scheduler requests rekey]

    Launch --> Hello --> Verify --> HKDF --> Secure
    Secure --> Rekey --> Hello
```

- Scheduler starts the proxy with the right suite.
- The signed hello is verified before keys are accepted.
- HKDF produces fresh send/receive keys every epoch.

### Handshake & Encrypted Channel Layout

```mermaid
sequenceDiagramV
    participant Drone
    participant Tunnel as Encrypted Channel (AES-256-GCM)
    participant GCS

    Drone->>GCS: 1. TCP connect to handshake port
    GCS-->>Drone: 2. Signed ServerHello<br/>(ML-KEM public key, suite IDs, session ID, challenge)
    Note over Drone: Verify signature with stored GCS certificate<br/>Check suite policy + wire version
    Drone->>GCS: 3. ML-KEM ciphertext + HMAC tag (32B PSK)
    Note over GCS: Verify tag with drone PSK<br/>Decapsulate to recover shared secret
    Drone-->>Tunnel: 4. HKDF derives drone→gcs / gcs→drone AES keys
    GCS-->>Tunnel: 4. HKDF derives matching key pair
    Tunnel-->>Drone: 5. Encrypted UDP epoch 0 active (bidirectional)
    Tunnel-->>GCS: 5. Telemetry + data packets flow with replay protection
```

- **Credential use:** The GCS signs the ServerHello with its post-quantum signature private key stored under `secrets/`; the drone verifies using the corresponding public key (certificate-equivalent).
- **Drone authentication:** The drone appends an HMAC-SHA256 tag calculated with the 32-byte pre-shared key (`DRONE_PSK`). The GCS refuses the handshake if this tag fails.
- **Key agreement:** Both sides feed the ML-KEM shared secret into HKDF-SHA256 (`salt="pq-drone-gcs|hkdf|v1"`) to derive independent AES-256-GCM keys for each direction.
- **Data plane:** Once keys are in place, all UDP traffic traverses the encrypted channel, carrying 22-byte authenticated headers with session, epoch, and sequence fields.

## 3. Replay Protection

```mermaid
flowchart LR
    A[Receive packet] --> B{Header + epoch OK?}
    B -->|No| DROP1[Drop packet]
    B -->|Yes| C{Seq within
    1024-bit window?}
    C -->|No| DROP2[Drop packet]
    C -->|Yes| D[Decrypt with AES-256-GCM]
    D -->|Fail| DROP3[Drop silently]
    D -->|Success| E[Update high water mark + bitmap]
    E --> F[Forward plaintext up-stack]
```

- `core/aead.py` keeps, per direction, the highest sequence number plus a 1024-bit replay bitmap. Anything outside the window or already seen is dropped without raising an error.
- This window still allows moderate out-of-order delivery, so bursty networks do not break the tunnel.
- Because the proxy never sends detailed decrypt errors, attackers cannot learn why their injection failed.

### Replay Protection — Simplified View

```mermaid
flowchart TD
    Start[Packet arrives]
    Header{Header + epoch valid?}
    Window{Sequence inside window?}
    Decrypt{Decrypt succeeds?}
    Forward[Forward to app]
    Drop[Silently drop]

    Start --> Header
    Header -- No --> Drop
    Header -- Yes --> Window
    Window -- No --> Drop
    Window -- Yes --> Decrypt
    Decrypt -- No --> Drop
    Decrypt -- Yes --> Forward
```

- Checks run in constant time and never leak details.
- Only packets with fresh sequence numbers reach the application.

## 4. DDoS & Flooding Perspective

```mermaid
graph LR
    INTERNET[Untrusted network]
    INTERNET -->|UDP traffic| GATEWAY[core/async_proxy.py]
    GATEWAY -->|Token Bucket| PASS[Accepted packets]
    GATEWAY -->|Drop + counter| DROP[Rate limited]
    PASS --> TELEMETRY[Telemetry counters]
    PASS --> SCHEDULER[gcs_scheduler metrics]
    TELEMETRY --> DDOS_PIPE[XGBoost Screener -> TST Confirm]
    DDOS_PIPE -->|Alert| Operator
```

- The async proxy applies DSCP tags, pins peers, and throttles with a token bucket before traffic touches the drone.
- Scheduler-side analytics read the same counters plus live telemetry to spot sustained floods; the two-stage detector (XGBoost screener, Transformer confirmer) treats each 0.6 s window and issues alerts only on consistent abuse.
- Operators can rehearse failovers by replaying captures through `ddos/run_tst.py` or `tools/sim_driver.py` to make sure thresholds match field reality.

### DDoS Flow — Simplified View

```mermaid
flowchart LR
    Net[Internet]
    Proxy[Async Proxy]
    Accept[Accepted Traffic]
    Drop[Rate-Limited]
    Counters[Telemetry Counters]
    ML[ML Detectors]
    Operator[Operator Alert]

    Net --> Proxy
    Proxy -->|Token bucket| Accept
    Proxy -->|Excess| Drop
    Accept --> Counters --> ML --> Operator
```

- Proxy enforces rate limits before traffic reaches the drone.
- Telemetry feeds both dashboards and machine-learning detectors.

## 5. Artifacts & Telemetry At A Glance

| Stage | What is captured | Where it lives | Why it matters |
|-------|------------------|----------------|----------------|
| Handshake & suites | `gcs_status.json`, `drone_status.json`, markers in `logs/auto/*/` | Confirms which suite ran and whether rekeys succeeded |
| Traffic stats | `summary.csv`, `blaster_events.jsonl`, `packet_timing.csv` | Gives throughput, loss, RTT/OWD samples for reporting |
| System health | `system_monitoring_*.csv`, perf/psutil/thermal logs | Correlates CPU load, temp, and rekey duration |
| Power capture | CSV + JSON summaries under `power/` | Quantifies energy draw per suite/pass |
| Telemetry bus | TCP stream from follower → scheduler (`TelemetryPublisher` ↔ `TelemetryCollector`) | Keeps monitoring live even if data-plane is stressed |

## 6. Threat-Model Checklist

- **Key storage:** Both sides boot with pre-generated keys from `secrets/<suite>/`; nothing travels over the air in clear text.
- **MITM resistance:** Signed server hello + expected suite IDs stop impostor GCS nodes. HKDF rotates keys on every rekey.
- **Replay control:** 1024-bit sliding window plus epoch counters throw away duplicates and stale packets.
- **Flood handling:** Token bucket enforcement in the proxy and machine-learning detection on the GCS keep bandwidth hogs from starving mission traffic.
- **Observability:** All control errors respond with `{"ok": false, "error": ...}` and can be mirrored into telemetry so operators see issues quickly.

These diagrams mirror the current README + docs content and avoid any speculative claims, keeping the paper grounded in the code that actually ships.

## 7. Control Command Loop (Scheduler ↔ Follower ↔ Proxies)

```mermaid
sequenceDiagram
    participant S as Scheduler
    participant F as Drone Control Server
    participant M as Drone Monitors
    participant GP as GCS Proxy
    participant DP as Drone Proxy

    S->>F: mark(suite)
    F->>M: Rotate monitors & start_rekey
    S->>GP: Write suite ID to stdin
    GP->>DP: Rekey handshake over TCP
    DP-->>GP: Shared secret + epoch++
    GP-->>S: Counters report rekeys_ok
    S->>F: rekey_complete(status)
    F->>M: end_rekey() + update suite
    F-->>S: status() confirms suite active
    M-->>F: Telemetry publish rekey events
```

- Scheduler first aligns the follower’s monitors before telling the proxy to switch suites, ensuring logs land in the correct output directory.
- The GCS proxy performs the PQC handshake with the drone proxy; the scheduler only proceeds once counters show the new suite is live.
- Final `rekey_complete` closes the loop so the drone drops back to RUNNING state, and telemetry captures the entire transition for later audits.

### Control Loop — Simplified View

```mermaid
flowchart LR
    Sched[Scheduler]
    Follower[Follower]
    Proxies[GCS & Drone Proxies]
    Telemetry[Telemetry + Counters]

    Sched -->|mark/rekey| Follower
    Follower --> Proxies
    Proxies --> Telemetry
    Telemetry --> Sched
```

- Scheduler issues commands, follower orchestrates monitors, proxies execute.
- Telemetry closes the loop so the scheduler can verify success.

## 8. Data-Plane Packet Journey

```mermaid
flowchart LR
    APP_TX[GCS plaintext app] --> PT[GCS plaintext socket]
    PT --> TB{Token bucket OK?}
    TB -- No --> DROP[Drop & increment rate-limit counter]
    TB -- Yes --> AEAD[AES-256-GCM sender]
    AEAD --> DSCP[Tag DSCP + pin peer]
    DSCP --> NET[Encrypted UDP packet]
    NET --> RX[Drone proxy receiver]
    RX --> REPLAY{Within replay window?}
    REPLAY -- No --> DISCARD[Silently drop]
    REPLAY -- Yes --> DECRYPT[Decrypt & auth]
    DECRYPT --> APP_RX[Drone plaintext app]
```

- Packets leave the ground control app in plaintext, but the proxy immediately enforces a token bucket to slow abuse before encrypting with AES-256-GCM.
- The 22-byte header (version, suite IDs, session, seq, epoch) travels as AAD; any mismatch fails fast without touching the decryptor.
- On the drone side, the replay bitmap and epoch gates drop stale traffic silently, keeping adversaries from probing timing differences.

### Packet Journey — Simplified View

```mermaid
flowchart LR
    App[GCS App]
    Gate[Token Bucket]
    Enc[AES-GCM Encrypt]
    Net[Encrypted UDP]
    Check[Replay Window]
    Plain[Drone App]

    App --> Gate --> Enc --> Net --> Check --> Plain
```

- Each packet is throttled, encrypted, tagged, then checked again on the drone.
- Replay checks happen before plaintext is released to the mission app.

## 9. Telemetry, Power, and DDoS Analytics Pipeline

```mermaid
graph TD
    MON[HighSpeedMonitor<br/>Perf / psutil / thermal] --> PUB[TelemetryPublisher]
    POWER_CAP[PowerCaptureManager] --> PUB
    UDP_ECHO[UDP Echo metrics] --> PUB
    PUB -->|TCP JSON stream| COLLECT[TelemetryCollector]
    COLLECT --> DASH[Combined run workbook<br/>+ live dashboards]
    COLLECT --> DDOS["DDoS Analytics:<br/>XGBoost screener → TST"]
    COLLECT --> ALERTS[Operator alerts]
    DDOS --> ALERTS
```

- The follower aggregates high-frequency metrics (system load, perf counters, INA219 power summaries, UDP echo latency) and pushes them through a single TCP stream.
- The scheduler records the live feed for the run workbook while also piping samples into the two-stage DDoS detector to catch slow-building floods.
- Because telemetry is decoupled from the encrypted data path, even heavy rekeys or network churn still deliver health data to operators.

### Telemetry Pipeline — Simplified View

```mermaid
flowchart LR
    Monitors[Monitors & Power]
    Publisher[Telemetry Publisher]
    Collector[Scheduler Collector]
    Outputs[Workbook & Alerts]
    DDoS[ML DDoS Detector]

    Monitors --> Publisher --> Collector --> Outputs
    Collector --> DDoS --> Outputs
```

- All monitors feed one publisher, simplifying transport.
- Collector fans out the same stream to reports and DDoS analytics.
