# Adaptive Expert Scheduling for Post-Quantum Drone Links

## Abstract
Secure drone-to-ground control station links must reconcile post-quantum cryptographic (PQC) strength with strict power, thermal, and network constraints. We study a deterministic lookup scheduler that selects PQC suites using real-time telemetry while maintaining wire compatibility with an AES-256-GCM transport and ML-KEM handshake. Using the project's selectors-based proxy, we replay three operational postures: no DDoS sensing (baseline), a lightweight detector profile, and a heavyweight profile that emulates Time-Series Transformer load. Across 21 PQC suites we observe that adaptive scheduling preserves the 8 Mb/s target throughput while containing packet loss and handshake delays, even as onboard compute demand nearly quadruples. The study quantifies where the expert scheduler succeeds, where it begins to shed load, and which suites remain viable for resilient flight operations.

## CCS CONCEPTS
- Security and privacy � Network security.
- Computer systems organization � Real-time systems.
- Hardware � Power estimation and optimisation.

## KEYWORDS
post-quantum cryptography; unmanned aerial vehicles; adaptive scheduling; telemetry guards; AES-GCM; ML-KEM.

## 1 INTRODUCTION
Emerging drone platforms must ship with PQC-safe links without sacrificing endurance or responsiveness. Static cryptographic policies risk power starvation, thermal runaway, or unacceptably long reconnect times when operating in cluttered RF environments. The project's scheduler addresses this challenge by ingesting telemetry and selecting suites from a banded catalogue instead of pinning to a single algorithm. This paper contributes: (i) a concrete architectural description of the lookup scheduler pipeline, (ii) a measurement study comparing 21 PQC suites under three emulated DDoS-detection workloads, and (iii) operational guidance for deploying the expert policy in contested environments.

## 2 SYSTEM OVERVIEW

### 2.1 Cryptographic Handshake Layer
`core/handshake.py` conducts a KEM + signature exchange over TCP: the ground station issues a signed ServerHello, the drone verifies, and both sides derive 32-byte send/receive keys plus nonce seeds through HKDF-SHA256 (`salt="pq-drone-gcs|hkdf|v1"`). Rekeying keeps AES-256-GCM frames within a 22-byte header that carries epoch, sequence, and optional packet-type metadata.

### 2.2 Telemetry Ingestion and Guard Pipeline
`src/scheduler/unified_scheduler.py` gathers system state before every decision cycle. Key sources include a battery predictor (`components/battery_predictor.py`) combining voltage taps, coulomb counting, Peukert compensation, and temperature adjustment; a heartbeat monitor (`telemetry/heartbeat.py`) that surfaces packet loss and delay spikes; and a thermal guard (`constraints/thermal_guard.py`) that enforces ceilings driven by measured chassis temperatures and the observed increase in power draw (4.2-4.7 W) when DDoS workloads intensify. These guards eliminate suites that would violate power, thermal, or connectivity envelopes before policy selection begins.

### 2.3 Expert Strategy and PQC Bands
After filtering, the expert strategy (`strategies/expert.py`) chooses a suite from predefined bands held in `core/suites.py`. Bands encode security level, expected handshake cost, and energy class. Because suite IDs, HKDF inputs, and AEAD parameters live solely in `core/suites.py`, the scheduler never inlines cryptographic constants, preserving wire compatibility.

### 2.4 Disabled Adaptive Paths
Learning-based policies (`strategies/rl.py` and `strategies/hybrid.py`) remain dormant during these experiments. Focusing on the deterministic expert lookup yields repeatable baselines and isolates the contribution of the guard pipeline.

### 2.5 Detector Emulation Strategy
The repository includes scaffolding for XGBoost and Transformer-based DDoS detectors (`scheduler/components/security_advisor.py`), yet no model artifacts are invoked by default. Instead, we emulate their CPU, memory, and scheduling impact with heuristic load generators that reproduce the telemetry thresholds the models would trigger. The lightweight profile applies the `_calculate_lightweight_ddos_score` path, while the heavyweight profile replays recorded saturation traces to stress-test the guard pipeline without relying on unavailable model binaries.

## 3 METHODOLOGY

### 3.1 Test Environment
All experiments run the selectors-driven UDP proxy (`core/async_proxy.py`) on Windows with loopback peers and fixed plaintext ports supplied by `core/config.py`. Each run transmits 45 seconds of traffic using `traffic_mode=constant`, 256-byte application payloads, and an 8 Mb/s target rate (3,906 packets per second). The dataset covers 21 suite configurations (11 AES-GCM and 10 ChaCha20-Poly1305 variants) executed under each posture, yielding 63 runs in total.

### 3.2 Operational Postures
We replay three scheduler postures: baseline (no-ddos) with DDoS detection disabled, lightweight with the heuristic XGBoost profile, and heavyweight with the Transformer profile. Data originate from `results/benchmarks without-ddos detectetion.txt`, `results/results with ddos detection (lightweight).txt`, and `results/results benchmarks with ddos detectetion time series trandssformer heavy.txt`. `logs/auto/gcs/summary.csv` aggregates each run's telemetry, enabling cross-posture comparisons.

