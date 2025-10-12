#!/usr/bin/env python3
"""GCS-side MAVProxy scheduler that reacts to drone switch notifications."""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import suites as suites_mod
from core.config import CONFIG

DRONE_HOST = CONFIG["DRONE_HOST"]
CONTROL_PORT = int(CONFIG.get("DRONE_CONTROL_PORT", 48080))
LISTEN_PORT = int(CONFIG.get("DRONE_TO_GCS_CTL_PORT", 48181))
DEFAULT_PRE_GAP = float(CONFIG.get("AUTO_GCS", {}).get("pre_gap_s", 1.0) or 0.0)
SLEEP_SLICE = 0.2
CLOCK_OFFSET_TTL_S = 45.0


def ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _default_initial_suite() -> str:
    env_override = os.getenv("GCS_INITIAL_SUITE")
    if env_override:
        try:
            return suites_mod.get_suite(env_override)["suite_id"]
        except Exception:
            print(f"[gcs] unknown GCS_INITIAL_SUITE '{env_override}', using fallback", flush=True)
    config_suites = CONFIG.get("AUTO_GCS", {}).get("suites") or []
    for candidate in config_suites:
        try:
            return suites_mod.get_suite(candidate)["suite_id"]
        except Exception:
            continue
    try:
        return suites_mod.get_suite(CONFIG.get("SIMPLE_INITIAL_SUITE", ""))["suite_id"]
    except Exception:
        return "cs-mlkem768-aesgcm-mldsa65"


def _ctl_send(payload: dict, timeout: float = 1.5, retries: int = 2, backoff: float = 0.4) -> dict:
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with socket.create_connection((DRONE_HOST, CONTROL_PORT), timeout=timeout) as sock:
                sock.sendall((json.dumps(payload) + "\n").encode("ascii"))
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


def _format_power_status(status: Dict[str, object]) -> str:
    if not status:
        return "skipped"
    if status.get("error"):
        return f"error:{status['error']}"
    if status.get("busy"):
        return "busy"
    summary = status.get("last_summary")
    if isinstance(summary, dict):
        basename = summary.get("basename") or summary.get("path")
        if isinstance(basename, str) and basename:
            return basename
        label = summary.get("label")
        if isinstance(label, str) and label:
            return label
        return "ok"
    return "ok"


def _resolve_gcs_secret(suite_id: str) -> Path:
    matrix_path = ROOT / "secrets" / "matrix" / suite_id / "gcs_signing.key"
    if matrix_path.exists():
        return matrix_path
    fallback = ROOT / "secrets" / "gcs_signing.key"
    if fallback.exists():
        return fallback
    raise FileNotFoundError(
        f"Missing GCS signing key for suite '{suite_id}'. Expected {matrix_path} or {fallback}."
    )


def _schedule_mark(suite: str, pre_gap_s: float, *, clock_offset_ns: Optional[int]) -> bool:
    start_ns_local = time.time_ns() + int(max(pre_gap_s, 0.0) * 1e9)
    if clock_offset_ns is not None:
        start_ns = start_ns_local + clock_offset_ns
    else:
        start_ns = start_ns_local
    payload = {"cmd": "schedule_mark", "suite": suite, "t0_ns": start_ns}
    try:
        resp = _ctl_send(payload, timeout=1.2, retries=2, backoff=0.3)
    except Exception as exc:
        print(f"[gcs] schedule_mark failed: {exc}", flush=True)
        return False
    if resp and not resp.get("ok", True):
        print(f"[gcs] schedule_mark rejected: {resp}", flush=True)
        return False
    return True


def _poll_power_status(wait_hint_s: float) -> dict:
    max_wait = max(6.0, wait_hint_s * 0.25)
    deadline = time.time() + max_wait
    last: dict = {}
    while time.time() < deadline:
        try:
            result = _ctl_send({"cmd": "power_status"}, timeout=1.2, retries=1, backoff=0.3)
        except Exception as exc:
            last = {"ok": False, "error": str(exc)}
            time.sleep(0.6)
            continue
        last = result
        if not result.get("busy"):
            break
        time.sleep(0.6)
    return last


