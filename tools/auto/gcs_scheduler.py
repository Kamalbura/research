#!/usr/bin/env python3
"""GCS scheduler that drives rekeys and traffic using central configuration."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional

from core.config import CONFIG
from core import suites as suites_mod


DRONE_HOST = CONFIG["DRONE_HOST"]
GCS_HOST = CONFIG["GCS_HOST"]

CONTROL_PORT = int(CONFIG.get("DRONE_CONTROL_PORT", 48080))

APP_SEND_HOST = CONFIG.get("GCS_PLAINTEXT_HOST", "127.0.0.1")
APP_SEND_PORT = int(CONFIG.get("GCS_PLAINTEXT_TX", 47001))
APP_RECV_HOST = CONFIG.get("GCS_PLAINTEXT_HOST", "127.0.0.1")
APP_RECV_PORT = int(CONFIG.get("GCS_PLAINTEXT_RX", 47002))

OUTDIR = Path("logs/auto")
SECRETS_DIR = Path("secrets/matrix")
PROXY_STATUS_PATH = OUTDIR / "gcs_proxy_status.json"
PROXY_SUMMARY_PATH = OUTDIR / "gcs_proxy_summary.json"
SUMMARY_CSV = OUTDIR / "summary.csv"
EVENTS_FILENAME = "gcs_events.jsonl"


def ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def mkdirp(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def suite_outdir(suite: str) -> Path:
    return mkdirp(OUTDIR / suite)


def resolve_suites(requested: Optional[Iterable[str]]) -> List[str]:
    available = suites_mod.list_suites()
    if not available:
        raise RuntimeError("No suites registered in core.suites; cannot proceed")

    if not requested:
        return sorted(available.keys())

    resolved = []
    seen = set()
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


class UdpTraffic:
    def __init__(
        self,
        send_host: str,
        send_port: int,
        recv_host: str,
        recv_port: int,
        events_path: Path,
        rate_pps: int,
        max_packets: Optional[int] = None,
    ) -> None:
        self.send_addr = (send_host, send_port)
        self.recv_addr = (recv_host, recv_port)
        self.rate_pps = max(rate_pps, 1)
        self.max_packets = max_packets if max_packets and max_packets > 0 else None
        self.stop = threading.Event()
        self.sent = 0
        self.rcvd = 0
        mkdirp(events_path.parent)
        self.events = open(events_path, "w", encoding="utf-8")
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(self.recv_addr)
        self.rx.settimeout(0.2)

    def start(self) -> None:
        self.tx_thread = threading.Thread(target=self._sender, daemon=True)
        self.rx_thread = threading.Thread(target=self._receiver, daemon=True)
        self.tx_thread.start()
        self.rx_thread.start()

    def _sender(self) -> None:
        interval = 1.0 / self.rate_pps
        seq = 0
        while not self.stop.is_set():
            payload = seq.to_bytes(4, "big") + int(time.time_ns()).to_bytes(8, "big")
            try:
                self.tx.sendto(payload, self.send_addr)
                self.events.write(json.dumps({"event": "send", "seq": seq, "t_send_ns": time.time_ns()}) + "\n")
                self.events.flush()
                self.sent += 1
            except Exception as exc:
                self.events.write(json.dumps({"event": "send_error", "err": str(exc), "ts": ts()}) + "\n")
                self.events.flush()
            if self.max_packets is not None and self.sent >= self.max_packets:
                self.stop.set()
            seq += 1
            time.sleep(interval)

    def _receiver(self) -> None:
        while not self.stop.is_set():
            try:
                data, _ = self.rx.recvfrom(65535)
            except socket.timeout:
                continue
            except Exception as exc:
                self.events.write(json.dumps({"event": "recv_error", "err": str(exc), "ts": ts()}) + "\n")
                self.events.flush()
                continue
            now = time.time_ns()
            seq = int.from_bytes(data[:4], "big") if len(data) >= 4 else -1
            self.events.write(json.dumps({"event": "recv", "seq": seq, "t_recv_ns": now}) + "\n")
            self.events.flush()
            self.rcvd += 1

    def stop_and_close(self) -> None:
        self.stop.set()
        for thread in (self.tx_thread, self.rx_thread):
            if thread.is_alive():
                thread.join(timeout=1.0)
        self.events.close()
        self.tx.close()
        self.rx.close()


def wait_handshake(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if PROXY_STATUS_PATH.exists():
            try:
                js = json.load(open(PROXY_STATUS_PATH, encoding="utf-8"))
                if js.get("state") in {"running", "completed", "ready"}:
                    return True
            except Exception:
                pass
        time.sleep(0.3)
    return False


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


def read_proxy_summary() -> dict:
    if not PROXY_SUMMARY_PATH.exists():
        return {}
    try:
        return json.load(open(PROXY_SUMMARY_PATH, encoding="utf-8"))
    except Exception:
        return {}


def run_suite(
    gcs: subprocess.Popen,
    suite: str,
    is_first: bool,
    duration_s: float,
    rate_pps: int,
    packets_per_suite: Optional[int],
    delay_between_suites: float,
    pass_index: int,
) -> dict:
    if gcs.poll() is not None:
        raise RuntimeError("GCS proxy is not running; cannot continue")

    if is_first:
        try:
            ctl_send({"cmd": "mark", "suite": suite})
        except Exception as exc:
            print(f"[WARN] control mark failed for {suite}: {exc}", file=sys.stderr)
    else:
        assert gcs.stdin is not None
        print(f"[{ts()}] rekey -> {suite}")
        gcs.stdin.write(suite + "\n")
        gcs.stdin.flush()
        try:
            ctl_send({"cmd": "mark", "suite": suite})
        except Exception as exc:
            print(f"[WARN] control mark failed for {suite}: {exc}", file=sys.stderr)

    events_path = suite_outdir(suite) / EVENTS_FILENAME
    traffic = UdpTraffic(
        APP_SEND_HOST,
        APP_SEND_PORT,
        APP_RECV_HOST,
        APP_RECV_PORT,
        events_path,
        rate_pps,
        packets_per_suite,
    )
    start_ns = time.time_ns()
    traffic.start()

    timeout = duration_s if duration_s > 0 else None
    if timeout is None:
        traffic.stop.wait()
    else:
        traffic.stop.wait(timeout=timeout)

    traffic.stop_and_close()
    end_ns = time.time_ns()

    snapshot_proxy_artifacts(suite)
    proxy_stats = read_proxy_summary()

    row = {
        "pass": pass_index,
        "suite": suite,
        "duration_s": round((end_ns - start_ns) / 1e9, 3),
        "sent": traffic.sent,
        "rcvd": traffic.rcvd,
        "enc_out": proxy_stats.get("enc_out", 0),
        "enc_in": proxy_stats.get("enc_in", 0),
        "drops": proxy_stats.get("drops", 0),
        "rekeys_ok": proxy_stats.get("rekeys_ok", 0),
        "rekeys_fail": proxy_stats.get("rekeys_fail", 0),
        "start_ns": start_ns,
        "end_ns": end_ns,
    }

    print(
        f"[{ts()}] {suite}: sent={traffic.sent} rcvd={traffic.rcvd} "
        f"enc_out={row['enc_out']} enc_in={row['enc_in']}"
    )

    if delay_between_suites > 0:
        time.sleep(delay_between_suites)

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


def main() -> None:
    OUTDIR.mkdir(parents=True, exist_ok=True)

    parser = argparse.ArgumentParser(description="GCS automation scheduler (CONFIG-driven)")
    parser.add_argument("--duration", type=float, default=25.0, help="Seconds per suite (0 = until packets are sent)")
    parser.add_argument("--rate", type=int, default=100, help="Packet rate in packets/sec")
    parser.add_argument("--packets-per-suite", type=int, default=0, help="Optional cap on packets per suite")
    parser.add_argument("--passes", type=int, default=1, help="Number of full sweeps across suites")
    parser.add_argument("--delay-between-suites", type=float, default=0.0, help="Seconds to wait between suites")
    parser.add_argument("--suites", nargs="*", help="Optional subset of suites to exercise")
    args = parser.parse_args()

    if args.duration <= 0 and args.packets_per_suite <= 0:
        raise ValueError("Provide --duration > 0 or --packets-per-suite > 0 so runs terminate deterministically")
    if args.rate <= 0:
        raise ValueError("--rate must be a positive integer")
    if args.passes <= 0:
        raise ValueError("--passes must be >= 1")

    suites = resolve_suites(args.suites)
    if not suites:
        raise RuntimeError("No suites selected for execution")

    initial_suite = preferred_initial_suite(suites)
    if initial_suite and suites[0] != initial_suite:
        suites = [initial_suite] + [s for s in suites if s != initial_suite]
        print(f"[{ts()}] reordered suites to start with {initial_suite} (from CONFIG)")

    try:
        ctl_send({"cmd": "ping"})
        print(f"[{ts()}] follower reachable at {DRONE_HOST}:{CONTROL_PORT}")
    except Exception as exc:
        print(f"[WARN] follower ping failed: {exc}", file=sys.stderr)

    gcs_proc, log_handle = start_gcs_proxy(suites[0])

    try:
        ready = wait_handshake(timeout=20.0)
        print(f"[{ts()}] initial handshake ready? {ready}")

        summary_rows: List[dict] = []
        for pass_index in range(args.passes):
            for idx, suite in enumerate(suites):
                row = run_suite(
                    gcs_proc,
                    suite,
                    is_first=(pass_index == 0 and idx == 0),
                    duration_s=args.duration,
                    rate_pps=args.rate,
                    packets_per_suite=(args.packets_per_suite or None),
                    delay_between_suites=args.delay_between_suites,
                    pass_index=pass_index,
                )
                summary_rows.append(row)

        write_summary(summary_rows)

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


if __name__ == "__main__":
    main()
