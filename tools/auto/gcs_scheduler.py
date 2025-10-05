#!/usr/bin/env python3
"""GCS scheduler that drives rekeys and UDP traffic using central configuration."""

from __future__ import annotations

import bisect
import csv
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from copy import deepcopy
from pathlib import Path
from typing import Dict, IO, Iterable, List, Optional, Set, Tuple

try:
    from openpyxl import Workbook
except ImportError:  # pragma: no cover
    Workbook = None

# Ensure project root is on sys.path so `import core` works when running this file
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import suites as suites_mod
from core.config import CONFIG


DRONE_HOST = CONFIG["DRONE_HOST"]
GCS_HOST = CONFIG["GCS_HOST"]

CONTROL_PORT = int(CONFIG.get("DRONE_CONTROL_PORT", 48080))

APP_SEND_HOST = CONFIG.get("GCS_PLAINTEXT_HOST", "127.0.0.1")
APP_SEND_PORT = int(CONFIG.get("GCS_PLAINTEXT_TX", 47001))
APP_RECV_HOST = CONFIG.get("GCS_PLAINTEXT_HOST", "127.0.0.1")
APP_RECV_PORT = int(CONFIG.get("GCS_PLAINTEXT_RX", 47002))

OUTDIR = ROOT / "logs/auto/gcs"
SUITES_OUTDIR = OUTDIR / "suites"
SECRETS_DIR = ROOT / "secrets/matrix"

EXCEL_OUTPUT_DIR = ROOT / Path(
    CONFIG.get("GCS_EXCEL_OUTPUT")
    or os.getenv("GCS_EXCEL_OUTPUT", "output/gcs")
)

COMBINED_OUTPUT_DIR = ROOT / Path(
    CONFIG.get("GCS_COMBINED_OUTPUT_BASE")
    or os.getenv("GCS_COMBINED_OUTPUT_BASE", "output/gcs")
)

DRONE_MONITOR_BASE = ROOT / Path(
    CONFIG.get("DRONE_MONITOR_OUTPUT_BASE")
    or os.getenv("DRONE_MONITOR_OUTPUT_BASE", "output/drone")
)

TELEMETRY_BIND_HOST = CONFIG.get("GCS_TELEMETRY_BIND", "0.0.0.0")
TELEMETRY_PORT = int(
    CONFIG.get("GCS_TELEMETRY_PORT")
    or CONFIG.get("DRONE_TELEMETRY_PORT")
    or 52080
)

PROXY_STATUS_PATH = OUTDIR / "gcs_status.json"
PROXY_SUMMARY_PATH = OUTDIR / "gcs_summary.json"
SUMMARY_CSV = OUTDIR / "summary.csv"
EVENTS_FILENAME = "blaster_events.jsonl"

SEQ_TS_OVERHEAD_BYTES = 12
UDP_HEADER_BYTES = 8
IPV4_HEADER_BYTES = 20
IPV6_HEADER_BYTES = 40
MIN_DELAY_SAMPLES = 30
HYSTERESIS_WINDOW = 3
MAX_BISECT_STEPS = 3
WARMUP_FRACTION = 0.1
MAX_WARMUP_SECONDS = 1.0
SATURATION_COARSE_RATES = [5, 25, 50, 75, 100, 125, 150, 175, 200]
SATURATION_LINEAR_RATES = [
    5,
    10,
    15,
    20,
    25,
    30,
    35,
    40,
    45,
    50,
    60,
    70,
    80,
    90,
    100,
    125,
    150,
    175,
    200,
]
SATURATION_SIGNALS = ("owd_p95_spike", "delivery_degraded", "loss_excess")


class P2Quantile:
    def __init__(self, p: float) -> None:
        if not 0.0 < p < 1.0:
            raise ValueError("p must be between 0 and 1")
        self.p = p
        self._initial: List[float] = []
        self._q: List[float] = []
        self._n: List[int] = []
        self._np: List[float] = []
        self._dn = [0.0, p / 2.0, p, (1.0 + p) / 2.0, 1.0]
        self.count = 0

    def add(self, sample: float) -> None:
        x = float(sample)
        self.count += 1
        if self.count <= 5:
            bisect.insort(self._initial, x)
            if self.count == 5:
                self._q = list(self._initial)
                self._n = [1, 2, 3, 4, 5]
                self._np = [1.0, 1.0 + 2.0 * self.p, 1.0 + 4.0 * self.p, 3.0 + 2.0 * self.p, 5.0]
            return

        if not self._q:
            # Should not happen, but guard for consistency
            self._q = list(self._initial)
            self._n = [1, 2, 3, 4, 5]
            self._np = [1.0, 1.0 + 2.0 * self.p, 1.0 + 4.0 * self.p, 3.0 + 2.0 * self.p, 5.0]

        if x < self._q[0]:
            self._q[0] = x
            k = 0
        elif x >= self._q[4]:
            self._q[4] = x
            k = 3
        else:
            k = 0
            for idx in range(4):
                if self._q[idx] <= x < self._q[idx + 1]:
                    k = idx
                    break

        for idx in range(k + 1, 5):
            self._n[idx] += 1

        for idx in range(5):
            self._np[idx] += self._dn[idx]

        for idx in range(1, 4):
            d = self._np[idx] - self._n[idx]
            if (d >= 1 and self._n[idx + 1] - self._n[idx] > 1) or (d <= -1 and self._n[idx - 1] - self._n[idx] < -1):
                step = 1 if d > 0 else -1
                candidate = self._parabolic(idx, step)
                if self._q[idx - 1] < candidate < self._q[idx + 1]:
                    self._q[idx] = candidate
                else:
                    self._q[idx] = self._linear(idx, step)
                self._n[idx] += step

    def value(self) -> float:
        if self.count == 0:
            return 0.0
        if self.count <= 5 and self._initial:
            rank = (self.count - 1) * self.p
            idx = max(0, min(len(self._initial) - 1, int(round(rank))))
            return float(self._initial[idx])
        if not self._q:
            return 0.0
        return float(self._q[2])

    def _parabolic(self, idx: int, step: int) -> float:
        numerator_left = self._n[idx] - self._n[idx - 1] + step
        numerator_right = self._n[idx + 1] - self._n[idx] - step
        denominator = self._n[idx + 1] - self._n[idx - 1]
        if denominator == 0:
            return self._q[idx]
        return self._q[idx] + (step / denominator) * (
            numerator_left * (self._q[idx + 1] - self._q[idx]) / max(self._n[idx + 1] - self._n[idx], 1)
            + numerator_right * (self._q[idx] - self._q[idx - 1]) / max(self._n[idx] - self._n[idx - 1], 1)
        )

    def _linear(self, idx: int, step: int) -> float:
        target = idx + step
        denominator = self._n[target] - self._n[idx]
        if denominator == 0:
            return self._q[idx]
        return self._q[idx] + step * (self._q[target] - self._q[idx]) / denominator


