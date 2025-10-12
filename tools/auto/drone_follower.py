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
import math
import os
import platform
import shlex
import signal
import socket
import struct
import subprocess
import threading
import time
import queue
from collections import deque
from datetime import datetime, timezone
from copy import deepcopy
from typing import IO, Callable, Dict, Iterable, Optional, Tuple

from dataclasses import dataclass


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

from bench_models import calculate_predicted_flight_constraint


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

GRAVITY = 9.80665  # m/s^2, standard gravity for synthetic flight modeling

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
    "mock_mass_kg": 6.5,
    "kinematics_horizontal_mps": 13.0,
    "kinematics_vertical_mps": 3.5,
    "kinematics_cycle_s": 18.0,
    "kinematics_yaw_rate_dps": 45.0,
}

AUTO_DRONE_CONFIG = _merge_defaults(AUTO_DRONE_DEFAULTS, CONFIG.get("AUTO_DRONE"))


def _collect_capabilities_snapshot() -> dict:
    """Probe local crypto/telemetry capabilities for scheduler negotiation."""

    timestamp_ns = time.time_ns()

    try:
        enabled_kems = {name for name in suites_mod.enabled_kems()}
        kem_probe_error = ""
    except Exception as exc:  # pragma: no cover - depends on oqs installation
        enabled_kems = set()
        kem_probe_error = str(exc)

    try:
        enabled_sigs = {name for name in suites_mod.enabled_sigs()}
        sig_probe_error = ""
    except Exception as exc:  # pragma: no cover - depends on oqs installation
        enabled_sigs = set()
        sig_probe_error = str(exc)

    available_aeads = set(suites_mod.available_aead_tokens())
    missing_aead_reasons = suites_mod.unavailable_aead_reasons()

    suite_map = suites_mod.list_suites()
    supported_suites: list[str] = []
    unsupported_suites: list[dict[str, object]] = []

    all_kems = set()
    all_sigs = set()

    for suite_id, info in sorted(suite_map.items()):
        kem_name = info.get("kem_name")
        sig_name = info.get("sig_name")
        aead_token = info.get("aead_token")

        if kem_name:
            all_kems.add(kem_name)
        if sig_name:
            all_sigs.add(sig_name)

        reasons: list[str] = []
        details: dict[str, object] = {
            "kem_name": kem_name,
            "sig_name": sig_name,
            "aead_token": aead_token,
        }

        if enabled_kems and kem_name not in enabled_kems:
            reasons.append("kem_unavailable")
        if enabled_sigs and sig_name not in enabled_sigs:
            reasons.append("sig_unavailable")
        if available_aeads and aead_token not in available_aeads:
            reasons.append("aead_unavailable")
            hint = missing_aead_reasons.get(str(aead_token))
            if hint:
                details["aead_hint"] = hint

        if reasons:
            unsupported_suites.append(
                {
                    "suite": suite_id,
                    "reasons": reasons,
                    "details": details,
                }
            )
            continue

        supported_suites.append(suite_id)

    missing_kems = sorted(kem for kem in (all_kems - enabled_kems)) if enabled_kems else sorted(all_kems)
    missing_sigs = sorted(sig for sig in (all_sigs - enabled_sigs)) if enabled_sigs else sorted(all_sigs)

    oqs_info: dict[str, object] = {}
    try:  # pragma: no cover - depends on oqs availability
        import oqs  # type: ignore

        oqs_info["python_version"] = getattr(oqs, "__version__", "unknown")
        get_version = getattr(oqs, "get_version", None)
        if callable(get_version):
            oqs_info["library_version"] = get_version()
        get_build_config = getattr(oqs, "get_build_config", None)
        if callable(get_build_config):
            try:
                build_cfg = get_build_config()
                oqs_info["build_config"] = build_cfg if isinstance(build_cfg, dict) else repr(build_cfg)
            except Exception as exc:  # pragma: no cover - defensive path
                oqs_info["build_config_error"] = str(exc)
    except Exception as exc:  # pragma: no cover - oqs missing
        oqs_info["error"] = str(exc)

    return {
        "timestamp_ns": timestamp_ns,
        "supported_suites": supported_suites,
        "unsupported_suites": unsupported_suites,
        "enabled_kems": sorted(enabled_kems),
        "enabled_sigs": sorted(enabled_sigs),
        "available_aeads": sorted(available_aeads),
        "missing_aead_reasons": missing_aead_reasons,
        "missing_kems": missing_kems,
        "missing_sigs": missing_sigs,
        "kem_probe_error": kem_probe_error,
        "sig_probe_error": sig_probe_error,
        "suite_registry_size": len(suite_map),
        "oqs": oqs_info,
    }


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


def _collect_hardware_context() -> dict:
    """Gather hardware, OS, and toolchain context for reproducibility logs."""

    info: dict[str, object] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
        "python_compiler": platform.python_compiler(),
        "python_build": platform.python_build(),
        "executable": sys.executable,
    }

    try:
        uname = os.uname()  # type: ignore[attr-defined]
    except AttributeError:
        uname = None
    if uname is not None:
        info["uname"] = {
            "sysname": uname.sysname,
            "nodename": uname.nodename,
            "release": uname.release,
            "version": uname.version,
            "machine": uname.machine,
        }

    # Capture relevant environment hints for compiler optimisation flags.
    flag_env_vars = {
        key: os.environ.get(key)
        for key in (
            "CFLAGS",
            "CXXFLAGS",
            "LDFLAGS",
            "OQS_OPT_FLAGS",
            "OQS_CFLAGS",
            "OQS_LDFLAGS",
            "OQS_OPT_LEVEL",
            "OQS_OPTIMIZATION",
        )
        if os.environ.get(key)
    }
    if flag_env_vars:
        info["build_flags"] = flag_env_vars

    try:
        import oqs  # type: ignore

        info["oqs_python_version"] = getattr(oqs, "__version__", "unknown")
        get_version = getattr(oqs, "get_version", None)
        if callable(get_version):
            info["oqs_library_version"] = get_version()
        get_build_config = getattr(oqs, "get_build_config", None)
        if callable(get_build_config):
            build_config = get_build_config()
            try:
                json.dumps(build_config)
                info["oqs_build_config"] = build_config
            except TypeError:
                info["oqs_build_config"] = repr(build_config)

            optimization_hint: Optional[str] = None
            if isinstance(build_config, dict):
                for candidate_key in (
                    "OQS_OPT_FLAG",
                    "OQS_OPT_FLAGS",
                    "OPT_FLAGS",
                    "OPTIMIZATION_FLAGS",
                    "CFLAGS",
                    "CMAKE_C_FLAGS",
                    "CMAKE_CXX_FLAGS",
                ):
                    value = build_config.get(candidate_key)
                    if isinstance(value, str) and value.strip():
                        optimization_hint = value.strip()
                        break
                if optimization_hint is None:
                    cmake_cache = build_config.get("CMAKE_ARGS")
                    if isinstance(cmake_cache, str) and cmake_cache:
                        for token in cmake_cache.split():
                            if token.startswith("-O"):
                                optimization_hint = token
                                break
            if optimization_hint is None and flag_env_vars:
                for key in ("OQS_OPT_FLAGS", "CFLAGS", "OQS_CFLAGS"):
                    candidate = flag_env_vars.get(key)
                    if candidate:
                        optimization_hint = candidate
                        break
            if optimization_hint:
                info["oqs_optimization_hint"] = optimization_hint
    except Exception as exc:  # pragma: no cover - diagnostic only
        info["oqs_info_error"] = str(exc)

    return info


