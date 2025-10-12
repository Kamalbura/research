#!/usr/bin/env python3
"""Standalone drone-side MAV scheduler and lightweight control server."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import CONFIG

DRONE_HOST = CONFIG["DRONE_HOST"]
GCS_HOST = CONFIG["GCS_HOST"]
CONTROL_HOST = CONFIG.get("DRONE_CONTROL_HOST", "0.0.0.0")
CONTROL_PORT = int(CONFIG.get("DRONE_CONTROL_PORT", 48080))
M2G_PORT = int(CONFIG.get("DRONE_TO_GCS_CTL_PORT", 48181))
DEFAULT_PRE_GAP = float(CONFIG.get("AUTO_GCS", {}).get("pre_gap_s", 1.0) or 0.0)

OUTDIR = ROOT / "logs" / "mavproxy" / "drone"
POWER_DIR = OUTDIR / "power"
MARK_DIR = OUTDIR / "marks"

PlanItem = Tuple[str, str, float]


def _resolve_public_key_for_suite(suite: str) -> Path:
    matrix_path = ROOT / "secrets" / "matrix" / suite / "gcs_signing.pub"
    if matrix_path.exists():
        return matrix_path
    default_path = ROOT / "secrets" / "gcs_signing.pub"
    if default_path.exists():
        return default_path
    raise FileNotFoundError(
        f"Unable to locate GCS public key for suite '{suite}'. Expected {matrix_path} or {default_path}."
    )


def _start_drone_proxy(suite: str) -> Tuple[subprocess.Popen, Optional[object], Path]:
    pub_path = _resolve_public_key_for_suite(suite)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    log_path = OUTDIR / f"drone_proxy_{time.strftime('%Y%m%d-%H%M%S')}.log"
    log_handle = open(log_path, "w", encoding="utf-8")

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

    status_dir = OUTDIR / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    status_path = status_dir / "drone_status.json"
    summary_path = status_dir / "drone_summary.json"

    print(f"[drone] launching proxy suite={suite} (log -> {log_path})", flush=True)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "core.run_proxy",
            "drone",
            "--suite",
            suite,
            "--peer-pubkey-file",
            str(pub_path),
            "--control-manual",
            "--status-file",
            str(status_path),
            "--json-out",
            str(summary_path),
        ],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        stdin=subprocess.PIPE,
        bufsize=1,
        env=env,
        cwd=str(ROOT),
    )
    return proc, log_handle, status_path


def _stop_process(proc: Optional[subprocess.Popen], log_handle: Optional[object], *, timeout: float = 5.0) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        if log_handle:
            log_handle.close()
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    finally:
        if log_handle:
            log_handle.close()


def _wait_for_proxy_state(
    status_path: Path,
    suite: str,
    *,
    proc: Optional[subprocess.Popen],
    timeout_s: float = 25.0,
) -> Tuple[bool, Optional[str]]:
    deadline = time.time() + timeout_s
    last_reason: Optional[str] = None
    while time.time() < deadline:
        if proc and proc.poll() is not None:
            return False, f"proxy_exited:{proc.returncode}"
        try:
            data = status_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            time.sleep(0.4)
            continue
        except OSError:
            time.sleep(0.4)
            continue
        try:
            status = json.loads(data)
        except json.JSONDecodeError:
            time.sleep(0.3)
            continue
        state = status.get("status") or status.get("state")
        if state == "rekey_fail":
            reason = status.get("error") or status.get("reason") or "rekey_fail"
            return False, str(reason)
        if status.get("suite") == suite:
            return True, None
        counters = status.get("counters")
        if isinstance(counters, dict):
            if counters.get("last_rekey_suite") == suite or counters.get("suite") == suite:
                return True, None
        new_suite = status.get("new_suite")
        if new_suite == suite and state in {"rekey_ok", "running", "ready"}:
            return True, None
        last_reason = state or last_reason
        time.sleep(0.4)
    return False, last_reason or "timeout"


def _switch_drone_suite(
    proc: subprocess.Popen,
    status_path: Path,
    suite: str,
    *,
    timeout_s: float = 25.0,
) -> Tuple[bool, int, Optional[str]]:
    if proc.poll() is not None:
        return False, 0, "proxy_exited"
    if proc.stdin is None:
        return False, 0, "stdin_closed"
    start = time.perf_counter()
    try:
        proc.stdin.write(f"{suite}\n")
        proc.stdin.flush()
    except Exception as exc:
        return False, 0, f"write_error:{exc}"
    ok, note = _wait_for_proxy_state(status_path, suite, proc=proc, timeout_s=timeout_s)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return ok, elapsed_ms, note


class PowerCaptureManager:
    """Simulated power capture that produces placeholder JSON summaries."""

    def __init__(self, output_dir: Path, session_id: str) -> None:
        self.output_dir = output_dir
        self.session_id = session_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.busy = False
        self.capture_index = 0
        self.last_summary: Optional[dict] = None
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

    def start_capture(self, suite: str, duration_s: float, start_ns: Optional[int]) -> Tuple[bool, Optional[str]]:
        with self.lock:
            if self.busy:
                return False, "busy"
            if duration_s <= 0:
                return False, "invalid_duration"
            self.busy = True
            self.capture_index += 1
            capture_id = self.capture_index
            worker = threading.Thread(
                target=self._run_capture,
                args=(capture_id, suite, max(duration_s, 0.0), start_ns),
                name=f"power-capture-{capture_id}",
                daemon=True,
            )
            self.worker = worker
            worker.start()
            return True, None

    def status(self) -> dict:
        with self.lock:
            return {
                "ok": True,
                "busy": self.busy,
                "last_summary": self.last_summary,
            }

    def stop(self) -> None:
        self.stop_event.set()
        worker = None
        with self.lock:
            worker = self.worker
        if worker and worker.is_alive():
            worker.join(timeout=1.0)

    def _run_capture(self, capture_id: int, suite: str, duration_s: float, start_ns: Optional[int]) -> None:
        label = f"suite-{suite}_capture-{capture_id}"
        path = self.output_dir / f"{label}.json"
        started_ns = start_ns or time.time_ns()
        try:
            time.sleep(duration_s)
            summary = {
                "label": label,
                "suite": suite,
                "duration_s": duration_s,
                "session_id": self.session_id,
                "start_ns": started_ns,
                "end_ns": time.time_ns(),
            }
            path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        finally:
            with self.lock:
                self.busy = False
                self.last_summary = {
                    "label": label,
                    "path": str(path),
                    "suite": suite,
                    "duration_s": duration_s,
                }


class ControlServer(threading.Thread):
    """Minimal control server compatible with the GCS MAV scheduler."""

    def __init__(
        self,
        host: str,
        port: int,
        session_id: str,
        power_manager: PowerCaptureManager,
        mark_dir: Path,
    ) -> None:
        super().__init__(name="mav-control-server", daemon=True)
        self.host = host
        self.port = port
        self.session_id = session_id
        self.power_manager = power_manager
        self.mark_dir = mark_dir
        self.mark_dir.mkdir(parents=True, exist_ok=True)
        self.stop_event = threading.Event()
        self.state_lock = threading.Lock()
        self.current_suite = "unknown"
        self.pending_suite: Optional[str] = None
        self.last_requested_suite: Optional[str] = None
        self.last_mark: Optional[dict] = None
        self._server_socket: Optional[socket.socket] = None

    def set_current_suite(self, suite: str) -> None:
        with self.state_lock:
            self.current_suite = suite
            self.pending_suite = None

    def set_pending_suite(self, suite: str) -> None:
        with self.state_lock:
            self.pending_suite = suite
            self.last_requested_suite = suite

    def request_power_capture(self, suite: str, duration_s: float) -> Tuple[bool, Optional[str]]:
        return self.power_manager.start_capture(suite, duration_s, time.time_ns())

    def stop(self) -> None:
        self.stop_event.set()
        if self._server_socket:
            try:
                self._server_socket.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._server_socket.close()
            except Exception:
                pass
        self.power_manager.stop()

    def run(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self.host, self.port))
            server.listen(8)
            server.settimeout(0.5)
            self._server_socket = server
            print(f"[drone] control server listening on {self.host}:{self.port}", flush=True)
            while not self.stop_event.is_set():
                try:
                    conn, addr = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    continue
                threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
        finally:
            try:
                server.close()
            except Exception:
                pass

    def _handle_client(self, conn: socket.socket) -> None:
        with conn:
            try:
                line = conn.makefile().readline()
            except Exception:
                return
            if not line:
                return
            received_ns = time.time_ns()
            try:
                payload = json.loads(line.strip())
            except Exception as exc:
                self._send(conn, {"ok": False, "error": f"bad_json:{exc}"})
                return
            response = self._handle_command(payload, received_ns)
            self._send(conn, response)

    def _handle_command(self, payload: dict, received_ns: int) -> dict:
        cmd = payload.get("cmd")
        if cmd == "ping":
            return {"ok": True}
        if cmd == "session_info":
            return {"ok": True, "session_id": self.session_id}
        if cmd == "power_capture":
            suite = str(payload.get("suite") or "unknown")
            duration_s = float(payload.get("duration_s", 0.0) or 0.0)
            start_ns = payload.get("start_ns")
            try:
                start_ns_int = int(start_ns) if start_ns is not None else None
            except (TypeError, ValueError):
                start_ns_int = None
            ok, error = self.power_manager.start_capture(suite, duration_s, start_ns_int)
            return {"ok": ok, "error": error} if not ok else {"ok": True, "scheduled": True}
        if cmd == "power_status":
            result = self.power_manager.status()
            result.setdefault("ok", True)
            return result
        if cmd == "schedule_mark":
            suite = str(payload.get("suite") or "unknown")
            t0_ns = payload.get("t0_ns")
            try:
                t0_ns_val = int(t0_ns)
            except (TypeError, ValueError):
                t0_ns_val = time.time_ns()
            mark = {
                "timestamp_ns": time.time_ns(),
                "suite": suite,
                "t0_ns": t0_ns_val,
            }
            self._record_mark(mark)
            return {"ok": True}
        if cmd == "status":
            with self.state_lock:
                return {
                    "ok": True,
                    "suite": self.current_suite,
                    "pending_suite": self.pending_suite,
                    "last_requested_suite": self.last_requested_suite,
                    "last_mark": self.last_mark,
                }
        if cmd == "timesync":
            t1_ns = payload.get("t1_ns")
            try:
                t1_ns_val = int(t1_ns) if t1_ns is not None else None
            except (TypeError, ValueError):
                t1_ns_val = None
            response = {
                "ok": True,
                "t2_ns": received_ns,
                "t3_ns": time.time_ns(),
            }
            if t1_ns_val is not None:
                response["t1_ns"] = t1_ns_val
            return response
        if cmd == "stop":
            self.stop_event.set()
            return {"ok": True}
        return {"ok": False, "error": "unknown_cmd"}

    def _record_mark(self, mark: dict) -> None:
        filename = f"{mark['timestamp_ns']}_{mark['suite']}.json"
        path = self.mark_dir / filename
        path.write_text(json.dumps(mark, indent=2), encoding="utf-8")
        with self.state_lock:
            self.last_mark = mark

    @staticmethod
    def _send(conn: socket.socket, obj: dict) -> None:
        try:
            conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except Exception:
            pass


def _load_plan(path_hint: Optional[str]) -> Sequence[PlanItem]:
    if path_hint:
        candidate = Path(path_hint)
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                return _parse_plan(data, source=str(candidate))
            except Exception as exc:
                print(f"[drone] failed to load plan {candidate}: {exc}", flush=True)
    override = os.getenv("DRONE_MAV_PLAN_JSON")
    if override:
        try:
            return _parse_plan(json.loads(override), source="env")
        except Exception as exc:
            print(f"[drone] invalid DRONE_MAV_PLAN_JSON: {exc}", flush=True)
    return [
        ("algo-baseline", "cs-mlkem768-aesgcm-mldsa65", 30.0),
        ("algo-variantA", "cs-mlkem1024-aesgcm-mldsa87", 30.0),
        ("algo-variantB", "cs-mlkem512-aesgcm-mldsa44", 30.0),
    ]


def _parse_plan(payload: Sequence[dict], *, source: str) -> Sequence[PlanItem]:
    plan: List[PlanItem] = []
    for entry in payload:
        try:
            algo = str(entry.get("algorithm"))
            suite = str(entry.get("suite"))
            duration = float(entry.get("duration_s"))
        except Exception:
            continue
        if not algo or not suite or duration <= 0:
            continue
        plan.append((algo, suite, duration))
    if not plan:
        raise ValueError(f"no valid steps found in plan source {source}")
    return plan


def _notify_gcs_switch(algorithm: str, suite: str, duration_s: float, pre_gap_s: float) -> None:
    message = {
        "cmd": "switch_suite",
        "algorithm": algorithm,
        "suite": suite,
        "duration_s": duration_s,
        "pre_gap_s": pre_gap_s,
        "ts_ns": time.time_ns(),
    }
    targets = [GCS_HOST]
    if "127.0.0.1" not in targets:
        targets.append("127.0.0.1")
    for host in targets:
        try:
            with socket.create_connection((host, M2G_PORT), timeout=2.0) as sock:
                sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
            return
        except Exception:
            continue
    print(f"[drone] notify switch failed (no listener on port {M2G_PORT})", flush=True)


def _launch_terminal_command(command: str, cwd: Optional[Path] = None) -> subprocess.Popen:
    if os.name == "nt":
        return subprocess.Popen(
            ["powershell", "-NoExit", "-Command", command],
            cwd=str(cwd) if cwd else None,
            creationflags=CREATE_NEW_CONSOLE,
        )
    return subprocess.Popen(
        ["bash", "-lc", command],
        cwd=str(cwd) if cwd else None,
    )


def _start_mavproxy(autostart: bool) -> Optional[subprocess.Popen]:
    if not autostart:
        return None
    cmd_override = os.getenv("DRONE_MAVPROXY_CMD") or CONFIG.get("DRONE_MAVPROXY_CMD")
    if isinstance(cmd_override, str) and cmd_override.strip():
        print(f"[drone] launching MAVProxy via override command: {cmd_override}", flush=True)
        return _launch_terminal_command(cmd_override.strip(), cwd=ROOT / "drone")
    script_sh = Path(__file__).with_name("run_mavproxy.sh")
    script_ps = script_sh.with_suffix(".ps1")
    if os.name == "nt" and script_ps.exists():
        print(f"[drone] launching MAVProxy via {script_ps}", flush=True)
        return _launch_terminal_command(f'& "{script_ps}"', cwd=script_ps.parent)
    if script_sh.exists():
        print(f"[drone] launching MAVProxy via {script_sh}", flush=True)
        return subprocess.Popen(["/bin/bash", str(script_sh)], cwd=str(script_sh.parent))
    print("[drone] MAVProxy launcher not found; skipping autostart", flush=True)
    return None


def _stop_mavproxy(proc: Optional[subprocess.Popen]) -> None:
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run standalone MAV drone scheduler")
    parser.add_argument("--plan-json", help="Path to JSON plan file (list of algorithm/suite/duration dicts)")
    parser.add_argument("--session-id", help="Override generated session identifier")
    parser.add_argument("--initial-suite", help="Suite to start the drone proxy with (defaults to first plan entry)")
    parser.add_argument("--pre-gap", type=float, help="Override pre-gap before each step")
    parser.add_argument("--no-power", action="store_true", help="Disable simulated power captures")
    parser.add_argument("--no-mavproxy", action="store_true", help="Skip launching the helper MAVProxy script")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = _load_plan(args.plan_json)
    except ValueError as exc:
        print(f"[drone] {exc}", flush=True)
        return 1

    if not plan:
        print("[drone] no plan provided", flush=True)
        return 1

    session_id = args.session_id or f"mav_{int(time.time())}"
    OUTDIR.mkdir(parents=True, exist_ok=True)
    print(f"[drone] session_id={session_id}", flush=True)

    power_manager = PowerCaptureManager(POWER_DIR, session_id)
    control_server = ControlServer(CONTROL_HOST, CONTROL_PORT, session_id, power_manager, MARK_DIR)

    pre_gap_s = args.pre_gap if args.pre_gap is not None else DEFAULT_PRE_GAP
    request_power = not args.no_power and os.getenv("DRONE_REQUEST_POWER", "1").strip().lower() not in {"0", "false", "no", "off"}
    autostart_mavproxy = not args.no_mavproxy and os.getenv("DRONE_AUTOSTART_MAVPROXY", "1").strip().lower() in {"1", "true", "yes", "on"}

    initial_suite = args.initial_suite or plan[0][1]

    control_server.set_current_suite(initial_suite)
    control_server.start()

    drone_proc: Optional[subprocess.Popen] = None
    drone_log = None
    drone_status_path: Optional[Path] = None
    mavproxy_proc: Optional[subprocess.Popen] = None

    try:
        drone_proc, drone_log, drone_status_path = _start_drone_proxy(initial_suite)
        if drone_status_path:
            ok_bootstrap, note_bootstrap = _wait_for_proxy_state(
                drone_status_path,
                initial_suite,
                proc=drone_proc,
                timeout_s=25.0,
            )
            if not ok_bootstrap:
                print(f"[drone] warning: initial proxy bootstrap incomplete: {note_bootstrap}", flush=True)
        mavproxy_proc = _start_mavproxy(autostart_mavproxy)

        current_suite = initial_suite
        for step, (algorithm, suite, duration_s) in enumerate(plan, start=1):
            print(
                f"[drone] step {step}: algo={algorithm} suite={suite} duration={duration_s:.1f}s pre_gap={pre_gap_s:.1f}s",
                flush=True,
            )
            control_server.set_pending_suite(suite)
            _notify_gcs_switch(algorithm, suite, duration_s, pre_gap_s)
            if drone_proc and drone_status_path and suite != current_suite:
                ok_rekey, rekey_ms, rekey_note = _switch_drone_suite(
                    drone_proc,
                    drone_status_path,
                    suite,
                    timeout_s=max(20.0, pre_gap_s + duration_s + 5.0),
                )
                if ok_rekey:
                    current_suite = suite
                    print(f"[drone] rekeyed to {suite} in {rekey_ms} ms", flush=True)
                else:
                    detail = rekey_note or "unknown"
                    print(f"[drone] rekey to {suite} failed: {detail}", flush=True)
            if pre_gap_s > 0:
                time.sleep(pre_gap_s)
            if request_power:
                ok, error = control_server.request_power_capture(suite, duration_s)
                if not ok and error != "busy":
                    print(f"[drone] power capture request rejected: {error}", flush=True)
            if duration_s > 0:
                time.sleep(duration_s)
            control_server.set_current_suite(current_suite)

        print("[drone] schedule complete", flush=True)
        return 0
    except KeyboardInterrupt:
        print("[drone] interrupted; shutting down", flush=True)
        return 130
    finally:
        control_server.stop()
        control_server.join(timeout=1.0)
        _stop_process(drone_proc, drone_log)
        _stop_mavproxy(mavproxy_proc)


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
