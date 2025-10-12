#!/usr/bin/env python3
"""GCS follower that exposes the control channel for a drone-side scheduler."""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

# Ensure project root is on sys.path so `import core` works when running this file
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import suites as suites_mod
from core.aead import is_aead_available
from core.config import CONFIG


DRONE_HOST = CONFIG["DRONE_HOST"]
GCS_HOST = CONFIG["GCS_HOST"]

CONTROL_HOST = CONFIG.get("GCS_CONTROL_HOST", "0.0.0.0")
CONTROL_PORT = int(CONFIG.get("GCS_CONTROL_PORT", CONFIG.get("DRONE_CONTROL_PORT", 48080)))

APP_BIND_HOST = CONFIG.get("GCS_PLAINTEXT_HOST", "127.0.0.1")
APP_RECV_PORT = int(CONFIG.get("GCS_PLAINTEXT_RX", 47002))
APP_SEND_HOST = CONFIG.get("GCS_PLAINTEXT_HOST", "127.0.0.1")
APP_SEND_PORT = int(CONFIG.get("GCS_PLAINTEXT_TX", 47001))

TELEMETRY_DEFAULT_HOST = (
    CONFIG.get("DRONE_TELEMETRY_HOST")
    or CONFIG.get("DRONE_HOST")
    or "127.0.0.1"
)
TELEMETRY_DEFAULT_PORT = int(
    CONFIG.get("DRONE_TELEMETRY_PORT")
    or CONFIG.get("GCS_TELEMETRY_PORT")
    or 52080
)

OUTDIR = Path("logs/auto/gcs_follower")
MARK_DIR = OUTDIR / "marks"
SECRETS_DIR = Path("secrets/matrix")

PROXY_STATUS_PATH = OUTDIR / "gcs_status.json"
PROXY_SUMMARY_PATH = OUTDIR / "gcs_summary.json"


def ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class TelemetryPublisher:
    """Best-effort telemetry transport towards the drone scheduler."""

    def __init__(self, host: str, port: int, session_id: str) -> None:
        self.host = host
        self.port = port
        self.session_id = session_id
        self.queue: "queue.Queue[dict]" = queue.Queue(maxsize=5000)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.sock: Optional[socket.socket] = None
        self.writer = None

    def start(self) -> None:
        self.thread.start()

    def publish(self, kind: str, payload: Dict[str, object]) -> None:
        if self.stop_event.is_set():
            return
        message = {"session_id": self.session_id, "kind": kind, **payload}
        try:
            self.queue.put_nowait(message)
        except queue.Full:
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
        try:
            sock = socket.create_connection((self.host, self.port), timeout=3.0)
            self.sock = sock
            self.writer = sock.makefile("w", encoding="utf-8", buffering=1)
            hello = {
                "session_id": self.session_id,
                "kind": "telemetry_hello",
                "timestamp_ns": time.time_ns(),
                "source": "gcs-follower",
            }
            self.writer.write(json.dumps(hello) + "\n")
            self.writer.flush()
            return True
        except Exception:
            self._close_socket()
            return False

    def _run(self) -> None:
        backoff = 1.0
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
            except Exception:
                self._close_socket()


def popen(cmd, **kw) -> subprocess.Popen:
    if isinstance(cmd, (list, tuple)):
        display = " ".join(str(part) for part in cmd)
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
        first = sorted(suite_map.keys())[0]
        return suites_mod.get_suite(first)["suite_id"]
    if SECRETS_DIR.exists():
        for path in sorted(SECRETS_DIR.iterdir()):
            if (path / "gcs_signing.key").exists():
                return path.name
    return "cs-mlkem768-aesgcm-mldsa65"


def suite_outdir(suite: str) -> Path:
    path = OUTDIR / "suites" / suite
    path.mkdir(parents=True, exist_ok=True)
    return path


def suite_secrets_dir(suite: str) -> Path:
    return SECRETS_DIR / suite


def write_marker(suite: str) -> None:
    MARK_DIR.mkdir(parents=True, exist_ok=True)
    marker = MARK_DIR / f"{int(time.time())}_{suite}.json"
    with open(marker, "w", encoding="utf-8") as handle:
        json.dump({"ts": ts(), "suite": suite}, handle)


def read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def read_proxy_counters() -> dict:
    data = read_json(PROXY_STATUS_PATH)
    counters = data.get("counters") if isinstance(data, dict) else None
    if isinstance(counters, dict) and counters:
        return counters
    summary = read_json(PROXY_SUMMARY_PATH)
    if isinstance(summary, dict):
        summary_counters = summary.get("counters")
        if isinstance(summary_counters, dict) and summary_counters:
            return summary_counters
        if any(key in summary for key in ("enc_out", "enc_in", "rekeys_ok", "rekeys_fail")):
            return summary
    return {}