def _record_hardware_context(session_dir: Path, telemetry: Optional[TelemetryPublisher]) -> None:
    """Persist hardware context to disk and telemetry for audit trails."""

    context = _collect_hardware_context()
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        target = session_dir / "hardware_context.json"
        target.write_text(json.dumps(context, indent=2), encoding="utf-8")
        print(f"[follower] hardware context -> {target}")
    except Exception as exc:
        print(f"[follower] failed to write hardware context: {exc}")

    if telemetry is not None:
        try:
            telemetry.publish("hardware_context", {"timestamp_ns": time.time_ns(), **context})
        except Exception:
            pass


@dataclass
class _TelemetryClient:
    conn: socket.socket
    writer: IO[str]
    peer: str


class TelemetryPublisher:
    """Server-side telemetry broadcaster that mirrors the control channel semantics."""

    def __init__(self, host: str, port: int, session_id: str) -> None:
        self.host = host
        self.port = port
        self.session_id = session_id
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.clients: Dict[socket.socket, _TelemetryClient] = {}
        self.server: Optional[socket.socket] = None
        self.accept_thread: Optional[threading.Thread] = None
        self._status_path: Optional[Path] = None
        self._last_status_flush = 0.0
        self._connected_once = False

    def start(self) -> None:
        self._start_server()

    def publish(self, kind: str, payload: dict) -> None:
        if self.stop_event.is_set():
            return
        message = {
            "session_id": self.session_id,
            "kind": kind,
            **payload,
        }
        message["component"] = "drone_follower"
        message.setdefault("timestamp_ns", time.time_ns())
        text = json.dumps(message) + "\n"
        with self.lock:
            clients = list(self.clients.values())
        for client in clients:
            try:
                client.writer.write(text)
                client.writer.flush()
            except Exception:
                self._remove_client(client, reason="send_error")

    def stop(self) -> None:
        self.stop_event.set()
        if self.accept_thread and self.accept_thread.is_alive():
            self.accept_thread.join(timeout=2.0)
        with self.lock:
            clients = list(self.clients.values())
            self.clients.clear()
        for client in clients:
            try:
                client.writer.close()
            except Exception:
                pass
            try:
                client.conn.close()
            except Exception:
                pass
        if self.server is not None:
            try:
                self.server.close()
            except Exception:
                pass
            self.server = None
        self._emit_status("stopped", active_clients=0)

    def configure_status_sink(self, path: Path) -> None:
        self._status_path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._emit_status("init", active_clients=len(self.clients))

    def _emit_status(self, event: str, **extra: object) -> None:
        if self._status_path is None:
            return
        payload = {
            "event": event,
            "timestamp_ns": time.time_ns(),
            "session_id": self.session_id,
            "host": self.host,
            "port": self.port,
            "connected_once": self._connected_once,
            "active_clients": len(self.clients),
        }
        if extra:
            payload.update(extra)
        try:
            self._status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self._last_status_flush = time.monotonic()
        except Exception:
            pass

    def _start_server(self) -> None:
        try:
            addrinfo = socket.getaddrinfo(
                self.host,
                self.port,
                0,
                socket.SOCK_STREAM,
                proto=0,
                flags=socket.AI_PASSIVE if not self.host else 0,
            )
        except socket.gaierror as exc:
            raise OSError(f"telemetry bind failed for {self.host}:{self.port}: {exc}") from exc

        last_exc: Optional[Exception] = None
        for family, socktype, proto, _canon, sockaddr in addrinfo:
            try:
                srv = socket.socket(family, socktype, proto)
                try:
                    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if family == socket.AF_INET6:
                        try:
                            srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                        except OSError:
                            pass
                    srv.bind(sockaddr)
                    srv.listen(5)
                    srv.settimeout(0.5)
                except Exception:
                    srv.close()
                    raise
            except Exception as exc:
                last_exc = exc
                continue
            self.server = srv
            break

        if self.server is None:
            message = last_exc or RuntimeError("no suitable address family")
            raise OSError(f"telemetry bind failed for {self.host}:{self.port}: {message}")

        print(f"[follower] telemetry listening on {self.host}:{self.port}", flush=True)
        self._emit_status("listening", active_clients=0)
        self.accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.accept_thread.start()

    def _accept_loop(self) -> None:
        assert self.server is not None
        while not self.stop_event.is_set():
            try:
                conn, addr = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stop_event.is_set():
                    break
                continue
            peer = f"{addr[0]}:{addr[1]}"
            client = self._register_client(conn, peer)
            if client is None:
                continue
            threading.Thread(target=self._monitor_client, args=(client,), daemon=True).start()

    def _register_client(self, conn: socket.socket, peer: str) -> Optional[_TelemetryClient]:
        try:
            writer = conn.makefile("w", encoding="utf-8", buffering=1)
        except Exception:
            conn.close()
            return None
        hello = {
            "session_id": self.session_id,
            "kind": "telemetry_hello",
            "timestamp_ns": time.time_ns(),
        }
        try:
            writer.write(json.dumps(hello) + "\n")
            writer.flush()
        except Exception:
            try:
                writer.close()
            except Exception:
                pass
            conn.close()
            return None
        client = _TelemetryClient(conn=conn, writer=writer, peer=peer)
        with self.lock:
            self.clients[conn] = client
        self._connected_once = True
        print(f"[follower] telemetry client {peer} connected", flush=True)
        self._emit_status("connected", peer=peer, active_clients=len(self.clients))
        return client

    def _monitor_client(self, client: _TelemetryClient) -> None:
        conn = client.conn
        try:
            while not self.stop_event.is_set():
                data = conn.recv(1024)
                if not data:
                    break
        except Exception:
            pass
        finally:
            self._remove_client(client, reason="disconnect")

    def _remove_client(self, client: _TelemetryClient, *, reason: str) -> None:
        with self.lock:
            existing = self.clients.pop(client.conn, None)
        if existing is None:
            return
        try:
            existing.writer.close()
        except Exception:
            pass
        try:
            existing.conn.close()
        except Exception:
            pass
        print(f"[follower] telemetry client {existing.peer} closed ({reason})", flush=True)
        self._emit_status("disconnected", peer=existing.peer, reason=reason, active_clients=len(self.clients))


