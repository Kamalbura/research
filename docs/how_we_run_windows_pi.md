# How We Run Windows + Pi Safely

## Preparation
- Ensure both machines share the repo at the same revision. On Windows activate the venv (`py -m venv .venv && .\.venv\Scripts\activate`), on the Pi use the per-user venv (`~/cenv/bin/python`). Avoid `sudo`; if elevation is required, export `PYTHONPATH` to include the repo root first.
- Verify `core` imports locally: `python - <<'PY'
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
print('python', sys.executable)
print('has_core', (ROOT/'core').exists())
PY`
- Generate signing material once per suite with `python -m core.run_proxy init-identity --suite <suite>` on the Windows host; copy the resulting `secrets/matrix/<suite>` directory to the Pi.

## Run Order
1. **Pi follower:** `~/cenv/bin/python tools/auto/drone_follower.py` (add `--pi5` when running on Pi 5). The script prints the interpreter path, telemetry target, and session directory. Confirm it reports `telemetry publisher started` and waits for the GCS collector.
2. **Windows scheduler:** From the repo root `python tools/auto/gcs_scheduler.py`. The scheduler logs its interpreter, binds `telemetry collector -> 0.0.0.0:52080`, and launches the GCS proxy.
3. Observe that the follower reports `telemetry connected` within 60 s. Rekeys now pause traffic until both proxy and follower acknowledge the new suite.

## Validation Checklist
- Status and summary files appear under `logs/auto/gcs` (host) and `logs/auto/drone/<suite>` (Pi) without errors.
- Rekey transitions never report `fail` or `timeout`; the scheduler would abort if that happened.
- Telemetry CSV/JSONL files grow on both sides; disconnecting the collector temporarily triggers retries rather than crashes.
- After the run, combined Excel output lives under `output/gcs/<session>/` and drone monitor data under `output/drone/<session>/`.

## Recovery Steps
- If the follower warns about telemetry mismatch, update `AUTO_DRONE.telemetry_host` to the scheduler's bind address (`GCS_TELEMETRY_BIND`).
- If directories are missing, rerun; both scripts now create parents automatically.
- When switching suites, ensure the corresponding secrets exist on both machines (`secrets/matrix/<suite>`). Missing keys cause the follower to exit before spawning the proxy.
