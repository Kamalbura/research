#!/usr/bin/env python3
"""Drone follower/loopback agent driven entirely by core configuration.

This script launches the drone proxy, exposes the TCP control channel for the
GCS scheduler, and runs the plaintext UDP echo used to validate the encrypted
path. All network endpoints originate from :mod:`core.config`. Test behaviour
can be tuned via optional CLI flags (e.g. to disable perf monitors), but no
network parameters are duplicated here.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_core_importable() -> Path:
    """Guarantee the repository root is on sys.path before importing core."""

    root = Path(__file__).resolve().parents[2]
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    try:
        __import__("core")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"Unable to import 'core'; repo root {root} missing from sys.path."
        ) from exc
    return root


ROOT = _ensure_core_importable()

import argparse
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
import queue
from datetime import datetime, timezone
from copy import deepcopy
from typing import IO, Optional, Tuple


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
from core.power_monitor import (
    PowerMonitor,
    PowerMonitorUnavailable,
    PowerSummary,
    create_power_monitor,
)


CONTROL_HOST = CONFIG.get("DRONE_CONTROL_HOST", "0.0.0.0")
CONTROL_PORT = int(CONFIG.get("DRONE_CONTROL_PORT", 48080))

APP_BIND_HOST = CONFIG.get("DRONE_PLAINTEXT_HOST", "127.0.0.1")
APP_RECV_PORT = int(CONFIG.get("DRONE_PLAINTEXT_RX", 47004))
APP_SEND_HOST = CONFIG.get("DRONE_PLAINTEXT_HOST", "127.0.0.1")
APP_SEND_PORT = int(CONFIG.get("DRONE_PLAINTEXT_TX", 47003))

DRONE_HOST = CONFIG["DRONE_HOST"]
GCS_HOST = CONFIG["GCS_HOST"]

TELEMETRY_DEFAULT_HOST = (
    CONFIG.get("DRONE_TELEMETRY_HOST")
    or CONFIG.get("GCS_HOST")
    or "127.0.0.1"
)
TELEMETRY_DEFAULT_PORT = int(
    CONFIG.get("DRONE_TELEMETRY_PORT")
    or CONFIG.get("GCS_TELEMETRY_PORT")
    or 52080
)

OUTDIR = ROOT / "logs/auto/drone"
MARK_DIR = OUTDIR / "marks"
SECRETS_DIR = ROOT / "secrets/matrix"

PI4_TARGET_KHZ = 1_800_000
PI5_TARGET_KHZ = 2_400_000

DEFAULT_MONITOR_BASE = Path(
    CONFIG.get("DRONE_MONITOR_OUTPUT_BASE")
    or os.getenv("DRONE_MONITOR_OUTPUT_BASE", "/home/dev/research/output/drone")
)
LOG_INTERVAL_MS = 100

PERF_EVENTS = "task-clock,cycles,instructions,cache-misses,branch-misses,context-switches,branches"

_VCGENCMD_WARNING_EMITTED = False


def _warn_vcgencmd_unavailable() -> None:
    global _VCGENCMD_WARNING_EMITTED
    if not _VCGENCMD_WARNING_EMITTED:
        print("[monitor] vcgencmd not available; thermal metrics disabled")
        _VCGENCMD_WARNING_EMITTED = True


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


AUTO_DRONE_DEFAULTS = {
    "session_prefix": "session",
    "monitors_enabled": True,
    "cpu_optimize": True,
    "telemetry_enabled": True,
    "telemetry_host": None,
    "telemetry_port": TELEMETRY_DEFAULT_PORT,
    "monitor_output_base": None,
    "power_env": {},
    "initial_suite": None,
}

AUTO_DRONE_CONFIG = _merge_defaults(AUTO_DRONE_DEFAULTS, CONFIG.get("AUTO_DRONE"))


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drone follower controller")
    parser.add_argument(
        "--5",
        "--pi5",
        dest="pi5",
        action="store_true",
        help="Treat hardware as Raspberry Pi 5 (defaults to Pi 4 governor settings)",
    )
    parser.add_argument(
        "--pi4",
        dest="pi5",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(pi5=False)
    return parser.parse_args(argv)


def ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log_runtime_environment(component: str) -> None:
    """Emit interpreter context to help debug sudo/venv mismatches."""

    preview = ";".join(sys.path[:5])
    print(f"[{ts()}] {component} python_exe={sys.executable}")
    print(f"[{ts()}] {component} cwd={Path.cwd()}")
    print(f"[{ts()}] {component} sys.path_prefix={preview}")


class TelemetryPublisher:
    """Best-effort telemetry pipe from the drone follower to the GCS scheduler."""

    def __init__(self, host: str, port: int, session_id: str) -> None:
        self.host = host
        self.port = port
        self.session_id = session_id
        self.queue: "queue.Queue[dict]" = queue.Queue(maxsize=5000)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.sock: Optional[socket.socket] = None
        self.writer = None
        self._connect_attempts = 0
        self._max_connect_attempts = 60
        self._initial_backoff = 1.0
        self._connect_deadline_s = 60.0
        self._connect_start_monotonic = time.monotonic()

    def start(self) -> None:
        self.thread.start()

    def publish(self, kind: str, payload: dict) -> None:
        if self.stop_event.is_set():
            return
        message = {"session_id": self.session_id, "kind": kind, **payload}
        try:
            self.queue.put_nowait(message)
        except queue.Full:
            # Drop oldest by removing one item to make space, then enqueue.
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(message)
            except queue.Full:
                pass

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self._close_socket()

    def _close_socket(self) -> None:
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass
            self.writer = None
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _ensure_connection(self) -> bool:
        if self.sock is not None and self.writer is not None:
            return True
        return self._attempt_connection()

    def _attempt_connection(self) -> bool:
        self._connect_attempts += 1
        attempt = self._connect_attempts
        try:
            sock = socket.create_connection((self.host, self.port), timeout=3.0)
        except OSError as exc:
            elapsed = time.monotonic() - self._connect_start_monotonic
            print(
                f"[follower] telemetry connect attempt {attempt}/{self._max_connect_attempts} to {self.host}:{self.port} failed after {elapsed:.1f}s: {exc}",
                flush=True,
            )
            self._close_socket()
            if elapsed >= self._connect_deadline_s and attempt >= self._max_connect_attempts:
                print(
                    f"[follower] telemetry collector still unavailable at {self.host}:{self.port}; will keep retrying with backoff",
                    flush=True,
                )
                # keep trying but slow down
            return False
        else:
            self.sock = sock
            self.writer = sock.makefile("w", encoding="utf-8", buffering=1)
            hello = {
                "session_id": self.session_id,
                "kind": "telemetry_hello",
                "timestamp_ns": time.time_ns(),
            }
            self.writer.write(json.dumps(hello) + "\n")
            self.writer.flush()
            print(
                f"[follower] telemetry connected to {self.host}:{self.port} on attempt {attempt}",
                flush=True,
            )
            self._connect_attempts = 0
            self._connect_start_monotonic = time.monotonic()
            return True

    def _run(self) -> None:
        backoff = self._initial_backoff
        while not self.stop_event.is_set():
            if not self._ensure_connection():
                time.sleep(min(backoff, 5.0))
                backoff = min(backoff * 1.5, 5.0)
                continue
            backoff = 1.0
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if self.writer is None:
                    continue
                if "timestamp_ns" not in item:
                    item["timestamp_ns"] = time.time_ns()
                self.writer.write(json.dumps(item) + "\n")
                self.writer.flush()
            except Exception as exc:
                print(f"[follower] telemetry send failed: {exc}")
                self._close_socket()


def _summary_to_dict(summary: PowerSummary, *, suite: str, session_id: str) -> dict:
    return {
        "timestamp_ns": summary.end_ns,
        "suite": suite,
        "label": summary.label,
        "session_id": session_id,
        "duration_s": summary.duration_s,
        "samples": summary.samples,
        "avg_current_a": summary.avg_current_a,
        "avg_voltage_v": summary.avg_voltage_v,
        "avg_power_w": summary.avg_power_w,
        "energy_j": summary.energy_j,
        "sample_rate_hz": summary.sample_rate_hz,
        "csv_path": summary.csv_path,
        "start_ns": summary.start_ns,
        "end_ns": summary.end_ns,
    }


class PowerCaptureManager:
    """Coordinates power captures for control commands."""

    def __init__(
        self,
        output_dir: Path,
        session_id: str,
        telemetry: Optional[TelemetryPublisher],
    ) -> None:
        self.telemetry = telemetry
        self.session_id = session_id
        self.lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_summary: Optional[dict] = None
        self._last_error: Optional[str] = None
        self._pending_suite: Optional[str] = None
        self.monitor: Optional[PowerMonitor] = None

        def _parse_int_env(name: str, default: int) -> int:
            raw = os.getenv(name)
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                print(f"[follower] invalid {name}={raw!r}, using {default}")
                return default

        def _parse_float_env(name: str, default: float) -> float:
            raw = os.getenv(name)
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                print(f"[follower] invalid {name}={raw!r}, using {default}")
                return default

        def _parse_float_optional(name: str) -> Optional[float]:
            raw = os.getenv(name)
            if raw is None or raw == "":
                return None
            try:
                return float(raw)
            except ValueError:
                print(f"[follower] invalid {name}={raw!r}, ignoring")
                return None

        backend = os.getenv("DRONE_POWER_BACKEND", "auto")
        sample_hz = _parse_int_env("DRONE_POWER_SAMPLE_HZ", 1000)
        shunt_ohm = _parse_float_env("DRONE_POWER_SHUNT_OHM", 0.1)
        sign_mode = os.getenv("DRONE_POWER_SIGN_MODE", "auto")
        hwmon_path = os.getenv("DRONE_POWER_HWMON_PATH")
        hwmon_name_hint = os.getenv("DRONE_POWER_HWMON_NAME")
        voltage_file = os.getenv("DRONE_POWER_VOLTAGE_FILE")
        current_file = os.getenv("DRONE_POWER_CURRENT_FILE")
        power_file = os.getenv("DRONE_POWER_POWER_FILE")
        voltage_scale = _parse_float_optional("DRONE_POWER_VOLTAGE_SCALE")
        current_scale = _parse_float_optional("DRONE_POWER_CURRENT_SCALE")
        power_scale = _parse_float_optional("DRONE_POWER_POWER_SCALE")

        try:
            self.monitor = create_power_monitor(
                output_dir,
                backend=backend,
                sample_hz=sample_hz,
                shunt_ohm=shunt_ohm,
                sign_mode=sign_mode,
                hwmon_path=hwmon_path,
                hwmon_name_hint=hwmon_name_hint,
                voltage_file=voltage_file,
                current_file=current_file,
                power_file=power_file,
                voltage_scale=voltage_scale,
                current_scale=current_scale,
                power_scale=power_scale,
            )
            self.available = True
        except PowerMonitorUnavailable as exc:
            self.monitor = None
            self.available = False
            self._last_error = str(exc)
            print(f"[follower] power monitor disabled: {exc}")
        except ValueError as exc:
            self.monitor = None
            self.available = False
            self._last_error = str(exc)
            print(f"[follower] power monitor configuration invalid: {exc}")

    def start_capture(self, suite: str, duration_s: float, start_ns: Optional[int]) -> tuple[bool, Optional[str]]:
        if not self.available or self.monitor is None:
            return False, self._last_error or "power_monitor_unavailable"
        if duration_s <= 0:
            return False, "invalid_duration"
        with self.lock:
            if self._thread and self._thread.is_alive():
                return False, "busy"
            self._last_error = None
            self._pending_suite = suite

            def worker() -> None:
                try:
                    summary = self.monitor.capture(label=suite, duration_s=duration_s, start_ns=start_ns)
                    summary_dict = _summary_to_dict(summary, suite=suite, session_id=self.session_id)
                    summary_json_path = Path(summary.csv_path).with_suffix(".json")
                    try:
                        summary_json_path.parent.mkdir(parents=True, exist_ok=True)
                        summary_json_path.write_text(json.dumps(summary_dict, indent=2), encoding="utf-8")
                        summary_dict["summary_json_path"] = str(summary_json_path)
                    except Exception as exc_json:
                        print(f"[follower] power summary write failed: {exc_json}")
                    with self.lock:
                        self._last_summary = summary_dict
                        self._pending_suite = None
                    if self.telemetry:
                        self.telemetry.publish("power_summary", dict(summary_dict))
                except Exception as exc:  # pragma: no cover - depends on hardware
                    with self.lock:
                        self._last_error = str(exc)
                        self._pending_suite = None
                    print(f"[follower] power capture failed: {exc}")
                    if self.telemetry:
                        self.telemetry.publish(
                            "power_summary_error",
                            {
                                "timestamp_ns": time.time_ns(),
                                "suite": suite,
                                "error": str(exc),
                            },
                        )
                finally:
                    with self.lock:
                        self._thread = None

            self._thread = threading.Thread(target=worker, daemon=True)
            self._thread.start()
        return True, None

    def status(self) -> dict:
        with self.lock:
            busy = bool(self._thread and self._thread.is_alive())
            summary = dict(self._last_summary) if self._last_summary else None
            error = self._last_error
            pending_suite = self._pending_suite
        return {
            "available": self.available,
            "busy": busy,
            "last_summary": summary,
            "error": error,
            "pending_suite": pending_suite,
        }



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


def start_drone_proxy(suite: str) -> tuple[subprocess.Popen, IO[str]]:
    suite_dir = suite_secrets_dir(suite)
    if not suite_dir.exists():
        raise FileNotFoundError(f"Suite directory missing: {suite_dir}")
    pub = suite_dir / "gcs_signing.pub"
    if not pub.exists() or not os.access(pub, os.R_OK):
        print(f"[follower] ERROR: missing {pub}", file=sys.stderr)
        sys.exit(2)

    os.environ["DRONE_HOST"] = DRONE_HOST
    os.environ["GCS_HOST"] = GCS_HOST
    os.environ["ENABLE_PACKET_TYPE"] = "1" if CONFIG.get("ENABLE_PACKET_TYPE", True) else "0"
    os.environ["STRICT_UDP_PEER_MATCH"] = "1" if CONFIG.get("STRICT_UDP_PEER_MATCH", True) else "0"

    suite_path = suite_outdir(suite)
    status = suite_path / "drone_status.json"
    summary = suite_path / "drone_summary.json"
    status.parent.mkdir(parents=True, exist_ok=True)
    summary.parent.mkdir(parents=True, exist_ok=True)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    log_path = OUTDIR / f"drone_{time.strftime('%Y%m%d-%H%M%S')}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle: IO[str] = open(log_path, "w", encoding="utf-8")

    env = os.environ.copy()
    root_str = str(ROOT)
    existing_py_path = env.get("PYTHONPATH")
    if existing_py_path:
        if root_str not in existing_py_path.split(os.pathsep):
            env["PYTHONPATH"] = root_str + os.pathsep + existing_py_path
    else:
        env["PYTHONPATH"] = root_str

    print(f"[follower] launching drone proxy on suite {suite}", flush=True)
    proc = popen([
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
    ], stdout=log_handle, stderr=subprocess.STDOUT, text=True, env=env, cwd=str(ROOT))
    return proc, log_handle


class HighSpeedMonitor(threading.Thread):
    def __init__(
        self,
        output_dir: Path,
        session_id: str,
        publisher: Optional[TelemetryPublisher],
    ):
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
        self.publisher = publisher
        self._vcgencmd_available = True

    def attach_proxy(self, pid: int) -> None:
        self.proxy_pid = pid

    def start_rekey(self, old_suite: str, new_suite: str) -> None:
        self.current_suite = new_suite
        self.rekey_start_ns = time.time_ns()
        print(f"[monitor] rekey transition {old_suite} -> {new_suite}")
        if self.publisher:
            self.publisher.publish(
                "rekey_transition_start",
                {
                    "timestamp_ns": self.rekey_start_ns,
                    "old_suite": old_suite,
                    "new_suite": new_suite,
                },
            )

    def end_rekey(self) -> None:
        if self.rekey_start_ns is None:
            return
        duration_ms = (time.time_ns() - self.rekey_start_ns) / 1_000_000
        print(f"[monitor] rekey completed in {duration_ms:.2f} ms")
        if self.publisher:
            self.publisher.publish(
                "rekey_transition_end",
                {
                    "timestamp_ns": time.time_ns(),
                    "suite": self.current_suite,
                    "duration_ms": duration_ms,
                },
            )
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
        timestamp_iso = datetime.fromtimestamp(
            timestamp_ns / 1e9,
            tz=timezone.utc,
        ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        cpu_percent = psutil.cpu_percent(interval=None)
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "r", encoding="utf-8") as handle:
                cpu_freq_mhz = int(handle.read().strip()) / 1000.0
        except Exception:
            cpu_freq_mhz = 0.0
        cpu_temp_c = 0.0
        try:
            if self._vcgencmd_available:
                result = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True, text=True)
                if result.returncode == 0 and "=" in result.stdout:
                    cpu_temp_c = float(result.stdout.split("=")[1].split("'")[0])
                else:
                    self._vcgencmd_available = False
                    _warn_vcgencmd_unavailable()
        except Exception:
            if self._vcgencmd_available:
                self._vcgencmd_available = False
                _warn_vcgencmd_unavailable()
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
        if self.publisher:
            sample = {
                "timestamp_ns": timestamp_ns,
                "timestamp_iso": timestamp_iso,
                "suite": self.current_suite,
                "proxy_pid": self.proxy_pid,
                "cpu_percent": cpu_percent,
                "cpu_freq_mhz": cpu_freq_mhz,
                "cpu_temp_c": cpu_temp_c,
                "mem_used_mb": mem.used / (1024 * 1024),
                "mem_percent": mem.percent,
            }
            if self.rekey_start_ns is not None:
                sample["rekey_elapsed_ms"] = (timestamp_ns - self.rekey_start_ns) / 1_000_000
            self.publisher.publish("system_sample", sample)

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
        publisher: Optional[TelemetryPublisher],
    ):
        super().__init__(daemon=True)
        self.bind_host = bind_host
        self.recv_port = recv_port
        self.send_host = send_host
        self.send_port = send_port
        self.stop_event = stop_event
        self.monitor = monitor
        self.session_dir = session_dir
        self.publisher = publisher
        self.rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.tx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        try:
            sndbuf = int(os.getenv("DRONE_SOCK_SNDBUF", str(16 << 20)))
            rcvbuf = int(os.getenv("DRONE_SOCK_RCVBUF", str(16 << 20)))
            self.rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
            self.tx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
            actual_snd = self.tx_sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
            actual_rcv = self.rx_sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            print(
                f"[{ts()}] follower UDP socket buffers: snd={actual_snd} rcv={actual_rcv}",
                flush=True,
            )
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
        # Last 8 bytes carry drone receive timestamp for upstream OWD inference.
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
            # Always flush to prevent data loss on crashes
            if self.packet_log_handle:
                self.packet_log_handle.flush()
            if self.publisher:
                suite = self.monitor.current_suite if self.monitor else "unknown"
                self.publisher.publish(
                    "udp_echo_sample",
                    {
                        "recv_timestamp_ns": recv_ns,
                        "send_timestamp_ns": send_ns,
                        "processing_ns": processing_ns,
                        "sequence": seq,
                        "suite": suite,
                    },
                )



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

    def __init__(self, enabled: bool, telemetry: Optional[TelemetryPublisher]):
        self.enabled = enabled
        self.telemetry = telemetry
        self.perf: Optional[subprocess.Popen] = None
        self.pidstat: Optional[subprocess.Popen] = None
        self.perf_thread: Optional[threading.Thread] = None
        self.perf_stop = threading.Event()
        self.perf_csv_handle: Optional[object] = None
        self.perf_writer: Optional[csv.DictWriter] = None
        self.perf_start_ns = 0
        self.current_suite = "unknown"

        self.psutil_thread: Optional[threading.Thread] = None
        self.psutil_stop = threading.Event()
        self.psutil_csv_handle: Optional[object] = None
        self.psutil_writer: Optional[csv.DictWriter] = None
        self.psutil_proc: Optional[psutil.Process] = None

        self.temp_thread: Optional[threading.Thread] = None
        self.temp_stop = threading.Event()
        self.temp_csv_handle: Optional[object] = None
        self.temp_writer: Optional[csv.DictWriter] = None
        self.pidstat_out: Optional[IO[str]] = None
        self._vcgencmd_available = True

    def start(self, pid: int, outdir: Path, suite: str) -> None:
        if not self.enabled:
            return
        outdir.mkdir(parents=True, exist_ok=True)
        self.current_suite = suite
        self._vcgencmd_available = True

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
        self.pidstat_out = open(outdir / f"pidstat_{suite}.txt", "w", encoding="utf-8")
        self.pidstat = popen(
            ["pidstat", "-hlur", "-p", str(pid), "1"],
            stdout=self.pidstat_out,
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

        if self.telemetry:
            self.telemetry.publish(
                "monitors_started",
                {
                    "timestamp_ns": time.time_ns(),
                    "suite": suite,
                    "proxy_pid": pid,
                },
            )

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
                raw_value = parts[1].replace(",", "")
                if event == "task-clock":
                    try:
                        value = float(raw_value)
                    except Exception:
                        value = ""
                else:
                    try:
                        value = int(raw_value)
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
                if self.telemetry:
                    sample = {k: row.get(k, "") for k in self.PERF_FIELDS}
                    sample["suite"] = self.current_suite
                    self.telemetry.publish("perf_sample", sample)
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
                if self.telemetry:
                    self.telemetry.publish(
                        "psutil_sample",
                        {
                            "timestamp_ns": ts_now,
                            "suite": self.current_suite,
                            "cpu_percent": cpu_percent,
                            "rss_bytes": rss_bytes,
                            "num_threads": num_threads,
                        },
                    )
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
            if self._vcgencmd_available:
                try:
                    out = subprocess.check_output(["vcgencmd", "measure_temp"]).decode(errors="ignore")
                    payload["temp_c"] = float(out.split("=")[1].split("'")[0])
                except Exception:
                    self._vcgencmd_available = False
                    _warn_vcgencmd_unavailable()

            freq_path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
            if freq_path.exists():
                try:
                    payload["freq_hz"] = int(freq_path.read_text().strip()) * 1000
                except Exception:
                    pass
            elif self._vcgencmd_available:
                try:
                    out = subprocess.check_output(["vcgencmd", "measure_clock", "arm"]).decode(errors="ignore")
                    payload["freq_hz"] = int(out.split("=")[1].strip())
                except Exception:
                    self._vcgencmd_available = False
                    _warn_vcgencmd_unavailable()

            if self._vcgencmd_available:
                try:
                    out = subprocess.check_output(["vcgencmd", "get_throttled"]).decode(errors="ignore")
                    payload["throttled_hex"] = out.strip().split("=")[1]
                except Exception:
                    self._vcgencmd_available = False
                    _warn_vcgencmd_unavailable()
            try:
                assert self.temp_writer is not None
                self.temp_writer.writerow(payload)
                self.temp_csv_handle.flush()
                if self.telemetry:
                    payload = dict(payload)
                    payload["suite"] = self.current_suite
                    self.telemetry.publish("thermal_sample", payload)
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
        if self.pidstat_out:
            try:
                self.pidstat_out.close()
            except Exception:
                pass
            self.pidstat_out = None

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

        if self.telemetry:
            self.telemetry.publish(
                "monitors_stopped",
                {
                    "timestamp_ns": time.time_ns(),
                    "suite": self.current_suite,
                },
            )


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
                response = {"ok": True, "t1_ns": t1, "t2_ns": t2}
                t3 = time.time_ns()
                response["t3_ns"] = t3
                self._send(conn, response)
                return
            state_lock = self.state.get("lock")
            if state_lock is None:
                state_lock = threading.Lock()
                self.state["lock"] = state_lock
            if cmd == "status":
                with state_lock:
                    proxy = self.state["proxy"]
                    suite = self.state["suite"]
                    monitors_enabled = self.state["monitors"].enabled
                    running = bool(proxy and proxy.poll() is None)
                    proxy_pid = proxy.pid if proxy else None
                    telemetry: Optional[TelemetryPublisher] = self.state.get("telemetry")
                    pending_suite = self.state.get("pending_suite")
                    last_requested = self.state.get("last_requested_suite")
                self._send(
                    conn,
                    {
                        "ok": True,
                        "suite": suite,
                        "pending_suite": pending_suite,
                        "last_requested_suite": last_requested,
                        "proxy_pid": proxy_pid,
                        "running": running,
                        "control_host": self.host,
                        "control_port": self.port,
                        "udp_recv_port": APP_RECV_PORT,
                        "udp_send_port": APP_SEND_PORT,
                        "monitors_enabled": monitors_enabled,
                    },
                )
                if telemetry:
                    telemetry.publish(
                        "status_reply",
                        {
                            "timestamp_ns": time.time_ns(),
                            "suite": suite,
                            "running": running,
                            "pending_suite": pending_suite,
                            "last_requested_suite": last_requested,
                        },
                    )
                return
            if cmd == "session_info":
                with state_lock:
                    session_id = self.state.get("session_id")
                session_value = str(session_id) if session_id is not None else ""
                self._send(
                    conn,
                    {
                        "ok": True,
                        "session_id": session_value,
                    },
                )
                return
            if cmd == "mark":
                suite = request.get("suite")
                telemetry: Optional[TelemetryPublisher] = None
                monitor: Optional[HighSpeedMonitor] = None
                monitors = None
                monitor_prev_suite: Optional[str] = None
                proxy = None
                rotate_args: Optional[Tuple[int, Path, str]] = None
                with state_lock:
                    if not suite:
                        self._send(conn, {"ok": False, "error": "missing suite"})
                        return
                    proxy = self.state["proxy"]
                    proxy_running = bool(proxy and proxy.poll() is None)
                    if not proxy_running:
                        self._send(conn, {"ok": False, "error": "proxy not running"})
                        return
                    old_suite = self.state.get("suite")
                    self.state["prev_suite"] = old_suite
                    self.state["pending_suite"] = suite
                    self.state["suite"] = suite
                    self.state["last_requested_suite"] = suite
                    suite_outdir = self.state["suite_outdir"]
                    outdir = suite_outdir(suite)
                    monitors = self.state["monitors"]
                    monitor = self.state.get("high_speed_monitor")
                    telemetry = self.state.get("telemetry")
                    monitor_prev_suite = old_suite
                    if proxy:
                        rotate_args = (proxy.pid, outdir, suite)
                if monitors and rotate_args:
                    pid, outdir, new_suite = rotate_args
                    monitors.rotate(pid, outdir, new_suite)
                if monitor and monitor_prev_suite != suite:
                    monitor.start_rekey(monitor_prev_suite or "unknown", suite)
                self._send(conn, {"ok": True, "marked": suite})
                if telemetry:
                    telemetry.publish(
                        "mark",
                        {
                            "timestamp_ns": time.time_ns(),
                            "suite": suite,
                            "prev_suite": monitor_prev_suite,
                            "requested_suite": suite,
                        },
                    )
                return
            if cmd == "rekey_complete":
                status_value = str(request.get("status", "ok"))
                requested_suite = str(request.get("suite") or "")
                monitor: Optional[HighSpeedMonitor] = None
                telemetry: Optional[TelemetryPublisher] = None
                monitors = None
                proxy = None
                rotate_args: Optional[Tuple[int, Path, str]] = None
                monitor_update_suite: Optional[str] = None
                with state_lock:
                    monitor = self.state.get("high_speed_monitor")
                    telemetry = self.state.get("telemetry")
                    monitors = self.state["monitors"]
                    proxy = self.state.get("proxy")
                    suite_outdir = self.state["suite_outdir"]
                    if requested_suite:
                        self.state["last_requested_suite"] = requested_suite
                    if status_value.lower() != "ok":
                        previous = self.state.get("prev_suite")
                        pending = self.state.get("pending_suite")
                        if previous is not None and pending and previous != pending:
                            print(f"[follower] rekey to {pending} reported {status_value}; reverting to {previous}", flush=True)
                        if previous is not None and previous != self.state.get("suite"):
                            self.state["suite"] = previous
                            if proxy and proxy.poll() is None:
                                outdir = suite_outdir(previous)
                                rotate_args = (proxy.pid, outdir, previous)
                        if previous:
                            monitor_update_suite = previous
                    else:
                        pending_suite = self.state.get("pending_suite")
                        if requested_suite and pending_suite and requested_suite != pending_suite:
                            print(
                                f"[follower] pending suite {pending_suite} does not match requested {requested_suite}; updating to requested",
                                flush=True,
                            )
                            pending_suite = requested_suite
                        if pending_suite:
                            self.state["suite"] = pending_suite
                            monitor_update_suite = pending_suite
                    self.state.pop("pending_suite", None)
                    self.state.pop("prev_suite", None)
                    current_suite = self.state.get("suite")
                    if status_value.lower() == "ok" and requested_suite and current_suite != requested_suite:
                        print(
                            f"[follower] active suite {current_suite} disagrees with requested {requested_suite}; forcing to requested",
                            flush=True,
                        )
                        self.state["suite"] = requested_suite
                        current_suite = requested_suite
                if rotate_args and monitors and proxy and proxy.poll() is None:
                    pid, outdir, suite_name = rotate_args
                    monitors.rotate(pid, outdir, suite_name)
                if monitor_update_suite and monitor:
                    monitor.current_suite = monitor_update_suite
                    monitor.end_rekey()
                elif monitor:
                    monitor.end_rekey()
                self._send(conn, {"ok": True})
                if telemetry:
                    telemetry.publish(
                        "rekey_complete",
                        {
                            "timestamp_ns": time.time_ns(),
                            "suite": current_suite,
                            "requested_suite": requested_suite or current_suite,
                            "status": status_value,
                        },
                    )
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
                    proxy = None
                    monitors = None
                    monitor: Optional[HighSpeedMonitor] = None
                    suite_outdir_fn = None
                    prev_suite: Optional[str] = None
                    with state_lock:
                        prev_suite = self.state.get("suite")
                        self.state["prev_suite"] = prev_suite
                        self.state["pending_suite"] = suite
                        proxy = self.state.get("proxy")
                        monitors = self.state.get("monitors")
                        suite_outdir_fn = self.state.get("suite_outdir")
                        monitor = self.state.get("high_speed_monitor")
                        self.state["last_requested_suite"] = suite
                        self.state["suite"] = suite
                    proxy_running = bool(proxy and proxy.poll() is None)
                    if proxy_running and monitors and suite_outdir_fn and proxy:
                        outdir = suite_outdir_fn(suite)
                        monitors.rotate(proxy.pid, outdir, suite)
                    else:
                        write_marker(suite)
                    if monitor and prev_suite != suite:
                        monitor.start_rekey(prev_suite or "unknown", suite)

                threading.Thread(target=_do_mark, daemon=True).start()
                self._send(conn, {"ok": True, "scheduled": suite, "t0_ns": t0_ns})
                telemetry = self.state.get("telemetry")
                if telemetry:
                    telemetry.publish(
                        "schedule_mark",
                        {
                            "timestamp_ns": time.time_ns(),
                            "suite": suite,
                            "t0_ns": t0_ns,
                            "requested_suite": suite,
                        },
                    )
                return
            if cmd == "power_capture":
                manager = self.state.get("power_manager")
                if not isinstance(manager, PowerCaptureManager):
                    self._send(conn, {"ok": False, "error": "power_monitor_unavailable"})
                    return
                duration_s = request.get("duration_s")
                suite = request.get("suite") or self.state.get("suite") or "unknown"
                try:
                    duration_val = float(duration_s)
                except (TypeError, ValueError):
                    self._send(conn, {"ok": False, "error": "invalid_duration"})
                    return
                start_ns = request.get("start_ns")
                try:
                    start_ns_val = int(start_ns) if start_ns is not None else None
                except (TypeError, ValueError):
                    start_ns_val = None
                ok, error = manager.start_capture(suite, duration_val, start_ns_val)
                if ok:
                    self._send(
                        conn,
                        {
                            "ok": True,
                            "scheduled": True,
                            "suite": suite,
                            "duration_s": duration_val,
                            "start_ns": start_ns_val,
                        },
                    )
                    telemetry = self.state.get("telemetry")
                    if telemetry:
                        telemetry.publish(
                            "power_capture_request",
                            {
                                "timestamp_ns": time.time_ns(),
                                "suite": suite,
                                "duration_s": duration_val,
                                "start_ns": start_ns_val,
                            },
                        )
                else:
                    self._send(conn, {"ok": False, "error": error or "power_capture_failed"})
                return
            if cmd == "power_status":
                manager = self.state.get("power_manager")
                if not isinstance(manager, PowerCaptureManager):
                    self._send(conn, {"ok": False, "error": "power_monitor_unavailable"})
                    return
                status = manager.status()
                self._send(conn, {"ok": True, **status})
                return
            if cmd == "stop":
                self.state["monitors"].stop()
                self.state["stop_event"].set()
                self._send(conn, {"ok": True, "stopping": True})
                telemetry = self.state.get("telemetry")
                if telemetry:
                    telemetry.publish(
                        "stop",
                        {"timestamp_ns": time.time_ns()},
                    )
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


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    device_generation = "pi5" if args.pi5 else "pi4"
    os.environ.setdefault("DRONE_DEVICE_GENERATION", device_generation)

    log_runtime_environment("follower")
    if hasattr(os, "geteuid"):
        try:
            if os.geteuid() == 0:
                print(
                    f"[{ts()}] follower running as root; ensure venv packages are available",
                    flush=True,
                )
        except Exception:
            pass

    OUTDIR.mkdir(parents=True, exist_ok=True)
    MARK_DIR.mkdir(parents=True, exist_ok=True)

    default_suite = discover_initial_suite()
    auto = AUTO_DRONE_CONFIG

    session_prefix = str(auto.get("session_prefix") or "session")
    session_id = os.environ.get("DRONE_SESSION_ID") or f"{session_prefix}_{int(time.time())}"
    stop_event = threading.Event()

    monitor_base_cfg = auto.get("monitor_output_base")
    if monitor_base_cfg:
        monitor_base = Path(monitor_base_cfg).expanduser()
    else:
        monitor_base = DEFAULT_MONITOR_BASE.expanduser()
    monitor_base = monitor_base.resolve()
    session_dir = monitor_base / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    print(f"[follower] session_id={session_id}")
    print(f"[follower] monitor output -> {session_dir}")
    print(f"[follower] device generation={device_generation}")

    for env_key, env_value in auto.get("power_env", {}).items():
        if env_value is None:
            continue
        os.environ.setdefault(env_key, str(env_value))

    telemetry: Optional[TelemetryPublisher] = None
    telemetry_enabled = bool(auto.get("telemetry_enabled", True))
    telemetry_host_cfg = auto.get("telemetry_host")
    if telemetry_host_cfg:
        telemetry_host = telemetry_host_cfg
    else:
        telemetry_host = TELEMETRY_DEFAULT_HOST
    telemetry_port_cfg = auto.get("telemetry_port")
    telemetry_port = TELEMETRY_DEFAULT_PORT if telemetry_port_cfg in (None, "") else int(telemetry_port_cfg)

    expected_bind = str(CONFIG.get("GCS_TELEMETRY_BIND") or "").strip()
    if expected_bind and expected_bind not in {"0.0.0.0", "::", ""} and telemetry_host != expected_bind:
        raise RuntimeError(
            f"Telemetry target {telemetry_host}:{telemetry_port} differs from GCS bind {expected_bind}; update AUTO_DRONE.telemetry_host"
        )
    print(f"[follower] telemetry target {telemetry_host}:{telemetry_port}")

    if telemetry_enabled:
        telemetry = TelemetryPublisher(telemetry_host, telemetry_port, session_id)
        telemetry.start()
        print(f"[follower] telemetry publisher started (session={session_id})")
    else:
        print("[follower] telemetry disabled via AUTO_DRONE configuration")

    if bool(auto.get("cpu_optimize", True)):
        target_khz = PI5_TARGET_KHZ if args.pi5 else PI4_TARGET_KHZ
        optimize_cpu_performance(target_khz=target_khz)
        print(
            f"[follower] cpu governor target ~{target_khz / 1000:.0f} MHz ({device_generation})",
            flush=True,
        )

    power_dir = session_dir / "power"
    power_manager = PowerCaptureManager(power_dir, session_id, telemetry)

    high_speed_monitor = HighSpeedMonitor(session_dir, session_id, telemetry)
    high_speed_monitor.start()

    initial_suite = auto.get("initial_suite") or default_suite
    proxy, proxy_log = start_drone_proxy(initial_suite)
    monitors_enabled = bool(auto.get("monitors_enabled", True))
    if not monitors_enabled:
        print("[follower] monitors disabled via AUTO_DRONE configuration")
    monitors = Monitors(enabled=monitors_enabled, telemetry=telemetry)
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
        telemetry,
    )
    echo.start()

    state = {
        "proxy": proxy,
        "suite": initial_suite,
        "suite_outdir": suite_outdir,
        "monitors": monitors,
        "stop_event": stop_event,
        "high_speed_monitor": high_speed_monitor,
        "telemetry": telemetry,
        "prev_suite": None,
        "pending_suite": None,
        "last_requested_suite": initial_suite,
        "power_manager": power_manager,
        "device_generation": device_generation,
        "lock": threading.Lock(),
        "session_id": session_id,
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
        if proxy_log:
            try:
                proxy_log.close()
            except Exception:
                pass
        if telemetry:
            telemetry.stop()


if __name__ == "__main__":
    # Test plan:
    # 1. Start the follower before the scheduler and confirm telemetry connects after retries.
    # 2. Run the Windows scheduler to drive a full suite cycle without rekey failures.
    # 3. Remove the logs/auto/drone/<suite> directory and confirm it is recreated automatically.
    # 4. Stop the telemetry collector mid-run and verify the follower reconnects without crashing.
    main()
