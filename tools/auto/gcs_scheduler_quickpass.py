#!/usr/bin/env python3
"""
Quick-pass scheduler: send-one?wait-echo?next suite.

Minimal, self-contained scheduler intended to run on the GCS host.
It assumes a running drone follower on the Pi exposing a tiny JSON control API
and a running GCS proxy launched with "--control-manual" so rekeys can be
performed by writing suite IDs to the proxy's stdin.

Outputs:
- logs/auto/quickpass_events.jsonl
- logs/auto/quickpass_summary.csv

Usage (example):
  python -m tools.auto.gcs_scheduler_quickpass \
    --gcs 192.168.0.101 --drone 192.168.0.102 \
    --control-port 48080 --app-send-port 47001 --app-recv-port 47002 \
    --passes 1
"""
import argparse
import csv
import json
import os
import pathlib
import socket
import subprocess
import sys
import time

SUITES = [
    "cs-mlkem512-aesgcm-mldsa44","cs-mlkem512-aesgcm-mldsa65","cs-mlkem512-aesgcm-mldsa87",
    "cs-mlkem512-aesgcm-falcon512","cs-mlkem512-aesgcm-falcon1024",
    "cs-mlkem512-aesgcm-sphincs128fsha2","cs-mlkem512-aesgcm-sphincs256fsha2",
    "cs-mlkem768-aesgcm-mldsa44","cs-mlkem768-aesgcm-mldsa65","cs-mlkem768-aesgcm-mldsa87",
    "cs-mlkem768-aesgcm-falcon512","cs-mlkem768-aesgcm-falcon1024",
    "cs-mlkem768-aesgcm-sphincs128fsha2","cs-mlkem768-aesgcm-sphincs256fsha2",
    "cs-mlkem1024-aesgcm-mldsa44","cs-mlkem1024-aesgcm-mldsa65","cs-mlkem1024-aesgcm-mldsa87",
    "cs-mlkem1024-aesgcm-falcon512","cs-mlkem1024-aesgcm-falcon1024",
    "cs-mlkem1024-aesgcm-sphincs128fsha2","cs-mlkem1024-aesgcm-sphincs256fsha2"
]


def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def mkdirp(p):
    pathlib.Path(p).mkdir(parents=True, exist_ok=True)


# simple control client for drone follower
def ctl(host, port, obj, timeout=3.0):
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall((json.dumps(obj) + "\n").encode())
        s.shutdown(socket.SHUT_WR)
        line = s.makefile().readline()
        return json.loads(line.strip()) if line else {"ok": False, "error": "no reply"}