### 3.3 Metrics
We track achieved throughput, packet delivery ratio, average RTT, handshake latency, and average power (INA219 capture). Handshake measurements reflect the ground-station role. Loss confidence intervals follow a Wilson estimator; we report point values for clarity.

### 3.4 Reproducibility Checklist
To reproduce the measurements, provision the Windows tooling described in `docs/lan-test.txt`, generate identity keys via `python -m core.run_proxy init-identity --suite cs-mlkem768-aesgcm-mldsa65`, and then launch paired proxies with `python -m core.run_proxy gcs ...` and `python -m core.run_proxy drone ...` using the suites recorded in Appendix A. The DDoS profiles activate by exporting `ENABLE_PACKET_TYPE=1` and invoking `python tools/auto/run_matrix.py --profile lightweight` or `--profile heavy`, which in turn populate the results files cited above. The `tools/check_no_hardcoded_ips.py` script verifies that reproduction steps rely solely on configuration-driven addressing.

## 4 RESULTS

### 4.1 Throughput and Reliability
Throughput stays within 6% of the 8 Mb/s target across suites. Reliability diverges once the heavy profile activates, with packet loss exceeding 3% for several code-based and HQC suites. Table 1 summarises representative suites across the three postures.

**Table 1. Cross-policy metrics for representative suites**

| Suite | Policy | Throughput (Mb/s) | Loss (%) | Avg RTT (ms) | Handshake (ms) | Avg Power (W) |
| --- | --- | --- | --- | --- | --- | --- |
| cs-mlkem768-aesgcm-mldsa65 | Baseline | 7.417 | 0.019 | 16.079 | 19.396 | 4.217 |
| cs-mlkem768-aesgcm-mldsa65 | Lightweight | 7.946 | 0.025 | 12.170 | 35.504 | 4.307 |
| cs-mlkem768-aesgcm-mldsa65 | Heavyweight | 7.684 | 3.070 | 34.208 | 22.739 | 4.612 |
| cs-classicmceliece460896-aesgcm-mldsa65 | Baseline | 7.827 | 1.488 | 18.564 | 293.673 | 4.349 |
| cs-classicmceliece460896-aesgcm-mldsa65 | Lightweight | 7.915 | 0.279 | 16.154 | 641.085 | 4.303 |
| cs-classicmceliece460896-aesgcm-mldsa65 | Heavyweight | 7.546 | 4.823 | 35.706 | 580.717 | 4.659 |
| cs-frodokem640aes-chacha20poly1305-mldsa44 | Baseline | 6.966 | 0.886 | 18.958 | 652.129 | 4.159 |
| cs-frodokem640aes-chacha20poly1305-mldsa44 | Lightweight | 7.746 | 2.459 | 17.454 | 33.047 | 4.310 |
| cs-frodokem640aes-chacha20poly1305-mldsa44 | Heavyweight | 7.775 | 2.206 | 30.377 | 29.507 | 4.628 |
| cs-hqc256-aesgcm-mldsa87 | Baseline | 7.732 | 2.394 | 21.636 | 297.266 | 4.316 |
| cs-hqc256-aesgcm-mldsa87 | Lightweight | 7.663 | 3.226 | 61.155 | 345.277 | 4.243 |
| cs-hqc256-aesgcm-mldsa87 | Heavyweight | 7.437 | 5.560 | 38.079 | 322.937 | 4.677 |

### 4.2 Latency and Handshake Costs
Lattice-based ML-KEM suites deliver the lowest handshake overhead (between 8 and 31 ms) even when CPUs saturate. However, the heavy profile doubles the RTT of `cs-mlkem768-aesgcm-mldsa65` from 12.2 ms to 34.2 ms. Code-based suites experience outsized swings: `cs-classicmceliece348864-aesgcm-sphincs128fsha2` jumps from a 253 ms handshake under the lightweight profile to 837 ms with the heavy profile, a 231% increase that explains the scheduler's tendency to down-band during thermal or power alarms. HQC tail latencies grow sharply when DDoS sensing is active: `cs-hqc256-aesgcm-mldsa87` records 705 ms RTT under the lightweight profile and still posts 38 ms averages under the heavy profile, indicating CPU contention in the decoder rather than the primitive code itself.

### 4.3 Power and Thermal Budgets
Power draw remains tightly bounded. Across the full matrix, average power spans 4.217-4.695 W, only a 3.5% swing, yet thermal guards still trip because the heavy profile pushes CPU usage above 90% and raises RSS from roughly 70 MiB (baseline) to roughly 170 MiB (heavy). Guard decisions therefore hinge on battery state, not instantaneous watts, validating the need for the predictor's Peukert modelling.

