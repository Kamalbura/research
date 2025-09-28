#!/usr/bin/env python3
"""
GCS scheduler (interactive by default; --auto runs whole list):
- Starts the GCS proxy in --control-manual with the first suite.
- For each chosen suite: rekey -> send UDP -> wait for echo(s) -> proceed.
- All networking parameters are sourced from core.config.CONFIG.
"""

import os, sys, time, json, csv, pathlib, socket, subprocess

from core.config import CONFIG
from core import suites as suite_registry

SECRETS_DIR = "secrets/matrix"
OUTDIR = "logs/auto"

CONTROL_HOST = CONFIG["DRONE_HOST"]
CONTROL_PORT = CONFIG.get("DRONE_CONTROL_PORT", 48080)

APP_BIND_HOST = CONFIG.get("GCS_PLAINTEXT_HOST", "127.0.0.1")
APP_SEND_HOST = CONFIG.get("GCS_PLAINTEXT_HOST", "127.0.0.1")
APP_SEND_PORT = CONFIG.get("GCS_PLAINTEXT_TX", 47001)
APP_RECV_PORT = CONFIG.get("GCS_PLAINTEXT_RX", 47002)

VERIFY_TIMEOUT_S = float(CONFIG.get("SIMPLE_VERIFY_TIMEOUT_S", 5.0))
PACKETS_PER_SUITE = max(1, int(CONFIG.get("SIMPLE_PACKETS_PER_SUITE", 1)))
PACKET_DELAY_S = max(0.0, float(CONFIG.get("SIMPLE_PACKET_DELAY_S", 0.0)))
SUITE_DWELL_S = max(0.0, float(CONFIG.get("SIMPLE_SUITE_DWELL_S", 0.0)))
DEFAULT_PASSES = max(1, int(CONFIG.get("SIMPLE_AUTO_PASSES", 1)))

SUITES = sorted(suite_registry.SUITES.keys())
FIRST_SUITE = suite_registry.get_suite(CONFIG.get("SIMPLE_INITIAL_SUITE", SUITES[0]))["suite_id"]

