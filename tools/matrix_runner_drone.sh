#!/usr/bin/env bash
set -u

usage() {
  cat <<'EOF' >&2
Usage: ./matrix_runner_drone.sh [options] [suite1 suite2 ...]

Options:
  --duration SEC         Duration for standard suites (default: 25)
  --slow-duration SEC    Duration for SPHINCS+ suites (default: 90)
  --pkts COUNT           Total packets to send per run (default: 200)
  --rate PPS             Packet rate for traffic generator (default: 50)
  --outdir PATH          Output directory for logs & summaries (default: ./logs)
  --suites ID1,ID2       Comma-separated suite list; can be repeated
  -f SEC                 Alias for --duration
  -s SEC                 Alias for --slow-duration
  -h, --help             Show this help message

If no suites are provided, the canonical set from core.suites.list_suites() is used.
EOF
}

FAST_SECS=25
SLOW_SECS=90
PKTS=200
RATE=50
OUTDIR="$(pwd)/logs"
declare -a SUITES=()
AUTO_SUITES=0

PYTHON_BIN=${PYTHON_BIN:-python3}

ORIG_DIR=$(pwd)
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
trap 'cd "$ORIG_DIR"' EXIT
cd "$REPO_ROOT"

while [ $# -gt 0 ]; do
  case "$1" in
    --duration) FAST_SECS="$2"; shift 2 ;;
    --slow-duration) SLOW_SECS="$2"; shift 2 ;;
    --pkts) PKTS="$2"; shift 2 ;;
    --rate) RATE="$2"; shift 2 ;;
    --outdir) OUTDIR="$2"; shift 2 ;;
    --suites)
      IFS=',' read -r -a tmp <<<"$2"
      SUITES+=("${tmp[@]}")
      shift 2
      ;;
    -f) FAST_SECS="$2"; shift 2 ;;
    -s) SLOW_SECS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --)
      shift
      break
      ;;
    --*)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
    *)
      SUITES+=("$1")
      shift
      ;;
  esac
done

