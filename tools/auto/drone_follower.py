#!/usr/bin/env python3
"""Drone follower/loopback agent driven entirely by core configuration.

This script launches the drone proxy, exposes the TCP control channel for the
GCS scheduler, and runs the plaintext UDP echo used to validate the encrypted
path. All network endpoints originate from :mod:`core.config`. Test behaviour
can be tuned via optional CLI flags (e.g. to disable perf monitors), but no
network parameters are duplicated here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
# Ensure project root is on sys.path so `import core` works when running this file
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import csv
import json
import os
import shlex
import signal
import socket
import struct
import subprocess
import threading
import time
from typing import Optional
def optimize_cpu_performance(target_khz: int = 1800000) -> None:
    governors = list(Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq"))
    for governor_dir in governors:
        gov = governor_dir / "scaling_governor"
        min_freq = governor_dir / "scaling_min_freq"
        max_freq = governor_dir / "scaling_max_freq"
        try:
            if gov.exists():
                gov.write_text("performance\n", encoding="utf-8")
            if min_freq.exists():
                min_freq.write_text(f"{target_khz}\n", encoding="utf-8")
            if max_freq.exists():
                current_max = int(max_freq.read_text().strip())
                if current_max < target_khz:
                    max_freq.write_text(f"{target_khz}\n", encoding="utf-8")
        except PermissionError:
            print("[follower] insufficient permissions to adjust CPU governor")
        except Exception as exc:
            print(f"[follower] governor tuning failed: {exc}")


import psutil

from core.config import CONFIG
from core import suites as suites_mod


CONTROL_HOST = CONFIG.get("DRONE_CONTROL_HOST", "0.0.0.0")
CONTROL_PORT = int(CONFIG.get("DRONE_CONTROL_PORT", 48080))

APP_BIND_HOST = CONFIG.get("DRONE_PLAINTEXT_HOST", "127.0.0.1")
APP_RECV_PORT = int(CONFIG.get("DRONE_PLAINTEXT_RX", 47004))
APP_SEND_HOST = CONFIG.get("DRONE_PLAINTEXT_HOST", "127.0.0.1")
APP_SEND_PORT = int(CONFIG.get("DRONE_PLAINTEXT_TX", 47003))

DRONE_HOST = CONFIG["DRONE_HOST"]
GCS_HOST = CONFIG["GCS_HOST"]

OUTDIR = Path("logs/auto/drone")
MARK_DIR = OUTDIR / "marks"
SECRETS_DIR = Path("secrets/matrix")

DEFAULT_MONITOR_BASE = Path(
    CONFIG.get("DRONE_MONITOR_OUTPUT_BASE")
    or os.getenv("DRONE_MONITOR_OUTPUT_BASE", "/home/dev/research/output/drone")
)
LOG_INTERVAL_MS = 100

PERF_EVENTS = "task-clock,cycles,instructions,cache-misses,branch-misses,context-switches,branches"


def ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def popen(cmd, **kw) -> subprocess.Popen:
    if isinstance(cmd, (list, tuple)):
        display = " ".join(shlex.quote(str(part)) for part in cmd)
    else:
        display = str(cmd)
    print(f"[{ts()}] exec: {display}", flush=True)
    return subprocess.Popen(cmd, **kw)


def killtree(proc: Optional[subprocess.Popen]) -> None:
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


def discover_initial_suite() -> str:
    configured = CONFIG.get("SIMPLE_INITIAL_SUITE")
    if configured:
        return configured

    suite_map = suites_mod.list_suites()
    if suite_map:
        return sorted(suite_map.keys())[0]

    if SECRETS_DIR.exists():
        for path in sorted(SECRETS_DIR.iterdir()):
            if (path / "gcs_signing.pub").exists():
                return path.name

    return "cs-mlkem768-aesgcm-mldsa65"


def suite_outdir(suite: str) -> Path:
    path = OUTDIR / suite
    path.mkdir(parents=True, exist_ok=True)
    return path


def suite_secrets_dir(suite: str) -> Path:
    return SECRETS_DIR / suite


def write_marker(suite: str) -> None:
    MARK_DIR.mkdir(parents=True, exist_ok=True)
    marker = MARK_DIR / f"{int(time.time())}_{suite}.json"
    with open(marker, "w", encoding="utf-8") as handle:
        json.dump({"ts": ts(), "suite": suite}, handle)


def start_drone_proxy(suite: str) -> subprocess.Popen:
    suite_dir = suite_secrets_dir(suite)
    pub = suite_dir / "gcs_signing.pub"
    if not pub.exists():
        print(f"[follower] ERROR: missing {pub}", file=sys.stderr)
        sys.exit(2)

    os.environ["DRONE_HOST"] = DRONE_HOST
    os.environ["GCS_HOST"] = GCS_HOST
    os.environ["ENABLE_PACKET_TYPE"] = "1" if CONFIG.get("ENABLE_PACKET_TYPE", True) else "0"
    os.environ["STRICT_UDP_PEER_MATCH"] = "1" if CONFIG.get("STRICT_UDP_PEER_MATCH", True) else "0"

    suite_path = suite_outdir(suite)
    status = suite_path / "drone_status.json"
    summary = suite_path / "drone_summary.json"
    OUTDIR.mkdir(parents=True, exist_ok=True)
    log_path = OUTDIR / f"drone_{time.strftime('%Y%m%d-%H%M%S')}.log"
    log_handle = open(log_path, "w", encoding="utf-8")

    print(f"[follower] launching drone proxy on suite {suite}", flush=True)
    return popen([
        sys.executable,
        "-m",
        "core.run_proxy",
        "drone",
        "--suite",
        suite,
        "--peer-pubkey-file",
        str(pub),
        "--status-file",
        str(status),
        "--json-out",
        str(summary),
    ], stdout=log_handle, stderr=subprocess.STDOUT, text=True)


class HighSpeedMonitor(threading.Thread):
    def __init__(self, output_dir: Path, session_id: str):
        super().__init__(daemon=True)
        self.output_dir = output_dir
        self.session_id = session_id
        self.stop_event = threading.Event()
        self.current_suite = "unknown"
        self.proxy_pid: Optional[int] = None
        self.rekey_start_ns: Optional[int] = None
        self.csv_handle: Optional[object] = None
        self.csv_writer: Optional[csv.writer] = None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.output_dir / f"system_monitoring_{session_id}.csv"

    def attach_proxy(self, pid: int) -> None:
        self.proxy_pid = pid

    def start_rekey(self, old_suite: str, new_suite: str) -> None:
        self.current_suite = new_suite
        self.rekey_start_ns = time.time_ns()
        print(f"[monitor] rekey transition {old_suite} -> {new_suite}")

    def end_rekey(self) -> None:
        if self.rekey_start_ns is None:
            return
        duration_ms = (time.time_ns() - self.rekey_start_ns) / 1_000_000
        print(f"[monitor] rekey completed in {duration_ms:.2f} ms")
        self.rekey_start_ns = None

    def run(self) -> None:
        self.csv_handle = open(self.csv_path, "w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_handle)
        self.csv_writer.writerow(
            [
                "timestamp_iso",
                "timestamp_ns",
                "suite",
                "proxy_pid",
                "cpu_percent",
                "cpu_freq_mhz",
                "cpu_temp_c",
                "mem_used_mb",
                "mem_percent",
                "rekey_duration_ms",
            ]
        )
        interval = LOG_INTERVAL_MS / 1000.0
        while not self.stop_event.is_set():
            start = time.time()
            self._sample()
            elapsed = time.time() - start
            sleep_for = max(0.0, interval - elapsed)
            if sleep_for:
                time.sleep(sleep_for)

    def _sample(self) -> None:
        timestamp_ns = time.time_ns()
        timestamp_iso = time.strftime("%Y-%m-%d %H:%M:%S.%f", time.gmtime(timestamp_ns / 1e9))[:-3]
        cpu_percent = psutil.cpu_percent(interval=None)
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "r", encoding="utf-8") as handle:
                cpu_freq_mhz = int(handle.read().strip()) / 1000.0
        except Exception:
            cpu_freq_mhz = 0.0
        cpu_temp_c = 0.0
        try:
            result = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True, text=True)
            if result.returncode == 0 and "=" in result.stdout:
                cpu_temp_c = float(result.stdout.split("=")[1].split("'" )[0])
        except Exception:
            pass
        mem = psutil.virtual_memory()
        rekey_ms = ""
        if self.rekey_start_ns is not None:
            rekey_ms = f"{(timestamp_ns - self.rekey_start_ns) / 1_000_000:.2f}"
        if self.csv_writer is None:
            return
        self.csv_writer.writerow(
            [
                timestamp_iso,
                str(timestamp_ns),
                self.current_suite,
                self.proxy_pid or "",
                f"{cpu_percent:.1f}",
                f"{cpu_freq_mhz:.1f}",
                f"{cpu_temp_c:.1f}",
                f"{mem.used / (1024 * 1024):.1f}",
                f"{mem.percent:.1f}",
                rekey_ms,
            ]
        )
        self.csv_handle.flush()

    def stop(self) -> None:
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=2.0)
        if self.csv_handle:
            self.csv_handle.close()


class UdpEcho(threading.Thread):
    def __init__(
        self,
        bind_host: str,
        recv_port: int,
        send_host: str,
        send_port: int,
        stop_event: threading.Event,
        monitor: Optional[HighSpeedMonitor],
        session_dir: Path,
    ):
        super().__init__(daemon=True)
        self.bind_host = bind_host
        self.recv_port = recv_port
        self.send_host = send_host
        self.send_port = send_port
        self.stop_event = stop_event
        self.monitor = monitor
        self.session_dir = session_dir
        self.rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sndbuf = int(os.getenv("DRONE_SOCK_SNDBUF", str(16 << 20)))
            rcvbuf = int(os.getenv("DRONE_SOCK_RCVBUF", str(16 << 20)))
            self.rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
            self.tx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
        except Exception:
            pass
        self.rx_sock.bind((self.bind_host, self.recv_port))
        self.packet_log_path = self.session_dir / "packet_timing.csv"
        self.packet_log_handle: Optional[object] = None
        self.packet_writer: Optional[csv.writer] = None
        self.samples = 0

    def run(self) -> None:
        print(
            f"[follower] UDP echo up: recv:{self.bind_host}:{self.recv_port} -> send:{self.send_host}:{self.send_port}",
            flush=True,
        )
        self.packet_log_handle = open(self.packet_log_path, "w", newline="", encoding="utf-8")
        self.packet_writer = csv.writer(self.packet_log_handle)
        self.packet_writer.writerow([
            "recv_timestamp_ns",
            "send_timestamp_ns",
            "processing_ns",
            "processing_ms",
            "sequence",
        ])
        self.rx_sock.settimeout(0.001)
        while not self.stop_event.is_set():
            try:
                data, _ = self.rx_sock.recvfrom(65535)
                recv_ns = time.time_ns()
                enhanced = self._annotate_packet(data, recv_ns)
                send_ns = time.time_ns()
                self.tx_sock.sendto(enhanced, (self.send_host, self.send_port))
                self._record_packet(data, recv_ns, send_ns)
            except socket.timeout:
                continue
            except Exception as exc:
                print(f"[follower] UDP echo error: {exc}", flush=True)
        self.rx_sock.close()
        self.tx_sock.close()
        if self.packet_log_handle:
            self.packet_log_handle.close()

    def _annotate_packet(self, data: bytes, recv_ns: int) -> bytes:
        if len(data) >= 20:
            return data[:-8] + recv_ns.to_bytes(8, "big")
        return data + recv_ns.to_bytes(8, "big")

    def _record_packet(self, data: bytes, recv_ns: int, send_ns: int) -> None:
        if self.packet_writer is None or len(data) < 4:
            return
        try:
            seq, = struct.unpack("!I", data[:4])
        except struct.error:
            return
        processing_ns = send_ns - recv_ns
        if seq % 100 == 0:
            self.packet_writer.writerow([
                recv_ns,
                send_ns,
                processing_ns,
                f"{processing_ns / 1_000_000:.6f}",
                seq,
            ])
            if self.packet_log_handle:
                self.packet_log_handle.flush()



class Monitors:
    """Structured performance/telemetry collectors for the drone proxy."""

    PERF_FIELDS = [
        "ts_unix_ns",
        "t_offset_ms",
        "instructions",
        "cycles",
        "cache-misses",
        "branch-misses",
        "task-clock",
        "context-switches",
        "branches",
    ]

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.perf: Optional[subprocess.Popen] = None
        self.pidstat: Optional[subprocess.Popen] = None
        self.perf_thread: Optional[threading.Thread] = None
        self.perf_stop = threading.Event()
        self.perf_csv_handle: Optional[object] = None
        self.perf_writer: Optional[csv.DictWriter] = None
        self.perf_start_ns = 0

        self.psutil_thread: Optional[threading.Thread] = None
        self.psutil_stop = threading.Event()
        self.psutil_csv_handle: Optional[object] = None
        self.psutil_writer: Optional[csv.DictWriter] = None
        self.psutil_proc: Optional[psutil.Process] = None

        self.temp_thread: Optional[threading.Thread] = None
        self.temp_stop = threading.Event()
        self.temp_csv_handle: Optional[object] = None
        self.temp_writer: Optional[csv.DictWriter] = None

    def start(self, pid: int, outdir: Path, suite: str) -> None:
        if not self.enabled:
            return
        outdir.mkdir(parents=True, exist_ok=True)

        # Structured perf samples
        perf_cmd = [
            "perf",
            "stat",
            "-I",
            "1000",
            "-x",
            ",",
            "-e",
            PERF_EVENTS,
            "-p",
            str(pid),
            "--log-fd",
            "1",
        ]
        perf_path = outdir / f"perf_samples_{suite}.csv"
        self.perf_csv_handle = open(perf_path, "w", newline="", encoding="utf-8")
        self.perf_writer = csv.DictWriter(self.perf_csv_handle, fieldnames=self.PERF_FIELDS)
        self.perf_writer.writeheader()
        self.perf_start_ns = time.time_ns()

        self.perf = popen(
            perf_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.perf_stop.clear()
        self.perf_thread = threading.Thread(
            target=self._consume_perf,
            args=(self.perf.stdout,),
            daemon=True,
        )
        self.perf_thread.start()

        # pidstat baseline dump for parity with legacy tooling
        self.pidstat = popen(
            ["pidstat", "-hlur", "-p", str(pid), "1"],
            stdout=open(outdir / f"pidstat_{suite}.txt", "w"),
            stderr=subprocess.STDOUT,
        )

        # psutil metrics (CPU%, RSS, threads)
        self.psutil_proc = psutil.Process(pid)
        self.psutil_proc.cpu_percent(interval=None)
        psutil_path = outdir / f"psutil_proc_{suite}.csv"
        self.psutil_csv_handle = open(psutil_path, "w", newline="", encoding="utf-8")
        self.psutil_writer = csv.DictWriter(
            self.psutil_csv_handle,
            fieldnames=["ts_unix_ns", "cpu_percent", "rss_bytes", "num_threads"],
        )
        self.psutil_writer.writeheader()
        self.psutil_stop.clear()
        self.psutil_thread = threading.Thread(target=self._psutil_loop, daemon=True)
        self.psutil_thread.start()

        # Temperature / frequency / throttled flags
        temp_path = outdir / f"sys_telemetry_{suite}.csv"
        self.temp_csv_handle = open(temp_path, "w", newline="", encoding="utf-8")
        self.temp_writer = csv.DictWriter(
            self.temp_csv_handle,
            fieldnames=["ts_unix_ns", "temp_c", "freq_hz", "throttled_hex"],
        )
        self.temp_writer.writeheader()
        self.temp_stop.clear()
        self.temp_thread = threading.Thread(target=self._telemetry_loop, daemon=True)
        self.temp_thread.start()

    def _consume_perf(self, stream) -> None:
        if not self.perf_writer:
            return
        current_ms = None
        row = None
        try:
            for line in iter(stream.readline, ""):
                if self.perf_stop.is_set():
                    break
                parts = [part.strip() for part in line.strip().split(",")]
                if len(parts) < 4:
                    continue
                try:
                    offset_ms = float(parts[0])
                except ValueError:
                    continue
                event = parts[3]
                if event.startswith("#"):
                    continue
                try:
                    value = int(parts[1].replace(",", ""))
                except Exception:
                    value = ""

                if current_ms is None or abs(offset_ms - current_ms) >= 0.5:
                    if row:
                        self.perf_writer.writerow(row)
                        self.perf_csv_handle.flush()
                    current_ms = offset_ms
                    row = {field: "" for field in self.PERF_FIELDS}
                    row["t_offset_ms"] = f"{offset_ms:.0f}"
                    row["ts_unix_ns"] = str(self.perf_start_ns + int(offset_ms * 1_000_000))

                key_map = {
                    "instructions": "instructions",
                    "cycles": "cycles",
                    "cache-misses": "cache-misses",
                    "branch-misses": "branch-misses",
                    "task-clock": "task-clock",
                    "context-switches": "context-switches",
                    "branches": "branches",
                }
                column = key_map.get(event)
                if row is not None and column:
                    row[column] = value

            if row:
                self.perf_writer.writerow(row)
                self.perf_csv_handle.flush()
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _psutil_loop(self) -> None:
        while not self.psutil_stop.is_set():
            try:
                assert self.psutil_writer is not None
                ts_now = time.time_ns()
                cpu_percent = self.psutil_proc.cpu_percent(interval=None)  # type: ignore[arg-type]
                rss_bytes = self.psutil_proc.memory_info().rss  # type: ignore[union-attr]
                num_threads = self.psutil_proc.num_threads()  # type: ignore[union-attr]
                self.psutil_writer.writerow({
                    "ts_unix_ns": ts_now,
                    "cpu_percent": cpu_percent,
                    "rss_bytes": rss_bytes,
                    "num_threads": num_threads,
                })
                self.psutil_csv_handle.flush()
            except Exception:
                pass
            time.sleep(1.0)
            try:
                self.psutil_proc.cpu_percent(interval=None)  # type: ignore[arg-type]
            except Exception:
                pass

    def _telemetry_loop(self) -> None:
        while not self.temp_stop.is_set():
            payload = {
                "ts_unix_ns": time.time_ns(),
                "temp_c": None,
                "freq_hz": None,
                "throttled_hex": "",
            }
            try:
                out = subprocess.check_output(["vcgencmd", "measure_temp"]).decode(errors="ignore")
                payload["temp_c"] = float(out.split("=")[1].split("'")[0])
            except Exception:
                pass
            try:
                freq_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
                if freq_path.exists():
                    payload["freq_hz"] = int(freq_path.read_text().strip()) * 1000
                else:
                    out = subprocess.check_output(["vcgencmd", "measure_clock", "arm"]).decode(errors="ignore")
                    payload["freq_hz"] = int(out.split("=")[1].strip())
            except Exception:
                pass
            try:
                out = subprocess.check_output(["vcgencmd", "get_throttled"]).decode(errors="ignore")
                payload["throttled_hex"] = out.strip().split("=")[1]
            except Exception:
                pass
            try:
                assert self.temp_writer is not None
                self.temp_writer.writerow(payload)
                self.temp_csv_handle.flush()
            except Exception:
                pass
            time.sleep(1.0)

    def rotate(self, pid: int, outdir: Path, suite: str) -> None:
        if not self.enabled:
            write_marker(suite)
            return
        self.stop()
        self.start(pid, outdir, suite)
        write_marker(suite)

    def stop(self) -> None:
        if not self.enabled:
            return

        self.perf_stop.set()
        if self.perf_thread:
            self.perf_thread.join(timeout=1.0)
        if self.perf:
            killtree(self.perf)
            self.perf = None
        if self.perf_csv_handle:
            try:
                self.perf_csv_handle.close()
            except Exception:
                pass
            self.perf_csv_handle = None

        killtree(self.pidstat)
        self.pidstat = None

        self.psutil_stop.set()
        if self.psutil_thread:
            self.psutil_thread.join(timeout=1.0)
            self.psutil_thread = None
        if self.psutil_csv_handle:
            try:
                self.psutil_csv_handle.close()
            except Exception:
                pass
            self.psutil_csv_handle = None

        self.temp_stop.set()
        if self.temp_thread:
            self.temp_thread.join(timeout=1.0)
            self.temp_thread = None
        if self.temp_csv_handle:
            try:
                self.temp_csv_handle.close()
            except Exception:
                pass
            self.temp_csv_handle = None


class ControlServer(threading.Thread):
    """Line-delimited JSON control server for the scheduler."""

    def __init__(self, host: str, port: int, state: dict):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.state = state
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(5)

    def run(self) -> None:
        print(f"[follower] control listening on {self.host}:{self.port}", flush=True)
        while not self.state["stop_event"].is_set():
            try:
                self.sock.settimeout(0.5)
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()
        self.sock.close()

    def handle(self, conn: socket.socket) -> None:
        try:
            line = conn.makefile().readline()
            request = json.loads(line.strip()) if line else {}
        except Exception:
            request = {}

        try:
            cmd = request.get("cmd")
            if cmd == "ping":
                self._send(conn, {"ok": True, "ts": ts()})
                return
            if cmd == "timesync":
                t1 = int(request.get("t1_ns", 0))
                t2 = time.time_ns()
                t3 = time.time_ns()
                self._send(conn, {"ok": True, "t1_ns": t1, "t2_ns": t2, "t3_ns": t3})
                return
            if cmd == "status":
                proxy = self.state["proxy"]
                running = bool(proxy and proxy.poll() is None)
                self._send(
                    conn,
                    {
                        "ok": True,
                        "suite": self.state["suite"],
                        "proxy_pid": proxy.pid if proxy else None,
                        "running": running,
                        "control_host": self.host,
                        "control_port": self.port,
                        "udp_recv_port": APP_RECV_PORT,
                        "udp_send_port": APP_SEND_PORT,
                        "monitors_enabled": self.state["monitors"].enabled,
                    },
                )
                return
            if cmd == "mark":
                suite = request.get("suite")
                if not suite:
                    self._send(conn, {"ok": False, "error": "missing suite"})
                    return
                proxy = self.state["proxy"]
                if not proxy or proxy.poll() is not None:
                    self._send(conn, {"ok": False, "error": "proxy not running"})
                    return
                old_suite = self.state["suite"]
                self.state["suite"] = suite
                outdir = self.state["suite_outdir"](suite)
                self.state["monitors"].rotate(proxy.pid, outdir, suite)
                monitor = self.state.get("high_speed_monitor")
                if monitor and old_suite != suite:
                    monitor.start_rekey(old_suite, suite)
                self._send(conn, {"ok": True, "marked": suite})
                return
            if cmd == "rekey_complete":
                monitor = self.state.get("high_speed_monitor")
                if monitor:
                    monitor.end_rekey()
                self._send(conn, {"ok": True})
                return
            if cmd == "schedule_mark":
                suite = request.get("suite")
                t0_ns = int(request.get("t0_ns", 0))
                if not suite or not t0_ns:
                    self._send(conn, {"ok": False, "error": "missing suite or t0_ns"})
                    return

                def _do_mark() -> None:
                    delay = max(0.0, (t0_ns - time.time_ns()) / 1e9)
                    if delay:
                        time.sleep(delay)
                    old = self.state.get("suite", "unknown")
                    proxy = self.state["proxy"]
                    if proxy and proxy.poll() is None:
                        outdir = self.state["suite_outdir"](suite)
                        self.state["monitors"].rotate(proxy.pid, outdir, suite)
                    else:
                        write_marker(suite)
                    self.state["suite"] = suite
                    monitor = self.state.get("high_speed_monitor")
                    if monitor and old != suite:
                        monitor.start_rekey(old, suite)

                threading.Thread(target=_do_mark, daemon=True).start()
                self._send(conn, {"ok": True, "scheduled": suite, "t0_ns": t0_ns})
                return
            if cmd == "stop":
                self.state["monitors"].stop()
                self.state["stop_event"].set()
                self._send(conn, {"ok": True, "stopping": True})
                return
            self._send(conn, {"ok": False, "error": "unknown_cmd"})
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _send(conn: socket.socket, obj: dict) -> None:
        conn.sendall((json.dumps(obj) + "\n").encode())


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    MARK_DIR.mkdir(parents=True, exist_ok=True)

    default_suite = discover_initial_suite()

    parser = argparse.ArgumentParser(description="Drone follower driven by core configuration")
    parser.add_argument(
        "--initial-suite",
        default=default_suite,
        help="Initial suite to launch (default: discover from config/secrets)",
    )
    parser.add_argument(
        "--disable-monitors",
        action="store_true",
        help="Disable perf/pidstat monitors",
    )
    parser.add_argument(
        "--session-id",
        help="Session identifier for monitoring output",
    )
    parser.add_argument(
        "--no-cpu-optimization",
        action="store_true",
        help="Skip CPU governor adjustments",
    )
    args = parser.parse_args()

    initial_suite = args.initial_suite
    session_id = args.session_id or f"session_{int(time.time())}"
    stop_event = threading.Event()

    monitor_base = DEFAULT_MONITOR_BASE.expanduser().resolve()
    session_dir = monitor_base / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    print(f"[follower] monitor output -> {session_dir}")

    if not args.no_cpu_optimization:
        optimize_cpu_performance()

    high_speed_monitor = HighSpeedMonitor(session_dir, session_id)
    high_speed_monitor.start()

    proxy = start_drone_proxy(initial_suite)
    monitors = Monitors(enabled=not args.disable_monitors)
    time.sleep(1)
    if proxy.poll() is None:
        monitors.start(proxy.pid, suite_outdir(initial_suite), initial_suite)
        high_speed_monitor.attach_proxy(proxy.pid)
        high_speed_monitor.current_suite = initial_suite

    echo = UdpEcho(
        APP_BIND_HOST,
        APP_RECV_PORT,
        APP_SEND_HOST,
        APP_SEND_PORT,
        stop_event,
        high_speed_monitor,
        session_dir,
    )
    echo.start()

    state = {
        "proxy": proxy,
        "suite": initial_suite,
        "suite_outdir": suite_outdir,
        "monitors": monitors,
        "stop_event": stop_event,
        "high_speed_monitor": high_speed_monitor,
    }
    control = ControlServer(CONTROL_HOST, CONTROL_PORT, state)
    control.start()

    try:
        while not stop_event.is_set():
            if proxy.poll() is not None:
                print(f"[follower] proxy exited with {proxy.returncode}", flush=True)
                stop_event.set()
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        monitors.stop()
        high_speed_monitor.stop()
        killtree(proxy)
        try:
            proxy.send_signal(signal.SIGTERM)
        except Exception:
            pass


if __name__ == "__main__":
    main()
