#!/usr/bin/env python3
"""
GCS scheduler (interactive by default; --auto runs whole list):
- Starts the GCS proxy in --control-manual with the first suite.
- For each chosen suite: rekey -> send one UDP -> wait for echo -> immediately proceed
- Coordinates with the drone follower at 192.168.0.102:48080 ("ping","mark","stop").

No IP/port flags needed—constants below match your LAN.
"""

import os, sys, time, json, csv, pathlib, socket, subprocess

# ------- constants (your LAN) -------
GCS_HOST = "192.168.0.101"
DRONE_HOST = "192.168.0.102"
ENABLE_PACKET_TYPE = "1"
STRICT_UDP_PEER_MATCH = "1"

CONTROL_HOST = DRONE_HOST
CONTROL_PORT = 48080

APP_SEND_PORT = 47001  # local send (to proxy)
APP_RECV_PORT = 47002  # local recv (from proxy)

SECRETS_DIR = "secrets/matrix"
OUTDIR = "logs/auto"
VERIFY_TIMEOUT_S = 5.0
DEFAULT_PASSES = 1
# ------------------------------------

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
    rx.bind(("0.0.0.0", APP_RECV_PORT))
    rx.settimeout(min(0.2, timeout_s))
    t0 = time.time_ns()
    tx.sendto(payload, ("127.0.0.1", APP_SEND_PORT))
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
    os.environ["DRONE_HOST"] = DRONE_HOST
    os.environ["GCS_HOST"] = GCS_HOST
    os.environ["ENABLE_PACKET_TYPE"] = ENABLE_PACKET_TYPE
    os.environ["STRICT_UDP_PEER_MATCH"] = STRICT_UDP_PEER_MATCH

    keyfile = f"{SECRETS_DIR}/{first_suite}/gcs_signing.key"
    status = f"{OUTDIR}/{first_suite}/gcs_status.json"
    summary = f"{OUTDIR}/{first_suite}/gcs_summary.json"
    mkdirp(f"{OUTDIR}/{first_suite}")
    log = open(f"{OUTDIR}/gcs_{time.strftime('%Y%m%d-%H%M%S')}.log","w")
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

def run_one(gcs_proc, suite: str, writer):
    print(f"[scheduler] ? {suite}")
    rekey(gcs_proc, suite)
    payload = (1).to_bytes(4,"big") + int(time.time_ns()).to_bytes(8,"big")
    ok, t0, t1, n = send_and_wait_echo(payload, VERIFY_TIMEOUT_S)
    if ok:
        rtt = t1 - t0
        writer.writerow({"ts": ts(), "suite": suite, "ok": True, "rtt_ns": rtt, "bytes": n})
        print(f"[scheduler]   echo OK, rtt_ns={rtt}")
    else:
        writer.writerow({"ts": ts(), "suite": suite, "ok": False, "rtt_ns": "", "bytes": 0})
        print(f"[scheduler]   echo TIMEOUT")

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
        w = csv.DictWriter(f, fieldnames=["ts","suite","ok","rtt_ns","bytes"])
        if not have: w.writeheader()
        while True:
            cmd = input("rekey> ").strip()
            if cmd == "quit": break
            if cmd == "list":
                for s in SUITES: print("  ", s)
                continue
            if cmd == "next":
                suite = SUITES[idx % len(SUITES)]; idx += 1
                run_one(gcs_proc, suite, w); f.flush()
                continue
            if cmd == "all":
                for suite in SUITES:
                    run_one(gcs_proc, suite, w); f.flush()
                print("[scheduler] full sweep done")
                continue
            # treat as suite-id
            if cmd in SUITES:
                run_one(gcs_proc, cmd, w); f.flush()
            else:
                print("Unknown command or suite. Type 'list'.")

def auto_sweep(gcs_proc, passes=DEFAULT_PASSES):
    csvp = f"{OUTDIR}/quickpass_summary.csv"
    have = os.path.exists(csvp)
    with open(csvp, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts","suite","ok","rtt_ns","bytes"])
        if not have: w.writeheader()
        for _ in range(passes):
            for suite in SUITES:
                run_one(gcs_proc, suite, w); f.flush()
    print("[scheduler] auto sweep complete")

def main():
    pathlib.Path(OUTDIR).mkdir(parents=True, exist_ok=True)
    try:
        ctl({"cmd":"ping"})
        print(f"[scheduler] follower reachable at {CONTROL_HOST}:{CONTROL_PORT}")
    except Exception as e:
        print(f"[scheduler] WARNING follower not reachable: {e}")

    first = SUITES[0]
    gcs = start_gcs_proxy(first)
    try:
        mode = "manual"
        if len(sys.argv) > 1 and sys.argv[1] == "--auto":
            mode = "auto"
        if mode == "manual":
            interactive_loop(gcs)
        else:
            auto_sweep(gcs, passes=DEFAULT_PASSES)
    finally:
        try: gcs.stdin.write("quit\n"); gcs.stdin.flush()
        except Exception: pass
        try: gcs.wait(timeout=2)
        except Exception: gcs.kill()
        try: ctl({"cmd":"stop"})
        except Exception: pass

if __name__ == "__main__":
    main()
