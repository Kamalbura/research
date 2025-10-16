# Phase 6: Mermaid Diagrams for Performance Chapter

This document contains 5 flowchart/state-machine definitions in Mermaid syntax for integration into the LaTeX document.

---

## Diagram 1: Suite Selection Pipeline

```mermaid
flowchart TD
    A[Input Requirements] --> B{NIST Security Level?}
    B -->|Level 1-3| C{Drone RAM Available?}
    B -->|Level 5| D{Latency SLA?}
    
    C -->|< 512 MiB| E[ML-KEM512 Variants]
    C -->|≥ 512 MiB| F[ML-KEM768 Variants]
    
    D -->|< 50 ms| G[ML-KEM1024 Recommended]
    D -->|< 100 ms| H[FrodoKEM976 Acceptable]
    D -->|> 100 ms| I[Any Level 5 Suite]
    
    E --> J{AEAD Choice}
    F --> J
    G --> J
    H --> J
    I --> J
    
    J -->|ARM Platform| K[ChaCha20-Poly1305]
    J -->|AES-NI Support| L[AES-GCM]
    
    K --> M{Signature Selection}
    L --> M
    
    M -->|Min Size| N[Falcon512/1024]
    M -->|Performance| O[ML-DSA44/65/87]
    M -->|Conservative| P[SPHINCS+]
    
    N --> Q[Recommended Suite]
    O --> Q
    P --> Q
```

**Figure Caption:** Suite selection decision tree based on operational requirements. Latency SLA and NIST security level determine KEM family, followed by platform-specific AEAD and signature scheme selection.

---

## Diagram 2: Telemetry Ingestion Flow

```mermaid
flowchart LR
    A[INA219 Power Sensor<br/>1000 Hz ADC] --> B[JSON Export<br/>Voltage/Current]
    B --> C[summary.csv<br/>power_avg_w Column]
    
    D[iperf3 UDP Client<br/>8 Mb/s Target] --> E[RTT Sample Buffer<br/>45s Window]
    E --> F[Aggregate Percentiles<br/>p50/p95/max]
    F --> C
    
    G[Packet Counter<br/>sent/received] --> H[Loss Tracker<br/>delivered_ratio]
    H --> C
    
    I[psutil Monitor<br/>CPU/RSS Polling] --> C
    
    C --> J[TXT Report Generator<br/>30 Suites × 21 Metrics]
    
    J --> K[Baseline Report<br/>benchmarks without-ddos.txt]
    J --> L[Lightweight Report<br/>results with ddos detection.txt]
    J --> M[Transformer Report<br/>results benchmarks...heavy.txt]
```

**Figure Caption:** Telemetry ingestion pipeline capturing power, network, and resource metrics from hardware sensors and OS monitors. All streams converge to summary.csv before TXT report generation.

---

## Diagram 3: Power Capture Pipeline

```mermaid
flowchart TD
    A[INA219 I2C Sensor<br/>Bus Voltage: 0-26V<br/>Shunt Current: ±3.2A] --> B[Python Monitor Script<br/>power/monitor.py]
    
    B --> C[Sample Loop<br/>1000 Hz Target<br/>Actual: 995-1005 Hz]
    
    C --> D[Timestamp + Reading<br/>ISO8601, mV, mA]
    
    D --> E[CSV Export<br/>power_*.csv<br/>~450 MB/45s]
    
    E --> F[Aggregator<br/>Mean/Std/Min/Max]
    
    F --> G[summary.csv Columns<br/>power_avg_w<br/>avg_current_a<br/>avg_voltage_v]
    
    G --> H[Energy Calculation<br/>energy_j = power_w × duration_s]
    
    H --> I[Per-Operation Energy<br/>kem_keygen_mJ = keygen_ms × power_w<br/>kem_decap_mJ = decap_ms × power_w<br/>sig_sign_mJ = sign_ms × power_w]
    
    I --> J[TXT Report Columns<br/>power X.XXX W avg over 45.0 s<br/>energy (XXX.XXX J)]
```

**Figure Caption:** Power measurement pipeline from INA219 hardware sensor to per-operation energy breakdown. High-frequency sampling (1000 Hz) enables precise energy attribution to cryptographic primitives.

---

## Diagram 4: DDOS Escalation Logic