class SyntheticKinematicsModel:
    """Deterministic mock flight profile used for telemetry and PFC estimation."""

    def __init__(
        self,
        *,
        weight_n: float,
        horizontal_peak_mps: float,
        vertical_peak_mps: float,
        yaw_rate_dps: float,
        cycle_s: float,
    ) -> None:
        self.weight_n = max(0.0, weight_n)
        self.horizontal_peak_mps = max(0.0, horizontal_peak_mps)
        self.vertical_peak_mps = float(vertical_peak_mps)
        self.yaw_rate_dps = float(yaw_rate_dps)
        self.cycle_s = max(4.0, float(cycle_s))
        self._start_monotonic = time.monotonic()
        self._last_monotonic = self._start_monotonic
        self._altitude_m = 30.0
        self._heading_rad = 0.0
        self._prev_horizontal_mps = 0.0
        self._prev_vertical_mps = 0.0
        self._sequence = 0

    def _phase(self, now: float) -> float:
        elapsed = now - self._start_monotonic
        return (elapsed % self.cycle_s) / self.cycle_s

    def step(self, timestamp_ns: int) -> dict:
        now = time.monotonic()
        dt = max(0.0, now - self._last_monotonic)
        self._last_monotonic = now
        phase = self._phase(now)
        phase_rad = 2.0 * math.pi * phase

        horiz_mps = self.horizontal_peak_mps * math.sin(phase_rad)
        vert_mps = self.vertical_peak_mps * math.sin(phase_rad + math.pi / 3.0)
        speed_mps = math.hypot(horiz_mps, vert_mps)

        yaw_rate_rps = math.radians(self.yaw_rate_dps) * math.cos(phase_rad + math.pi / 6.0)
        self._heading_rad = (self._heading_rad + yaw_rate_rps * dt) % (2.0 * math.pi)
        self._altitude_m = max(0.0, self._altitude_m + vert_mps * dt)

        horiz_accel = 0.0 if dt == 0.0 else (horiz_mps - self._prev_horizontal_mps) / dt
        vert_accel = 0.0 if dt == 0.0 else (vert_mps - self._prev_vertical_mps) / dt
        self._prev_horizontal_mps = horiz_mps
        self._prev_vertical_mps = vert_mps

        pfc_w = calculate_predicted_flight_constraint(abs(horiz_mps), vert_mps, self.weight_n)
        tilt_deg = math.degrees(math.atan2(abs(vert_mps), max(0.1, abs(horiz_mps))))

        self._sequence += 1
        return {
            "timestamp_ns": timestamp_ns,
            "sequence": self._sequence,
            "velocity_horizontal_mps": horiz_mps,
            "velocity_vertical_mps": vert_mps,
            "speed_mps": speed_mps,
            "horizontal_accel_mps2": horiz_accel,
            "vertical_accel_mps2": vert_accel,
            "yaw_rate_dps": math.degrees(yaw_rate_rps),
            "heading_deg": math.degrees(self._heading_rad),
            "altitude_m": self._altitude_m,
            "tilt_deg": tilt_deg,
            "predicted_flight_constraint_w": pfc_w,
        }


def _summary_to_dict(
    summary: PowerSummary,
    *,
    suite: str,
    session_id: str,
    session_dir: Optional[Path] = None,
    monitor_manifest: Optional[Path] = None,
    telemetry_status: Optional[Path] = None,
) -> dict:
    data = {
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
    if session_dir is not None:
        data["session_dir"] = str(session_dir)
    if monitor_manifest is not None:
        data["monitor_manifest_path"] = str(monitor_manifest)
    if telemetry_status is not None:
        data["telemetry_status_path"] = str(telemetry_status)
    return data


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
        self.monitor_backend: Optional[str] = None
        self.session_dir = output_dir.parent
        self._monitor_manifest: Optional[Path] = None
        self._telemetry_status: Optional[Path] = None
        self._artifact_sink: Optional[Callable[[Iterable[Path]], None]] = None

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
            self.monitor_backend = getattr(self.monitor, "backend_name", self.monitor.__class__.__name__)
            print(f"[follower] power monitor backend: {self.monitor_backend}")
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
                    summary_dict = _summary_to_dict(
                        summary,
                        suite=suite,
                        session_id=self.session_id,
                        session_dir=self.session_dir,
                        monitor_manifest=self._monitor_manifest,
                        telemetry_status=self._telemetry_status,
                    )
                    summary_json_path = Path(summary.csv_path).with_suffix(".json")
                    try:
                        summary_json_path.parent.mkdir(parents=True, exist_ok=True)
                        summary_json_path.write_text(json.dumps(summary_dict, indent=2), encoding="utf-8")
                        summary_dict["summary_json_path"] = str(summary_json_path)
                        self._notify_artifacts([Path(summary.csv_path), summary_json_path])
                    except Exception as exc_json:
                        print(f"[follower] power summary write failed: {exc_json}")
                        self._notify_artifacts([Path(summary.csv_path)])
                    print(
                        f"[follower] power summary suite={suite} avg={summary.avg_power_w:.3f} W "
                        f"energy={summary.energy_j:.3f} J duration={summary.duration_s:.3f}s"
                    )
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
            "session_dir": str(self.session_dir) if self.session_dir else "",
            "monitor_manifest_path": str(self._monitor_manifest) if self._monitor_manifest else "",
            "telemetry_status_path": str(self._telemetry_status) if self._telemetry_status else "",
        }

    def register_monitor_manifest(self, manifest_path: Path) -> None:
        self._monitor_manifest = manifest_path

    def register_telemetry_status(self, status_path: Path) -> None:
        self._telemetry_status = status_path

    def register_artifact_sink(self, sink: Callable[[Iterable[Path]], None]) -> None:
        self._artifact_sink = sink

    def _notify_artifacts(self, paths: Iterable[Path]) -> None:
        if not paths:
            return
        sink = self._artifact_sink
        if sink is None:
            return
        try:
            sink(list(paths))
        except Exception:
            pass



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