def _perform_timesync_rpc() -> Tuple[int, int]:
    t1 = time.time_ns()
    resp = _ctl_send({"cmd": "timesync", "t1_ns": t1}, timeout=1.2, retries=2, backoff=0.3)
    t4 = time.time_ns()
    if not isinstance(resp, dict):
        raise RuntimeError("invalid_timesync_response")
    try:
        t2 = int(resp.get("t2_ns"))
        t3 = int(resp.get("t3_ns"))
    except (TypeError, ValueError):
        raise RuntimeError("missing_timesync_fields") from None
    delay_ns = (t4 - t1) - (t3 - t2)
    offset_ns = ((t2 - t1) + (t3 - t4)) // 2
    return offset_ns, delay_ns


def _start_gcs_proxy(initial_suite: str, status_path: Path, counters_path: Path) -> subprocess.Popen:
    secret_path = _resolve_gcs_secret(initial_suite)
    cmd = [
        sys.executable,
        "-m",
        "core.run_proxy",
        "gcs",
        "--suite",
        initial_suite,
        "--gcs-secret-file",
        str(secret_path),
        "--control-manual",
        "--status-file",
        str(status_path),
        "--json-out",
        str(counters_path),
    ]
    print(f"[{ts()}] starting GCS proxy: {' '.join(cmd)}", flush=True)
    env = os.environ.copy()
    env.setdefault("DRONE_HOST", DRONE_HOST)
    env.setdefault("GCS_HOST", CONFIG.get("GCS_HOST", "127.0.0.1"))
    env.setdefault("ENABLE_PACKET_TYPE", "1" if CONFIG.get("ENABLE_PACKET_TYPE", True) else "0")
    env.setdefault("STRICT_UDP_PEER_MATCH", "1" if CONFIG.get("STRICT_UDP_PEER_MATCH", True) else "0")
    root_str = str(ROOT)
    existing_py_path = env.get("PYTHONPATH")
    if existing_py_path:
        if root_str not in existing_py_path.split(os.pathsep):
            env["PYTHONPATH"] = root_str + os.pathsep + existing_py_path
    else:
        env["PYTHONPATH"] = root_str
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )


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


def _start_mavproxy() -> Optional[subprocess.Popen]:
    cmd_override = os.getenv("GCS_MAVPROXY_CMD") or CONFIG.get("GCS_MAVPROXY_CMD")
    if isinstance(cmd_override, str) and cmd_override.strip():
        print(f"[{ts()}] starting MAVProxy via override command: {cmd_override}", flush=True)
        return _launch_terminal_command(cmd_override.strip(), cwd=ROOT / "gcs")

    script_sh = ROOT / "gcs" / "run_mavproxy.sh"
    script_ps = ROOT / "gcs" / "run_mavproxy.ps1"

    if os.name == "nt" and script_ps.exists():
        print(f"[{ts()}] starting MAVProxy via {script_ps}", flush=True)
        return _launch_terminal_command(f'& "{script_ps}"', cwd=script_ps.parent)

    if script_sh.exists():
        print(f"[{ts()}] starting MAVProxy via {script_sh}", flush=True)
        return subprocess.Popen(
            ["/bin/bash", str(script_sh)],
            cwd=str(script_sh.parent),
            env=os.environ.copy(),
        )

    print("[gcs] MAVProxy launcher not found; skipping", flush=True)
    return None


