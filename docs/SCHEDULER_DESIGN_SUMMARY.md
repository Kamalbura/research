# UAV Scheduler and Telemetry Fusion: Design Summary

This document captures the minimal, practical design needed for paper-quality experiments while keeping code changes low-risk for the proxy.

## Modeling approaches

- Theoretical (physics-first)
  - Battery: Peukert-like discharge, propulsion power P ≈ k1·v_h^3 + k2·|v_v|·W + overhead; calibrate with INA219 traces.
  - Thermal: first-order RC with ambient; fit parameters from psutil/vcgencmd during suite sweeps.
- Practical (data-first)
  - Train regressors from DJI/in-house logs and INA219 tracks; prefer monotone models for interpretability.
  - Replay logs with real timestamps; no simulation timewarp.

Recommendation: report both; use theoretical for safety guards and practical for fine-grained efficiency ranking.

## Telemetry fusion and dataset

- Source assets
  - GCS `logs/auto/gcs/summary.csv` (throughput, goodput, loss, blackout, rekey, power summary refs)
  - Drone `power_*.csv` + `summary_json` from INA219 captures
  - Telemetry status `telemetry_status.json` (connectivity heartbeat)
- Builder
  - `tools/dataset/build_master_dataset.py` writes `master_dataset.csv` and `.parquet`
  - Adds peak/gradient features from power trace; preserves all summary fields

## Scheduler strategies

- Expert: rules over battery SoC, thermal state, and threat level; maps to suite and DDOS tier.
- RL: trained on master dataset (no simulators); consumes time-aligned features; emits target suite and tier.
- Hybrid: fuses RL/expert with hysteresis and confidence threshold.

Adapters live in `src/scheduler/strategies/` to decouple from `schedulers/` layout.

## DDOS dual-stage detection

- Lightweight: XGBoost-like features (loss, RTT p95, goodput/throughput gap)
- Heavyweight: Transformer/TST confirmation; invoked only on suspicious spikes or escalations
- Heartbeat: pre-encrypted UDP heartbeat for passive signalling during congestion; stop-after-N retries

## Guardrails

- No changes to core wire format, AEAD framing, or replay window.
- All network/port knobs continue through `core.config`.
- No secret logging; constant-time compare in heartbeat allowlist path.

## Next steps

- Add MAVLink ingestion (battery %, flight mode) to telemetry stream and dataset builder.
- Train lightweight DDOS and integrate scores into `unified_scheduler`.
- Publish ablation: no-DDOS vs light vs light+heavy; power/thermal overheads and resilience.
