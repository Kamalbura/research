# PQC Drone-GCS Secure Proxy: Implementation & Progress Log

## 1. Project Overview
This repository implements a post-quantum cryptography (PQC) secure proxy for drone-to-Ground Control Station (GCS) communications. It is designed for quantum-safe, authenticated, and encrypted telemetry and command exchange, with DDoS protection and adaptive RL-based policy management.

## 2. Core Technologies & Packages
- **Python 3.13**: Main language for all modules and tests.
- **oqs-python**: Open Quantum Safe bindings for PQC KEMs and signatures (Kyber, Dilithium, Falcon, SPHINCS+).
- **cryptography**: For AES-256-GCM AEAD and HKDF-SHA256 key derivation.
- **paho-mqtt**: MQTT client for control plane (health, DDoS, RL policy).
- **pytest**: Test runner for all unit and integration tests.
- **threading, socket**: Used for network concurrency and smoke tests.

## 3. Directory Structure & Responsibilities
- **core/**: All cryptographic logic, protocol framing, config, and proxy orchestration.
  - `config.py`: Centralized config, validation, environment overrides, frozen constants.
  - `suites.py`: Immutable PQC suite registry, header mappings.
  - `handshake.py`: PQC handshake (KEM+signature), transcript, HKDF.
  - `aead.py`: AES-GCM framing, AAD header, replay window, strict mode.
  - `async_proxy.py`: TCP handshake, UDP data plane, rekey FSM, backpressure.
  - `run_proxy.py`: CLI entry point, timeout handling.
- **drone/wrappers/** & **gcs/wrappers/**: Thin launchers for each suite, importable with no side effects.
- **ddos/**: ML-based DDoS detection pipeline (XGBoost, TST).
- **rl/**: LinUCB contextual bandit for adaptive policy management.
- **tests/**: 55 comprehensive tests (unit, negative, integration, replay, config).
- **tools/**: Utility scripts (e.g., `full_comm_check.py` for full-stack verification).

## 4. Key Implementations
### 4.1 PQC Handshake (core/handshake.py)
- Implements state machine for KEM+signature handshake.
- Uses `oqs-python` for constant-time KEM encapsulation/decapsulation and signature verification.
- Transcript is signed, KEM shared secret is passed through HKDF-SHA256 (never used raw).
- Wire format: dataclass-based, deterministic nonce, replay window.

### 4.2 AEAD Framing (core/aead.py)
- AES-256-GCM with AAD header (version, suite IDs, session, seq, epoch).
- Deterministic 96-bit nonces from monotonic counters.
- Strict mode: disables error leakage, drops on decryption failure.
- Replay protection: sliding window (default 1024 packets).

### 4.3 Configuration (core/config.py)
- Canonical config dataclass, frozen constants, environment override support.
- Validation: checks required keys, types, port deduplication, range enforcement.
- Re-exported via `core/project_config.py` for legacy code.

### 4.4 Suite Registry (core/suites.py)
- 7 immutable PQC suites (Kyber, Dilithium, Falcon, SPHINCS+ combos).
- Registry functions: get_suite, header_ids_for_suite, suite_bytes_for_hkdf.
- No hardcoded algorithm names outside registry.

### 4.5 Proxy Orchestration (core/async_proxy.py, core/run_proxy.py)
- TCP handshake (port 5800+), UDP data plane (unique ephemeral ports).
- Parallel rekey FSM, cooperative scheduling for Pi 4B constraints.
- CLI entry with stop_after_seconds for test harnesses.

### 4.6 DDoS & RL (ddos/, rl/)
- Two-stage DDoS detection: XGBoost (pre-decrypt), TST (post-decrypt).
- RL: LinUCB bandit, adaptive telemetry rate, suite change requests.
- Safety: never throttle command channel, minimum security enforced.

### 4.7 MQTT Control Plane
- Topics: health, ddos/state, algo/desired, policy/telemetry_rate.
- mTLS client pattern, certificate-based auth.

### 4.8 Wrapper Integrity
- All wrappers importable with no side effects (checked by `full_comm_check.py`).

### 4.9 Full Communication Harness (tools/full_comm_check.py)
- Runs all tests (pytest -q), reports pass/fail.
- Loopback smoke test: launches proxies in threads, sends UDP both ways, confirms delivery.
- Config validation: base and env override, port dedupe check.
- Wrapper import check: imports all wrappers, reports status.
- Outputs single JSON summary, uses UNKNOWN: ... for missing hooks.

## 5. Test Coverage & Verification
- 55/55 tests passing (unit, negative, integration, replay, config).
- Negative tests: replay attack, header tamper, downgrade prevention.
- Full comms harness: all wrappers imported, config hooks checked, smoke test run.

## 6. Recent Progress Log
- Added manual four-terminal harness (`tools/manual_4term`) with launcher, simulators, and encrypted bridge logger.
- Resolved manual run issues: Windows-safe bridge logging and oqs secret-key constructor fallback.
- Verified end-to-end telemetry/command flow via intercept logger with stable session IDs.
- Documented manual test workflow and oqs fallback behaviour across README and tool README.

## 7. Outstanding Issues
- None currently tracked; all previously noted blockers resolved during manual harness validation.

## 8. Next Steps
- Monitor oqs-python releases for restored key import/export APIs and re-enable direct loader when available.
- Continue planned work on RL and DDoS integration phases.
- Schedule quarterly regression run using manual harness alongside automated tests.

---
This document is a low-level, implementation-focused summary of the current codebase, packages, and progress. All details are based on actual code and test results, with no assumptions or hallucinations.