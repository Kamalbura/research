# Raspberry Pi MAVLink DDoS Detection Suite

Two-stage DDoS detection stack for a Raspberry Pi 4B flying a MAVLink-over-UDP link. A light XGBoost screener guards every 0.6 s window; a deep Time Series Transformer (TST) confirmer validates escalations. The codebase also includes a manual detector, offline diagnostics, and a synthetic simulator to exercise the gating logic.

## 1. Platform prerequisites

| Component | Minimum version | Notes |
|-----------|-----------------|-------|
| Raspberry Pi OS | Bullseye / Debian 11 | 64-bit preferred |
| Python | 3.9+ | Install via `sudo apt install python3 python3-venv` |
| Pip packages | `scapy`, `xgboost`, `torch`, `joblib`, `pandas`, `numpy` | Install in a venv |

### Optional but recommended packages
- `psutil` (telemetry)
- `python-systemd` (structured logging under systemd)

## 2. Prepare the Python environment

```bash
python3 -m venv ~/ddos-venv
source ~/ddos-venv/bin/activate
pip install --upgrade pip
pip install scapy xgboost torch joblib pandas numpy
```

Add the project directory to your `PYTHONPATH` if you plan to run scripts from other folders:
```bash
export PYTHONPATH="/home/pi/ddos:${PYTHONPATH}"
```

## 3. Required artifacts

All binaries live alongside the scripts by default (override paths in `config.py`).

| Artifact | Default path | How to create |
|----------|--------------|---------------|
| `xgboost_model.bin` | `./xgboost_model.bin` | Trained offline |
| `tst_model.torchscript` (preferred) | `./tst_model.torchscript` | `torch.jit.trace` or `torch.jit.script` your trained model |
| `tst_model.pth` (fallback) | `./tst_model.pth` | PTH state_dict; requires `tstplus.py` on device |
| `scaler.pkl` | `./scaler.pkl` | Persist the training `StandardScaler` via `joblib.dump(scaler, 'scaler.pkl')` |

If you only have the original CSVs, freeze the scaler like this:
```python
import pandas as pd
from sklearn.preprocessing import StandardScaler
import joblib

train = pd.read_csv("train_ddos_data_0.1.csv")
scaler = StandardScaler().fit(train[["Mavlink_Count"]])
joblib.dump(scaler, "scaler.pkl")
```

## 4. Configuration (`config.py`)

Modify once, reuse everywhere. Environment variables override defaults.

| Setting | Default | Description |
|---------|---------|-------------|
| `IFACE` | `wlan0` | Capture interface (`MAV_IFACE`) |
| `PORT` | `14550` | MAVLink UDP port (`MAV_UDP_PORT`) |
| `WINDOW_SIZE` | `0.60` | Window duration in seconds |
| `XGB_SEQ_LENGTH` | `5` | Screener lookback windows |
| `TST_SEQ_LENGTH` | `400` | Confirmer lookback windows |
| `XGB_CONSECUTIVE_POSITIVES` | `3` | Trigger gate for TST |
| `TST_COOLDOWN_WINDOWS` | `5` | Cooldown after a confirmation |
| `LOG_LEVEL_NAME` | `INFO` | Logging level (`DDOS_LOG_LEVEL`) |
| `LOG_FILE` | `stderr` | File sink (`DDOS_LOG_FILE`) |

Update model paths if you relocate binaries:
```python
XGB_MODEL_FILE = BASE_DIR / "models" / "xgboost_model.bin"
```

## 5. Running the detectors

> **Tip:** Always activate your venv first.

### Hybrid screener + confirmer
```bash
sudo --preserve-env=PYTHONPATH,DDOS_LOG_LEVEL python3 hybrid_detector.py
```
Outputs one log per 0.6 s window, queues TST on streaks, and enforces cooldown.

### Manual control detector
```bash
sudo python3 manual_control_detector.py
```
Interactive shell:
- `1` → XGBoost only
- `2` → TST only
- `q` → Quit

### Realtime TST-only pipeline
```bash
sudo python3 realtime_tst.py
```
Useful for validating pure TST latency on the deployment device.

## 6. Diagnostics & simulation

| Script | Purpose |
|--------|---------|
| `run_xgboost.py` | Confirms model compatibility and prints sample predictions |
| `run_tst.py` | Loads scaler + TST, runs against the CSV test slice, reports probabilities |
| `tools/sim_driver.py` | Generates synthetic counts (benign/pulse/flood) and exercises the screener gate and optional TST |

Example simulator run:
```bash
python3 tools/sim_driver.py pulse --run-tst
```

## 7. Logging & observability

- Logging defaults to stdout/stderr; point `DDOS_LOG_FILE` at `/var/log/pqcdetect.log` for persistent storage.
- Each log line contains timestamp, thread, and the most recent window summary.
- TST confirmations are logged at `WARNING` level.

## 8. Systemd integration

Install the provided units (edit `User`, `WorkingDirectory`, and `Environment` for your setup):
```bash
sudo cp ddos-hybrid.service /etc/systemd/system/
sudo cp ddos-tst-realtime.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ddos-hybrid.service
```

## 9. Acceptance checklist

- [ ] `scaler.pkl` present and matches training pipeline
- [ ] `xgboost_model.bin` and `tst_model.torchscript` reachable via `config.py`
- [ ] `MAV_IFACE`, `MAV_UDP_PORT` exported for non-default hardware
- [ ] `python3 hybrid_detector.py` starts cleanly and logs windows
- [ ] `tools/sim_driver.py flood --run-tst` shows TST trigger with cooldown respected
- [ ] systemd units run from boot and restart on failure (`systemctl status` clean)

## 10. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Scapy` import failure | `pip install scapy` inside venv |
| `FileNotFoundError: scaler.pkl` | Generate with the snippet above |
| TorchScript unavailable | Place `tst_model.pth` and ensure `tstplus.py` is present |
| High CPU on capture thread | Confirm BPF filter matches port/payload and interface |
| No TST triggers under attack | Lower `XGB_CONSECUTIVE_POSITIVES` or validate screener training |

Fly safe! 🛩️
