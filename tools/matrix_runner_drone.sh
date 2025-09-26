#!/usr/bin/env bash
set -u

FAST_SECS=25
SLOW_SECS=90

while getopts "f:s:" opt; do
  case "$opt" in
    f) FAST_SECS=$OPTARG ;;
    s) SLOW_SECS=$OPTARG ;;
    *) echo "Usage: $0 [-f fast_seconds] [-s slow_seconds] suite1 [suite2 ...]" >&2; exit 1 ;;
  esac
done
shift $((OPTIND-1))

if [ $# -lt 1 ]; then
  echo "Usage: $0 [-f fast_seconds] [-s slow_seconds] suite1 [suite2 ...]" >&2
  exit 1
fi

PYTHON_BIN=${PYTHON_BIN:-python3}
LOGS_ROOT="$(pwd)/logs"
SUMMARY_CSV="$LOGS_ROOT/matrix_drone_summary.csv"

mkdir -p "$LOGS_ROOT"

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

for suite in "$@"; do
  if [[ "$suite" == *sphincs* ]]; then
    duration="$SLOW_SECS"
  else
    duration="$FAST_SECS"
  fi

  safe="$(safe_name "$suite")"
  proxy_json="$LOGS_ROOT/drone_${safe}.json"
  traffic_dir="$LOGS_ROOT/traffic/$suite"
  mkdir -p "$traffic_dir"
  traffic_out="$traffic_dir/drone_events.jsonl"
  traffic_summary="$traffic_dir/drone_summary.json"

  echo "[DRONE] Starting proxy for suite $suite ($duration s)"
  $PYTHON_BIN -m core.run_proxy drone --suite "$suite" --stop-seconds "$duration" --json-out "$proxy_json" --quiet &
  proxy_pid=$!

  sleep 3

  run_duration=$((duration - 5))
  if [ "$run_duration" -le 0 ]; then
    run_duration=$duration
  fi

  echo "[DRONE] Running traffic generator"
  if ! $PYTHON_BIN tools/traffic_drone.py --count 200 --rate 50 --duration "$run_duration" --out "$traffic_out" --summary "$traffic_summary"; then
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
done
