#!/usr/bin/env bash
# tools/pi_check_env.sh
# Usage: ./tools/pi_check_env.sh
# This script is intended to be run on the Raspberry Pi (drone).
# It performs a repo git pull, attempts to activate ~/cenv, and produces a JSON/text report under logs/pi_check/<ts>/

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT_DIR="$REPO_ROOT/logs/pi_check/$TIMESTAMP"
mkdir -p "$OUT_DIR"
REPORT_JSON="$OUT_DIR/report.json"
REPORT_TXT="$OUT_DIR/report.txt"

echo "[pi_check] Repo root: $REPO_ROOT"
cd "$REPO_ROOT"

# 1) Ensure working tree is updated
echo "[pi_check] Running sudo git pull..." | tee -a "$REPORT_TXT"
sudo git pull 2>&1 | tee -a "$REPORT_TXT"

# 2) Activate virtualenv if present
VE_PATH="$HOME/cenv/bin/activate"
ACTIVATED=false
if [ -f "$VE_PATH" ]; then
  echo "[pi_check] Activating virtualenv at $VE_PATH" | tee -a "$REPORT_TXT"
  # shellcheck disable=SC1090
  . "$VE_PATH"
  ACTIVATED=true
else
  echo "[pi_check] No virtualenv found at $VE_PATH. Not activating." | tee -a "$REPORT_TXT"
fi

# 3) Collect environment info using Python
PY_REPORT="$OUT_DIR/py_env_report.json"
python - <<'PY' > "$PY_REPORT" 2>> "$REPORT_TXT"
import json, sys
from pathlib import Path

info = {}
try:
    import platform
    info['python_executable'] = sys.executable
    info['python_version'] = platform.python_version()
    info['platform'] = platform.platform()
except Exception as e:
    info['python_error'] = repr(e)

# pip freeze
try:
    import pkg_resources
    pkgs = {p.key: p.version for p in pkg_resources.working_set}
    info['packages'] = pkgs
except Exception:
    try:
        import pip
        from subprocess import check_output
        out = check_output([sys.executable, '-m', 'pip', 'freeze']).decode('utf-8')
        info['pip_freeze'] = out.splitlines()
    except Exception as e:
        info['pip_error'] = repr(e)

# specific module checks
mods = {}
for m in ('oqs', 'oqs.oqs', 'cryptography', 'cryptography.hazmat'):
    try:
        __import__(m)
        mods[m] = 'ok'
    except Exception as e:
        mods[m] = repr(e)
info['module_checks'] = mods

# core repo checks
repo = Path(__file__).resolve().parents[2]
info['repo_root'] = str(repo)
info['repo_cwd'] = str(Path.cwd())
# git status
try:
    from subprocess import check_output
    info['git_branch'] = check_output(['git','rev-parse','--abbrev-ref','HEAD']).decode().strip()
    info['git_commit'] = check_output(['git','rev-parse','HEAD']).decode().strip()
except Exception as e:
    info['git_error'] = repr(e)

print(json.dumps(info, indent=2))
PY

# 4) Summarize into report.json
jq -n --slurpfile py "$PY_REPORT" '{timestamp:$ENV.TIMESTAMP, activated:env.ACTIVATED|test("true"), python:$py[0]}' > "$REPORT_JSON" 2>> "$REPORT_TXT" || true

# Fallback if jq not installed: copy py_report
if [ ! -s "$REPORT_JSON" ]; then
  cp "$PY_REPORT" "$REPORT_JSON"
fi

echo "[pi_check] Wrote: $REPORT_JSON" | tee -a "$REPORT_TXT"
echo "[pi_check] Also wrote text log: $REPORT_TXT"

echo "Done"

exit 0
