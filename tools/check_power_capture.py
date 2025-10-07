#!/usr/bin/env python3
"""Quick health check for the drone power capture backend."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import CONFIG

DRONE_HOST = CONFIG.get("DRONE_HOST", "127.0.0.1")
CONTROL_PORT = int(CONFIG.get("DRONE_CONTROL_PORT", 48080))


def _ctl_send(payload: dict, timeout: float = 2.0) -> dict:
    with socket.create_connection((DRONE_HOST, CONTROL_PORT), timeout=timeout) as sock:
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        line = sock.makefile().readline()
        return json.loads(line.strip()) if line else {}


def poll_power_status(timeout_s: float = 15.0, poll_s: float = 0.5) -> Optional[dict]:
    deadline = time.time() + timeout_s
    last: Optional[dict] = None
    while time.time() < deadline:
        try:
            resp = _ctl_send({"cmd": "power_status"}, timeout=2.0)
        except Exception as exc:  # pragma: no cover - best effort
            last = {"ok": False, "error": str(exc)}
            time.sleep(poll_s)
            continue
        last = resp if isinstance(resp, dict) else {}
        if not last.get("busy"):
            break
        time.sleep(poll_s)
    return last


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify drone power capture readiness")
    parser.add_argument("--duration", type=float, default=3.0, help="Seconds to sample (default 3)")
    parser.add_argument("--suite", default="health-check", help="Label recorded with the capture")
    args = parser.parse_args()

    try:
        status = poll_power_status(timeout_s=2.0)
    except Exception as exc:
        print(f"[power-check] failed to query status: {exc}")
        return 1

    if status and status.get("busy"):
        print("[power-check] power backend is busy; wait for current capture to finish")
        return 2

    start_ns = time.time_ns()
    request = {
        "cmd": "power_capture",
        "suite": args.suite,
        "duration_s": args.duration,
        "start_ns": start_ns,
    }
    try:
        resp = _ctl_send(request, timeout=3.0)
    except Exception as exc:
        print(f"[power-check] failed to request capture: {exc}")
        return 1

    if not resp.get("ok"):
        print(f"[power-check] capture rejected: {resp}")
        return 1

    summary = poll_power_status(timeout_s=max(6.0, args.duration + 5.0))
    if not summary:
        print("[power-check] no status returned after capture")
        return 1
    if summary.get("error"):
        print(f"[power-check] backend reported error: {summary['error']}")
        return 1
    last = summary.get("last_summary")
    if not isinstance(last, dict):
        print("[power-check] capture finished but no summary available")
        return 1

    print("[power-check] capture complete")
    print(f"  samples: {last.get('samples')}")
    print(f"  avg_power_w: {last.get('avg_power_w')}")
    print(f"  energy_j: {last.get('energy_j')}")
    print(f"  csv_path: {last.get('csv_path')}")
    print(f"  summary_json_path: {last.get('summary_json_path')}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