def ts(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
def mkdirp(p): pathlib.Path(p).mkdir(parents=True, exist_ok=True)

def ctl(obj, timeout=3.0):
    with socket.create_connection((CONTROL_HOST, CONTROL_PORT), timeout=timeout) as s:
        s.sendall((json.dumps(obj)+"\n").encode()); s.shutdown(socket.SHUT_WR)
        line = s.makefile().readline()
        return json.loads(line.strip()) if line else {"ok": False, "error": "no reply"}

def send_and_wait_echo(payload: bytes, timeout_s: float):
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind((APP_BIND_HOST, APP_RECV_PORT))
    rx.settimeout(min(0.2, timeout_s))
    t0 = time.time_ns()
    tx.sendto(payload, (APP_SEND_HOST, APP_SEND_PORT))
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            data, _ = rx.recvfrom(65535)
            t1 = time.time_ns()
            tx.close(); rx.close()
            return True, t0, t1, len(data)
        except socket.timeout:
            pass
    tx.close(); rx.close()
    return False, t0, None, 0

def start_gcs_proxy(first_suite: str):
    os.environ["DRONE_HOST"] = CONFIG["DRONE_HOST"]
    os.environ["GCS_HOST"] = CONFIG["GCS_HOST"]
    os.environ["ENABLE_PACKET_TYPE"] = "1" if CONFIG.get("ENABLE_PACKET_TYPE", True) else "0"
    os.environ["STRICT_UDP_PEER_MATCH"] = "1" if CONFIG.get("STRICT_UDP_PEER_MATCH", True) else "0"

    keyfile = f"{SECRETS_DIR}/{first_suite}/gcs_signing.key"
    status = f"{OUTDIR}/{first_suite}/gcs_status.json"
    summary = f"{OUTDIR}/{first_suite}/gcs_summary.json"
    mkdirp(f"{OUTDIR}/{first_suite}")
    log = open(f"{OUTDIR}/gcs_{time.strftime('%Y%m%d-%H%M%S')}.log","w", encoding="utf-8", errors="replace")
    print(f"[scheduler] launching GCS proxy on suite {first_suite}")
    p = subprocess.Popen([
        sys.executable,"-m","core.run_proxy","gcs",
        "--suite", first_suite, "--gcs-secret-file", keyfile,
        "--control-manual","--status-file", status, "--json-out", summary
    ], stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True, bufsize=1)
    return p

def rekey(gcs_proc, suite: str):
    try:
        gcs_proc.stdin.write(suite + "\n"); gcs_proc.stdin.flush()
    except Exception as e:
        print(f"[scheduler] ERROR writing to proxy stdin: {e}", flush=True)
    try:
        ctl({"cmd":"mark","suite": suite})
    except Exception:
        pass

def run_one(gcs_proc, suite: str, writer, csv_file):
    print(f"[scheduler] ? {suite}")
    # If the proxy died, restart it
    try:
        if gcs_proc is None or (hasattr(gcs_proc, 'poll') and gcs_proc.poll() is not None):
            print('[scheduler] GCS proxy not running; restarting...')
            try:
                gcs_proc = start_gcs_proxy(suite)
                # give proxy a short moment to warm up
                time.sleep(0.5)
            except Exception as e:
                print(f"[scheduler] failed to restart proxy: {e}")
                return gcs_proc

    except Exception:
        # Defensive: if any introspection fails, proceed to attempt rekey and catch errors
        pass

    rekey(gcs_proc, suite)
    attempts = []
    for attempt_idx in range(PACKETS_PER_SUITE):
        seq = (attempt_idx + 1) & 0xFFFFFFFF
        payload = seq.to_bytes(4, "big") + int(time.time_ns()).to_bytes(8, "big")
        ok, t0, t1, n = send_and_wait_echo(payload, VERIFY_TIMEOUT_S)
        attempts.append((ok, t0, t1, n))
        if ok:
            print(f"[scheduler]   packet {attempt_idx+1}/{PACKETS_PER_SUITE} OK")
        else:
            print(f"[scheduler]   packet {attempt_idx+1}/{PACKETS_PER_SUITE} TIMEOUT")
        if PACKET_DELAY_S > 0 and attempt_idx < PACKETS_PER_SUITE - 1:
            time.sleep(PACKET_DELAY_S)

    successes = sum(1 for ok, *_ in attempts if ok)
    best_rtt = min((t1 - t0) for ok, t0, t1, _ in attempts if ok) if successes else ""
    last_bytes = next((n for ok, _, _, n in reversed(attempts) if ok), 0)
    note = "" if successes == PACKETS_PER_SUITE else "timeout"

    writer.writerow({
        "ts": ts(),
        "suite": suite,
        "packets": PACKETS_PER_SUITE,
        "success_packets": successes,
        "ok": successes == PACKETS_PER_SUITE,
        "best_rtt_ns": best_rtt,
        "bytes": last_bytes,
        "note": note,
    })
    csv_file.flush()

    if SUITE_DWELL_S > 0:
        time.sleep(SUITE_DWELL_S)
    return gcs_proc

def interactive_loop(gcs_proc):
    print("\nMANUAL MODE. Commands:")
    print("  list                - show suites")
    print("  next                - advance to the next suite in the list")
    print("  all                 - run full quick-pass across all suites once")
    print("  <suite-id>          - switch to a specific suite and test once")
    print("  quit                - exit\n")

    idx = 0
    csvp = f"{OUTDIR}/quickpass_summary.csv"
    have = os.path.exists(csvp)
    with open(csvp, "a", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "ts",
                "suite",
                "packets",
                "success_packets",
                "ok",
                "best_rtt_ns",
                "bytes",
                "note",
            ],
        )
        if not have: w.writeheader()
        while True:
            cmd = input("rekey> ").strip()
            if cmd == "quit": break
            if cmd == "list":
                for s in SUITES: print("  ", s)
                continue
            if cmd == "next":
                suite = SUITES[idx % len(SUITES)]; idx += 1
                gcs_proc = run_one(gcs_proc, suite, w, f)
                continue
            if cmd == "all":
                for suite in SUITES:
                    gcs_proc = run_one(gcs_proc, suite, w, f)
                print("[scheduler] full sweep done")
                continue
            # treat as suite-id
            try:
                target_suite = suite_registry.get_suite(cmd)["suite_id"]
            except NotImplementedError:
                print("Unknown command or suite. Type 'list'.")
                continue
            gcs_proc = run_one(gcs_proc, target_suite, w, f)
    return gcs_proc

def auto_sweep(gcs_proc, passes=DEFAULT_PASSES):
    csvp = f"{OUTDIR}/quickpass_summary.csv"
    have = os.path.exists(csvp)
    with open(csvp, "a", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "ts",
                "suite",
                "packets",
                "success_packets",
                "ok",
                "best_rtt_ns",
                "bytes",
                "note",
            ],
        )
        if not have: w.writeheader()
        for _ in range(passes):
            for suite in SUITES:
                gcs_proc = run_one(gcs_proc, suite, w, f)
    print("[scheduler] auto sweep complete")
    return gcs_proc

def main():
    pathlib.Path(OUTDIR).mkdir(parents=True, exist_ok=True)
    try:
        ctl({"cmd":"ping"})
        print(f"[scheduler] follower reachable at {CONTROL_HOST}:{CONTROL_PORT}")
    except Exception as e:
        print(f"[scheduler] WARNING follower not reachable: {e}")

    gcs = start_gcs_proxy(FIRST_SUITE)
    try:
        mode = "manual"
        if len(sys.argv) > 1 and sys.argv[1] == "--auto":
            mode = "auto"
        if mode == "manual":
            gcs = interactive_loop(gcs)
        else:
            gcs = auto_sweep(gcs, passes=DEFAULT_PASSES)
    finally:
        try: gcs.stdin.write("quit\n"); gcs.stdin.flush()
        except Exception: pass
        try: gcs.wait(timeout=2)
        except Exception: gcs.kill()
        try: ctl({"cmd":"stop"})
        except Exception: pass

if __name__ == "__main__":
    main()
