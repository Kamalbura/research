"""Utility to inspect and optionally free known test ports on Windows.

Usage (Windows):
  python tools/cleanup_bound_ports.py --ports 46000 46011 47001 --kill

This script is intentionally conservative: it will list processes bound to the
given ports and only attempt to terminate them if --kill is provided.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Ensure repo root is on sys.path when executed from tools/ directory
_HERE = Path(__file__).resolve()
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _get_pid_for_port(port: int) -> int | None:
    # Uses netstat -ano and finds the PID for lines containing :port
    try:
        out = subprocess.check_output(['netstat', '-ano'], shell=True, text=True)
    except subprocess.CalledProcessError:
        return None

    for line in out.splitlines():
        if f':{port} ' in line or f':{port}\r' in line:
            parts = line.split()
            if parts:
                try:
                    pid = int(parts[-1])
                    return pid
                except Exception:
                    continue
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--ports', type=int, nargs='+', required=True)
    p.add_argument('--kill', action='store_true', help='Terminate processes owning the ports')
    args = p.parse_args()

    for port in args.ports:
        pid = _get_pid_for_port(port)
        if pid is None:
            print(f'Port {port}: free')
            continue
        print(f'Port {port}: PID {pid}')
        if args.kill:
            try:
                subprocess.check_call(['taskkill', '/PID', str(pid), '/F'], shell=True)
                print(f'  Killed PID {pid}')
            except subprocess.CalledProcessError as e:
                print(f'  Failed to kill PID {pid}: {e}')


if __name__ == '__main__':
    if sys.platform != 'win32':
        print('This cleanup helper is primarily intended for Windows hosts.')
    main()
