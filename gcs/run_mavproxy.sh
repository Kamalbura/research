#!/usr/bin/env bash
set -euo pipefail

MAVPROXY=${MAVPROXY_BINARY:-mavproxy.py}

# GCS_PLAINTEXT_RX in core.config defaults to 47002; connect console/map here.
exec "${MAVPROXY}" \
  --master=udp:127.0.0.1:47002 \
  --console --map \
  "${@}"
