#!/usr/bin/env python3
"""GCS-side controller for automated matrix tests.

Usage: python tools/auto_test_gcs.py --listen-port 47010

Protocol (simple):
- GCS listens on TCP control port.
- Drone connects and awaits a JSON command from GCS: {"suite":"<suite>", "count":N, "udp_dest": [host,port]}
- Drone performs N UDP messages to udp_dest and replies over the TCP control channel with results JSON.
"""
from __future__ import annotations

import argparse
import json
import socket
import time
import sys
from pathlib import Path
from typing import Tuple

# Ensure repository root is on sys.path when executed directly
_HERE = Path(__file__).resolve()
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def handle_client(conn: socket.socket, addr: Tuple[str,int], args):
    print(f'Client connected: {addr}')
    # For demo: pick a suite and count
    cmd = {"suite": args.suite, "count": args.count, "udp_dest": [args.udp_host, args.udp_port]}
    raw = json.dumps(cmd).encode('utf-8') + b'\n'
    conn.sendall(raw)
    print('Sent command:', cmd)

    # Wait for a line-terminated JSON result
    data = b''
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            print('Connection closed by client')
            return
        data += chunk
        if b'\n' in data:
            break
    line, _ = data.split(b'\n', 1)
    try:
        res = json.loads(line.decode('utf-8'))
    except Exception as e:
        print('Failed to parse result JSON:', e)
        return
    print('Result from drone:')
    print(json.dumps(res, indent=2))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--listen-port', type=int, default=47010)
    p.add_argument('--suite', default='cs-mlkem512-aesgcm-mldsa44')
    p.add_argument('--count', type=int, default=8)
    p.add_argument('--udp-host', default='192.168.0.101', help='UDP destination host (proxy plaintext endpoint)')
    p.add_argument('--udp-port', type=int, default=47001, help='UDP destination port')
    args = p.parse_args()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('0.0.0.0', args.listen_port))
        s.listen(1)
        print(f'Listening for drone control on port {args.listen_port}...')
        conn, addr = s.accept()
        with conn:
            handle_client(conn, addr, args)


if __name__ == '__main__':
    main()
