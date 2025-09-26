#!/usr/bin/env python3
"""Automated two-host harness for PQC drone↔GCS proxy validation.

This script orchestrates a local GCS proxy and a remote drone proxy using SSH.
It drives traffic on both plaintext interfaces, triggers an in-band rekey, and
collects artefacts (counters, logs, and traffic summaries) for post-run
analysis. The helper is intended for repeatable LAN tests between a Windows
GCS host and a Linux-based drone (e.g., Raspberry Pi).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import posixpath
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import paramiko

from tools.counter_utils import (
    ProxyCounters,
    TrafficSummary,
    load_proxy_counters,
    load_traffic_summary,
)


HANDSHAKE_PATTERN = "PQC handshake completed successfully"
REKEY_OK_PATTERN = "Control rekey successful"
REKEY_FAIL_MARKERS = ("Control rekey failed", "prepare_fail", "rekeys_fail")


@dataclass
class StreamRelay:
    """Background copier that streams text from a process to a log file."""

    stream: Iterable[str]
    log_path: Path
    patterns: Dict[str, threading.Event]
    failure_hook: Optional[callable]
    thread: threading.Thread

    @classmethod
    def start(
        cls,
        stream: Iterable[str],
        log_path: Path,
        *,
        patterns: Optional[Dict[str, threading.Event]] = None,
        failure_hook: Optional[callable] = None,
    ) -> "StreamRelay":
        log_path.parent.mkdir(parents=True, exist_ok=True)
        relay = cls(stream, log_path, patterns or {}, failure_hook, threading.Thread())
        relay.thread = threading.Thread(target=relay._pump, name=f"relay-{log_path.name}", daemon=True)
        relay.thread.start()
        return relay

    def _pump(self) -> None:
        with open(self.log_path, "w", encoding="utf-8") as sink:
            for raw in iter(self.stream.readline, ""):
                if isinstance(raw, bytes):  # pragma: no cover - defensive
                    raw = raw.decode("utf-8", "replace")
                if not raw:
                    break
                sink.write(raw)
                sink.flush()
                line = raw.rstrip("\r\n")
                for pattern, event in self.patterns.items():
                    if pattern in line:
                        event.set()
                if self.failure_hook:
                    self.failure_hook(line)


@dataclass
class LocalProcess:
    proc: subprocess.Popen[str]
    stdout: StreamRelay
    stderr: StreamRelay

    def terminate(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


@dataclass
class RemoteProcess:
    command: str
    channel: paramiko.Channel
    stdin: paramiko.ChannelFile
    stdout_relay: StreamRelay
    stderr_relay: StreamRelay

    def close(self) -> None:
        if not self.channel.closed:
            try:
                self.channel.close()
            except Exception:  # pragma: no cover - best effort
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Two-host PQC proxy orchestrator")
    parser.add_argument("--suite", required=True, help="Initial suite identifier (e.g. cs-kyber768-aesgcm-dilithium3)")
    parser.add_argument("--rekey-suite", required=True, help="Suite identifier to switch to during the run")
    parser.add_argument("--remote-host", required=True, help="Drone host/IP reachable via SSH")
    parser.add_argument("--remote-user", required=True, help="SSH username for the drone host")
    parser.add_argument("--ssh-key", help="Path to SSH private key for the drone host")
    parser.add_argument("--ssh-password", help="SSH password (discouraged; key auth preferred)")
    parser.add_argument("--remote-root", default="~/research", help="Remote repository root containing this project")
    parser.add_argument("--remote-python", default="python", help="Python executable on the drone host")
    default_local_python = Path(os.environ.get("PYTHON_EXECUTABLE", sys.executable)).resolve()
    parser.add_argument(
        "--local-python",
        default=str(default_local_python),
        help="Python executable on the GCS host",
    )
    parser.add_argument("--artifact-dir", default="artifacts/harness", help="Local directory for collected artefacts")
    parser.add_argument("--remote-artifact-dir", default="artifacts/harness", help="Remote directory (within repo) for run artefacts")
    parser.add_argument("--label", help="Optional label appended to the run identifier")

    parser.add_argument("--traffic-count", type=int, default=400, help="Packets to send from each traffic generator")
    parser.add_argument("--traffic-rate", type=float, default=40.0, help="Packets per second for traffic generators")
    parser.add_argument("--traffic-duration", type=float, default=40.0, help="Duration (seconds) cap for traffic generators")

    parser.add_argument("--stop-seconds", type=float, default=90.0, help="Auto-stop duration supplied to each proxy")
    parser.add_argument("--handshake-timeout", type=float, default=30.0, help="Timeout for initial handshake detection")
    parser.add_argument("--rekey-delay", type=float, default=15.0, help="Delay (seconds) before requesting rekey once traffic is flowing")
    parser.add_argument("--rekey-timeout", type=float, default=60.0, help="Timeout waiting for successful rekey events")
    parser.add_argument("--post-rekey-wait", type=float, default=10.0, help="Additional wait after rekey before teardown")

    return parser.parse_args()


def build_run_id(base_suite: str, label: Optional[str]) -> str:
    stamp = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    suite_token = base_suite.replace("-", "_")
    if label:
        label_clean = "".join(ch for ch in label if ch.isalnum() or ch in ("_", "-"))
        return f"{stamp}_{suite_token}_{label_clean}"
    return f"{stamp}_{suite_token}"


def wait_event(event: threading.Event, timeout: float, description: str) -> None:
    if not event.wait(timeout):
        raise TimeoutError(f"Timed out waiting for {description}")


def make_failure_hook(errors: List[str], label: str):
    def _hook(line: str) -> None:
        lower = line.lower()
        if any(marker in lower for marker in REKEY_FAIL_MARKERS):
            errors.append(f"{label}: {line}")
    return _hook


def resolve_remote_root(client: paramiko.SSHClient, remote_root: str) -> str:
    cmd = f"cd {shlex.quote(remote_root)} && pwd"
    _stdin, stdout, stderr = client.exec_command(cmd)
    resolved = stdout.read().decode("utf-8", "ignore").strip()
    err = stderr.read().decode("utf-8", "ignore").strip()
    if not resolved:
        raise RuntimeError(f"Failed to resolve remote root: {err or 'unknown error'}")
    return resolved


def start_remote_process(
    client: paramiko.SSHClient,
    command: str,
    stdout_log: Path,
    stderr_log: Path,
    *,
    patterns: Optional[Dict[str, threading.Event]] = None,
    failure_hook=None,
) -> RemoteProcess:
    stdin, stdout, stderr = client.exec_command(command, get_pty=False)
    stdout_file = stdout.channel.makefile("r", encoding="utf-8", errors="replace")
    stderr_file = stderr.channel.makefile("r", encoding="utf-8", errors="replace")

    stdout_relay = StreamRelay.start(stdout_file, stdout_log, patterns=patterns, failure_hook=failure_hook)
    stderr_relay = StreamRelay.start(stderr_file, stderr_log, patterns=patterns, failure_hook=failure_hook)

    return RemoteProcess(command, stdout.channel, stdin, stdout_relay, stderr_relay)


def wait_remote(process: RemoteProcess, timeout: float) -> int:
    deadline = time.time() + timeout
    while not process.channel.exit_status_ready():
        if time.time() > deadline:
            raise TimeoutError(f"Remote command timed out: {process.command}")
        time.sleep(1)
    return process.channel.recv_exit_status()


def start_local_process(
    cmd: List[str],
    *,
    env: Dict[str, str],
    stdout_log: Path,
    stderr_log: Path,
    patterns: Optional[Dict[str, threading.Event]] = None,
    failure_hook=None,
) -> LocalProcess:
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    if proc.stdout is None or proc.stderr is None:
        raise RuntimeError("Failed to capture local process pipes")

    stdout_relay = StreamRelay.start(proc.stdout, stdout_log, patterns=patterns, failure_hook=failure_hook)
    stderr_relay = StreamRelay.start(proc.stderr, stderr_log, patterns=patterns, failure_hook=failure_hook)

    return LocalProcess(proc=proc, stdout=stdout_relay, stderr=stderr_relay)


def send_rekey_command(local_proxy: LocalProcess, suite_id: str) -> None:
    if local_proxy.proc.stdin is None:
        raise RuntimeError("Local proxy stdin not available for rekey command")
    local_proxy.proc.stdin.write(f"{suite_id}\n")
    local_proxy.proc.stdin.flush()


def download_file(sftp: paramiko.SFTPClient, remote_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    sftp.get(remote_path, str(local_path))


def summarize_run(
    run_dir: Path,
    run_id: str,
    suite_initial: str,
    suite_rekey: str,
    gcs_proxy_json: Path,
    drone_proxy_json: Path,
    gcs_traffic_summary: Path,
    drone_traffic_summary: Path,
    errors: List[str],
) -> Dict[str, object]:
    gcs_counters = load_proxy_counters(gcs_proxy_json)
    drone_counters = load_proxy_counters(drone_proxy_json)
    gcs_counters.ensure_rekey(suite_rekey)
    drone_counters.ensure_rekey(suite_rekey)

    gcs_traffic = load_traffic_summary(gcs_traffic_summary)
    drone_traffic = load_traffic_summary(drone_traffic_summary)

    summary = {
        "run_id": run_id,
        "timestamp_utc": _dt.datetime.utcnow().isoformat() + "Z",
        "suite_initial": suite_initial,
        "suite_rekey": suite_rekey,
        "artifacts": {
            "root": str(run_dir.resolve()),
            "gcs_proxy": str(gcs_proxy_json.resolve()),
            "drone_proxy": str(drone_proxy_json.resolve()),
            "gcs_traffic": str(gcs_traffic_summary.resolve()),
            "drone_traffic": str(drone_traffic_summary.resolve()),
        },
        "gcs": {
            "role": gcs_counters.role,
            "suite": gcs_counters.suite,
            "counters": gcs_counters.counters,
        },
        "drone": {
            "role": drone_counters.role,
            "suite": drone_counters.suite,
            "counters": drone_counters.counters,
        },
        "traffic": {
            "gcs": {
                "sent_total": gcs_traffic.sent_total,
                "recv_total": gcs_traffic.recv_total,
                "tx_bytes_total": gcs_traffic.tx_bytes_total,
                "rx_bytes_total": gcs_traffic.rx_bytes_total,
            },
            "drone": {
                "sent_total": drone_traffic.sent_total,
                "recv_total": drone_traffic.recv_total,
                "tx_bytes_total": drone_traffic.tx_bytes_total,
                "rx_bytes_total": drone_traffic.rx_bytes_total,
            },
        },
        "errors": errors,
    }
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    args = parse_args()
    run_id = build_run_id(args.suite, args.label)

    run_dir = Path(args.artifact_dir).expanduser().resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    gcs_proxy_json = run_dir / "gcs_proxy.json"
    drone_proxy_json = run_dir / "drone_proxy.json"
    gcs_traffic_out = run_dir / "gcs_traffic.jsonl"
    drone_traffic_out = run_dir / "drone_traffic.jsonl"
    gcs_traffic_summary = run_dir / "gcs_traffic_summary.json"
    drone_traffic_summary = run_dir / "drone_traffic_summary.json"

    logs_dir = run_dir / "logs"
    gcs_stdout_log = logs_dir / "gcs_proxy_stdout.log"
    gcs_stderr_log = logs_dir / "gcs_proxy_stderr.log"
    drone_stdout_log = logs_dir / "drone_proxy_stdout.log"
    drone_stderr_log = logs_dir / "drone_proxy_stderr.log"
    gcs_traffic_stdout = logs_dir / "gcs_traffic_stdout.log"
    gcs_traffic_stderr = logs_dir / "gcs_traffic_stderr.log"
    drone_traffic_stdout = logs_dir / "drone_traffic_stdout.log"
    drone_traffic_stderr = logs_dir / "drone_traffic_stderr.log"

    errors: List[str] = []

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        args.remote_host,
        username=args.remote_user,
        key_filename=args.ssh_key,
        password=args.ssh_password,
        look_for_keys=args.ssh_key is None,
    )

    remote_root_abs = resolve_remote_root(client, args.remote_root)
    remote_run_rel = posixpath.join(args.remote_artifact_dir.rstrip("/"), run_id)
    remote_run_abs = posixpath.join(remote_root_abs, remote_run_rel)

    mkdir_cmd = f"cd {shlex.quote(args.remote_root)} && mkdir -p {shlex.quote(remote_run_rel)}"
    client.exec_command(mkdir_cmd)

    handshake_gcs = threading.Event()
    handshake_drone = threading.Event()
    rekey_gcs = threading.Event()
    rekey_drone = threading.Event()

    failure_hook_gcs = make_failure_hook(errors, "gcs")
    failure_hook_drone = make_failure_hook(errors, "drone")

    # Start remote drone proxy
    remote_env = "ENABLE_PACKET_TYPE=1 PYTHONUNBUFFERED=1"
    remote_proxy_json_rel = posixpath.join(remote_run_rel, "drone_proxy.json")
    remote_proxy_cmd = (
        f"cd {shlex.quote(args.remote_root)} && {remote_env} {shlex.quote(args.remote_python)} -m core.run_proxy "
        f"drone --suite {shlex.quote(args.suite)} --stop-seconds {args.stop_seconds} "
        f"--json-out {shlex.quote(remote_proxy_json_rel)}"
    )
    drone_process = start_remote_process(
        client,
        remote_proxy_cmd,
        drone_stdout_log,
        drone_stderr_log,
        patterns={HANDSHAKE_PATTERN: handshake_drone, REKEY_OK_PATTERN: rekey_drone},
        failure_hook=failure_hook_drone,
    )

    # Start local GCS proxy with manual control enabled
    local_env = os.environ.copy()
    local_env["ENABLE_PACKET_TYPE"] = "1"
    local_env.setdefault("PYTHONUNBUFFERED", "1")

    gcs_cmd = [
        args.local_python,
        "-m",
        "core.run_proxy",
        "gcs",
        "--suite",
        args.suite,
        "--stop-seconds",
        str(args.stop_seconds),
        "--json-out",
        str(gcs_proxy_json),
        "--control-manual",
    ]
    gcs_process = start_local_process(
        gcs_cmd,
        env=local_env,
        stdout_log=gcs_stdout_log,
        stderr_log=gcs_stderr_log,
        patterns={HANDSHAKE_PATTERN: handshake_gcs, REKEY_OK_PATTERN: rekey_gcs},
        failure_hook=failure_hook_gcs,
    )

    drone_traffic: Optional[RemoteProcess] = None
    gcs_traffic: Optional[LocalProcess] = None

    try:
        wait_event(handshake_gcs, args.handshake_timeout, "GCS handshake")
        wait_event(handshake_drone, args.handshake_timeout, "drone handshake")

        # Launch traffic generators
        remote_traffic_summary_rel = posixpath.join(remote_run_rel, "drone_traffic_summary.json")
        remote_traffic_out_rel = posixpath.join(remote_run_rel, "drone_traffic.jsonl")
        remote_traffic_cmd = (
            f"cd {shlex.quote(args.remote_root)} && {remote_env} {shlex.quote(args.remote_python)} tools/traffic_drone.py "
            f"--count {args.traffic_count} --rate {args.traffic_rate} --duration {args.traffic_duration} "
            f"--out {shlex.quote(remote_traffic_out_rel)} --summary {shlex.quote(remote_traffic_summary_rel)}"
        )
        drone_traffic = start_remote_process(
            client,
            remote_traffic_cmd,
            drone_traffic_stdout,
            drone_traffic_stderr,
            failure_hook=failure_hook_drone,
        )

        gcs_traffic_cmd = [
            args.local_python,
            "tools/traffic_gcs.py",
            "--count",
            str(args.traffic_count),
            "--rate",
            str(args.traffic_rate),
            "--duration",
            str(args.traffic_duration),
            "--out",
            str(gcs_traffic_out),
            "--summary",
            str(gcs_traffic_summary),
        ]
        gcs_traffic = start_local_process(
            gcs_traffic_cmd,
            env=local_env,
            stdout_log=gcs_traffic_stdout,
            stderr_log=gcs_traffic_stderr,
            failure_hook=failure_hook_gcs,
        )

        time.sleep(max(0.0, args.rekey_delay))
        send_rekey_command(gcs_process, args.rekey_suite)

        wait_event(rekey_gcs, args.rekey_timeout, "GCS rekey completion")
        wait_event(rekey_drone, args.rekey_timeout, "drone rekey completion")

        time.sleep(max(0.0, args.post_rekey_wait))

        # Wait for traffic to complete (they exit once duration reached)
        if gcs_traffic is not None:
            gcs_traffic.proc.wait(timeout=args.traffic_duration + 20)
        if drone_traffic is not None:
            wait_remote(drone_traffic, args.traffic_duration + 20)

        # Wait for proxies to exit after stop-seconds window
        gcs_process.proc.wait(timeout=args.stop_seconds + 30)
        wait_remote(drone_process, args.stop_seconds + 30)

    finally:
        # Cleanup
        gcs_process.terminate()
        drone_process.close()
        # ensure traffic processes stopped
        if gcs_traffic is not None:
            try:
                gcs_traffic.terminate()
            except Exception:
                pass
        if drone_traffic is not None:
            try:
                drone_traffic.close()
            except Exception:
                pass

    # Download remote artefacts
    with client.open_sftp() as sftp:
        download_file(sftp, posixpath.join(remote_root_abs, remote_proxy_json_rel), drone_proxy_json)
        download_file(sftp, posixpath.join(remote_root_abs, remote_traffic_summary_rel), drone_traffic_summary)
        download_file(sftp, posixpath.join(remote_root_abs, remote_traffic_out_rel), drone_traffic_out)

    client.close()

    summary = summarize_run(
        run_dir,
        run_id,
        args.suite,
        args.rekey_suite,
        gcs_proxy_json,
        drone_proxy_json,
        gcs_traffic_summary,
        drone_traffic_summary,
        errors,
    )

    summary_txt = run_dir / "summary.txt"
    summary_txt.write_text(
        "Run ID: {run_id}\nInitial suite: {suite}\nRekey suite: {rekey}\nGCS rekeys_ok: {gcs_ok}\n"
        "Drone rekeys_ok: {drone_ok}\nArtefacts: {root}\n".format(
            run_id=run_id,
            suite=args.suite,
            rekey=args.rekey_suite,
            gcs_ok=summary["gcs"]["counters"].get("rekeys_ok"),
            drone_ok=summary["drone"]["counters"].get("rekeys_ok"),
            root=summary["artifacts"]["root"],
        ),
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