if [ $# -gt 0 ]; then
  SUITES+=("$@")
fi

if [ ${#SUITES[@]} -eq 0 ]; then
  AUTO_SUITES=1
  mapfile -t SUITES < <("$PYTHON_BIN" - <<'PY'
import json
from core.suites import list_suites
for suite in list_suites().keys():
    print(suite)
PY
)
fi

if [ -z "$OUTDIR" ]; then
  OUTDIR="$(pwd)/logs"
fi

clean_suites=()
for entry in "${SUITES[@]}"; do
  IFS=',' read -r -a parts <<<"$entry"
  for part in "${parts[@]}"; do
    trimmed=$(printf '%s' "$part" | sed 's/^ *//;s/ *$//')
    if [ -n "$trimmed" ]; then
      clean_suites+=("$trimmed")
    fi
  done
done
if [ ${#clean_suites[@]} -gt 0 ]; then
  SUITES=("${clean_suites[@]}")
else
  SUITES=()
fi

OUTDIR=$("$PYTHON_BIN" - <<'PY' "$OUTDIR"
import os
import sys
print(os.path.abspath(sys.argv[1]))
PY
)

LOGS_ROOT="$OUTDIR"
SUMMARY_CSV="$LOGS_ROOT/matrix_drone_summary.csv"

mkdir -p "$LOGS_ROOT"

if [ $AUTO_SUITES -eq 1 ]; then
  IFS=$'\n' SUITES=($(printf '%s\n' "${SUITES[@]}" | sort))
fi
schedule_pretty=$(printf '%s, ' "${SUITES[@]}")
schedule_pretty=${schedule_pretty%, }
echo "[DRONE] Suite plan: $schedule_pretty"

total_suites=${#SUITES[@]}
suite_index=0

safe_name() {
  echo "$1" | sed 's/[^A-Za-z0-9_-]/_/g'
}

append_csv() {
  local suite="$1" proxy_json="$2" traffic_summary="$3"
  "$PYTHON_BIN" - <<'PY' "$suite" "$proxy_json" "$traffic_summary" "$SUMMARY_CSV"
import csv
import json
import os
import sys

suite, proxy_path, traffic_path, csv_path = sys.argv[1:5]
with open(proxy_path, "r", encoding="utf-8") as fp:
    proxy = json.load(fp)
with open(traffic_path, "r", encoding="utf-8") as fp:
    traffic = json.load(fp)
row = {
    "suite": suite,
    "host": "drone",
    "ptx_out": proxy["counters"]["ptx_out"],
    "ptx_in": proxy["counters"]["ptx_in"],
    "enc_out": proxy["counters"]["enc_out"],
    "enc_in": proxy["counters"]["enc_in"],
    "drops": proxy["counters"]["drops"],
    "drop_auth": proxy["counters"]["drop_auth"],
    "drop_header": proxy["counters"]["drop_header"],
    "drop_replay": proxy["counters"]["drop_replay"],
    "traffic_sent_total": traffic["sent_total"],
    "traffic_recv_total": traffic["recv_total"],
}
write_header = not os.path.exists(csv_path)
with open(csv_path, "a", encoding="utf-8", newline="") as fp:
    writer = csv.DictWriter(fp, fieldnames=row.keys())
    if write_header:
        writer.writeheader()
    writer.writerow(row)
PY
}

for suite in "${SUITES[@]}"; do
  suite_index=$((suite_index + 1))
  if [[ "$suite" == *sphincs* ]]; then
    duration="$SLOW_SECS"
  else
    duration="$FAST_SECS"
  fi

  safe="$(safe_name "$suite")"
  proxy_json="$LOGS_ROOT/drone_${safe}.json"
  traffic_dir="$LOGS_ROOT/traffic/$safe"
  mkdir -p "$traffic_dir"
  traffic_out="$traffic_dir/drone_events.jsonl"
  traffic_summary="$traffic_dir/drone_summary.json"

  printf '[DRONE][%d/%d] Starting proxy for suite %s (%s s)\n' "$suite_index" "$total_suites" "$suite" "$duration"
  suite_start=$(date +%s)
  $PYTHON_BIN -m core.run_proxy drone --suite "$suite" --stop-seconds "$duration" --json-out "$proxy_json" --quiet &
  proxy_pid=$!

  sleep 3

  run_duration=$((duration - 5))
  if [ "$run_duration" -le 0 ]; then
    run_duration=$duration
  fi

  printf '[DRONE][%d/%d] Running traffic generator\n' "$suite_index" "$total_suites"
  if ! $PYTHON_BIN -m tools.traffic_drone --count "$PKTS" --rate "$RATE" --duration "$run_duration" --out "$traffic_out" --summary "$traffic_summary"; then
    echo "WARNING: traffic_drone.py exited with code $?" >&2
  fi

  wait $proxy_pid
  proxy_rc=$?
  if [ $proxy_rc -ne 0 ]; then
    echo "WARNING: proxy exited with code $proxy_rc" >&2
  fi

  if [ ! -f "$proxy_json" ] || [ ! -f "$traffic_summary" ]; then
    echo "WARNING: Missing outputs for suite $suite" >&2
    continue
  fi

  append_csv "$suite" "$proxy_json" "$traffic_summary"

  suite_end=$(date +%s)
  elapsed=$((suite_end - suite_start))

  suite_summary=$("$PYTHON_BIN" - <<'PY' "$proxy_json" "$traffic_summary"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fp:
    proxy = json.load(fp)
with open(sys.argv[2], "r", encoding="utf-8") as fp:
    traffic = json.load(fp)

sent = traffic.get("sent_total", 0)
recv = traffic.get("recv_total", 0)
drops = proxy.get("counters", {}).get("drops", 0)
print(f"{sent}|{recv}|{drops}")
PY
)
  IFS='|' read -r sent_total recv_total drops_total <<<"$suite_summary"
  printf '[DRONE][%d/%d] Completed suite %s in %ds (sent=%s recv=%s drops=%s)\n' "$suite_index" "$total_suites" "$suite" "$elapsed" "$sent_total" "$recv_total" "$drops_total"
done
