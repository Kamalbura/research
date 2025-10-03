#!/usr/bin/env python3
"""GCS scheduler that drives rekeys and UDP traffic using central configuration."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

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

OUTDIR = Path("logs/auto/gcs")
SUITES_OUTDIR = OUTDIR / "suites"
SECRETS_DIR = Path("secrets/matrix")

EXCEL_OUTPUT_DIR = Path(
    CONFIG.get("GCS_EXCEL_OUTPUT")
    or os.getenv("GCS_EXCEL_OUTPUT", "output/gcs")
)

COMBINED_OUTPUT_DIR = Path(
    CONFIG.get("GCS_COMBINED_OUTPUT_BASE")
    or os.getenv("GCS_COMBINED_OUTPUT_BASE", "output/gcs")
)

DRONE_MONITOR_BASE = Path(
    CONFIG.get("DRONE_MONITOR_OUTPUT_BASE")
    or os.getenv("DRONE_MONITOR_OUTPUT_BASE", "output/drone")
)

TELEMETRY_BIND_HOST = CONFIG.get("GCS_TELEMETRY_BIND", "0.0.0.0")
TELEMETRY_PORT = int(
    CONFIG.get("GCS_TELEMETRY_PORT")
    or CONFIG.get("DRONE_TELEMETRY_PORT")
    or 52080
)

SATURATION_TEST_RATES = [5, 10, 15, 20, 25, 30, 35, 40, 50, 60, 70, 80, 90, 100, 125, 150, 175, 200]
SATURATION_RTT_SPIKE = 1.8

PROXY_STATUS_PATH = OUTDIR / "gcs_status.json"
PROXY_SUMMARY_PATH = OUTDIR / "gcs_summary.json"
SUMMARY_CSV = OUTDIR / "summary.csv"
EVENTS_FILENAME = "blaster_events.jsonl"


def ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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


class Blaster:
    """High-rate UDP blaster with RTT sampling and throughput accounting."""

    def __init__(
        self,
        send_host: str,
        send_port: int,
        recv_host: str,
        recv_port: int,
        events_path: Path,
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
        except Exception:
            # best-effort; continue even if setting buffers fails
            pass

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

    def _log_event(self, payload: dict) -> None:
        # Buffered write; caller flushes at end of run()
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
            sends_this_loop = burst
            while sends_this_loop > 0:
                if self._now() >= stop_at:
                    break
                t_send = self._now()
                packet = seq.to_bytes(4, "big") + int(t_send).to_bytes(8, "big") + payload_pad
                try:
                    self.tx.sendto(packet, self.send_addr)
                    if self.sample_every == 0 or (self.sample_every and seq % self.sample_every == 0):
                        self.pending[seq] = int(t_send)
                    self.sent += 1
                    self.sent_bytes += len(packet)
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

        tail_deadline = self._now() + int(0.25 * 1e9)
        while self._now() < tail_deadline:
            if not self._rx_once():
                time.sleep(0)
        stop_event.set()
        rx_thread.join(timeout=0.2)
        # Bug #5 fix: Ensure cleanup happens even on exceptions
        try:
            try:
                self.events.flush()
            except Exception:
                pass
        finally:
            try:
                self.events.close()
            except Exception:
                pass
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
                time.sleep(0)

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
        if len(data) >= 12:
            seq = int.from_bytes(data[:4], "big")
            t_send = self.pending.pop(seq, None)
            if t_send is not None:
                rtt = t_recv - t_send
                self.rtt_sum_ns += rtt
                self.rtt_samples += 1
                if rtt > self.rtt_max_ns:
                    self.rtt_max_ns = rtt
                if self.rtt_min_ns is None or rtt < self.rtt_min_ns:
                    self.rtt_min_ns = rtt
                self._maybe_log("recv", seq, int(t_recv))
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


def start_gcs_proxy(initial_suite: str) -> tuple[subprocess.Popen, Path]:
    key_path = SECRETS_DIR / initial_suite / "gcs_signing.key"
    if not key_path.exists():
        raise FileNotFoundError(f"Missing GCS signing key for suite {initial_suite}: {key_path}")

    mkdirp(OUTDIR)
    log_path = OUTDIR / f"gcs_{time.strftime('%Y%m%d-%H%M%S')}.log"
    log_handle = open(log_path, "w", encoding="utf-8", errors="replace")

    os.environ["DRONE_HOST"] = DRONE_HOST
    os.environ["GCS_HOST"] = GCS_HOST
    os.environ["ENABLE_PACKET_TYPE"] = "1" if CONFIG.get("ENABLE_PACKET_TYPE", True) else "0"
    os.environ["STRICT_UDP_PEER_MATCH"] = "1" if CONFIG.get("STRICT_UDP_PEER_MATCH", True) else "0"

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
) -> dict:
    rekey_duration_ms = activate_suite(gcs, suite, is_first)

    events_path = suite_outdir(suite) / EVENTS_FILENAME
    start_mark_ns = time.time_ns() + offset_ns + int(0.150 * 1e9) + int(max(pre_gap, 0.0) * 1e9)
    try:
        ctl_send({"cmd": "schedule_mark", "suite": suite, "t0_ns": start_mark_ns})
    except Exception as exc:
        print(f"[WARN] schedule_mark failed for {suite}: {exc}", file=sys.stderr)

    print(
        f"[{ts()}] ===== POWER: START in {pre_gap:.1f}s | suite={suite} | duration={duration_s:.1f}s mode={traffic_mode} ====="
    )
    if pre_gap > 0:
        time.sleep(pre_gap)

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
    print(f"[{ts()}] ===== POWER: STOP | suite={suite} =====")

    snapshot_proxy_artifacts(suite)
    proxy_stats = read_proxy_stats_live() or read_proxy_summary()

    elapsed_s = max(1e-9, (end_perf_ns - start_perf_ns) / 1e9)
    pps = sent_packets / elapsed_s
    throughput_mbps = (rcvd_bytes * 8) / (elapsed_s * 1_000_000)
    sent_mbps = (blaster_sent_bytes * 8) / (elapsed_s * 1_000_000) if blaster_sent_bytes else 0.0
    delivered_ratio = throughput_mbps / sent_mbps if sent_mbps > 0 else 0.0
    avg_rtt_ms = avg_rtt_ns / 1_000_000
    max_rtt_ms = max_rtt_ns / 1_000_000

    loss_pct = 0.0
    if sent_packets:
        loss_pct = max(0.0, (sent_packets - rcvd_packets) * 100.0 / sent_packets)

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
        "rtt_avg_ms": round(avg_rtt_ms, 3),
        "rtt_max_ms": round(max_rtt_ms, 3),
        "rtt_samples": rtt_samples,
        "loss_pct": round(loss_pct, 3),
        "enc_out": proxy_stats.get("enc_out", 0),
        "enc_in": proxy_stats.get("enc_in", 0),
        "drops": proxy_stats.get("drops", 0),
        "rekeys_ok": proxy_stats.get("rekeys_ok", 0),
        "rekeys_fail": proxy_stats.get("rekeys_fail", 0),
        "start_ns": start_wall_ns,
        "end_ns": end_wall_ns,
        "rekey_ms": round(rekey_duration_ms, 3),
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
    ):
        self.suite = suite
        self.payload_bytes = payload_bytes
        self.duration_s = duration_s
        self.event_sample = event_sample
        self.offset_ns = offset_ns
        self.output_dir = output_dir
        self.max_rate_mbps = max_rate_mbps
        self.records: List[Dict[str, float]] = []

    def run(self) -> Dict[str, Optional[float]]:
        baseline_rtt = None
        saturation_point = None
        for rate in SATURATION_TEST_RATES:
            if rate > self.max_rate_mbps:
                break
            metrics = self._run_rate(rate)
            metrics["suite"] = self.suite
            self.records.append(metrics)
            avg_rtt = metrics["avg_rtt_ms"]
            achieved = metrics["throughput_mbps"]
            sent_mbps = metrics.get("sent_mbps", 0.0)
            ratio = metrics.get("delivered_ratio", 1.0) if sent_mbps > 0 else 1.0
            samples = metrics.get("rtt_samples", 0)
            if baseline_rtt is None and samples and avg_rtt > 0:
                baseline_rtt = avg_rtt
            if baseline_rtt is not None and samples:
                delivery_degraded = sent_mbps > 0 and ratio < 0.8
                if avg_rtt > baseline_rtt * SATURATION_RTT_SPIKE or delivery_degraded:
                    saturation_point = rate
                    break
        return {
            "suite": self.suite,
            "baseline_rtt_ms": baseline_rtt,
            "saturation_point_mbps": saturation_point,
        }

    def _run_rate(self, rate_mbps: int) -> Dict[str, float]:
        rate_pps = int((rate_mbps * 1_000_000) / (self.payload_bytes * 8))
        if rate_pps <= 0:
            rate_pps = 1
        events_path = self.output_dir / f"saturation_{rate_mbps}Mbps.jsonl"
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
        blaster.run(duration_s=self.duration_s, rate_pps=rate_pps)
        duration = max(self.duration_s, 1e-3)
        throughput_mbps = (blaster.rcvd_bytes * 8) / (duration * 1_000_000)
        sent_mbps = (blaster.sent_bytes * 8) / (duration * 1_000_000)
        delivered_ratio = throughput_mbps / sent_mbps if sent_mbps > 0 else 0.0
        loss_pct = 0.0
        if blaster.sent:
            loss_pct = max(0.0, (blaster.sent - blaster.rcvd) * 100.0 / blaster.sent)
        if blaster.rtt_samples:
            avg_rtt_ms = (blaster.rtt_sum_ns / blaster.rtt_samples) / 1_000_000
        else:
            avg_rtt_ms = 0.0
        min_rtt_ms = (blaster.rtt_min_ns or 0) / 1_000_000
        max_rtt_ms = blaster.rtt_max_ns / 1_000_000
        return {
            "rate_mbps": float(rate_mbps),
            "pps": float(rate_pps),
            "throughput_mbps": round(throughput_mbps, 3),
            "sent_mbps": round(sent_mbps, 3),
            "loss_pct": round(loss_pct, 3),
            "delivered_ratio": round(delivered_ratio, 3) if sent_mbps > 0 else 0.0,
            "avg_rtt_ms": round(avg_rtt_ms, 3),
            "min_rtt_ms": round(min_rtt_ms, 3),
            "max_rtt_ms": round(max_rtt_ms, 3),
            "rtt_samples": blaster.rtt_samples,
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
            "sent_mbps",
            "throughput_mbps",
            "loss_pct",
            "delivered_ratio",
            "avg_rtt_ms",
            "min_rtt_ms",
            "max_rtt_ms",
            "rtt_samples",
        ])
        for record in self.records:
            ws.append([
                record["rate_mbps"],
                record["pps"],
                record.get("sent_mbps", 0),
                record["throughput_mbps"],
                record["loss_pct"],
                record.get("delivered_ratio", 0),
                record["avg_rtt_ms"],
                record["min_rtt_ms"],
                record["max_rtt_ms"],
                record.get("rtt_samples", 0),
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
        self.samples: deque = deque(maxlen=100000)  # ~10MB limit for long tests
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
    return path if path.is_absolute() else ROOT / path


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

    parser = argparse.ArgumentParser(description="GCS automation scheduler (CONFIG-driven)")
    parser.add_argument(
        "--traffic",
        choices=["blast", "mavproxy", "saturation"],
        default="blast",
        help="blast = internal UDP blaster; saturation = ramp rates until RTT spike.",
    )
    parser.add_argument(
        "--pre-gap",
        type=float,
        default=1.0,
        help="Seconds to wait after (re)key before sending.",
    )
    parser.add_argument(
        "--inter-gap",
        type=float,
        default=15.0,
        help="Seconds to wait between suites.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=45.0,
        help="Active send window per suite.",
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=0,
        help="Packets/sec for blast; 0 = as fast as possible.",
    )
    parser.add_argument(
        "--max-rate",
        type=int,
        default=200,
        help="Upper bound Mbps for saturation testing.",
    )
    parser.add_argument(
        "--payload-bytes",
        type=int,
        default=256,
        help="UDP payload size used for throughput calc.",
    )
    parser.add_argument(
        "--event-sample",
        type=int,
        default=100,
        help="Log every Nth send/recv event (0 = disable).",
    )
    parser.add_argument("--passes", type=int, default=1, help="Number of full sweeps across suites")
    parser.add_argument("--suites", nargs="*", help="Optional subset of suites to exercise")
    parser.add_argument("--session-id", help="Identifier for output artifacts")
    args = parser.parse_args()

    if args.duration <= 0:
        raise ValueError("--duration must be positive")
    if args.pre_gap < 0:
        raise ValueError("--pre-gap must be >= 0")
    if args.inter_gap < 0:
        raise ValueError("--inter-gap must be >= 0")
    if args.rate < 0:
        raise ValueError("--rate must be >= 0")
    if args.passes <= 0:
        raise ValueError("--passes must be >= 1")

    suites = resolve_suites(args.suites)
    if not suites:
        raise RuntimeError("No suites selected for execution")

    session_id = args.session_id or f"session_{int(time.time())}"

    initial_suite = preferred_initial_suite(suites)
    if initial_suite and suites[0] != initial_suite:
        suites = [initial_suite] + [s for s in suites if s != initial_suite]
        print(f"[{ts()}] reordered suites to start with {initial_suite} (from CONFIG)")

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

    telemetry_collector = TelemetryCollector(TELEMETRY_BIND_HOST, TELEMETRY_PORT)
    telemetry_collector.start()

    gcs_proc, log_handle = start_gcs_proxy(suites[0])

    try:
        ready = wait_handshake(timeout=20.0)
        print(f"[{ts()}] initial handshake ready? {ready}")

        summary_rows: List[dict] = []
        saturation_reports: List[dict] = []
        all_rate_samples: List[dict] = []
        telemetry_samples: List[dict] = []

        if args.traffic == "saturation":
            for idx, suite in enumerate(suites):
                rekey_ms = activate_suite(gcs_proc, suite, is_first=(idx == 0))
                outdir = suite_outdir(suite)
                tester = SaturationTester(
                    suite=suite,
                    payload_bytes=args.payload_bytes,
                    duration_s=args.duration,
                    event_sample=args.event_sample,
                    offset_ns=offset_ns,
                    output_dir=outdir,
                    max_rate_mbps=args.max_rate,
                )
                summary = tester.run()
                summary["rekey_ms"] = rekey_ms
                excel_path = tester.export_excel(session_id)
                if excel_path:
                    summary["excel_path"] = str(excel_path)
                saturation_reports.append(summary)
                all_rate_samples.extend(dict(record) for record in tester.records)
                if args.inter_gap > 0 and idx < len(suites) - 1:
                    time.sleep(args.inter_gap)
            report_path = OUTDIR / f"saturation_summary_{session_id}.json"
            with open(report_path, "w", encoding="utf-8") as handle:
                json.dump(saturation_reports, handle, indent=2)
            print(f"[{ts()}] saturation summary written to {report_path}")
        else:
            for pass_index in range(args.passes):
                for idx, suite in enumerate(suites):
                    row = run_suite(
                        gcs_proc,
                        suite,
                        is_first=(pass_index == 0 and idx == 0),
                        duration_s=args.duration,
                        payload_bytes=args.payload_bytes,
                        event_sample=args.event_sample,
                        offset_ns=offset_ns,
                        pass_index=pass_index,
                        traffic_mode=args.traffic,
                        pre_gap=args.pre_gap,
                        rate_pps=args.rate,
                    )
                    summary_rows.append(row)
                    is_last_suite = idx == len(suites) - 1
                    is_last_pass = pass_index == args.passes - 1
                    if args.inter_gap > 0 and not (is_last_suite and is_last_pass):
                        time.sleep(args.inter_gap)

            write_summary(summary_rows)

        if telemetry_collector.enabled:
            telemetry_samples = telemetry_collector.snapshot()

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

        if gcs_proc.stdin:
            try:
                gcs_proc.stdin.write("quit\n")
                gcs_proc.stdin.flush()
            except Exception:
                pass
        try:
            gcs_proc.wait(timeout=5)
        except Exception:
            gcs_proc.kill()

        try:
            log_handle.close()
        except Exception:
            pass

        telemetry_collector.stop()


if __name__ == "__main__":
    main()
