# Loopback Matrix Runner

The helper `scripts/run_loopback_matrix.py` launches the drone follower and GCS scheduler locally, drives both blast and saturation runs, and shuts everything down when each scenario completes. It is designed for fast smoke tests on a single host.

## Requirements
- Python 3.10+
- At least one suite defined in `core.suites` with matching secrets in `secrets/matrix/<suite>/`.
- Loopback (127.0.0.1) connectivity is assumed. Override hosts with environment variables if needed.

## Quick Start
```powershell
# (Optional) activate your virtual environment first
python scripts/run_loopback_matrix.py --suites cs-mlkem768-aesgcm-mldsa65 cs-mlkem512-aesgcm-mldsa44
```

The script will:
- Set `AUTO_DRONE`/`AUTO_GCS` env values for each scenario.
- Start the follower, wait a few seconds, run the scheduler, and collect logs under `artifacts/loopback_matrix/<timestamp>_<scenario>/`.
- Run the following scenarios by default:
  - Blast (telemetry on)
  - Blast with telemetry disabled
  - Blast with monitors disabled
  - Saturation (linear search)
  - Saturation (auto search)
  - Saturation with telemetry disabled

If a scenario fails, the run stops and the corresponding directory contains `status.txt` plus stdout/stderr logs for investigation.

## Useful Flags
- `--scenarios blast saturation_auto` — run a subset by name.
- `--startup-delay 6` — wait longer after starting the follower before launching the scheduler.
- `--timeout 900` — extend the per-scenario scheduler timeout (seconds).
- `--dry-run` — print the prepared environment values without executing anything.
- `--output-dir D:\runs\matrix` — override the artifact root.

## Cleanup
Each scenario terminates the follower automatically. If the script aborts unexpectedly, you can stop any residual follower process with:
```powershell
Get-Process -Name python | Where-Object { $_.Path -like '*drone_follower.py' } | Stop-Process
```

## Extending the Matrix
Scenario definitions live near the top of `scripts/run_loopback_matrix.py`. Add or tweak entries there to cover additional combinations (different payload sizes, rate sweeps, manual suite lists, etc.).
