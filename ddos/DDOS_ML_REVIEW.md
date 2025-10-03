# ML Engineering Review – MAVLink DDoS Stack

_Date: 2025-10-03_

This review walks through the code in `ddos/` from an ML engineer’s perspective, with an emphasis on data handling, model expectations, and runtime safeguards. The goal was to validate the detection pipeline, surface anomalies, and harden areas that could silently fail when artifacts drift.

## 1. Pipeline understanding

- **Feature flow:** Packet counts are aggregated into 0.60 s windows (`WINDOW_SIZE`). The XGBoost screener consumes the most recent `XGB_SEQ_LENGTH` counts, while the TST confirmer ingests `TST_SEQ_LENGTH` windows once the screener raises a streak of positives.
- **Preprocessing:** The confirmer normalizes counts with the persisted `StandardScaler` (`scaler.pkl`). All inference threads clamp PyTorch to a single CPU thread (`torch.set_num_threads(1)`), keeping latency predictable on the Raspberry Pi target.
- **Threading & gating:** Bounded queues (`XGB_QUEUE_MAX`, `TST_QUEUE_MAX`) and cooldowns (`TST_COOLDOWN_WINDOWS`) prevent overload when attacks persist. Rate-limited warnings surface queue pressure without spamming logs.
- **Operational scripts:** `run_xgboost.py` and `run_tst.py` validate models offline, while `manual_control_detector.py` enables lab toggling between the screener and confirmer.

## 2. Anomaly detected

**Symptom:** There was no runtime verification that the loaded scaler/model pair actually accepts sequences of length `TST_SEQ_LENGTH` or that the model outputs the expected class dimension. A mismatched TorchScript file, stale scaler, or retrained model with a different input shape would only fail deep inside the confirmer threads, resulting in cryptic stack traces or silent drops.

**Fix implemented:**
- Added a compatibility check in both `hybrid_detector.load_tst_model()` and `realtime_tst.load_tst_model()`:
  - Transform a zero vector using the loaded scaler to ensure preprocessing still works.
  - Run a dry inference with the transformed dummy tensor and verify the logits tensor is 2-D with at least two classes.
  - Raise a descriptive `ValueError` if either step fails so operators can correct artifacts before live capture.
- Normalized the import order at the top of `hybrid_detector.py` (the file previously triggered a `from __future__` ordering error under compilation checks).

The verification runs during start-up, so misconfigured artifacts are caught immediately instead of after the system begins sniffing traffic.

## 3. Validation

- Executed `python -m py_compile ddos/hybrid_detector.py ddos/realtime_tst.py` to confirm the refactor compiles cleanly.

## 4. Recommendations

- Keep XGBoost feature counts in sync with `XGB_SEQ_LENGTH`. If retraining changes the window count, update the config before deployment.
- Consider extending the offline diagnostics to load `scaler.pkl` alongside the screener so feature scaling mismatches are caught there too.
- When distributing new TorchScript binaries, re-run `run_tst.py` after copying the artifact to the device to double-check the threshold behaviour logged in production.

No additional issues were observed during this review. The detection loop now fails fast on artifact drift, improving reliability during redeployments.
