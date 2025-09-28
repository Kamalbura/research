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
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

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

PERF_EVENTS = "task-clock,cycles,instructions,cache-misses,context-switches"


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


class UdpEcho(threading.Thread):
    def __init__(self, bind_host: str, recv_port: int, send_host: str, send_port: int, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.bind_host = bind_host
        self.recv_port = recv_port
        self.send_host = send_host
        self.send_port = send_port
        self.stop_event = stop_event
        self.rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx_sock.bind((self.bind_host, self.recv_port))

    def run(self) -> None:
        print(
            f"[follower] UDP echo up: recv:{self.bind_host}:{self.recv_port} -> send:{self.send_host}:{self.send_port}",
            flush=True,
        )
        self.rx_sock.settimeout(0.5)
        while not self.stop_event.is_set():
            try:
                data, _ = self.rx_sock.recvfrom(65535)
                self.tx_sock.sendto(data, (self.send_host, self.send_port))
            except socket.timeout:
                continue
            except Exception as exc:
                print(f"[follower] UDP echo error: {exc}", flush=True)
        self.rx_sock.close()
        self.tx_sock.close()


class Monitors:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.perf: Optional[subprocess.Popen] = None
        self.pidstat: Optional[subprocess.Popen] = None

    def start(self, pid: int, outdir: Path, suite: str) -> None:
        if not self.enabled:
            return
        outdir.mkdir(parents=True, exist_ok=True)
        perf_cmd = f"perf stat -I 1000 -e {PERF_EVENTS} -p {pid} --log-fd 1"
        self.perf = popen(shlex.split(perf_cmd), stdout=open(outdir / f"perf_{suite}.csv", "w"), stderr=subprocess.STDOUT)
        self.pidstat = popen([
            "pidstat",
            "-hlur",
            "-p",
            str(pid),
            "1",
        ], stdout=open(outdir / f"pidstat_{suite}.txt", "w"), stderr=subprocess.STDOUT)

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
        killtree(self.perf)
        killtree(self.pidstat)
        self.perf = None
        self.pidstat = None


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
                self.state["suite"] = suite
                outdir = self.state["suite_outdir"](suite)
                self.state["monitors"].rotate(proxy.pid, outdir, suite)
                self._send(conn, {"ok": True, "marked": suite})
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
    args = parser.parse_args()

    initial_suite = args.initial_suite
    stop_event = threading.Event()

    proxy = start_drone_proxy(initial_suite)
    monitors = Monitors(enabled=not args.disable_monitors)
    time.sleep(1)
    if proxy.poll() is None:
        monitors.start(proxy.pid, suite_outdir(initial_suite), initial_suite)

    echo = UdpEcho(APP_BIND_HOST, APP_RECV_PORT, APP_SEND_HOST, APP_SEND_PORT, stop_event)
    echo.start()

    state = {
        "proxy": proxy,
        "suite": initial_suite,
        "suite_outdir": suite_outdir,
        "monitors": monitors,
        "stop_event": stop_event,
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
        killtree(proxy)
        try:
            proxy.send_signal(signal.SIGTERM)
        except Exception:
            pass


if __name__ == "__main__":
    main()