# send a single UDP packet and wait for an echo
def send_and_wait_echo(send_port: int, recv_port: int, payload: bytes, timeout_s: float):
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        rx.bind(("0.0.0.0", recv_port))
        rx.settimeout(min(0.2, timeout_s))
        t0 = time.time_ns()
        seq = int.from_bytes(payload[:4], "big") if len(payload) >= 4 else 0
        tx.sendto(payload, ("127.0.0.1", send_port))
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                data, _ = rx.recvfrom(65535)
                if len(data) >= 4 and int.from_bytes(data[:4], "big") == seq:
                    t1 = time.time_ns()
                    return True, t0, t1, len(data)
            except socket.timeout:
                pass
        return False, t0, None, 0
    finally:
        try:
            tx.close()
        except Exception:
            pass
        try:
            rx.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gcs", required=True)
    ap.add_argument("--drone", required=True)
    ap.add_argument("--control-port", type=int, default=48080)
    ap.add_argument("--app-send-port", type=int, default=47001)
    ap.add_argument("--app-recv-port", type=int, default=47002)
    ap.add_argument("--verify-timeout", type=float, default=5.0)
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--outdir", default="logs/auto")
    ap.add_argument("--secrets-dir", default="secrets/matrix")
    ap.add_argument("--initial-suite", default=None)
    ap.add_argument("--suites", nargs="*", default=SUITES)
    args = ap.parse_args()

    os.environ["DRONE_HOST"] = args.drone
    os.environ["GCS_HOST"] = args.gcs
    os.environ["ENABLE_PACKET_TYPE"] = "1"
    os.environ["STRICT_UDP_PEER_MATCH"] = "1"

    mkdirp(args.outdir)
    evlog = open(f"{args.outdir}/quickpass_events.jsonl", "a", encoding="utf-8")

    def log_event(**row):
        row.setdefault("ts", ts())
        evlog.write(json.dumps(row) + "\n")
        evlog.flush()

    suites = list(args.suites)
    if args.initial_suite and args.initial_suite in suites:
        i = suites.index(args.initial_suite)
        suites = suites[i:] + suites[:i]

    first = suites[0]
    keyfile = f"{args.secrets_dir}/{first}/gcs_signing.key"
    mkdirp(f"{args.outdir}/{first}")
    status_file = f"{args.outdir}/{first}/gcs_status.json"
    summary_file = f"{args.outdir}/{first}/gcs_summary.json"
    gcs_log = open(f"{args.outdir}/gcs_{time.strftime('%Y%m%d-%H%M%S')}.log", "w", encoding="utf-8", errors="replace")

    gcs = subprocess.Popen([
        sys.executable, "-m", "core.run_proxy", "gcs",
        "--suite", first, "--gcs-secret-file", keyfile,
        "--control-manual",
        "--status-file", status_file, "--json-out", summary_file
    ], stdin=subprocess.PIPE, stdout=gcs_log, stderr=subprocess.STDOUT, text=True, bufsize=1)

    # initial ping/mark
    try:
        ctl(args.drone, args.control_port, {"cmd": "ping"})
        ctl(args.drone, args.control_port, {"cmd": "mark", "suite": first})
    except Exception as e:
        log_event(event="control_warn", msg=str(e))

    csv_path = f"{args.outdir}/quickpass_summary.csv"
    have_header = os.path.exists(csv_path)
    csvf = open(csv_path, "a", newline="", encoding="utf-8")
    w = csv.DictWriter(csvf, fieldnames=["pass_idx", "suite", "ok", "attempt_ns", "payload_bytes", "note"])
    if not have_header:
        w.writeheader(); csvf.flush()

    def rekey(to_suite: str):
        try:
            gcs.stdin.write(to_suite + "\n"); gcs.stdin.flush()
        except Exception as e:
            log_event(event="gcs_write_fail", msg=str(e))
        try:
            ctl(args.drone, args.control_port, {"cmd": "mark", "suite": to_suite})
        except Exception as e:
            log_event(event="control_warn", msg=f"mark failed: {e}")

    try:
        for p in range(args.passes):
            for idx, suite in enumerate(suites):
                if p == 0 and idx == 0:
                    current = first
                else:
                    current = suite
                    log_event(event="rekey", to=current, pass_idx=p)
                    rekey(current)

                seq = int(time.time_ns() & 0xFFFFFFFF)
                payload = seq.to_bytes(4, "big") + int(time.time_ns()).to_bytes(8, "big")
                ok, t0_ns, t1_ns, nbytes = send_and_wait_echo(args.app_send_port, args.app_recv_port, payload, args.verify_timeout)

                if ok:
                    attempt_ns = t1_ns - t0_ns
                    w.writerow({"pass_idx": p, "suite": current, "ok": True, "attempt_ns": attempt_ns, "payload_bytes": nbytes, "note": "echo"})
                    csvf.flush()
                    log_event(event="echo_ok", suite=current, pass_idx=p, rtt_ns=attempt_ns)
                else:
                    w.writerow({"pass_idx": p, "suite": current, "ok": False, "attempt_ns": "", "payload_bytes": 0, "note": "timeout"})
                    csvf.flush()
                    log_event(event="echo_timeout", suite=current, pass_idx=p)
    finally:
        try:
            ctl(args.drone, args.control_port, {"cmd": "stop"})
        except Exception:
            pass
        try:
            gcs.stdin.write("quit\n"); gcs.stdin.flush()
        except Exception:
            pass
        try:
            gcs.wait(timeout=3)
        except Exception:
            gcs.kill()
        evlog.close(); csvf.close(); gcs_log.close()


if __name__ == "__main__":
    main()