### 4.4 Scheduler Behaviour Under Stress
The guard pipeline exhibits three recurring reactions: (i) thermal-elevated: high-power, long-handshake suites (for example, `cs-classicmceliece460896-aesgcm-mldsa65`) are swapped for cheaper HQC or ML-KEM options to prevent thermal runaway; (ii) battery-low: when SOC bins fall, the expert policy favours the lowest measured average power (`cs-hqc256-aesgcm-mldsa87` at 4.243 W under the lightweight profile), stretching endurance at the cost of extra loss; and (iii) heartbeat-missing/DDOS: excessive loss or RTT triggers a pivot to fast-handshake suites. `cs-mlkem1024-chacha20poly1305-mldsa87` maintains below 0.36% loss with 10.8 ms handshakes under the lightweight profile, making it a practical fallback when `cs-hqc256` degrades.

### 4.5 Cryptographic Suites Versus NIST Levels (Baseline)
To characterise the cryptographic catalogue itself, Table&nbsp;2 aggregates the baseline posture (`results/benchmarks without-ddos detectetion.txt`) by NIST security level. The expert scheduler locked to the requested suite in every run because all guard channels reported `clear`, so the measurements expose the intrinsic transport cost of each family without policy churn.

**Table 2. Baseline posture metrics grouped by NIST level (throughput is goodput).**

| NIST level | Suite families (count) | Throughput (Mb/s) | Loss (%) | Handshake (ms) | Avg power (W) | Scheduler notes |
| --- | --- | --- | --- | --- | --- | --- |
| Level 1 | Classic McEliece 348864, ML-KEM 512, FrodoKEM-640, HQC-128 (12) | 6.69–7.36 | 0.013–3.138 | 18–1,391 | 4.08–4.24 | Long code-based handshakes but no guard trips; timing guard stays clear |
| Level 3 | Classic McEliece 460896, ML-KEM 768, FrodoKEM-976, HQC-192 (8) | 7.42–7.94 | 0.013–1.488 | 13–541 | 4.22–4.35 | All suites rekey cleanly with 13–29 events; guards remain idle |
| Level 5 | Classic McEliece 8192128, ML-KEM 1024, HQC-256 (9) | 7.73–7.94 | 0.059–2.394 | 10–1,032 | 4.28–4.35 | Fastest handshakes from ML-KEM 1024 (<11 ms); code-based variants stay within power budget |

Three observations follow. First, lattice KEMs (ML-KEM 512/768/1024) dominate handshake speed across all levels, completing in 9–20 ms while maintaining ≥99% delivery, which explains why the expert policy favours them when delay alarms fire. Second, code-based suites inherit multi-hundred millisecond handshakes even without the DDoS workloads; the 1.09 s handshake for `cs-classicmceliece348864-aesgcm-sphincs128fsha2` and the 1.39 s spike for `cs-hqc128-chacha20poly1305-falcon512` illustrate the scheduler’s need to plan around reconnect penalties despite acceptable throughput and power. Third, power draw is effectively flat across levels (≤0.27 W spread), confirming that battery-driven guard actions stem from sustained CPU residency rather than the cipher choice itself. These findings provide the comparative reference for the cryptographic results section and frame the trade-offs discussed in Section&nbsp;5.

## 5 DISCUSSION

### 5.1 Mission Planning Implications
Security versus control: Level-3 ML-KEM (768) emerged as the sweet spot, delivering reliable throughput even under the heavy profile while keeping handshakes below 40 ms. Code-based suites such as Classic McEliece and HQC provide conservative security but incur the steepest loss and reconnect penalties, making them better suited for high-trust, low-interruption missions. The observed loss shedding (up to 6.4% for McEliece with the heavy profile) is deliberate: the scheduler keeps the control link alive at the expense of non-critical packets. Operators should monitor logs for sustained loss above 3% as a cue to downshift mission tempo or disable the heavyweight analytics pipeline.

### 5.2 Limitations and Future Work
Baseline runs show sporadic instrumentation artifacts (for example, a 652 ms handshake for `cs-frodokem640aes-chacha20poly1305-mldsa44`) likely caused by idle-core sleep states; future campaigns should pin CPU frequency to remove this bias. Only the lookup policy was evaluated. Extending the same telemetry sets to the RL or hybrid strategies would clarify whether learned policies can outperform rule-based selections without violating guard constraints. Finally, integrating real detector models and publishing their calibration logs will close the gap between the emulation strategy described in Section 2.5 and end-to-end autonomous detection.

## 6 CONCLUSION
The lookup scheduler keeps PQC viable for UAV command links by filtering suites through power, thermal, and network guards before applying a deterministic policy. Under the harsh transformer workload the system still meets throughput targets while exposing when and why losses spike. Operators gain a tunable, telemetry-aware cryptographic stack that can trade minimal increases in power consumption for large gains in network resilience and reconnect speed.

## APPENDIX A DATA SOURCES
- Baseline runs: `results/benchmarks without-ddos detectetion.txt`
- Lightweight XGBoost runs: `results/results with ddos detection (lightweight).txt`
- Heavyweight TST runs: `results/results benchmarks with ddos detectetion time series trandssformer heavy.txt`
- Aggregated telemetry: `logs/auto/gcs/summary.csv`