def _stop_process(proc: Optional[subprocess.Popen]) -> None:
    if not proc:
        return
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class GCSScheduler:
    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        initial_suite: str,
        outdir: Path,
        status_path: Path,
        summary_path: Path,
        counters_path: Path,
        pre_gap_default: float,
        autostart_mavproxy: bool,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.initial_suite = initial_suite
        self.outdir = outdir
        self.status_path = status_path
        self.summary_path = summary_path
        self.counters_path = counters_path
        self.pre_gap_default = pre_gap_default
        self.autostart_mavproxy = autostart_mavproxy
        self.stop_event = False
        self.gcs_proc: Optional[subprocess.Popen] = None
        self.mavproxy_proc: Optional[subprocess.Popen] = None
        self.step = 0
        self.current_suite: Optional[str] = None
        self._clock_offset_ns: Optional[int] = None
        self._clock_offset_expiry = 0.0
        self._last_timesync_error: Optional[str] = None
        self._last_timesync_log = 0.0

    def start(self) -> None:
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self.counters_path.parent.mkdir(parents=True, exist_ok=True)
        if self.initial_suite:
            try:
                suite_id = suites_mod.get_suite(self.initial_suite)["suite_id"]
            except Exception:
                suite_id = self.initial_suite
        else:
            suite_id = _default_initial_suite()
        self.initial_suite = suite_id
        if self.autostart_mavproxy:
            self.mavproxy_proc = _start_mavproxy()
        self._ensure_timesync(force=True)
        print(
            f"[{ts()}] GCS scheduler listening on {self.listen_host}:{self.listen_port}; "
            f"waiting for drone schedule (fallback {self.initial_suite})",
            flush=True,
        )
        self._install_signal_handlers()
        try:
            self._serve()
        finally:
            self.stop()

    def stop(self) -> None:
        if not self.stop_event:
            self.stop_event = True
        _stop_process(self.gcs_proc)
        _stop_process(self.mavproxy_proc)
        self.gcs_proc = None
        self.current_suite = None

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame) -> None:
            print(f"[{ts()}] received signal {signum}; shutting down", flush=True)
            self.stop()

        try:
            signal.signal(signal.SIGINT, handler)
            signal.signal(signal.SIGTERM, handler)
        except Exception:
            pass

    def _serve(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            bind_host = self.listen_host or "0.0.0.0"
            server.bind((bind_host, self.listen_port))
            server.listen(5)
            server.settimeout(1.0)
            while not self.stop_event:
                try:
                    conn, addr = server.accept()
                except socket.timeout:
                    self._check_processes()
                    continue
                except OSError as exc:
                    if self.stop_event:
                        break
                    print(f"[gcs] accept failed: {exc}", flush=True)
                    time.sleep(SLEEP_SLICE)
                    continue
                with conn:
                    try:
                        line = conn.makefile().readline()
                    except Exception as exc:
                        print(f"[gcs] read failed from {addr}: {exc}", flush=True)
                        continue
                    if not line:
                        continue
                    try:
                        payload = json.loads(line.strip())
                    except Exception as exc:
                        print(f"[gcs] invalid JSON from {addr}: {exc}", flush=True)
                        continue
                    self._handle_payload(payload)

    def _check_processes(self) -> None:
        if self.gcs_proc and self.gcs_proc.poll() is not None:
            code = self.gcs_proc.returncode
            print(f"[gcs] proxy exited with code {code}", flush=True)
            self.gcs_proc = None
            self.current_suite = None
        if self.mavproxy_proc and self.mavproxy_proc.poll() is not None:
            code = self.mavproxy_proc.returncode
            print(f"[gcs] MAVProxy exited with code {code}", flush=True)
            self.mavproxy_proc = None

    def _handle_payload(self, payload: dict) -> None:
        cmd = payload.get("cmd")
        if cmd != "switch_suite":
            print(f"[gcs] ignoring payload: {payload}", flush=True)
            return
        algorithm = str(payload.get("algorithm") or "unknown")
        suite_name = str(payload.get("suite"))
        duration_s = float(payload.get("duration_s", 0.0) or 0.0)
        pre_gap_s = float(payload.get("pre_gap_s", self.pre_gap_default) or 0.0)
        try:
            suite_id = suites_mod.get_suite(suite_name)["suite_id"]
        except Exception:
            suite_id = suite_name
        self.step += 1
        print(
            f"[{ts()}] step {self.step}: algorithm={algorithm} suite={suite_id} "
            f"duration={duration_s:.1f}s pre_gap={pre_gap_s:.1f}s",
            flush=True,
        )
        mark_ok = False
        power_status: dict = {}
        rekey_status = "skip"
        rekey_ms = 0
        note: Optional[str] = None

        offset_ns = self._ensure_timesync()
        if offset_ns is None and self._clock_offset_ns is None:
            print("[gcs] warning: timesync unavailable, using local clock", flush=True)

        if self.gcs_proc is None:
            ok, ready_note = self._launch_proxy_for_suite(suite_id)
            if ok:
                rekey_status = "bootstrap"
                note = ready_note
                self.current_suite = suite_id
            else:
                rekey_status = "fail"
                note = ready_note
        elif self.current_suite != suite_id:
            rekey_status, rekey_ms, note = self._activate_suite(suite_id)
            if rekey_status == "ok":
                self.current_suite = suite_id
        else:
            rekey_status = "noop"

        if rekey_status in {"ok", "bootstrap", "noop"}:
            mark_ok = _schedule_mark(suite_id, pre_gap_s, clock_offset_ns=offset_ns)
            if pre_gap_s > 0:
                self._sleep_with_checks(pre_gap_s)
            if duration_s > 0:
                self._sleep_with_checks(duration_s)
                power_status = _poll_power_status(duration_s)
            else:
                power_status = _poll_power_status(5.0)
        else:
            print(f"[gcs] suite change failed for {suite_id}: {note}", flush=True)
        power_note = _format_power_status(power_status)
        self._write_summary(
            algorithm,
            suite_id,
            duration_s,
            pre_gap_s,
            rekey_status,
            rekey_ms,
            mark_ok,
            power_note,
            note,
        )

    def _sleep_with_checks(self, duration: float) -> None:
        end = time.time() + max(0.0, duration)
        while not self.stop_event and time.time() < end:
            time.sleep(min(SLEEP_SLICE, end - time.time()))
            self._check_processes()

    def _activate_suite(self, suite_id: str) -> Tuple[str, int, Optional[str]]:
        if not self.gcs_proc:
            return "fail", 0, "proxy_not_running"
        if self.gcs_proc.poll() is not None:
            return "fail", 0, "proxy_exited"
        if self.gcs_proc.stdin is None:
            return "fail", 0, "stdin_closed"
        start = time.perf_counter()
        try:
            self.gcs_proc.stdin.write(f"{suite_id}\n")
            self.gcs_proc.stdin.flush()
        except Exception as exc:
            return "fail", 0, f"write_error:{exc}"
        ok, note = self._wait_for_rekey(suite_id)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return ("ok" if ok else "fail", elapsed_ms, note)

    def _wait_for_rekey(self, suite_id: str, timeout_s: float = 25.0) -> Tuple[bool, Optional[str]]:
        deadline = time.time() + timeout_s
        while time.time() < deadline and not self.stop_event:
            if self.gcs_proc and self.gcs_proc.poll() is not None:
                return False, "proxy_exited"
            try:
                data = self.status_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                time.sleep(SLEEP_SLICE)
                continue
            except OSError:
                time.sleep(SLEEP_SLICE)
                continue
            try:
                status = json.loads(data)
            except json.JSONDecodeError:
                time.sleep(SLEEP_SLICE)
                continue
            state = status.get("status")
            if state == "rekey_ok" and status.get("new_suite") == suite_id:
                return True, None
            if state == "rekey_fail":
                reason = status.get("error") or status.get("reason") or "rekey_fail"
                return False, str(reason)
            counters = status.get("counters")
            if isinstance(counters, dict) and counters.get("last_rekey_suite") == suite_id:
                return True, None
            time.sleep(SLEEP_SLICE)
        return False, "timeout"

    def _launch_proxy_for_suite(self, suite_id: str, timeout_s: float = 25.0) -> Tuple[bool, Optional[str]]:
        if self.stop_event:
            return False, "stopping"
        if self.gcs_proc and self.gcs_proc.poll() is None:
            return True, "already_running"
        try:
            self.status_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        try:
            self.gcs_proc = _start_gcs_proxy(suite_id, self.status_path, self.counters_path)
        except Exception as exc:
            return False, f"launch_failed:{exc}"
        deadline = time.time() + timeout_s
        while time.time() < deadline and not self.stop_event:
            if self.gcs_proc and self.gcs_proc.poll() is not None:
                return False, "proxy_exited"
            try:
                data = self.status_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                time.sleep(SLEEP_SLICE)
                continue
            except OSError:
                time.sleep(SLEEP_SLICE)
                continue
            try:
                status = json.loads(data)
            except json.JSONDecodeError:
                time.sleep(SLEEP_SLICE)
                continue
            state = status.get("status") or status.get("state")
            if state in {"running", "ready", "handshake_ok", "rekey_ok"}:
                return True, "proxy_started"
            counters = status.get("counters")
            if isinstance(counters, dict) and counters.get("last_rekey_suite") == suite_id:
                return True, "proxy_started"
            time.sleep(SLEEP_SLICE)
        return False, "bootstrap_timeout"

    def _ensure_timesync(self, force: bool = False) -> Optional[int]:
        now = time.time()
        if not force and self._clock_offset_ns is not None and now < self._clock_offset_expiry:
            return self._clock_offset_ns
        try:
            offset_ns, delay_ns = _perform_timesync_rpc()
        except Exception as exc:
            if force:
                self._clock_offset_ns = None
            if self._last_timesync_error != str(exc) or (now - self._last_timesync_log) > 30.0:
                print(f"[gcs] timesync failed: {exc}", flush=True)
                self._last_timesync_error = str(exc)
                self._last_timesync_log = now
            return self._clock_offset_ns
        self._clock_offset_ns = offset_ns
        self._clock_offset_expiry = now + CLOCK_OFFSET_TTL_S
        if delay_ns < 0:
            delay_ns = 0
        if self._last_timesync_error is not None or (now - self._last_timesync_log) > 30.0:
            print(
                f"[gcs] timesync ok: offset={offset_ns/1e6:.3f}ms rtt={delay_ns/1e6:.3f}ms",
                flush=True,
            )
        self._last_timesync_error = None
        self._last_timesync_log = now
        return self._clock_offset_ns

    def _write_summary(
        self,
        algorithm: str,
        suite_id: str,
        duration_s: float,
        pre_gap_s: float,
        rekey_status: str,
        rekey_ms: int,
        mark_ok: bool,
        power_note: str,
        note: Optional[str],
    ) -> None:
        new_file = not self.summary_path.exists()
        row_note_parts = []
        if note:
            row_note_parts.append(str(note))
        if not mark_ok:
            row_note_parts.append("mark_failed")
        final_note = ";".join(row_note_parts)
        with self.summary_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(
                    [
                        "timestamp_utc",
                        "algorithm",
                        "suite",
                        "duration_s",
                        "pre_gap_s",
                        "rekey_status",
                        "rekey_ms",
                        "mark_ok",
                        "power_note",
                        "notes",
                    ]
                )
            writer.writerow(
                [
                    ts(),
                    algorithm,
                    suite_id,
                    f"{duration_s:.2f}",
                    f"{pre_gap_s:.2f}",
                    rekey_status,
                    rekey_ms if rekey_ms else "",
                    "1" if mark_ok else "0",
                    power_note,
                    final_note,
                ]
            )


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run GCS MAV scheduler")
    parser.add_argument("--listen-host", default=os.getenv("GCS_MAV_LISTEN_HOST", "0.0.0.0"))
    parser.add_argument("--listen-port", type=int, default=LISTEN_PORT)
    parser.add_argument("--initial-suite", default=_default_initial_suite())
    parser.add_argument("--outdir", default=os.getenv("GCS_MAV_OUT", "logs/mavproxy/gcs"))
    parser.add_argument("--pre-gap", type=float, default=DEFAULT_PRE_GAP)
    parser.add_argument("--no-mavproxy", action="store_true")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    outdir = Path(args.outdir)
    status_path = outdir / "gcs_status.json"
    summary_path = outdir / "summary.csv"
    counters_path = outdir / "gcs_counters.json"
    scheduler = GCSScheduler(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        initial_suite=args.initial_suite,
        outdir=outdir,
        status_path=status_path,
        summary_path=summary_path,
        counters_path=counters_path,
        pre_gap_default=args.pre_gap,
        autostart_mavproxy=not args.no_mavproxy,
    )
    try:
        scheduler.start()
    except KeyboardInterrupt:
        scheduler.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
