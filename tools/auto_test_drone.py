#!/usr/bin/env python3
"""Drone-side runner for automated matrix tests.

Usage: python tools/auto_test_drone.py --gcs-host 100.101.93.23 --gcs-port 47010

Behavior:
- Connect to GCS control TCP port, wait for JSON command.
- Command will include suite, count, udp_dest [host,port].
- Send 'count' UDP messages to udp_dest; for each message include a sequence number and timestamp.
- Listen for replies on the same UDP socket and compute RTT per message.
- Send results JSON back to GCS over the TCP control connection.
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import time
import sys
from pathlib import Path
from typing import Tuple

# Ensure repository root is on sys.path when executed directly
_HERE = Path(__file__).resolve()
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tools.socket_utils import open_udp_socket, close_socket


def now_ms() -> float:
    return time.time() * 1000.0


def run_test(control_sock: socket.socket, cmd: dict):
    udp_host, udp_port = cmd.get('udp_dest', ['127.0.0.1', 47001])
    count = int(cmd.get('count', 8))
    suite = cmd.get('suite', 'unknown')

    print(f'Running test: suite={suite} count={count} -> {udp_host}:{udp_port}')

    # Use an ephemeral bound UDP socket so replies are received reliably and
    # the socket is registered with our cleanup helper.
    sock = open_udp_socket('0.0.0.0', 0, timeout=2.0)

    results = []
    for i in range(count):
        payload = json.dumps({'seq': i, 'ts': now_ms(), 'suite': suite}).encode('utf-8')
        send_t = now_ms()
        try:
            sock.sendto(payload, (udp_host, int(udp_port)))
        except Exception as e:
            results.append({'seq': i, 'error': f'send-fail: {e}'})
            continue

        try:
            data, addr = sock.recvfrom(8192)
            recv_t = now_ms()
            # Expect the peer to echo back or the proxy to return something
            results.append({'seq': i, 'rtt_ms': (recv_t - send_t), 'reply_len': len(data)})
        except socket.timeout:
            results.append({'seq': i, 'error': 'timeout'})

        # small pacing
        time.sleep(0.05)

    try:
        # send results back over control socket
        out = {'suite': suite, 'count': count, 'results': results}
        control_sock.sendall(json.dumps(out).encode('utf-8') + b'\n')
        print('Sent results back to GCS control')
    finally:
        try:
            close_socket(sock)
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--gcs-host', required=True)
    p.add_argument('--gcs-port', type=int, default=47010)
    p.add_argument('--local-bind', default='0.0.0.0')
    args = p.parse_args()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        print(f'Connecting to GCS control {args.gcs_host}:{args.gcs_port}...')
        s.connect((args.gcs_host, args.gcs_port))
        # read a line
        data = b''
        while True:
            chunk = s.recv(4096)
            if not chunk:
                print('Control connection closed')
                return
            data += chunk
            if b'\n' in data:
                break
        line, _ = data.split(b'\n', 1)
        cmd = json.loads(line.decode('utf-8'))
        print('Received command:', cmd)
        run_test(s, cmd)


if __name__ == '__main__':
    main()