def read_proxy_status() -> dict:
    data = read_json(PROXY_STATUS_PATH)
    if isinstance(data, dict) and data:
        return data
    return read_json(PROXY_SUMMARY_PATH)


def start_gcs_proxy(suite: str) -> tuple[subprocess.Popen, object]:
    key_path = suite_secrets_dir(suite) / "gcs_signing.key"
    if not key_path.exists():
        raise FileNotFoundError(f"Missing GCS signing key for suite {suite}: {key_path}")

    OUTDIR.mkdir(parents=True, exist_ok=True)
    log_path = OUTDIR / f"gcs_{time.strftime('%Y%m%d-%H%M%S')}.log"
    log_handle = open(log_path, "w", encoding="utf-8", errors="replace")

    os.environ["DRONE_HOST"] = DRONE_HOST
    os.environ["GCS_HOST"] = GCS_HOST
    os.environ["ENABLE_PACKET_TYPE"] = "1" if CONFIG.get("ENABLE_PACKET_TYPE", True) else "0"
    os.environ["STRICT_UDP_PEER_MATCH"] = "1" if CONFIG.get("STRICT_UDP_PEER_MATCH", True) else "0"

    proc = popen(
        [
            sys.executable,
            "-m",
            "core.run_proxy",
            "gcs",
            "--suite",
            suite,
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
    if proc.stdin is None:
        raise RuntimeError("GCS proxy did not expose stdin for manual control")
    return proc, log_handle


class UdpEcho(threading.Thread):
    def __init__(
        self,
        bind_host: str,
        recv_port: int,
        send_host: str,
        send_port: int,
        stop_event: threading.Event,
        publisher: Optional[TelemetryPublisher],
    ) -> None:
        super().__init__(daemon=True)
        self.bind_host = bind_host
        self.recv_port = recv_port
        self.send_host = send_host
        self.send_port = send_port
        self.stop_event = stop_event
        self.publisher = publisher
        self.rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sndbuf = int(os.getenv("GCS_SOCK_SNDBUF", str(8 << 20)))
            rcvbuf = int(os.getenv("GCS_SOCK_RCVBUF", str(8 << 20)))
            self.rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
            self.tx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
        except Exception:
            pass
        self.rx_sock.bind((self.bind_host, self.recv_port))

    def run(self) -> None:
        print(
            f"[gcs-follower] UDP echo listening {self.bind_host}:{self.recv_port} -> {self.send_host}:{self.send_port}",
            flush=True,
        )
        self.rx_sock.settimeout(0.001)
        while not self.stop_event.is_set():
            try:
                data, addr = self.rx_sock.recvfrom(65535)
                recv_ns = time.time_ns()
                annotated = self._annotate_packet(data, recv_ns)
                self.tx_sock.sendto(annotated, (self.send_host, self.send_port))
                if self.publisher:
                    self.publisher.publish(
                        "udp_echo",
                        {
                            "recv_timestamp_ns": recv_ns,
                            "payload_len": len(data),
                            "peer": f"{addr[0]}:{addr[1]}",
                        },
                    )
            except socket.timeout:
                continue
            except Exception as exc:
                print(f"[gcs-follower] UDP echo error: {exc}", flush=True)
        self.rx_sock.close()
        self.tx_sock.close()

    @staticmethod
    def _annotate_packet(data: bytes, recv_ns: int) -> bytes:
        if len(data) >= 20:
            return data[:-8] + recv_ns.to_bytes(8, "big")
        return data + recv_ns.to_bytes(8, "big")


class ControlServer(threading.Thread):
    def __init__(self, host: str, port: int, state: dict, lock: threading.RLock):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.state = state
        self.lock = lock
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(5)

    def run(self) -> None:
        print(f"[gcs-follower] control listening on {self.host}:{self.port}", flush=True)
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
            if cmd == "capabilities":
                capabilities = self.state.get("oqs_capabilities") or {}
                self._send(conn, {"ok": True, "oqs_capabilities": capabilities})
                return
            if cmd == "status":
                with self.lock:
                    proxy: Optional[subprocess.Popen] = self.state.get("proxy")
                    running = bool(proxy and proxy.poll() is None)
                    suite = self.state.get("suite")
                    pending = self.state.get("pending_suite")
                counters = read_proxy_counters()
                status = read_proxy_status()
                capabilities = self.state.get("oqs_capabilities") or {}
                self._send(
                    conn,
                    {
                        "ok": True,
                        "suite": suite,
                        "pending_suite": pending,
                        "running": running,
                        "counters": counters,
                        "status": status,
                        "oqs_capabilities": capabilities,
                    },
                )
                telemetry: Optional[TelemetryPublisher] = self.state.get("telemetry")
                if telemetry:
                    telemetry.publish(
                        "status_reply",
                        {
                            "timestamp_ns": time.time_ns(),
                            "suite": suite,
                            "running": running,
                            "oqs_enabled_kems": capabilities.get("enabled_kems"),
                            "oqs_enabled_sigs": capabilities.get("enabled_sigs"),
                            "supported_aead_tokens": capabilities.get("supported_aead_tokens"),
                        },
                    )
                return
            if cmd == "mark":
                suite = request.get("suite")
                if not suite:
                    self._send(conn, {"ok": False, "error": "missing suite"})
                    return
                with self.lock:
                    current = self.state.get("suite")
                    self.state["prev_suite"] = current
                    self.state["pending_suite"] = suite
                    self.state["suite"] = suite
                write_marker(suite)
                telemetry = self.state.get("telemetry")
                if telemetry:
                    telemetry.publish(
                        "mark",
                        {
                            "timestamp_ns": time.time_ns(),
                            "suite": suite,
                            "prev_suite": current,
                        },
                    )
                self._send(conn, {"ok": True, "marked": suite})
                return
            if cmd == "rekey":
                suite = request.get("suite")
                if not suite:
                    self._send(conn, {"ok": False, "error": "missing suite"})
                    return
                with self.lock:
                    proxy: Optional[subprocess.Popen] = self.state.get("proxy")
                    stdin = self.state.get("proxy_stdin")
                if not proxy or proxy.poll() is not None or stdin is None:
                    self._send(conn, {"ok": False, "error": "proxy_not_running"})
                    return
                try:
                    stdin.write(suite + "\n")
                    stdin.flush()
                except Exception as exc:
                    self._send(conn, {"ok": False, "error": f"stdin_write_failed: {exc}"})
                    return
                with self.lock:
                    self.state["pending_suite"] = suite
                    self.state["last_rekey_started_ns"] = time.time_ns()
                telemetry = self.state.get("telemetry")
                if telemetry:
                    telemetry.publish(
                        "rekey_initiated",
                        {
                            "timestamp_ns": time.time_ns(),
                            "suite": suite,
                        },
                    )
                self._send(conn, {"ok": True, "suite": suite})
                return
            if cmd == "rekey_complete":
                status_value = str(request.get("status", "ok"))
                suite = request.get("suite")
                telemetry = self.state.get("telemetry")
                with self.lock:
                    if status_value.lower() == "ok" and suite:
                        self.state["suite"] = suite
                    self.state.pop("pending_suite", None)
                if telemetry:
                    telemetry.publish(
                        "rekey_complete",
                        {
                            "timestamp_ns": time.time_ns(),
                            "suite": suite,
                            "status": status_value,
                        },
                    )
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
                    with self.lock:
                        current = self.state.get("suite")
                        self.state["prev_suite"] = current
                        self.state["pending_suite"] = suite
                        self.state["suite"] = suite
                    write_marker(suite)
                    telemetry_inner = self.state.get("telemetry")
                    if telemetry_inner:
                        telemetry_inner.publish(
                            "scheduled_mark",
                            {
                                "timestamp_ns": time.time_ns(),
                                "suite": suite,
                                "prev_suite": current,
                            },
                        )

                threading.Thread(target=_do_mark, daemon=True).start()
                self._send(conn, {"ok": True, "scheduled": suite, "t0_ns": t0_ns})
                return
            if cmd == "stop":
                self.state["stop_event"].set()
                telemetry = self.state.get("telemetry")
                if telemetry:
                    telemetry.publish("stop", {"timestamp_ns": time.time_ns()})
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

    parser = argparse.ArgumentParser(description="GCS follower driven by core configuration")
    parser.add_argument(
        "--initial-suite",
        default=default_suite,
        help="Initial suite to launch (default: discover from config/secrets)",
    )
    parser.add_argument(
        "--session-id",
        help="Session identifier for telemetry",
    )
    parser.add_argument(
        "--telemetry-host",
        default=TELEMETRY_DEFAULT_HOST,
        help="Telemetry collector host (default: drone host)",
    )
    parser.add_argument(
        "--telemetry-port",
        type=int,
        default=TELEMETRY_DEFAULT_PORT,
        help="Telemetry collector TCP port",
    )
    parser.add_argument(
        "--disable-telemetry",
        action="store_true",
        help="Disable telemetry publisher",
    )
    parser.add_argument(
        "--disable-echo",
        action="store_true",
        help="Disable UDP echo service",
    )
    args = parser.parse_args()

    initial_suite = args.initial_suite
    session_id = args.session_id or f"session_{int(time.time())}"
    stop_event = threading.Event()

    telemetry: Optional[TelemetryPublisher] = None
    if not args.disable_telemetry:
        telemetry = TelemetryPublisher(args.telemetry_host, args.telemetry_port, session_id)
        telemetry.start()

    proxy = None
    log_handle = None
    echo_thread: Optional[UdpEcho] = None
    control_thread: Optional[ControlServer] = None

    try:
        proxy, log_handle = start_gcs_proxy(initial_suite)
        if proxy.poll() is not None:
            raise RuntimeError(f"gcs proxy exited immediately with {proxy.returncode}")

        if not args.disable_telemetry and telemetry:
            telemetry.publish(
                "proxy_started",
                {
                    "timestamp_ns": time.time_ns(),
                    "suite": initial_suite,
                    "proxy_pid": proxy.pid,
                },
            )

        if not args.disable_echo:
            echo_thread = UdpEcho(
                APP_BIND_HOST,
                APP_RECV_PORT,
                APP_SEND_HOST,
                APP_SEND_PORT,
                stop_event,
                telemetry,
            )
            echo_thread.start()

        state = {
            "suite": initial_suite,
            "pending_suite": None,
            "prev_suite": None,
            "proxy": proxy,
            "proxy_stdin": proxy.stdin,
            "stop_event": stop_event,
            "telemetry": telemetry,
            "oqs_capabilities": _detect_oqs_capabilities(),
        }
        lock = threading.RLock()
        control_thread = ControlServer(CONTROL_HOST, CONTROL_PORT, state, lock)
        control_thread.start()

        print(f"[gcs-follower] awaiting stop signal (session {session_id})", flush=True)
        while not stop_event.is_set():
            if proxy.poll() is not None:
                print(f"[gcs-follower] proxy exited with {proxy.returncode}", flush=True)
                stop_event.set()
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        try:
            ctl_sock = socket.create_connection((CONTROL_HOST, CONTROL_PORT), timeout=1.0)
            ctl_sock.sendall((json.dumps({"cmd": "stop"}) + "\n").encode())
            ctl_sock.close()
        except Exception:
            pass

        if control_thread and control_thread.is_alive():
            control_thread.join(timeout=1.5)

        stop_event.set()
        if echo_thread and echo_thread.is_alive():
            echo_thread.join(timeout=1.0)

        if proxy and proxy.stdin:
            try:
                proxy.stdin.write("quit\n")
                proxy.stdin.flush()
            except Exception:
                pass
        if proxy:
            try:
                proxy.wait(timeout=5)
            except Exception:
                killtree(proxy)

        if log_handle:
            try:
                log_handle.close()
            except Exception:
                pass

        if telemetry:
            telemetry.stop()


if __name__ == "__main__":
    main()
def _canonical_identifier(value: object) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _detect_oqs_capabilities() -> Dict[str, object]:
    capabilities: Dict[str, object] = {
        "enabled_kems": [],
        "enabled_sigs": [],
        "supported_aead_tokens": [],
        "oqs_version": None,
        "liboqs_version": None,
        "error": None,
    }
    try:
        import oqs  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on deployment
        capabilities["error"] = f"{exc.__class__.__name__}: {exc}"
        return capabilities

    try:
        kem_mechanisms = sorted(set(oqs.get_enabled_KEM_mechanisms()))  # type: ignore[attr-defined]
        sig_mechanisms = sorted(set(oqs.get_enabled_sig_mechanisms()))  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - defensive
        capabilities["error"] = f"oqs_query_failed: {exc}"
        kem_mechanisms = []
        sig_mechanisms = []

    capabilities["enabled_kems"] = kem_mechanisms
    capabilities["enabled_sigs"] = sig_mechanisms
    capabilities["oqs_version"] = getattr(oqs, "__version__", None)
    if hasattr(oqs, "get_version"):
        try:
            capabilities["liboqs_version"] = oqs.get_version()
        except Exception:
            capabilities["liboqs_version"] = None

    supported_tokens = []
    for token in ("aesgcm", "chacha20poly1305", "ascon128"):
        try:
            if is_aead_available(token):
                supported_tokens.append(token)
        except NotImplementedError:
            continue
    capabilities["supported_aead_tokens"] = supported_tokens
    return capabilities
