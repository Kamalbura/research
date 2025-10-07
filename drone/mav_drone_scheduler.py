#!/usr/bin/env python3
"""Drone-side MAVProxy scheduler that requests suite switches and power captures."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import CONFIG

DRONE_HOST = CONFIG["DRONE_HOST"]
GCS_HOST = CONFIG["GCS_HOST"]
CONTROL_PORT = int(CONFIG.get("DRONE_CONTROL_PORT", 48080))
M2G_PORT = int(CONFIG.get("DRONE_TO_GCS_CTL_PORT", 48181))
DEFAULT_PRE_GAP = float(CONFIG.get("AUTO_GCS", {}).get("pre_gap_s", 1.0) or 0.0)

PlanItem = Tuple[str, str, float]


def _load_plan() -> Sequence[PlanItem]:
    override = os.getenv("DRONE_MAV_PLAN_JSON")
    if override:
        try:
            data = json.loads(override)
            plan: List[PlanItem] = []
            for entry in data:
                algo = str(entry.get("algorithm"))
                suite = str(entry.get("suite"))
                duration = float(entry.get("duration_s"))
                if not algo or not suite or duration <= 0:
                    continue
                plan.append((algo, suite, duration))
            if plan:
                return plan
        except Exception as exc:
            print(f"[drone] invalid DRONE_MAV_PLAN_JSON: {exc}", flush=True)
    return [
        ("algo-baseline", "cs-mlkem768-aesgcm-mldsa65", 30.0),
        ("algo-variantA", "cs-mlkem1024-aesgcm-mldsa87", 30.0),
        ("algo-variantB", "cs-mlkem512-aesgcm-mldsa44", 30.0),
    ]


def _ctl_send(payload: dict, timeout: float = 2.0) -> dict:
    with socket.create_connection((DRONE_HOST, CONTROL_PORT), timeout=timeout) as sock:
        sock.sendall((json.dumps(payload) + "\n").encode())
        sock.shutdown(socket.SHUT_WR)
        line = sock.makefile().readline()
        return json.loads(line.strip()) if line else {}


def _notify_gcs_switch(algorithm: str, suite: str, duration_s: float, pre_gap_s: float) -> None:
    message = {
        "cmd": "switch_suite",
        "algorithm": algorithm,
        "suite": suite,
        "duration_s": duration_s,
        "pre_gap_s": pre_gap_s,
        "ts_ns": time.time_ns(),
    }
    try:
        with socket.create_connection((GCS_HOST, M2G_PORT), timeout=2.0) as sock:
            sock.sendall((json.dumps(message) + "\n").encode())
    except Exception as exc:
        print(f"[drone] notify switch failed: {exc}", flush=True)


def _start_mavproxy() -> subprocess.Popen | None:
    autostart = os.getenv("DRONE_AUTOSTART_MAVPROXY", "1").strip().lower() in {"1", "true", "yes", "on"}
    if not autostart:
        return None
    script = Path(__file__).with_name("run_mavproxy.sh")
    if not script.exists():
        print("[drone] MAVProxy launcher missing; skipping autostart", flush=True)
        return None
    return subprocess.Popen([str(script)], cwd=str(script.parent))


def _stop(proc: subprocess.Popen | None) -> None:
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main(argv: Iterable[str] | None = None) -> int:
    plan = _load_plan()
    if not plan:
        print("[drone] no plan provided", flush=True)
        return 1

    print("[drone] MAV schedule starting; follower control must be running", flush=True)
    mavproxy_proc = _start_mavproxy()
    pre_gap_s = float(os.getenv("DRONE_MAV_PRE_GAP", DEFAULT_PRE_GAP) or 0.0)
    request_power = os.getenv("DRONE_REQUEST_POWER", "1").strip().lower() not in {"0", "false", "no", "off"}

    try:
        for step, (algorithm, suite, duration_s) in enumerate(plan, start=1):
            print(f"[drone] step {step}: algo={algorithm} suite={suite} duration={duration_s:.1f}s", flush=True)
            _notify_gcs_switch(algorithm, suite, duration_s, pre_gap_s)
            if pre_gap_s > 0:
                time.sleep(pre_gap_s)
            if request_power:
                start_ns = time.time_ns()
                payload = {
                    "cmd": "power_capture",
                    "suite": suite,
                    "duration_s": duration_s,
                    "start_ns": start_ns,
                }
                try:
                    resp = _ctl_send(payload)
                    if not resp.get("ok"):
                        print(f"[drone] power capture rejected: {resp}", flush=True)
                except Exception as exc:
                    print(f"[drone] power capture request failed: {exc}", flush=True)
            time.sleep(max(0.0, duration_s))
        print("[drone] schedule complete", flush=True)
        return 0
    finally:
        _stop(mavproxy_proc)


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