def _tail_file_lines(path: Path, limit: int = 120) -> list[str]:
    limit = max(1, min(int(limit), 500))
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = list(deque(handle, maxlen=limit))
    except FileNotFoundError:
        return []
    except OSError:
        return []
    return [line.rstrip("\n") for line in lines]


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
        self.pending_suite: Optional[str] = None
        self.proxy_pid: Optional[int] = None
        self.rekey_start_ns: Optional[int] = None
        self.csv_handle: Optional[object] = None
        self.csv_writer: Optional[csv.writer] = None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = self.output_dir / f"system_monitoring_{session_id}.csv"
        self.publisher = publisher
        self._vcgencmd_available = True
        self.rekey_marks_path = self.output_dir / f"rekey_marks_{session_id}.csv"
        self._rekey_marks_lock = threading.Lock()
        self._summary_lock = threading.Lock()
        self._max_pfc_w = 0.0
        self._last_pfc_w = 0.0
        self._last_kin_sample_ns = 0
        auto_cfg = AUTO_DRONE_CONFIG
        mass_kg = auto_cfg.get("mock_mass_kg", 6.5)
        horiz_mps = auto_cfg.get("kinematics_horizontal_mps", 13.0)
        vert_mps = auto_cfg.get("kinematics_vertical_mps", 3.5)
        yaw_rate_dps = auto_cfg.get("kinematics_yaw_rate_dps", 45.0)
        cycle_s = auto_cfg.get("kinematics_cycle_s", 18.0)
        try:
            weight_n = max(0.0, float(mass_kg) * GRAVITY)
        except (TypeError, ValueError):
            weight_n = 0.0
        try:
            horiz_peak = float(horiz_mps)
        except (TypeError, ValueError):
            horiz_peak = 0.0
        try:
            vert_peak = float(vert_mps)
        except (TypeError, ValueError):
            vert_peak = 0.0
        try:
            yaw_peak = float(yaw_rate_dps)
        except (TypeError, ValueError):
            yaw_peak = 0.0
        try:
            cycle = float(cycle_s)
        except (TypeError, ValueError):
            cycle = 18.0
        self._kinematics_model = SyntheticKinematicsModel(
            weight_n=weight_n,
            horizontal_peak_mps=max(0.0, horiz_peak),
            vertical_peak_mps=vert_peak,
            yaw_rate_dps=yaw_peak,
            cycle_s=cycle,
        ) if weight_n > 0.0 else None

    def attach_proxy(self, pid: int) -> None:
        self.proxy_pid = pid

    def start_rekey(self, old_suite: str, new_suite: str) -> None:
        self.pending_suite = new_suite
        self.rekey_start_ns = time.time_ns()
        print(f"[monitor] rekey transition {old_suite} -> {new_suite}")
        if self.publisher:
            self.publisher.publish(
                "rekey_transition_start",
                {
                    "timestamp_ns": self.rekey_start_ns,
                    "old_suite": old_suite,
                    "new_suite": new_suite,
                    "pending_suite": new_suite,
                },
            )
        self._append_rekey_mark([
            "start",
            str(self.rekey_start_ns),
            old_suite or "",
            new_suite or "",
            self.pending_suite or "",
        ])

    def end_rekey(self, *, success: bool, new_suite: Optional[str]) -> None:
        if self.rekey_start_ns is None:
            self.pending_suite = None
            return
        duration_ms = (time.time_ns() - self.rekey_start_ns) / 1_000_000
        target_suite = new_suite or self.pending_suite or self.current_suite
        if success and new_suite:
            self.current_suite = new_suite
        status_text = "completed" if success else "failed"
        print(f"[monitor] rekey {status_text} in {duration_ms:.2f} ms (target={target_suite})")
        if self.publisher:
            payload = {
                "timestamp_ns": time.time_ns(),
                "suite": self.current_suite,
                "duration_ms": duration_ms,
                "success": success,
            }
            if target_suite:
                payload["requested_suite"] = target_suite
            if self.pending_suite:
                payload["pending_suite"] = self.pending_suite
            self.publisher.publish("rekey_transition_end", payload)
        end_timestamp = time.time_ns()
        self._append_rekey_mark([
            "end",
            str(end_timestamp),
            "ok" if success else "fail",
            target_suite or "",
            f"{duration_ms:.3f}",
        ])
        self.rekey_start_ns = None
        self.pending_suite = None

    def _append_rekey_mark(self, row: list[str]) -> None:
        try:
            self.rekey_marks_path.parent.mkdir(parents=True, exist_ok=True)
            with self._rekey_marks_lock:
                new_file = not self.rekey_marks_path.exists()
                with self.rekey_marks_path.open("a", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    if new_file:
                        writer.writerow(["kind", "timestamp_ns", "field1", "field2", "field3"])
                    writer.writerow(row)
        except Exception as exc:
            print(f"[monitor] rekey mark append failed: {exc}")

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
        kin_payload: Optional[dict] = None
        if self._kinematics_model is not None:
            kin = self._kinematics_model.step(timestamp_ns)
            kin_payload = dict(kin)
            kin_payload.setdefault("suite", self.current_suite)
            kin_payload.setdefault("weight_n", self._kinematics_model.weight_n)
            kin_payload.setdefault("mass_kg", self._kinematics_model.weight_n / GRAVITY if GRAVITY else 0.0)
            pfc_value = kin_payload.get("predicted_flight_constraint_w")
            if isinstance(pfc_value, (int, float)):
                with self._summary_lock:
                    self._last_pfc_w = float(pfc_value)
                    self._last_kin_sample_ns = timestamp_ns
                    if pfc_value > self._max_pfc_w:
                        self._max_pfc_w = float(pfc_value)

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
            if kin_payload is not None:
                self.publisher.publish("kinematics", kin_payload)

    def kinematics_summary(self) -> dict:
        with self._summary_lock:
            return {
                "last_sample_ns": self._last_kin_sample_ns,
                "last_predicted_flight_constraint_w": self._last_pfc_w,
                "peak_predicted_flight_constraint_w": self._max_pfc_w,
            }

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
        def _bind_socket(host: str, port: int) -> socket.socket:
            flags = socket.AI_PASSIVE if not host else 0
            try:
                addrinfo = socket.getaddrinfo(host, port, 0, socket.SOCK_DGRAM, 0, flags)
            except socket.gaierror as exc:
                raise OSError(f"UDP echo bind failed for {host}:{port}: {exc}") from exc

            last_exc: Optional[Exception] = None
            for family, socktype, proto, _canon, sockaddr in addrinfo:
                sock: Optional[socket.socket] = None
                try:
                    sock = socket.socket(family, socktype, proto)
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    except OSError:
                        pass
                    if family == socket.AF_INET6:
                        try:
                            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                        except OSError:
                            pass
                    sock.bind(sockaddr)
                    return sock
                except Exception as exc:
                    last_exc = exc
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:
                            pass
                    continue

            message = last_exc or RuntimeError("no suitable address family")
            raise OSError(f"UDP echo bind failed for {host}:{port}: {message}")

        def _connect_tuple(host: str, port: int, preferred_family: int) -> tuple[socket.socket, tuple]:
            addrinfo: list[tuple] = []
            try:
                addrinfo = socket.getaddrinfo(host, port, preferred_family, socket.SOCK_DGRAM)
            except socket.gaierror:
                pass
            if not addrinfo:
                try:
                    addrinfo = socket.getaddrinfo(host, port, 0, socket.SOCK_DGRAM)
                except socket.gaierror as exc:
                    raise OSError(f"UDP echo resolve failed for {host}:{port}: {exc}") from exc

            last_exc: Optional[Exception] = None
            for family, socktype, proto, _canon, sockaddr in addrinfo:
                sock: Optional[socket.socket] = None
                try:
                    sock = socket.socket(family, socktype, proto)
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    except OSError:
                        pass
                    return sock, sockaddr
                except Exception as exc:
                    last_exc = exc
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:
                            pass
                    continue

            message = last_exc or RuntimeError("no suitable address family")
            raise OSError(f"UDP echo socket creation failed for {host}:{port}: {message}")

        self.rx_sock = _bind_socket(self.bind_host, self.recv_port)
        self.tx_sock, self.send_addr = _connect_tuple(self.send_host, self.send_port, self.rx_sock.family)
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
        self.packet_log_path = self.session_dir / "packet_timing.csv"
        self.packet_log_handle: Optional[object] = None
        self.packet_writer: Optional[csv.writer] = None
        self.samples = 0
        self.log_every_packet = False

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
            "payload_len",
        ])
        self.rx_sock.settimeout(0.001)
        while not self.stop_event.is_set():
            try:
                data, _ = self.rx_sock.recvfrom(65535)
                recv_ns = time.time_ns()
                enhanced = self._annotate_packet(data, recv_ns)
                send_ns = time.time_ns()
                self.tx_sock.sendto(enhanced, self.send_addr)
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
        monitor_active = bool(self.monitor and self.monitor.rekey_start_ns is not None)
        if monitor_active and not self.log_every_packet:
            self.log_every_packet = True
        elif not monitor_active and self.log_every_packet:
            self.log_every_packet = False

        should_log = self.log_every_packet or (seq % 100 == 0)
        if should_log:
            self.packet_writer.writerow([
                recv_ns,
                send_ns,
                processing_ns,
                f"{processing_ns / 1_000_000:.6f}",
                seq,
                len(data),
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

    def __init__(self, enabled: bool, telemetry: Optional[TelemetryPublisher], session_dir: Path):
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
        self._stats_lock = threading.Lock()
        self._max_cpu_percent = 0.0
        self._max_rss_bytes = 0
        self._last_cpu_percent = 0.0
        self._last_rss_bytes = 0
        self._last_num_threads = 0
        self._last_sample_ns = 0

        self.temp_thread: Optional[threading.Thread] = None
        self.temp_stop = threading.Event()
        self.temp_csv_handle: Optional[object] = None
        self.temp_writer: Optional[csv.DictWriter] = None
        self.pidstat_out: Optional[IO[str]] = None
        self._vcgencmd_available = True

        self.session_dir = session_dir
        self.manifest_path = session_dir / "monitor_manifest.json"
        self._artifact_lock = threading.Lock()
        self._artifact_paths: set[str] = set()
        self._write_manifest()

    def start(self, pid: int, outdir: Path, suite: str, *, session_dir: Optional[Path] = None) -> None:
        if not self.enabled:
            return
        outdir.mkdir(parents=True, exist_ok=True)
        self.current_suite = suite
        self._vcgencmd_available = True
        if session_dir is not None:
            self.session_dir = session_dir
            self.manifest_path = self.session_dir / "monitor_manifest.json"
            self._write_manifest()

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
        self._record_artifacts(
            perf_path,
            self.pidstat_out.name if self.pidstat_out else None,
            psutil_path,
            temp_path,
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
                with self._stats_lock:
                    self._last_sample_ns = ts_now
                    self._last_cpu_percent = cpu_percent
                    self._last_rss_bytes = rss_bytes
                    self._last_num_threads = num_threads
                    if cpu_percent > self._max_cpu_percent:
                        self._max_cpu_percent = cpu_percent
                    if rss_bytes > self._max_rss_bytes:
                        self._max_rss_bytes = rss_bytes
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

    def resource_summary(self) -> dict:
        with self._stats_lock:
            rss_mb = self._last_rss_bytes / (1024 * 1024)
            peak_rss_mb = self._max_rss_bytes / (1024 * 1024)
            return {
                "last_sample_ns": self._last_sample_ns,
                "last_cpu_percent": self._last_cpu_percent,
                "last_rss_bytes": self._last_rss_bytes,
                "last_rss_mb": rss_mb,
                "last_num_threads": self._last_num_threads,
                "peak_cpu_percent": self._max_cpu_percent,
                "peak_rss_bytes": self._max_rss_bytes,
                "peak_rss_mb": peak_rss_mb,
            }

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
        self.start(pid, outdir, suite, session_dir=self.session_dir)
        self._record_artifacts(outdir / f"perf_samples_{suite}.csv", outdir / f"psutil_proc_{suite}.csv", outdir / f"sys_telemetry_{suite}.csv")
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
        self._write_manifest()

    def register_artifacts(self, *paths: Path) -> None:
        self._record_artifacts(*paths)

    def _write_manifest(self) -> None:
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "session_dir": str(self.session_dir),
                "artifacts": sorted(self._artifact_paths),
            }
            self.manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _record_artifacts(self, *paths: Path) -> None:
        updated = False
        with self._artifact_lock:
            for candidate in paths:
                if candidate is None:
                    continue
                try:
                    path_obj = Path(candidate)
                except TypeError:
                    continue
                path_str = str(path_obj)
                if not path_str:
                    continue
                if path_str not in self._artifact_paths:
                    self._artifact_paths.add(path_str)
                    updated = True
        if updated:
            self._write_manifest()


class ControlServer(threading.Thread):
    """Line-delimited JSON control server for the scheduler."""

    def __init__(self, host: str, port: int, state: dict):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.state = state
        try:
            addrinfo = socket.getaddrinfo(
                self.host,
                self.port,
                0,
                socket.SOCK_STREAM,
                proto=0,
                flags=socket.AI_PASSIVE if not self.host else 0,
            )
        except socket.gaierror as exc:
            raise OSError(f"control server bind failed for {self.host}:{self.port}: {exc}") from exc

        last_exc: Optional[Exception] = None
        bound_sock: Optional[socket.socket] = None
        for family, socktype, proto, _canon, sockaddr in addrinfo:
            try:
                candidate = socket.socket(family, socktype, proto)
                try:
                    candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if family == socket.AF_INET6:
                        try:
                            candidate.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                        except OSError:
                            pass
                    candidate.bind(sockaddr)
                    candidate.listen(5)
                except Exception:
                    candidate.close()
                    raise
            except Exception as exc:
                last_exc = exc
                continue
            bound_sock = candidate
            break

        if bound_sock is None:
            message = last_exc or RuntimeError("no suitable address family")
            raise OSError(f"control server bind failed for {self.host}:{self.port}: {message}")

        self.sock = bound_sock

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
            if cmd == "capabilities":
                with state_lock:
                    snapshot = dict(self.state.get("capabilities") or {})
                    telemetry: Optional[TelemetryPublisher] = self.state.get("telemetry")
                self._send(conn, {"ok": True, "capabilities": snapshot})
                if telemetry and snapshot:
                    try:
                        telemetry.publish(
                            "capabilities_response",
                            {
                                "timestamp_ns": time.time_ns(),
                                "capabilities": snapshot,
                            },
                        )
                    except Exception:
                        pass
                return
            if cmd == "status":
                with state_lock:
                    proxy = self.state["proxy"]
                    suite = self.state["suite"]
                    monitors_obj: Monitors = self.state["monitors"]
                    high_speed_monitor: HighSpeedMonitor = self.state.get("high_speed_monitor")
                    manager: Optional[PowerCaptureManager] = self.state.get("power_manager")
                    monitors_enabled = monitors_obj.enabled
                    running = bool(proxy and proxy.poll() is None)
                    proxy_pid = proxy.pid if proxy else None
                    telemetry: Optional[TelemetryPublisher] = self.state.get("telemetry")
                    pending_suite = self.state.get("pending_suite")
                    last_requested = self.state.get("last_requested_suite")
                    session_id = self.state.get("session_id")
                    session_dir = self.state.get("session_dir")
                    telemetry_status_path = self.state.get("telemetry_status_path")
                    monitor_manifest_path = getattr(monitors_obj, "manifest_path", None)
                    resource_summary = monitors_obj.resource_summary() if monitors_obj else {}
                    kinematics_summary = high_speed_monitor.kinematics_summary() if high_speed_monitor else {}
                    power_status = manager.status() if isinstance(manager, PowerCaptureManager) else {}
                    log_path = self.state.get("log_path")
                    status_payload = {
                        "suite": suite,
                        "pending_suite": pending_suite,
                        "last_requested_suite": last_requested,
                        "proxy_pid": proxy_pid,
                        "running": running,
                        "control_host": self.host,
                        "control_port": self.port,
                        "udp_recv_port": APP_RECV_PORT,
                        "udp_send_port": APP_SEND_PORT,
                        "session_id": session_id,
                        "session_dir": str(session_dir) if session_dir else "",
                        "monitors_enabled": monitors_enabled,
                        "monitor_manifest_path": str(monitor_manifest_path) if monitor_manifest_path else "",
                        "telemetry_status_path": str(telemetry_status_path) if telemetry_status_path else "",
                        "log_path": str(log_path) if log_path else "",
                    }
                    if resource_summary:
                        status_payload.update(
                            {
                                "resource_last_sample_ns": resource_summary.get("last_sample_ns", 0),
                                "resource_last_cpu_percent": resource_summary.get("last_cpu_percent", 0.0),
                                "resource_last_rss_mb": resource_summary.get("last_rss_mb", 0.0),
                                "resource_last_num_threads": resource_summary.get("last_num_threads", 0),
                                "resource_peak_cpu_percent": resource_summary.get("peak_cpu_percent", 0.0),
                                "resource_peak_rss_mb": resource_summary.get("peak_rss_mb", 0.0),
                            }
                        )
                    if kinematics_summary:
                        status_payload.update(
                            {
                                "pfc_last_sample_ns": kinematics_summary.get("last_sample_ns", 0),
                                "pfc_last_w": kinematics_summary.get("last_predicted_flight_constraint_w", 0.0),
                                "pfc_peak_w": kinematics_summary.get("peak_predicted_flight_constraint_w", 0.0),
                            }
                        )
                    if power_status:
                        status_payload.update(
                            {
                                "power_available": bool(power_status.get("available", False)),
                                "power_busy": bool(power_status.get("busy", False)),
                                "power_error": power_status.get("error") or "",
                                "power_pending_suite": power_status.get("pending_suite") or "",
                            }
                        )
                        summary = power_status.get("last_summary")
                        if isinstance(summary, dict):
                            def _coerce_float(value: object) -> float:
                                try:
                                    return float(value)
                                except (TypeError, ValueError):
                                    return 0.0

                            def _coerce_int(value: object) -> int:
                                try:
                                    return int(value)
                                except (TypeError, ValueError):
                                    return 0

                            status_payload.update(
                                {
                                    "power_last_suite": summary.get("suite", ""),
                                    "power_last_energy_j": _coerce_float(summary.get("energy_j")),
                                    "power_last_avg_w": _coerce_float(summary.get("avg_power_w")),
                                    "power_last_duration_s": _coerce_float(summary.get("duration_s")),
                                    "power_last_samples": _coerce_int(summary.get("samples")),
                                    "power_last_csv_path": summary.get("csv_path", ""),
                                    "power_last_summary_path": summary.get("summary_json_path", ""),
                                }
                            )
                self._send(conn, {"ok": True, **status_payload})
                if telemetry:
                    telemetry.publish(
                        "status_reply",
                        {
                            "timestamp_ns": time.time_ns(),
                            "suite": status_payload["suite"],
                            "running": status_payload["running"],
                            "pending_suite": status_payload["pending_suite"],
                            "last_requested_suite": status_payload["last_requested_suite"],
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
            if cmd == "log_tail":
                with state_lock:
                    log_path = self.state.get("log_path")
                override = request.get("path")
                if override:
                    try:
                        candidate = Path(str(override))
                        log_path = candidate
                    except Exception:
                        pass
                if not log_path:
                    self._send(conn, {"ok": False, "error": "log_path_unavailable"})
                    return
                lines_requested = request.get("lines")
                try:
                    line_count = int(lines_requested) if lines_requested is not None else 120
                except (TypeError, ValueError):
                    line_count = 120
                tail_lines = _tail_file_lines(Path(log_path), line_count)
                banner = f"[follower] LOG TAIL ({log_path}) last {len(tail_lines)} lines"
                print(banner, flush=True)
                for entry in tail_lines:
                    print(entry, flush=True)
                self._send(
                    conn,
                    {
                        "ok": True,
                        "path": str(log_path),
                        "lines": tail_lines,
                        "count": len(tail_lines),
                    },
                )
                return
            if cmd == "mark":
                suite = request.get("suite")
                kind = str(request.get("kind") or "rekey")
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
                    supported = list((self.state.get("capabilities") or {}).get("supported_suites", []))
                    if supported and suite not in supported:
                        self._send(conn, {"ok": False, "error": "suite unsupported"})
                        return
                    proxy = self.state["proxy"]
                    proxy_running = bool(proxy and proxy.poll() is None)
                    if not proxy_running:
                        self._send(conn, {"ok": False, "error": "proxy not running"})
                        return
                    old_suite = self.state.get("suite")
                    self.state["prev_suite"] = old_suite
                    self.state["pending_suite"] = suite
                    self.state["last_requested_suite"] = suite
                    suite_outdir = self.state["suite_outdir"]
                    outdir = suite_outdir(suite)
                    monitors = self.state["monitors"]
                    monitor = self.state.get("high_speed_monitor")
                    telemetry = self.state.get("telemetry")
                    monitor_prev_suite = old_suite
                    if proxy:
                        rotate_args = (proxy.pid, outdir, suite)
                if monitor and monitor_prev_suite != suite:
                    monitor.start_rekey(monitor_prev_suite or "unknown", suite)
                if monitors and rotate_args:
                    pid, outdir, new_suite = rotate_args
                    monitors.rotate(pid, outdir, new_suite)
                self._send(conn, {"ok": True, "marked": suite})
                if telemetry:
                    telemetry.publish(
                        "mark",
                        {
                            "timestamp_ns": time.time_ns(),
                            "suite": suite,
                            "prev_suite": monitor_prev_suite,
                            "requested_suite": suite,
                            "kind": kind,
                        },
                    )
                self._append_mark_entry([
                    "mark",
                    str(time.time_ns()),
                    kind,
                    suite or "",
                    monitor_prev_suite or "",
                ])
                return
            if cmd == "rekey_complete":
                status_value = str(request.get("status", "ok"))
                success = status_value.lower() == "ok"
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
                    previous_suite = self.state.get("prev_suite")
                    pending_suite = self.state.get("pending_suite")
                    if success:
                        if requested_suite and pending_suite and requested_suite != pending_suite:
                            print(
                                f"[follower] pending suite {pending_suite} does not match requested {requested_suite}; updating to requested",
                                flush=True,
                            )
                            pending_suite = requested_suite
                        if pending_suite:
                            self.state["suite"] = pending_suite
                            monitor_update_suite = pending_suite
                        elif requested_suite:
                            self.state["suite"] = requested_suite
                            monitor_update_suite = requested_suite
                    else:
                        if previous_suite is not None:
                            self.state["suite"] = previous_suite
                            monitor_update_suite = previous_suite
                            if proxy and proxy.poll() is None:
                                outdir = suite_outdir(previous_suite)
                                rotate_args = (proxy.pid, outdir, previous_suite)
                        elif pending_suite:
                            monitor_update_suite = pending_suite
                    self.state.pop("pending_suite", None)
                    self.state.pop("prev_suite", None)
                    current_suite = self.state.get("suite")
                    if success and requested_suite and current_suite != requested_suite:
                        print(
                            f"[follower] active suite {current_suite} disagrees with requested {requested_suite}; forcing to requested",
                            flush=True,
                        )
                        self.state["suite"] = requested_suite
                        current_suite = requested_suite
                        monitor_update_suite = requested_suite
                if rotate_args and monitors and proxy and proxy.poll() is None:
                    pid, outdir, suite_name = rotate_args
                    monitors.rotate(pid, outdir, suite_name)
                if monitor and monitor_update_suite:
                    monitor.current_suite = monitor_update_suite
                    monitor.end_rekey(success=success, new_suite=monitor_update_suite)
                elif monitor:
                    monitor.end_rekey(success=success, new_suite=current_suite)
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
                kind = str(request.get("kind") or "window")
                t0_ns = int(request.get("t0_ns", 0))
                if not suite or not t0_ns:
                    self._send(conn, {"ok": False, "error": "missing suite or t0_ns"})
                    return
                with state_lock:
                    supported = list((self.state.get("capabilities") or {}).get("supported_suites", []))
                if supported and suite not in supported:
                    self._send(conn, {"ok": False, "error": "suite unsupported"})
                    return

                def _do_mark() -> None:
                    delay = max(0.0, (t0_ns - time.time_ns()) / 1e9)
                    if delay:
                        time.sleep(delay)
                    proxy = None
                    monitors = None
                    monitor: Optional[HighSpeedMonitor] = None
                    suite_outdir_fn = None
                    with state_lock:
                        proxy = self.state.get("proxy")
                        monitors = self.state.get("monitors")
                        suite_outdir_fn = self.state.get("suite_outdir")
                        monitor = self.state.get("high_speed_monitor")
                    proxy_running = bool(proxy and proxy.poll() is None)
                    if monitor and suite and monitor.current_suite != suite:
                        monitor.current_suite = suite
                    if proxy_running and monitors and suite_outdir_fn and proxy:
                        outdir = suite_outdir_fn(suite)
                        monitors.rotate(proxy.pid, outdir, suite)
                    else:
                        write_marker(suite)
                    self._append_mark_entry([
                        "mark",
                        str(time.time_ns()),
                        kind,
                        suite or "",
                        "",
                    ])

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
                            "kind": kind,
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
            if cmd == "artifact_status":
                session_dir = self.state.get("session_dir")
                telemetry_status_path = self.state.get("telemetry_status_path")
                monitors_obj = self.state.get("monitors")
                manager = self.state.get("power_manager")
                manifest_path: Optional[Path] = None
                monitor_artifacts: list[str] = []
                if isinstance(monitors_obj, Monitors):
                    manifest_path = getattr(monitors_obj, "manifest_path", None)
                    artifact_paths = getattr(monitors_obj, "_artifact_paths", set())
                    lock_obj = getattr(monitors_obj, "_artifact_lock", None)
                    lock_acquired = False
                    if isinstance(lock_obj, threading.Lock):
                        try:
                            lock_acquired = lock_obj.acquire(timeout=1.0)
                        except TypeError:
                            lock_obj.acquire()
                            lock_acquired = True
                    try:
                        if artifact_paths:
                            monitor_artifacts = sorted(str(path) for path in artifact_paths)
                    finally:
                        if isinstance(lock_obj, threading.Lock) and lock_acquired:
                            lock_obj.release()
                power_status = {}
                if isinstance(manager, PowerCaptureManager):
                    try:
                        power_status = manager.status()
                    except Exception:
                        power_status = {}
                response = {
                    "ok": True,
                    "session_dir": str(session_dir) if session_dir else "",
                    "monitor_manifest_path": str(manifest_path) if manifest_path else "",
                    "telemetry_status_path": str(telemetry_status_path) if telemetry_status_path else "",
                    "artifact_paths": monitor_artifacts,
                    "power_status": power_status,
                }
                self._send(conn, response)
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

    def _append_mark_entry(self, row: list[str]) -> None:
        monitor = self.state.get("high_speed_monitor")
        if monitor and hasattr(monitor, "_append_rekey_mark"):
            try:
                monitor._append_rekey_mark(row)
                return
            except Exception:
                pass
        session_dir = self.state.get("session_dir")
        session_id = self.state.get("session_id")
        if not session_dir or not session_id:
            return
        path = Path(session_dir) / f"rekey_marks_{session_id}.csv"
        lock = self.state.setdefault("_marks_lock", threading.Lock())
        try:
            lock_acquired = lock.acquire(timeout=1.5)
        except TypeError:
            lock.acquire()
            lock_acquired = True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not path.exists()
            with path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                if new_file:
                    writer.writerow(["kind", "timestamp_ns", "field1", "field2", "field3"])
                writer.writerow(row)
        except Exception as exc:
            print(f"[{ts()}] follower mark append failed: {exc}", flush=True)
        finally:
            if lock_acquired:
                lock.release()


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

    capabilities = _collect_capabilities_snapshot()
    supported_suites = list(capabilities.get("supported_suites", []))
    if not supported_suites:
        print(
            "[follower] ERROR: no cryptographic suites available; check oqs/AEAD dependencies",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(3)

    print(f"[follower] supported suites={supported_suites}")
    unavailable_count = len(capabilities.get("unsupported_suites", []))
    if unavailable_count:
        print(
            f"[follower] note: {unavailable_count} suites filtered due to missing KEM/SIG/AEAD",
            flush=True,
        )

    for env_key, env_value in auto.get("power_env", {}).items():
        if env_value is None:
            continue
        os.environ.setdefault(env_key, str(env_value))

    telemetry: Optional[TelemetryPublisher] = None
    telemetry_status_path: Optional[Path] = None
    telemetry_enabled = bool(auto.get("telemetry_enabled", True))
    telemetry_host_cfg = auto.get("telemetry_host")
    telemetry_host = str(telemetry_host_cfg or CONTROL_HOST or "0.0.0.0").strip() or "0.0.0.0"
    telemetry_port_cfg = auto.get("telemetry_port")
    telemetry_port = TELEMETRY_DEFAULT_PORT if telemetry_port_cfg in (None, "") else int(telemetry_port_cfg)

    if telemetry_enabled:
        telemetry = TelemetryPublisher(telemetry_host, telemetry_port, session_id)
        telemetry.start()
        print(f"[follower] telemetry publisher started (session={session_id})")
        telemetry_status_path = session_dir / "telemetry_status.json"
        telemetry.configure_status_sink(telemetry_status_path)
        try:
            telemetry.publish(
                "capabilities_snapshot",
                {
                    "sent_timestamp_ns": time.time_ns(),
                    "capabilities": capabilities,
                },
            )
        except Exception:
            pass
    else:
        print("[follower] telemetry disabled via AUTO_DRONE configuration")

    if bool(auto.get("cpu_optimize", True)):
        target_khz = PI5_TARGET_KHZ if args.pi5 else PI4_TARGET_KHZ
        optimize_cpu_performance(target_khz=target_khz)
        print(
            f"[follower] cpu governor target ~{target_khz / 1000:.0f} MHz ({device_generation})",
            flush=True,
        )

    _record_hardware_context(session_dir, telemetry)

    power_dir = session_dir / "power"
    power_dir.mkdir(parents=True, exist_ok=True)
    power_manager = PowerCaptureManager(power_dir, session_id, telemetry)
    if telemetry_status_path is not None:
        power_manager.register_telemetry_status(telemetry_status_path)

    high_speed_monitor = HighSpeedMonitor(session_dir, session_id, telemetry)
    high_speed_monitor.start()

    candidate_initial = auto.get("initial_suite") or default_suite
    if candidate_initial not in supported_suites:
        fallback_suite = supported_suites[0]
        print(
            f"[follower] initial suite {candidate_initial} unsupported; falling back to {fallback_suite}",
            flush=True,
        )
        candidate_initial = fallback_suite
    initial_suite = candidate_initial
    proxy, proxy_log = start_drone_proxy(initial_suite)
    monitors_enabled = bool(auto.get("monitors_enabled", True))
    if not monitors_enabled:
        print("[follower] monitors disabled via AUTO_DRONE configuration")
    monitors = Monitors(enabled=monitors_enabled, telemetry=telemetry, session_dir=session_dir)
    power_manager.register_monitor_manifest(monitors.manifest_path)
    monitors.register_artifacts(session_dir / "hardware_context.json")
    if telemetry_status_path is not None:
        monitors.register_artifacts(telemetry_status_path)
    power_manager.register_artifact_sink(lambda paths: monitors.register_artifacts(*paths))
    time.sleep(1)
    if proxy.poll() is None:
        monitors.start(proxy.pid, suite_outdir(initial_suite), initial_suite, session_dir=session_dir)
        high_speed_monitor.attach_proxy(proxy.pid)
        high_speed_monitor.current_suite = initial_suite
        monitors.register_artifacts(high_speed_monitor.csv_path, high_speed_monitor.rekey_marks_path)

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
    monitors.register_artifacts(echo.packet_log_path)

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
        "session_dir": session_dir,
        "telemetry_status_path": telemetry_status_path,
        "log_path": Path(getattr(proxy_log, "name", "")) if proxy_log else None,
        "capabilities": capabilities,
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
        if proxy:
            try:
                proxy.send_signal(signal.SIGTERM)
            except Exception:
                pass
            killtree(proxy)
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