```mermaid
stateDiagram-v2
    [*] --> NoDetector: Baseline Mode
    [*] --> XGBoost: Lightweight Mode
    [*] --> Transformer: Transformer Mode
    
    NoDetector --> PacketForward: All Traffic
    PacketForward --> ReportMetrics: 45s Window
    ReportMetrics --> [*]
    
    XGBoost --> InferenceWindow: 1s Telemetry Window<br/>150 Features
    InferenceWindow --> AnomalyCheck: < 2ms Inference
    
    AnomalyCheck --> NormalTraffic: Score < 0.5 Threshold
    AnomalyCheck --> DetectedAnomaly: Score ≥ 0.5
    
    NormalTraffic --> PacketForward
    DetectedAnomaly --> AdaptiveRekey: Trigger Scheduler
    AdaptiveRekey --> PacketForward: New Keys Active
    
    Transformer --> TSTInference: 1s Window<br/>6-Layer Attention
    TSTInference --> AnomalyDecision: 15-20ms Inference
    
    AnomalyDecision --> NormalTrafficTST: Clean Traffic
    AnomalyDecision --> LossThresholdCheck: Anomaly Detected
    
    LossThresholdCheck --> CriticalLoss: Loss > 5%
    LossThresholdCheck --> ElevatedLoss: Loss 1-5%
    LossThresholdCheck --> NormalTrafficTST: Loss < 1%
    
    CriticalLoss --> EmergencyRekey: Force Immediate Rekey
    ElevatedLoss --> ScheduledRekey: Queue Rekey in 2s
    
    EmergencyRekey --> PacketForward
    ScheduledRekey --> PacketForward
    NormalTrafficTST --> PacketForward
```

**Figure Caption:** DDOS detection escalation state machine across three operational modes. Lightweight (XGBoost) uses binary threshold, while Transformer (TST) employs multi-tier loss-based escalation.

---

## Diagram 5: Rekey State Machine

```mermaid
stateDiagram-v2
    [*] --> RUNNING: Initial Handshake Complete
    
    RUNNING --> RUNNING: Normal Traffic Processing
    RUNNING --> NEGOTIATING: Adaptive Trigger<br/>or Scheduler Event
    
    NEGOTIATING --> NEGOTIATING_KEYGEN: KEM Keygen<br/>(5-390 ms)
    NEGOTIATING_KEYGEN --> NEGOTIATING_ENCAP: KEM Encap<br/>(1-5 ms)
    NEGOTIATING_ENCAP --> NEGOTIATING_SIGN: Signature Sign<br/>(1-112 ms)
    
    NEGOTIATING_SIGN --> PREPARE_COMMIT: Build Handshake Packet
    PREPARE_COMMIT --> SWAPPING: 2-Phase Commit<br/>Network Round-Trip
    
    SWAPPING --> APPLY_NEW_KEYS: Handshake ACK Received
    SWAPPING --> REKEY_FAIL: Timeout or Packet Loss
    
    APPLY_NEW_KEYS --> UPDATE_SESSION: Install New Keys<br/>Rotate Nonces
    UPDATE_SESSION --> LOG_METRICS: Record rekey_window_ms<br/>rekeys_ok++
    LOG_METRICS --> RUNNING: Resume Traffic
    
    REKEY_FAIL --> LOG_FAILURE: Record rekeys_fail++
    LOG_FAILURE --> RUNNING: Retry in 5s<br/>or Fallback to Old Keys
    
    RUNNING --> [*]: Session Teardown
```

**Figure Caption:** Rekey state machine with 2-phase commit protocol. Metrics captured: rekey_window_ms (NEGOTIATING → RUNNING transition time), rekeys_ok (successful transitions), rekeys_fail (REKEY_FAIL occurrences).

---

## Integration Notes for LaTeX

Each diagram should be included in the LaTeX document using:

```latex
\begin{figure}[htbp]
\centering
\includegraphics[width=0.9\textwidth]{diagrams/diagram01_suite_selection.png}
\caption{Suite Selection Pipeline...}
\label{fig:suite_selection}
\end{figure}
```

To generate PNG images from Mermaid:
1. Use `mmdc` (mermaid-cli): `mmdc -i diagram.mmd -o diagram.png -t neutral -b transparent`
2. Or use online editor: https://mermaid.live
3. Export at 300 DPI for publication quality

Alternatively, if compiling with lualatex, use the mermaid LaTeX package directly:

```latex
\usepackage{mermaid}
\begin{mermaid}
... diagram code ...
\end{mermaid}
```
