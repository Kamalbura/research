#!/usr/bin/env bash
set -euo pipefail

SERIAL_DEVICE="${SERIAL:-/dev/ttyACM0}"
MAVPROXY=${MAVPROXY_BINARY:-mavproxy.py}

# Ports come from core.config: DRONE_PLAINTEXT_TX (47003) and DRONE_PLAINTEXT_RX (47004).
exec "${MAVPROXY}" \
  --master="${SERIAL_DEVICE}" \
  --out=udp:127.0.0.1:47003 \
  --out=udp-listen:127.0.0.1:47004 \
  --streamrate=10 \
  "${@}"