def wilson_interval(successes: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    proportion = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (proportion + z2 / (2.0 * n)) / denom
    margin = (z * math.sqrt((proportion * (1.0 - proportion) / n) + (z2 / (4.0 * n * n)))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def ip_header_bytes_for_host(host: str) -> int:
    return IPV6_HEADER_BYTES if ":" in host else IPV4_HEADER_BYTES


APP_IP_HEADER_BYTES = ip_header_bytes_for_host(APP_SEND_HOST)


def ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _merge_defaults(defaults: dict, override: Optional[dict]) -> dict:
    result = deepcopy(defaults)
    if isinstance(override, dict):
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                merged = result[key].copy()
                merged.update(value)
                result[key] = merged
            else:
                result[key] = value
    return result


AUTO_GCS_DEFAULTS = {
    "session_prefix": "session",
    "traffic": "blast",
    "duration_s": 45.0,
    "pre_gap_s": 1.0,
    "inter_gap_s": 15.0,
    "payload_bytes": 256,
    "event_sample": 100,
    "passes": 1,
    "rate_pps": 0,
    "bandwidth_mbps": 0.0,
    "max_rate_mbps": 200.0,
    "sat_search": "auto",
    "sat_delivery_threshold": 0.85,
    "sat_loss_threshold_pct": 5.0,
    "sat_rtt_spike_factor": 1.6,
    "suites": None,
    "launch_proxy": True,
    "monitors_enabled": True,
    "telemetry_enabled": True,
    "telemetry_bind_host": TELEMETRY_BIND_HOST,
    "telemetry_port": TELEMETRY_PORT,
    "export_combined_excel": True,
    "power_capture": True,
}

AUTO_GCS_CONFIG = _merge_defaults(AUTO_GCS_DEFAULTS, CONFIG.get("AUTO_GCS"))

SATURATION_SEARCH_MODE = str(AUTO_GCS_CONFIG.get("sat_search") or "auto").lower()
SATURATION_RTT_SPIKE = float(AUTO_GCS_CONFIG.get("sat_rtt_spike_factor") or 1.6)
SATURATION_DELIVERY_THRESHOLD = float(AUTO_GCS_CONFIG.get("sat_delivery_threshold") or 0.85)
SATURATION_LOSS_THRESHOLD = float(AUTO_GCS_CONFIG.get("sat_loss_threshold_pct") or 5.0)


def mkdirp(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def suite_outdir(suite: str) -> Path:
    target = SUITES_OUTDIR / suite
    mkdirp(target)
    return target


def resolve_suites(requested: Optional[Iterable[str]]) -> List[str]:
    available = list(suites_mod.list_suites())
    if not available:
        raise RuntimeError("No suites registered in core.suites; cannot proceed")

    if not requested:
        return available

    resolved: List[str] = []
    seen: Set[str] = set()
    for name in requested:
        info = suites_mod.get_suite(name)
        suite_id = info["suite_id"]
        if suite_id not in available:
            raise RuntimeError(f"Suite {name} not present in core registry")
        if suite_id not in seen:
            resolved.append(suite_id)
            seen.add(suite_id)
    return resolved


def preferred_initial_suite(candidates: List[str]) -> Optional[str]:
    configured = CONFIG.get("SIMPLE_INITIAL_SUITE")
    if not configured:
        return None
    try:
        suite_id = suites_mod.get_suite(configured)["suite_id"]
    except NotImplementedError:
        return None
    return suite_id if suite_id in candidates else None


def ctl_send(obj: dict, timeout: float = 2.0, retries: int = 4, backoff: float = 0.5) -> dict:
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with socket.create_connection((DRONE_HOST, CONTROL_PORT), timeout=timeout) as sock:
                sock.sendall((json.dumps(obj) + "\n").encode())
                sock.shutdown(socket.SHUT_WR)
                line = sock.makefile().readline()
                return json.loads(line.strip()) if line else {}
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff * attempt)
                continue
            raise
    if last_exc:
        raise last_exc
    return {}


def request_power_capture(suite: str, duration_s: float, start_ns: Optional[int]) -> dict:
    payload = {
        "cmd": "power_capture",
        "suite": suite,
        "duration_s": duration_s,
    }
    if start_ns is not None:
        payload["start_ns"] = int(start_ns)
    try:
        resp = ctl_send(payload, timeout=1.5, retries=2, backoff=0.4)
    except Exception as exc:
        print(f"[WARN] power_capture request failed: {exc}", file=sys.stderr)
        return {"ok": False, "error": str(exc)}
    return resp


def poll_power_status(max_wait_s: float = 12.0, poll_s: float = 0.6) -> dict:
    deadline = time.time() + max_wait_s
    last: dict = {}
    while time.time() < deadline:
        try:
            resp = ctl_send({"cmd": "power_status"}, timeout=1.5, retries=1, backoff=0.3)
        except Exception as exc:
            last = {"ok": False, "error": str(exc)}
            time.sleep(poll_s)
            continue
        last = resp
        if not resp.get("ok"):
            break
        if not resp.get("available", True):
            break
        if not resp.get("busy", False):
            break
        time.sleep(poll_s)
    return last


class Blaster:
    """High-rate UDP blaster with RTT sampling and throughput accounting."""

    def __init__(
        self,
        send_host: str,
        send_port: int,
    recv_host: str,
    recv_port: int,
    events_path: Optional[Path],
        payload_bytes: int,
        sample_every: int,
        offset_ns: int,
    ) -> None:
        self.send_addr = (send_host, send_port)
        self.recv_addr = (recv_host, recv_port)
        self.payload_bytes = max(12, int(payload_bytes))
        self.sample_every = max(0, int(sample_every))
        self.offset_ns = offset_ns
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(self.recv_addr)
        self.rx.settimeout(0.001)
        self.rx_burst = max(1, int(os.getenv("GCS_RX_BURST", "32")))
        try:
            # Allow overriding socket buffer sizes via environment variables
            # Use GCS_SOCK_SNDBUF and GCS_SOCK_RCVBUF if present, otherwise default to 1 MiB
            sndbuf = int(os.getenv("GCS_SOCK_SNDBUF", str(1 << 20)))
            rcvbuf = int(os.getenv("GCS_SOCK_RCVBUF", str(1 << 20)))
            self.tx.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
            self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
            actual_snd = self.tx.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
            actual_rcv = self.rx.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            print(
                f"[{ts()}] blaster UDP socket buffers: snd={actual_snd} rcv={actual_rcv}",
                flush=True,
            )
        except Exception:
            # best-effort; continue even if setting buffers fails
            pass

        self.events_path = events_path
        self.events: Optional[IO[str]] = None
        if events_path is not None:
            mkdirp(events_path.parent)
            self.events = open(events_path, "w", encoding="utf-8")

        self.sent = 0
        self.rcvd = 0
        self.sent_bytes = 0
        self.rcvd_bytes = 0
        self.rtt_sum_ns = 0
        self.rtt_samples = 0
        self.rtt_max_ns = 0
        self.rtt_min_ns: Optional[int] = None
        self.pending: Dict[int, int] = {}
        self.rtt_p50 = P2Quantile(0.5)
        self.rtt_p95 = P2Quantile(0.95)
        self.owd_p50 = P2Quantile(0.5)
        self.owd_p95 = P2Quantile(0.95)
        self.owd_samples = 0
        self.owd_p50_ns = 0.0
        self.owd_p95_ns = 0.0
        self.rtt_p50_ns = 0.0
        self.rtt_p95_ns = 0.0

    def _log_event(self, payload: dict) -> None:
        # Buffered write; caller flushes at end of run()
        if self.events is None:
            return
        self.events.write(json.dumps(payload) + "\n")

    def _now(self) -> int:
        return time.time_ns() + self.offset_ns

    def _maybe_log(self, kind: str, seq: int, t_ns: int) -> None:
        if self.sample_every == 0:
            return
        if kind == "send":
            if seq % self.sample_every:
                return
        else:
            if self.rcvd % self.sample_every:
                return
        self._log_event({"event": kind, "seq": seq, "t_ns": t_ns})

    def run(self, duration_s: float, rate_pps: int, max_packets: Optional[int] = None) -> None:
        stop_at = self._now() + int(max(0.0, duration_s) * 1e9)
        payload_pad = b"\x00" * (self.payload_bytes - 12)
        interval = 0.0 if rate_pps <= 0 else 1.0 / max(1, rate_pps)
        stop_event = threading.Event()
        rx_thread = threading.Thread(target=self._rx_loop, args=(stop_event,), daemon=True)
        rx_thread.start()

        seq = 0
        burst = 32 if interval == 0.0 else 1
        while self._now() < stop_at:
            loop_progress = False
            sends_this_loop = burst
            while sends_this_loop > 0:
                if self._now() >= stop_at:
                    break
                t_send = self._now()
                packet = seq.to_bytes(4, "big") + int(t_send).to_bytes(8, "big") + payload_pad
                try:
                    self.tx.sendto(packet, self.send_addr)
                    if self.sample_every and (seq % self.sample_every == 0):
                        self.pending[seq] = int(t_send)
                    self.sent += 1
                    self.sent_bytes += len(packet)
                    loop_progress = True
                    self._maybe_log("send", seq, int(t_send))
                except Exception as exc:
                    self._log_event({"event": "send_error", "err": str(exc), "ts": ts()})
                seq += 1
                sends_this_loop -= 1
            if max_packets is not None and self.sent >= max_packets:
                break
            if interval > 0.0:
                time.sleep(interval)
            elif (seq & 0x3FFF) == 0:
                time.sleep(0)
            if not loop_progress:
                time.sleep(0.0005)

        tail_deadline = self._now() + int(0.25 * 1e9)
        while self._now() < tail_deadline:
            if not self._rx_once():
                time.sleep(0)
        stop_event.set()
        rx_thread.join(timeout=0.2)
        self.owd_p50_ns = self.owd_p50.value()
        self.owd_p95_ns = self.owd_p95.value()
        self.rtt_p50_ns = self.rtt_p50.value()
        self.rtt_p95_ns = self.rtt_p95.value()
        # Bug #5 fix: Ensure cleanup happens even on exceptions
        try:
            if self.events:
                try:
                    self.events.flush()
                except Exception:
                    pass
        finally:
            if self.events:
                try:
                    self.events.close()
                except Exception:
                    pass
                self.events = None
            try:
                self.tx.close()
            except Exception:
                pass
            try:
                self.rx.close()
            except Exception:
                pass
    def _rx_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            progressed = False
            for _ in range(self.rx_burst):
                if self._rx_once():
                    progressed = True
                else:
                    break
            if not progressed:
                time.sleep(0.0005)

    def _rx_once(self) -> bool:
        try:
            data, _ = self.rx.recvfrom(65535)
        except socket.timeout:
            return False
        except (socket.error, OSError) as exc:
            # Bug #4 fix: Catch specific exceptions, log unexpected errors
            if not isinstance(exc, (ConnectionResetError, ConnectionRefusedError)):
                self._log_event({"event": "rx_error", "err": str(exc), "ts": ts()})
            return False
        t_recv = self._now()
        self.rcvd += 1
        self.rcvd_bytes += len(data)
        if len(data) < 4:
            return True

        seq = int.from_bytes(data[:4], "big")
        t_send = self.pending.pop(seq, None)
        header_t_send = int.from_bytes(data[4:12], "big") if len(data) >= 12 else None
        if t_send is None:
            t_send = header_t_send

        drone_recv_ns = int.from_bytes(data[-8:], "big") if len(data) >= 20 else None

        if t_send is not None:
            rtt = t_recv - t_send
            if rtt >= 0:
                self.rtt_sum_ns += rtt
                self.rtt_samples += 1
                if rtt > self.rtt_max_ns:
                    self.rtt_max_ns = rtt
                if self.rtt_min_ns is None or rtt < self.rtt_min_ns:
                    self.rtt_min_ns = rtt
                self.rtt_p50.add(rtt)
                self.rtt_p95.add(rtt)
            self._maybe_log("recv", seq, int(t_recv))

        if t_send is not None and drone_recv_ns is not None:
            owd_up_ns = drone_recv_ns - t_send
            if owd_up_ns >= 0:
                self.owd_samples += 1
                self.owd_p50.add(owd_up_ns)
                self.owd_p95.add(owd_up_ns)
        return True


def wait_handshake(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if PROXY_STATUS_PATH.exists():
            try:
                with open(PROXY_STATUS_PATH, encoding="utf-8") as handle:
                    js = json.load(handle)
            except Exception:
                js = {}
            state = js.get("state") or js.get("status")
            if state in {"running", "completed", "ready", "handshake_ok"}:
                return True
        time.sleep(0.3)
    return False


def wait_active_suite(target: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = ctl_send({"cmd": "status"}, timeout=0.6, retries=1)
        except Exception:
            status = {}
        if status.get("suite") == target:
            return True
        time.sleep(0.2)
    return False


def timesync() -> dict:
    t1 = time.time_ns()
    resp = ctl_send({"cmd": "timesync", "t1_ns": t1})
    t4 = time.time_ns()
    t2 = int(resp.get("t2_ns", t1))
    t3 = int(resp.get("t3_ns", t4))
    delay_ns = (t4 - t1) - (t3 - t2)
    offset_ns = ((t2 - t1) + (t3 - t4)) // 2
    return {"offset_ns": offset_ns, "rtt_ns": delay_ns}


def snapshot_proxy_artifacts(suite: str) -> None:
    target_dir = suite_outdir(suite)
    if PROXY_STATUS_PATH.exists():
        shutil.copy(PROXY_STATUS_PATH, target_dir / "gcs_status.json")
    if PROXY_SUMMARY_PATH.exists():
        shutil.copy(PROXY_SUMMARY_PATH, target_dir / "gcs_summary.json")


def start_gcs_proxy(initial_suite: str) -> tuple[subprocess.Popen, IO[str]]:
    key_path = SECRETS_DIR / initial_suite / "gcs_signing.key"
    if not key_path.exists():
        raise FileNotFoundError(f"Missing GCS signing key for suite {initial_suite}: {key_path}")

    mkdirp(OUTDIR)
    log_path = OUTDIR / f"gcs_{time.strftime('%Y%m%d-%H%M%S')}.log"
    log_handle: IO[str] = open(log_path, "w", encoding="utf-8", errors="replace")

    env = os.environ.copy()
    env["DRONE_HOST"] = DRONE_HOST
    env["GCS_HOST"] = GCS_HOST
    env["ENABLE_PACKET_TYPE"] = "1" if CONFIG.get("ENABLE_PACKET_TYPE", True) else "0"
    env["STRICT_UDP_PEER_MATCH"] = "1" if CONFIG.get("STRICT_UDP_PEER_MATCH", True) else "0"

    root_str = str(ROOT)
    existing_py_path = env.get("PYTHONPATH")
    if existing_py_path:
        if root_str not in existing_py_path.split(os.pathsep):
            env["PYTHONPATH"] = root_str + os.pathsep + existing_py_path
    else:
        env["PYTHONPATH"] = root_str

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "core.run_proxy",
            "gcs",
            "--suite",
            initial_suite,
            "--gcs-secret-file",
            str(key_path),
            "--control-manual",
            "--status-file",
            str(PROXY_STATUS_PATH),
            "--json-out",
            str(PROXY_SUMMARY_PATH),
        ],
        stdin=subprocess.PIPE,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        cwd=str(ROOT),
    )
    return proc, log_handle


def read_proxy_stats_live() -> dict:
    try:
        with open(PROXY_STATUS_PATH, encoding="utf-8") as handle:
            js = json.load(handle)
    except Exception:
        return {}
    if isinstance(js, dict):
        counters = js.get("counters")
        if isinstance(counters, dict):
            return counters
        if any(k in js for k in ("enc_out", "enc_in")):
            return js
    return {}


def read_proxy_summary() -> dict:
    if not PROXY_SUMMARY_PATH.exists():
        return {}
    try:
        with open(PROXY_SUMMARY_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}



def _read_proxy_counters() -> dict:

    counters = read_proxy_stats_live()

    if isinstance(counters, dict) and counters:

        return counters

    summary = read_proxy_summary()

    if isinstance(summary, dict):

        summary_counters = summary.get("counters")

        if isinstance(summary_counters, dict):

            return summary_counters

        if any(key in summary for key in ("enc_out", "enc_in", "rekeys_ok", "rekeys_fail", "last_rekey_suite")):

            return summary

    return {}





def wait_proxy_rekey(

    target_suite: str,

    baseline: Dict[str, object],

    *,

    timeout: float = 20.0,

    poll_interval: float = 0.4,

    proc: subprocess.Popen,

) -> str:

    start = time.time()

    baseline_ok = int(baseline.get("rekeys_ok", 0) or 0)

    baseline_fail = int(baseline.get("rekeys_fail", 0) or 0)

    while time.time() - start < timeout:

        if proc.poll() is not None:

            raise RuntimeError("GCS proxy exited during rekey")

        counters = _read_proxy_counters()

        if counters:

            rekeys_ok = int(counters.get("rekeys_ok", 0) or 0)

            rekeys_fail = int(counters.get("rekeys_fail", 0) or 0)

            last_suite = counters.get("last_rekey_suite") or counters.get("suite") or ""

            if rekeys_fail > baseline_fail:

                return "fail"

            if rekeys_ok > baseline_ok and (not last_suite or last_suite == target_suite):

                return "ok"

        time.sleep(poll_interval)

    return "timeout"


def activate_suite(gcs: subprocess.Popen, suite: str, is_first: bool) -> float:

    if gcs.poll() is not None:

        raise RuntimeError("GCS proxy is not running; cannot continue")

    start_ns = time.time_ns()

    if is_first:

        try:

            ctl_send({"cmd": "mark", "suite": suite})

        except Exception as exc:

            print(f"[WARN] control mark failed for {suite}: {exc}", file=sys.stderr)

        finally:

            try:

                ctl_send({"cmd": "rekey_complete", "suite": suite, "status": "ok"})

            except Exception:

                pass

    else:

        assert gcs.stdin is not None

        print(f"[{ts()}] rekey -> {suite}")

        gcs.stdin.write(suite + "\n")
        gcs.stdin.flush()

        baseline = _read_proxy_counters()

        try:
            ctl_send({"cmd": "mark", "suite": suite})
        except Exception as exc:
            print(f"[WARN] control mark failed for {suite}: {exc}", file=sys.stderr)
        try:
            follower_ready = wait_active_suite(suite, timeout=5.0)
            if not follower_ready:
                print(f"[WARN] follower did not report suite {suite} active before timeout", file=sys.stderr)
        except Exception:
            print(f"[WARN] follower status check failed for suite {suite}", file=sys.stderr)

        rekey_status = "timeout"

        try:

            result = wait_proxy_rekey(suite, baseline, timeout=15.0, proc=gcs)

            rekey_status = result

            if result == "timeout":

                print(f"[WARN] timed out waiting for proxy to activate suite {suite}", file=sys.stderr)

            elif result == "fail":

                print(f"[WARN] proxy reported failed rekey for suite {suite}", file=sys.stderr)

        except RuntimeError as exc:

            rekey_status = "error"

            raise

        except Exception as exc:

            rekey_status = "error"

            print(f"[WARN] error while waiting for proxy rekey {suite}: {exc}", file=sys.stderr)

        finally:

            try:

                ctl_send({"cmd": "rekey_complete", "suite": suite, "status": rekey_status})

            except Exception as exc:

                print(f"[WARN] rekey_complete failed for {suite}: {exc}", file=sys.stderr)

    return (time.time_ns() - start_ns) / 1_000_000




def run_suite(
    gcs: subprocess.Popen,
    suite: str,
    is_first: bool,
    duration_s: float,
    payload_bytes: int,
    event_sample: int,
    offset_ns: int,
    pass_index: int,
    traffic_mode: str,
    pre_gap: float,
    rate_pps: int,
    power_capture_enabled: bool,
) -> dict:
    rekey_duration_ms = activate_suite(gcs, suite, is_first)

    events_path = suite_outdir(suite) / EVENTS_FILENAME
    start_mark_ns = time.time_ns() + offset_ns + int(0.150 * 1e9) + int(max(pre_gap, 0.0) * 1e9)
    try:
        ctl_send({"cmd": "schedule_mark", "suite": suite, "t0_ns": start_mark_ns})
    except Exception as exc:
        print(f"[WARN] schedule_mark failed for {suite}: {exc}", file=sys.stderr)

    power_request_ok = False
    power_request_error: Optional[str] = None
    power_status: dict = {}
    if power_capture_enabled:
        power_start_ns = time.time_ns() + offset_ns + int(max(pre_gap, 0.0) * 1e9)
        power_resp = request_power_capture(suite, duration_s, power_start_ns)
        power_request_ok = bool(power_resp.get("ok"))
        power_request_error = power_resp.get("error") if not power_request_ok else None
        if not power_request_ok and power_request_error:
            print(f"[WARN] power capture not scheduled: {power_request_error}", file=sys.stderr)
        banner = f"[{ts()}] ===== POWER: START in {pre_gap:.1f}s | suite={suite} | duration={duration_s:.1f}s mode={traffic_mode} ====="
    else:
        power_request_error = "disabled"
        banner = (
            f"[{ts()}] ===== TRAFFIC: START in {pre_gap:.1f}s | suite={suite} | duration={duration_s:.1f}s "
            f"mode={traffic_mode} (power capture disabled) ====="
        )
    print(banner)
    if pre_gap > 0:
        time.sleep(pre_gap)

    warmup_s = min(MAX_WARMUP_SECONDS, duration_s * WARMUP_FRACTION)
    start_wall_ns = time.time_ns()
    start_perf_ns = time.perf_counter_ns()
    sent_packets = 0
    rcvd_packets = 0
    rcvd_bytes = 0
    avg_rtt_ns = 0
    max_rtt_ns = 0
    rtt_samples = 0
    blaster_sent_bytes = 0

    if traffic_mode == "blast":
        if warmup_s > 0:
            warmup_blaster = Blaster(
                APP_SEND_HOST,
                APP_SEND_PORT,
                APP_RECV_HOST,
                APP_RECV_PORT,
                events_path=None,
                payload_bytes=payload_bytes,
                sample_every=0,
                offset_ns=offset_ns,
            )
            warmup_blaster.run(duration_s=warmup_s, rate_pps=rate_pps)
        start_wall_ns = time.time_ns()
        start_perf_ns = time.perf_counter_ns()
        blaster = Blaster(
            APP_SEND_HOST,
            APP_SEND_PORT,
            APP_RECV_HOST,
            APP_RECV_PORT,
            events_path,
            payload_bytes=payload_bytes,
            sample_every=event_sample,
            offset_ns=offset_ns,
        )
        blaster.run(duration_s=duration_s, rate_pps=rate_pps)
        sent_packets = blaster.sent
        rcvd_packets = blaster.rcvd
        rcvd_bytes = blaster.rcvd_bytes
        blaster_sent_bytes = blaster.sent_bytes
        sample_count = max(1, blaster.rtt_samples)
        avg_rtt_ns = blaster.rtt_sum_ns // sample_count
        max_rtt_ns = blaster.rtt_max_ns
        rtt_samples = blaster.rtt_samples
    else:
        time.sleep(duration_s)

    end_wall_ns = time.time_ns()
    end_perf_ns = time.perf_counter_ns()
    if power_capture_enabled:
        print(f"[{ts()}] ===== POWER: STOP | suite={suite} =====")
    else:
        print(f"[{ts()}] ===== TRAFFIC: STOP | suite={suite} =====")

    snapshot_proxy_artifacts(suite)
    proxy_stats = read_proxy_stats_live() or read_proxy_summary()

    if power_capture_enabled and power_request_ok:
        power_status = poll_power_status(max_wait_s=max(6.0, duration_s * 0.25))
        if power_status.get("error"):
            print(f"[WARN] power status error: {power_status['error']}", file=sys.stderr)

    power_summary = power_status.get("last_summary") if isinstance(power_status, dict) else None
    power_capture_complete = bool(power_summary)
    power_error = None
    if not power_capture_complete:
        if isinstance(power_status, dict):
            power_error = power_status.get("error")
            if not power_error and power_status.get("busy"):
                power_error = "capture_incomplete"
        if power_error is None:
            power_error = power_request_error

    elapsed_s = max(1e-9, (end_perf_ns - start_perf_ns) / 1e9)
    pps = sent_packets / elapsed_s if elapsed_s > 0 else 0.0
    throughput_mbps = (rcvd_bytes * 8) / (elapsed_s * 1_000_000) if elapsed_s > 0 else 0.0
    sent_mbps = (blaster_sent_bytes * 8) / (elapsed_s * 1_000_000) if blaster_sent_bytes else 0.0
    delivered_ratio = throughput_mbps / sent_mbps if sent_mbps > 0 else 0.0
    avg_rtt_ms = avg_rtt_ns / 1_000_000
    max_rtt_ms = max_rtt_ns / 1_000_000

    app_packet_bytes = payload_bytes + SEQ_TS_OVERHEAD_BYTES
    wire_packet_bytes_est = app_packet_bytes + UDP_HEADER_BYTES + APP_IP_HEADER_BYTES
    goodput_mbps = (rcvd_packets * payload_bytes * 8) / (elapsed_s * 1_000_000) if elapsed_s > 0 else 0.0
    wire_throughput_mbps_est = (
        (rcvd_packets * wire_packet_bytes_est * 8) / (elapsed_s * 1_000_000)
        if elapsed_s > 0
        else 0.0
    )
    if sent_mbps > 0:
        goodput_ratio = goodput_mbps / sent_mbps
        goodput_ratio = max(0.0, min(1.0, goodput_ratio))
    else:
        goodput_ratio = 0.0

    owd_p50_ms = 0.0
    owd_p95_ms = 0.0
    rtt_p50_ms = 0.0
    rtt_p95_ms = 0.0
    sample_quality = "low"
    owd_samples = 0

    if traffic_mode == "blast":
        owd_p50_ms = blaster.owd_p50_ns / 1_000_000
        owd_p95_ms = blaster.owd_p95_ns / 1_000_000
        rtt_p50_ms = blaster.rtt_p50_ns / 1_000_000
        rtt_p95_ms = blaster.rtt_p95_ns / 1_000_000
        owd_samples = blaster.owd_samples
        if blaster.rtt_samples >= MIN_DELAY_SAMPLES and blaster.owd_samples >= MIN_DELAY_SAMPLES:
            sample_quality = "ok"

    loss_pct = 0.0
    if sent_packets:
        loss_pct = max(0.0, (sent_packets - rcvd_packets) * 100.0 / sent_packets)
    loss_successes = max(0, sent_packets - rcvd_packets)
    loss_low, loss_high = wilson_interval(loss_successes, sent_packets)

    row = {
        "pass": pass_index,
        "suite": suite,
        "duration_s": round(elapsed_s, 3),
        "sent": sent_packets,
        "rcvd": rcvd_packets,
        "pps": round(pps, 1),
        "throughput_mbps": round(throughput_mbps, 3),
        "sent_mbps": round(sent_mbps, 3),
        "delivered_ratio": round(delivered_ratio, 3) if sent_mbps > 0 else 0.0,
        "goodput_mbps": round(goodput_mbps, 3),
        "wire_throughput_mbps_est": round(wire_throughput_mbps_est, 3),
        "app_packet_bytes": app_packet_bytes,
        "wire_packet_bytes_est": wire_packet_bytes_est,
        "goodput_ratio": round(goodput_ratio, 3),
        "rtt_avg_ms": round(avg_rtt_ms, 3),
        "rtt_max_ms": round(max_rtt_ms, 3),
        "rtt_p50_ms": round(rtt_p50_ms, 3),
        "rtt_p95_ms": round(rtt_p95_ms, 3),
        "owd_p50_ms": round(owd_p50_ms, 3),
        "owd_p95_ms": round(owd_p95_ms, 3),
        "rtt_samples": rtt_samples,
        "owd_samples": owd_samples,
        "sample_quality": sample_quality,
        "loss_pct": round(loss_pct, 3),
        "loss_pct_wilson_low": round(loss_low * 100.0, 3),
        "loss_pct_wilson_high": round(loss_high * 100.0, 3),
        "enc_out": proxy_stats.get("enc_out", 0),
        "enc_in": proxy_stats.get("enc_in", 0),
        "drops": proxy_stats.get("drops", 0),
        "rekeys_ok": proxy_stats.get("rekeys_ok", 0),
        "rekeys_fail": proxy_stats.get("rekeys_fail", 0),
        "start_ns": start_wall_ns,
        "end_ns": end_wall_ns,
        "rekey_ms": round(rekey_duration_ms, 3),
        "power_request_ok": power_request_ok,
        "power_capture_ok": power_capture_complete,
        "power_error": power_error,
        "power_avg_w": round(power_summary.get("avg_power_w", 0.0), 6) if power_summary else 0.0,
        "power_energy_j": round(power_summary.get("energy_j", 0.0), 6) if power_summary else 0.0,
        "power_samples": power_summary.get("samples") if power_summary else 0,
        "power_avg_current_a": round(power_summary.get("avg_current_a", 0.0), 6) if power_summary else 0.0,
        "power_avg_voltage_v": round(power_summary.get("avg_voltage_v", 0.0), 6) if power_summary else 0.0,
        "power_sample_rate_hz": round(power_summary.get("sample_rate_hz", 0.0), 3) if power_summary else 0.0,
        "power_duration_s": round(power_summary.get("duration_s", 0.0), 3) if power_summary else 0.0,
        "power_csv_path": power_summary.get("csv_path") if power_summary else "",
        "power_summary_path": power_summary.get("summary_json_path") if power_summary else "",
    }

    print(
        f"[{ts()}] <<< FINISH suite={suite} mode={traffic_mode} sent={sent_packets} rcvd={rcvd_packets} "
        f"pps~{pps:.0f} thr~{throughput_mbps:.2f} Mb/s sent~{sent_mbps:.2f} Mb/s loss={loss_pct:.2f}% "
        f"rtt_avg={avg_rtt_ms:.3f}ms rtt_max={max_rtt_ms:.3f}ms rekey={rekey_duration_ms:.2f}ms "
        f"enc_out={row['enc_out']} enc_in={row['enc_in']} >>>"
    )

    return row


def write_summary(rows: List[dict]) -> None:
    if not rows:
        return
    mkdirp(OUTDIR)
    with open(SUMMARY_CSV, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"[{ts()}] wrote {SUMMARY_CSV}")


class SaturationTester:
    def __init__(
        self,
        suite: str,
        payload_bytes: int,
        duration_s: float,
        event_sample: int,
        offset_ns: int,
        output_dir: Path,
        max_rate_mbps: int,
        search_mode: str,
        delivery_threshold: float,
        loss_threshold: float,
        spike_factor: float,
    ) -> None:
        self.suite = suite
        self.payload_bytes = payload_bytes
        self.duration_s = duration_s
        self.event_sample = event_sample
        self.offset_ns = offset_ns
        self.output_dir = output_dir
        self.max_rate_mbps = max_rate_mbps
        self.search_mode = search_mode
        self.delivery_threshold = delivery_threshold
        self.loss_threshold = loss_threshold
        self.spike_factor = spike_factor
        self.records: List[Dict[str, float]] = []
        self._rate_cache: Dict[int, Tuple[Dict[str, float], bool, Optional[str]]] = {}
        self._baseline: Optional[Dict[str, float]] = None
        self._signal_history = {key: deque(maxlen=HYSTERESIS_WINDOW) for key in SATURATION_SIGNALS}
        self._last_ok_rate: Optional[int] = None
        self._first_bad_rate: Optional[int] = None
        self._stop_cause: Optional[str] = None
        self._stop_samples = 0

    def run(self) -> Dict[str, Optional[float]]:
        self.records = []
        self._rate_cache.clear()
        self._baseline = None
        self._signal_history = {key: deque(maxlen=HYSTERESIS_WINDOW) for key in SATURATION_SIGNALS}
        self._last_ok_rate = None
        self._first_bad_rate = None
        self._stop_cause = None
        self._stop_samples = 0

        used_mode = self.search_mode
        if self.search_mode == "linear":
            self._linear_search()
        else:
            self._coarse_search()
            if self._first_bad_rate is not None and self._last_ok_rate is not None:
                self._bisect_search()
            elif self.search_mode == "bisect" and self._first_bad_rate is None:
                self._linear_search()
                used_mode = "linear"

        resolution = None
        if self._first_bad_rate is not None and self._last_ok_rate is not None:
            resolution = max(0, self._first_bad_rate - self._last_ok_rate)
        saturation_point = self._last_ok_rate if self._last_ok_rate is not None else self._first_bad_rate
        confidence = min(1.0, self._stop_samples / 200.0) if self._stop_samples > 0 else 0.0

        baseline = self._baseline or {}
        return {
            "suite": self.suite,
            "baseline_owd_p50_ms": baseline.get("owd_p50_ms"),
            "baseline_owd_p95_ms": baseline.get("owd_p95_ms"),
            "baseline_rtt_p50_ms": baseline.get("rtt_p50_ms"),
            "baseline_rtt_p95_ms": baseline.get("rtt_p95_ms"),
            "saturation_point_mbps": saturation_point,
            "stop_cause": self._stop_cause,
            "confidence": round(confidence, 3),
            "search_mode": used_mode,
            "resolution_mbps": resolution,
        }

    def _linear_search(self) -> None:
        for rate in SATURATION_LINEAR_RATES:
            if rate > self.max_rate_mbps:
                break
            _, is_bad, _ = self._evaluate_rate(rate)
            if is_bad:
                break

    def _coarse_search(self) -> None:
        for rate in SATURATION_COARSE_RATES:
            if rate > self.max_rate_mbps:
                break
            _, is_bad, _ = self._evaluate_rate(rate)
            if is_bad:
                break

    def _bisect_search(self) -> None:
        if self._first_bad_rate is None:
            return
        lo = self._last_ok_rate if self._last_ok_rate is not None else 0
        hi = self._first_bad_rate
        steps = 0
        while hi - lo > 5 and steps < MAX_BISECT_STEPS:
            mid = max(1, int(round((hi + lo) / 2)))
            if mid == hi or mid == lo:
                break
            _, is_bad, _ = self._evaluate_rate(mid)
            steps += 1
            metrics = self._rate_cache[mid][0]
            sample_ok = metrics.get("sample_quality") == "ok"
            if not sample_ok:
                is_bad = True
            if is_bad:
                if mid < hi:
                    hi = mid
                if self._first_bad_rate is None or mid < self._first_bad_rate:
                    self._first_bad_rate = mid
            else:
                if mid > lo:
                    lo = mid
                if self._last_ok_rate is None or mid > self._last_ok_rate:
                    self._last_ok_rate = mid

    def _evaluate_rate(self, rate: int) -> Tuple[Dict[str, float], bool, Optional[str]]:
        cached = self._rate_cache.get(rate)
        if cached:
            return cached

        metrics = self._run_rate(rate)
        metrics["suite"] = self.suite
        self.records.append(metrics)

        if self._baseline is None and metrics.get("sample_quality") == "ok":
            self._baseline = {
                "owd_p50_ms": metrics.get("owd_p50_ms"),
                "owd_p95_ms": metrics.get("owd_p95_ms"),
                "rtt_p50_ms": metrics.get("rtt_p50_ms"),
                "rtt_p95_ms": metrics.get("rtt_p95_ms"),
            }

        signals = self._classify_signals(metrics)
        is_bad = any(signals.values())
        cause = self._update_history(signals, rate, metrics)
        if is_bad:
            if self._first_bad_rate is None or rate < self._first_bad_rate:
                self._first_bad_rate = rate
        else:
            if metrics.get("sample_quality") == "ok":
                if self._last_ok_rate is None or rate > self._last_ok_rate:
                    self._last_ok_rate = rate

        result = (metrics, is_bad, cause)
        self._rate_cache[rate] = result
        return result

    def _classify_signals(self, metrics: Dict[str, float]) -> Dict[str, bool]:
        signals = {key: False for key in SATURATION_SIGNALS}
        baseline = self._baseline
        if baseline and baseline.get("owd_p95_ms"):
            baseline_p95 = baseline.get("owd_p95_ms", 0.0)
            if baseline_p95 > 0:
                signals["owd_p95_spike"] = metrics.get("owd_p95_ms", 0.0) > baseline_p95 * self.spike_factor
        signals["delivery_degraded"] = metrics.get("goodput_ratio", 0.0) < self.delivery_threshold
        signals["loss_excess"] = metrics.get("loss_pct", 0.0) > self.loss_threshold
        return signals

    def _update_history(
        self,
        signals: Dict[str, bool],
        rate: int,
        metrics: Dict[str, float],
    ) -> Optional[str]:
        cause = None
        for key in SATURATION_SIGNALS:
            history = self._signal_history[key]
            history.append(bool(signals.get(key)))
            if self._stop_cause is None and sum(history) >= 2:
                self._stop_cause = key
                self._stop_samples = max(metrics.get("rtt_samples", 0), metrics.get("owd_samples", 0))
                cause = key
        return cause

    def _run_rate(self, rate_mbps: int) -> Dict[str, float]:
        denominator = max(self.payload_bytes * 8, 1)
        rate_pps = int((rate_mbps * 1_000_000) / denominator)
        if rate_pps <= 0:
            rate_pps = 1
        events_path = self.output_dir / f"saturation_{rate_mbps}Mbps.jsonl"
        warmup_s = min(MAX_WARMUP_SECONDS, self.duration_s * WARMUP_FRACTION)
        if warmup_s > 0:
            warmup_blaster = Blaster(
                APP_SEND_HOST,
                APP_SEND_PORT,
                APP_RECV_HOST,
                APP_RECV_PORT,
                events_path=None,
                payload_bytes=self.payload_bytes,
                sample_every=0,
                offset_ns=self.offset_ns,
            )
            warmup_blaster.run(duration_s=warmup_s, rate_pps=rate_pps)
        blaster = Blaster(
            APP_SEND_HOST,
            APP_SEND_PORT,
            APP_RECV_HOST,
            APP_RECV_PORT,
            events_path,
            payload_bytes=self.payload_bytes,
            sample_every=max(1, self.event_sample),
            offset_ns=self.offset_ns,
        )
        start = time.perf_counter()
        blaster.run(duration_s=self.duration_s, rate_pps=rate_pps)
        elapsed = max(1e-9, time.perf_counter() - start)

        sent_packets = blaster.sent
        rcvd_packets = blaster.rcvd
        sent_bytes = blaster.sent_bytes
        rcvd_bytes = blaster.rcvd_bytes

        pps_actual = sent_packets / elapsed if elapsed > 0 else 0.0
        throughput_mbps = (rcvd_bytes * 8) / (elapsed * 1_000_000) if elapsed > 0 else 0.0
        sent_mbps = (sent_bytes * 8) / (elapsed * 1_000_000) if sent_bytes else 0.0
        delivered_ratio = throughput_mbps / sent_mbps if sent_mbps > 0 else 0.0

        avg_rtt_ms = (blaster.rtt_sum_ns / max(1, blaster.rtt_samples)) / 1_000_000 if blaster.rtt_samples else 0.0
        min_rtt_ms = (blaster.rtt_min_ns or 0) / 1_000_000
        max_rtt_ms = blaster.rtt_max_ns / 1_000_000

        app_packet_bytes = self.payload_bytes + SEQ_TS_OVERHEAD_BYTES
        wire_packet_bytes_est = app_packet_bytes + UDP_HEADER_BYTES + APP_IP_HEADER_BYTES
        goodput_mbps = (
            (rcvd_packets * self.payload_bytes * 8) / (elapsed * 1_000_000)
            if elapsed > 0
            else 0.0
        )
        wire_throughput_mbps_est = (
            (rcvd_packets * wire_packet_bytes_est * 8) / (elapsed * 1_000_000)
            if elapsed > 0
            else 0.0
        )
        if sent_mbps > 0:
            goodput_ratio = goodput_mbps / sent_mbps
            goodput_ratio = max(0.0, min(1.0, goodput_ratio))
        else:
            goodput_ratio = 0.0

        loss_pct = 0.0
        if sent_packets:
            loss_pct = max(0.0, (sent_packets - rcvd_packets) * 100.0 / sent_packets)
        loss_low, loss_high = wilson_interval(max(0, sent_packets - rcvd_packets), sent_packets)

        sample_quality = "low"
        if blaster.rtt_samples >= MIN_DELAY_SAMPLES and blaster.owd_samples >= MIN_DELAY_SAMPLES:
            sample_quality = "ok"

        return {
            "rate_mbps": float(rate_mbps),
            "pps": float(rate_pps),
            "pps_actual": round(pps_actual, 1),
            "sent_mbps": round(sent_mbps, 3),
            "throughput_mbps": round(throughput_mbps, 3),
            "goodput_mbps": round(goodput_mbps, 3),
            "wire_throughput_mbps_est": round(wire_throughput_mbps_est, 3),
            "goodput_ratio": round(goodput_ratio, 3),
            "loss_pct": round(loss_pct, 3),
            "loss_pct_wilson_low": round(loss_low * 100.0, 3),
            "loss_pct_wilson_high": round(loss_high * 100.0, 3),
            "delivered_ratio": round(delivered_ratio, 3) if sent_mbps > 0 else 0.0,
            "avg_rtt_ms": round(avg_rtt_ms, 3),
            "min_rtt_ms": round(min_rtt_ms, 3),
            "max_rtt_ms": round(max_rtt_ms, 3),
            "rtt_p50_ms": round(blaster.rtt_p50_ns / 1_000_000, 3),
            "rtt_p95_ms": round(blaster.rtt_p95_ns / 1_000_000, 3),
            "owd_p50_ms": round(blaster.owd_p50_ns / 1_000_000, 3),
            "owd_p95_ms": round(blaster.owd_p95_ns / 1_000_000, 3),
            "rtt_samples": blaster.rtt_samples,
            "owd_samples": blaster.owd_samples,
            "sample_quality": sample_quality,
            "app_packet_bytes": app_packet_bytes,
            "wire_packet_bytes_est": wire_packet_bytes_est,
        }

    def export_excel(self, session_id: str) -> Optional[Path]:
        if Workbook is None:
            print("[WARN] openpyxl not available; skipping Excel export")
            return None
        EXCEL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = EXCEL_OUTPUT_DIR / f"saturation_{self.suite}_{session_id}.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "Saturation"
        ws.append([
            "rate_mbps",
            "pps",
            "pps_actual",
            "sent_mbps",
            "throughput_mbps",
            "goodput_mbps",
            "wire_throughput_mbps_est",
            "goodput_ratio",
            "loss_pct",
            "loss_pct_wilson_low",
            "loss_pct_wilson_high",
            "delivered_ratio",
            "avg_rtt_ms",
            "min_rtt_ms",
            "max_rtt_ms",
            "rtt_p50_ms",
            "rtt_p95_ms",
            "owd_p50_ms",
            "owd_p95_ms",
            "rtt_samples",
            "owd_samples",
            "sample_quality",
            "app_packet_bytes",
            "wire_packet_bytes_est",
        ])
        for record in self.records:
            ws.append([
                record.get("rate_mbps", 0.0),
                record.get("pps", 0.0),
                record.get("pps_actual", 0.0),
                record.get("sent_mbps", 0.0),
                record.get("throughput_mbps", 0.0),
                record.get("goodput_mbps", 0.0),
                record.get("wire_throughput_mbps_est", 0.0),
                record.get("goodput_ratio", 0.0),
                record.get("loss_pct", 0.0),
                record.get("loss_pct_wilson_low", 0.0),
                record.get("loss_pct_wilson_high", 0.0),
                record.get("delivered_ratio", 0.0),
                record.get("avg_rtt_ms", 0.0),
                record.get("min_rtt_ms", 0.0),
                record.get("max_rtt_ms", 0.0),
                record.get("rtt_p50_ms", 0.0),
                record.get("rtt_p95_ms", 0.0),
                record.get("owd_p50_ms", 0.0),
                record.get("owd_p95_ms", 0.0),
                record.get("rtt_samples", 0),
                record.get("owd_samples", 0),
                record.get("sample_quality", "low"),
                record.get("app_packet_bytes", 0),
                record.get("wire_packet_bytes_est", 0),
            ])
        wb.save(path)
        return path


class TelemetryCollector:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.stop_event = threading.Event()
        self.server: Optional[socket.socket] = None
        self.accept_thread: Optional[threading.Thread] = None
        self.client_threads: List[threading.Thread] = []
        # Bug #9 fix: Use deque with maxlen to prevent unbounded memory growth
        from collections import deque

        env_maxlen = os.getenv("GCS_TELEM_MAXLEN")
        maxlen = 100000
        if env_maxlen is not None:
            try:
                maxlen = max(1000, int(env_maxlen))
            except (TypeError, ValueError):
                maxlen = 100000
        self.samples: deque = deque(maxlen=maxlen)  # ~10MB limit for long tests
        self.lock = threading.Lock()
        self.enabled = True

    def start(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.host, self.port))
            srv.listen(8)
            srv.settimeout(0.5)
            self.server = srv
            self.accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
            self.accept_thread.start()
            print(f"[{ts()}] telemetry collector listening on {self.host}:{self.port}")
        except Exception as exc:
            print(f"[WARN] telemetry collector disabled: {exc}", file=sys.stderr)
            self.enabled = False
            if self.server:
                try:
                    self.server.close()
                except Exception:
                    pass
            self.server = None

    def _accept_loop(self) -> None:
        assert self.server is not None
        while not self.stop_event.is_set():
            try:
                conn, addr = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as exc:
                if not self.stop_event.is_set():
                    print(f"[WARN] telemetry accept error: {exc}", file=sys.stderr)
                continue
            thread = threading.Thread(target=self._client_loop, args=(conn, addr), daemon=True)
            thread.start()
            self.client_threads.append(thread)

    def _client_loop(self, conn: socket.socket, addr) -> None:
        peer = f"{addr[0]}:{addr[1]}"
        try:
            conn.settimeout(1.0)
            with conn, conn.makefile("r", encoding="utf-8") as reader:
                for line in reader:
                    if self.stop_event.is_set():
                        break
                    data = line.strip()
                    if not data:
                        continue
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    payload.setdefault("collector_ts_ns", time.time_ns())
                    payload.setdefault("source", "drone")
                    payload.setdefault("peer", peer)
                    with self.lock:
                        self.samples.append(payload)
        except Exception:
            # drop connection silently
            pass

    def snapshot(self) -> List[dict]:
        with self.lock:
            # Convert deque to list for compatibility
            return list(self.samples)

    def stop(self) -> None:
        self.stop_event.set()
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass
        if self.accept_thread and self.accept_thread.is_alive():
            self.accept_thread.join(timeout=1.5)
        for thread in self.client_threads:
            if thread.is_alive():
                thread.join(timeout=1.0)

def resolve_under_root(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else ROOT / expanded


def safe_sheet_name(name: str) -> str:
    sanitized = "".join("_" if ch in '[]:*?/\\' else ch for ch in name).strip()
    if not sanitized:
        sanitized = "Sheet"
    return sanitized[:31]


def unique_sheet_name(workbook, base_name: str) -> str:
    base = safe_sheet_name(base_name)
    if base not in workbook.sheetnames:
        return base
    index = 1
    while True:
        suffix = f"_{index}"
        name = base[: 31 - len(suffix)] + suffix
        if name not in workbook.sheetnames:
            return name
        index += 1


def append_dict_sheet(workbook, title: str, rows: List[dict]) -> None:
    if not rows:
        return
    sheet_name = unique_sheet_name(workbook, title)
    ws = workbook.create_sheet(sheet_name)
    headers: List[str] = []
    for row in rows:
        for key in row.keys():
            if key not in headers:
                headers.append(key)
    ws.append(headers)
    for row in rows:
        ws.append([row.get(header, "") for header in headers])


def append_csv_sheet(workbook, path: Path, title: str) -> None:
    if not path.exists():
        return
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            rows = list(reader)
    except Exception as exc:
        print(f"[WARN] failed to read CSV {path}: {exc}", file=sys.stderr)
        return
    if not rows:
        return
    sheet_name = unique_sheet_name(workbook, title)
    ws = workbook.create_sheet(sheet_name)
    for row in rows:
        ws.append(row)


def locate_drone_session_dir(session_id: str) -> Optional[Path]:
    candidates = []
    try:
        candidates.append(resolve_under_root(DRONE_MONITOR_BASE) / session_id)
    except Exception:
        pass
    fallback = Path("/home/dev/research/output/drone") / session_id
    candidates.append(fallback)
    repo_default = ROOT / "output" / "drone" / session_id
    candidates.append(repo_default)
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            if candidate.exists():
                return candidate
        except Exception:
            continue
    return None


def export_combined_excel(
    session_id: str,
    summary_rows: List[dict],
    saturation_overview: List[dict],
    saturation_samples: List[dict],
    telemetry_samples: List[dict],
) -> Optional[Path]:
    if Workbook is None:
        print("[WARN] openpyxl not available; skipping combined Excel export", file=sys.stderr)
        return None

    workbook = Workbook()
    info_sheet = workbook.active
    info_sheet.title = "run_info"
    info_sheet.append(["generated_utc", ts()])
    info_sheet.append(["session_id", session_id])

    append_dict_sheet(workbook, "gcs_summary", summary_rows)
    append_dict_sheet(workbook, "saturation_overview", saturation_overview)
    append_dict_sheet(workbook, "saturation_samples", saturation_samples)
    append_dict_sheet(workbook, "telemetry_samples", telemetry_samples)

    if SUMMARY_CSV.exists():
        append_csv_sheet(workbook, SUMMARY_CSV, "gcs_summary_csv")

    drone_session_dir = locate_drone_session_dir(session_id)
    if drone_session_dir:
        info_sheet.append(["drone_session_dir", str(drone_session_dir)])
        for csv_path in sorted(drone_session_dir.glob("*.csv")):
            append_csv_sheet(workbook, csv_path, csv_path.stem[:31])
    else:
        info_sheet.append(["drone_session_dir", "not_found"])

    combined_dir = resolve_under_root(COMBINED_OUTPUT_DIR)
    combined_dir.mkdir(parents=True, exist_ok=True)
    target_path = combined_dir / f"{session_id}_combined.xlsx"
    workbook.save(target_path)
    return target_path


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)

    auto = AUTO_GCS_CONFIG

    traffic_mode = str(auto.get("traffic") or "blast").lower()
    pre_gap = float(auto.get("pre_gap_s") or 1.0)
    inter_gap = float(auto.get("inter_gap_s") or 15.0)
    duration = float(auto.get("duration_s") or 45.0)
    payload_bytes = int(auto.get("payload_bytes") or 256)
    event_sample = int(auto.get("event_sample") or 100)
    passes = int(auto.get("passes") or 1)
    rate_pps = int(auto.get("rate_pps") or 0)
    bandwidth_mbps = float(auto.get("bandwidth_mbps") or 0.0)
    max_rate_mbps = float(auto.get("max_rate_mbps") or 200.0)
    if bandwidth_mbps > 0:
        denominator = max(payload_bytes * 8, 1)
        rate_pps = max(1, int((bandwidth_mbps * 1_000_000) / denominator))

    sat_search_cfg = str(auto.get("sat_search") or SATURATION_SEARCH_MODE).lower()
    if sat_search_cfg not in {"auto", "linear", "bisect"}:
        sat_search_cfg = SATURATION_SEARCH_MODE
    sat_delivery_threshold = float(auto.get("sat_delivery_threshold") or SATURATION_DELIVERY_THRESHOLD)
    sat_loss_threshold = float(auto.get("sat_loss_threshold_pct") or SATURATION_LOSS_THRESHOLD)
    sat_spike_factor = float(auto.get("sat_rtt_spike_factor") or SATURATION_RTT_SPIKE)

    if duration <= 0:
        raise ValueError("AUTO_GCS.duration_s must be positive")
    if pre_gap < 0:
        raise ValueError("AUTO_GCS.pre_gap_s must be >= 0")
    if inter_gap < 0:
        raise ValueError("AUTO_GCS.inter_gap_s must be >= 0")
    if rate_pps < 0:
        raise ValueError("AUTO_GCS.rate_pps must be >= 0")
    if passes <= 0:
        raise ValueError("AUTO_GCS.passes must be >= 1")

    if traffic_mode not in {"blast", "mavproxy", "saturation"}:
        raise ValueError(f"Unsupported traffic mode: {traffic_mode}")

    suites_override = auto.get("suites")
    suites = resolve_suites(suites_override)
    if not suites:
        raise RuntimeError("No suites selected for execution")

    session_prefix = str(auto.get("session_prefix") or "session")
    session_id = os.environ.get("GCS_SESSION_ID") or f"{session_prefix}_{int(time.time())}"
    print(f"[{ts()}] session_id={session_id}")

    initial_suite = preferred_initial_suite(suites)
    if initial_suite and suites[0] != initial_suite:
        suites = [initial_suite] + [s for s in suites if s != initial_suite]
        print(f"[{ts()}] reordered suites to start with {initial_suite} (from CONFIG)")

    power_capture_enabled = bool(auto.get("power_capture", True))

    telemetry_enabled = bool(auto.get("telemetry_enabled", True))
    telemetry_bind_host = auto.get("telemetry_bind_host") or TELEMETRY_BIND_HOST
    telemetry_port_cfg = auto.get("telemetry_port")
    telemetry_port = TELEMETRY_PORT if telemetry_port_cfg in (None, "") else int(telemetry_port_cfg)

    print(
        f"[{ts()}] traffic={traffic_mode} duration={duration:.1f}s pre_gap={pre_gap:.1f}s "
        f"inter_gap={inter_gap:.1f}s payload={payload_bytes}B event_sample={event_sample} passes={passes} "
        f"rate_pps={rate_pps} sat_search={sat_search_cfg}"
    )
    if bandwidth_mbps > 0:
        print(f"[{ts()}] bandwidth target {bandwidth_mbps:.2f} Mbps -> approx {rate_pps} pps")
    print(f"[{ts()}] power capture: {'enabled' if power_capture_enabled else 'disabled'}")

    reachable = False
    for attempt in range(8):
        try:
            resp = ctl_send({"cmd": "ping"}, timeout=1.0, retries=1)
            if resp.get("ok"):
                reachable = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    if reachable:
        print(f"[{ts()}] follower reachable at {DRONE_HOST}:{CONTROL_PORT}")
    else:
        print(f"[WARN] follower not reachable at {DRONE_HOST}:{CONTROL_PORT}", file=sys.stderr)

    offset_ns = 0
    try:
        sync = timesync()
        offset_ns = sync["offset_ns"]
        print(f"[{ts()}] clocks synced: offset_ns={offset_ns} ns, link_rtt~{sync['rtt_ns']} ns")
    except Exception as exc:
        print(f"[WARN] timesync failed: {exc}", file=sys.stderr)

    telemetry_collector: Optional[TelemetryCollector] = None
    if telemetry_enabled:
        telemetry_collector = TelemetryCollector(telemetry_bind_host, telemetry_port)
        telemetry_collector.start()
        print(f"[{ts()}] telemetry collector -> {telemetry_bind_host}:{telemetry_port}")
    else:
        print(f"[{ts()}] telemetry collector disabled via AUTO_GCS configuration")

    if not bool(auto.get("launch_proxy", True)):
        raise NotImplementedError("AUTO_GCS.launch_proxy=False is not supported")

    gcs_proc: Optional[subprocess.Popen] = None
    log_handle = None
    gcs_proc, log_handle = start_gcs_proxy(suites[0])

    try:
        ready = wait_handshake(timeout=20.0)
        print(f"[{ts()}] initial handshake ready? {ready}")

        summary_rows: List[dict] = []
        saturation_reports: List[dict] = []
        all_rate_samples: List[dict] = []
        telemetry_samples: List[dict] = []

        if traffic_mode == "saturation":
            for idx, suite in enumerate(suites):
                rekey_ms = activate_suite(gcs_proc, suite, is_first=(idx == 0))
                outdir = suite_outdir(suite)
                tester = SaturationTester(
                    suite=suite,
                    payload_bytes=payload_bytes,
                    duration_s=duration,
                    event_sample=event_sample,
                    offset_ns=offset_ns,
                    output_dir=outdir,
                    max_rate_mbps=int(max_rate_mbps),
                    search_mode=sat_search_cfg,
                    delivery_threshold=sat_delivery_threshold,
                    loss_threshold=sat_loss_threshold,
                    spike_factor=sat_spike_factor,
                )
                summary = tester.run()
                summary["rekey_ms"] = rekey_ms
                excel_path = tester.export_excel(session_id)
                if excel_path:
                    summary["excel_path"] = str(excel_path)
                saturation_reports.append(summary)
                all_rate_samples.extend(dict(record) for record in tester.records)
                if inter_gap > 0 and idx < len(suites) - 1:
                    time.sleep(inter_gap)
            report_path = OUTDIR / f"saturation_summary_{session_id}.json"
            with open(report_path, "w", encoding="utf-8") as handle:
                json.dump(saturation_reports, handle, indent=2)
            print(f"[{ts()}] saturation summary written to {report_path}")
        else:
            for pass_index in range(passes):
                for idx, suite in enumerate(suites):
                    row = run_suite(
                        gcs_proc,
                        suite,
                        is_first=(pass_index == 0 and idx == 0),
                        duration_s=duration,
                        payload_bytes=payload_bytes,
                        event_sample=event_sample,
                        offset_ns=offset_ns,
                        pass_index=pass_index,
                        traffic_mode=traffic_mode,
                        pre_gap=pre_gap,
                        rate_pps=rate_pps,
                        power_capture_enabled=power_capture_enabled,
                    )
                    summary_rows.append(row)
                    is_last_suite = idx == len(suites) - 1
                    is_last_pass = pass_index == passes - 1
                    if inter_gap > 0 and not (is_last_suite and is_last_pass):
                        time.sleep(inter_gap)

            write_summary(summary_rows)

        if telemetry_collector and telemetry_collector.enabled:
            telemetry_samples = telemetry_collector.snapshot()

        if auto.get("export_combined_excel", True):
            combined_path = export_combined_excel(
                session_id=session_id,
                summary_rows=summary_rows,
                saturation_overview=saturation_reports,
                saturation_samples=all_rate_samples,
                telemetry_samples=telemetry_samples,
            )
            if combined_path:
                print(f"[{ts()}] combined workbook written to {combined_path}")

    finally:
        try:
            ctl_send({"cmd": "stop"})
        except Exception:
            pass

        if gcs_proc and gcs_proc.stdin:
            try:
                gcs_proc.stdin.write("quit\n")
                gcs_proc.stdin.flush()
            except Exception:
                pass
        if gcs_proc:
            try:
                gcs_proc.wait(timeout=5)
            except Exception:
                gcs_proc.kill()

        if log_handle:
            try:
                log_handle.close()
            except Exception:
                pass

        if telemetry_collector:
            telemetry_collector.stop()


if __name__ == "__main__":
    main()
