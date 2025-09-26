#!/usr/bin/env bash
set -u

usage() {
  cat <<'EOF' >&2
Usage: ./matrix_runner_drone.sh [options] [suite1 suite2 ...]

Options:
  --duration SEC           Duration for standard suites (default: 25)
  --slow-duration SEC      Duration for SPHINCS+ suites (default: 90)
  --pkts COUNT             Total packets to send per run (default: 200)
  --rate PPS               Packet rate for traffic generator (default: 50)
  --outdir PATH            Output directory for logs & summaries (default: ./logs)
  --secrets-dir PATH       Directory containing suite signing material (default: ./secrets)
  --handshake-timeout SEC  Timeout in seconds to wait for proxy handshake (default: 30)
  --suites ID1,ID2         Comma-separated suite list; can be repeated
  -f SEC                   Alias for --duration
  -s SEC                   Alias for --slow-duration
  -h, --help               Show this help message

If no suites are provided, the canonical set from core.suites.list_suites() is used.
EOF
}

FAST_SECS=25
SLOW_SECS=90
PKTS=200
RATE=50
OUTDIR="$(pwd)/logs"
SECRETS_DIR=""
HANDSHAKE_TIMEOUT=30
HANDSHAKE_PATTERN="PQC handshake completed successfully"
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
    --secrets-dir) SECRETS_DIR="$2"; shift 2 ;;
    --handshake-timeout) HANDSHAKE_TIMEOUT="$2"; shift 2 ;;
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

if [ ${#clean_suites[@]} -eq 0 ]; then
  echo "ERROR: No suites specified after parsing inputs" >&2
  exit 1
fi

IFS=$'\n' read -r -d '' -a SUITES < <(printf '%s\n' "${clean_suites[@]}" | sort -u && printf '\0')
total_suites=${#SUITES[@]}

if [ -z "$SECRETS_DIR" ]; then
  SECRETS_DIR="$REPO_ROOT/secrets"
fi

MATRIX_SECRETS="$SECRETS_DIR/matrix"
if [ ! -d "$MATRIX_SECRETS" ]; then
  echo "ERROR: Missing matrix secrets directory at $MATRIX_SECRETS" >&2
  echo "       Sync secrets/matrix from the GCS host before running." >&2
  exit 1
fi

LOGS_ROOT="$OUTDIR/matrix"
HANDSHAKE_DIR="$LOGS_ROOT/handshake"
TRAFFIC_DIR_ROOT="$LOGS_ROOT/traffic"
SUMMARY_CSV="$LOGS_ROOT/drone_matrix_summary.csv"

mkdir -p "$LOGS_ROOT" "$HANDSHAKE_DIR" "$TRAFFIC_DIR_ROOT"

safe_name() {
  printf '%s' "$1" | sed 's/[^A-Za-z0-9_-]/_/g'
}

wait_for_handshake() {
  local log_file="$1"
  local timeout_sec="$2"

  printf '[DRONE] Waiting for handshake signal in %s (timeout %ss)\n' "$log_file" "$timeout_sec"

  if timeout "${timeout_sec}s" tail -f "$log_file" 2>/dev/null | grep -q "$HANDSHAKE_PATTERN"; then
    printf '[DRONE] Handshake signal detected\n'
    return 0
  fi

  printf '[DRONE] WARNING: Timed out waiting for handshake in %s\n' "$log_file" >&2
  return 1
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
os.makedirs(os.path.dirname(csv_path), exist_ok=True)
with open(csv_path, "a", encoding="utf-8", newline="") as fh:
    writer = csv.DictWriter(fh, fieldnames=row.keys())
    if write_header:
        writer.writeheader()
    writer.writerow(row)
PY
}

(
  IFS=', '
  printf '[DRONE] Suite plan: %s\n' "${SUITES[*]}"
)

suite_index=0
for suite in "${SUITES[@]}"; do
  suite_index=$((suite_index + 1))
  if [[ "$suite" == *sphincs* ]]; then
    duration="$SLOW_SECS"
  else
    duration="$FAST_SECS"
  fi

  safe="$(safe_name "$suite")"
  key_dir="$MATRIX_SECRETS/$safe"
  pub_path="$key_dir/gcs_signing.pub"

  if [ ! -f "$pub_path" ]; then
    printf 'ERROR: Missing GCS public key for suite %s (%s)\n' "$suite" "$pub_path" >&2
    exit 1
  fi

  proxy_json="$LOGS_ROOT/drone_${safe}.json"
  traffic_dir="$TRAFFIC_DIR_ROOT/$safe"
  mkdir -p "$traffic_dir"

  traffic_out="$traffic_dir/drone_events.jsonl"
  traffic_summary="$traffic_dir/drone_summary.json"
  proxy_log="$HANDSHAKE_DIR/drone_${safe}_handshake.log"
  : >"$proxy_log"

  printf '[DRONE][%d/%d] Starting proxy for suite %s (%s s)\n' "$suite_index" "$total_suites" "$suite" "$duration"
  suite_start=$(date +%s)
  $PYTHON_BIN -m core.run_proxy drone \
    --suite "$suite" \
    --peer-pubkey-file "$pub_path" \
    --stop-seconds "$duration" \
    --json-out "$proxy_json" \
    --quiet >"$proxy_log" 2>&1 &
  proxy_pid=$!

  printf '[DRONE][%d/%d] Waiting for handshake signal\n' "$suite_index" "$total_suites"
  handshake_timeout="$HANDSHAKE_TIMEOUT"
  if ! wait_for_handshake "$proxy_log" "$handshake_timeout"; then
    printf 'WARNING: Skipping traffic for suite %s due to handshake failure\n' "$suite" >&2
    kill "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
    continue
  fi
  printf '[DRONE][%d/%d] Handshake confirmed\n' "$suite_index" "$total_suites"

  run_duration=$((duration - 5))
  if [ "$run_duration" -le 0 ]; then
    run_duration="$duration"
  fi

  printf '[DRONE][%d/%d] Running traffic generator\n' "$suite_index" "$total_suites"
  if ! $PYTHON_BIN -m tools.traffic_drone \
      --count "$PKTS" \
      --rate "$RATE" \
      --duration "$run_duration" \
      --out "$traffic_out" \
      --summary "$traffic_summary"; then
    traffic_rc=$?
    printf 'WARNING: traffic_drone exited with code %s\n' "$traffic_rc" >&2
  fi

  wait "$proxy_pid"
  proxy_rc=$?
  if [ "$proxy_rc" -ne 0 ]; then
    printf 'WARNING: proxy exited with code %s\n' "$proxy_rc" >&2
  fi

  if [ ! -f "$proxy_json" ] || [ ! -f "$traffic_summary" ]; then
    printf 'WARNING: Missing outputs for suite %s\n' "$suite" >&2
    continue
  fi

  append_csv "$suite" "$proxy_json" "$traffic_summary"

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
  suite_end=$(date +%s)
  elapsed=$((suite_end - suite_start))
  printf '[DRONE][%d/%d] Completed suite %s in %ds (sent=%s recv=%s drops=%s)\n' \
    "$suite_index" "$total_suites" "$suite" "$elapsed" "$sent_total" "$recv_total" "$drops_total"
done
