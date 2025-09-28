#!/usr/bin/env python3
"""
GCS matrix scheduler + traffic + rekey driver.

- Starts GCS proxy with --control-manual (interactive stdin).
- Ensures the drone follower is up via TCP control (48080).
- For each suite in the list:
    * if first: launch GCS proxy with that suite
      else: write suite id + '\n' to proxy stdin to trigger in-band rekey
    * tell the drone to "mark" to rotate perf/pidstat to the new suite
    * run UDP traffic for D seconds at rate R (pps) on local app ports
    * log NDJSON events (send/recv) for RTT/loss under logs/auto/<suite>/
- Produces per-suite JSON/NDJSON + one CSV summary.

Run:
  python -m tools.auto.gcs_scheduler \
    --gcs 192.168.0.101 --drone 192.168.0.102 \
    --control-port 48080 \
    --app-send-port 47001 --app-recv-port 47002 \
    --duration 25 --rate 100
"""
import argparse, json, os, pathlib, socket, subprocess, sys, threading, time, csv

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

# ---- Control client (to drone) ----
def ctl_send(host, port, obj, timeout=2.0):
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall((json.dumps(obj)+"\n").encode())
        s.shutdown(socket.SHUT_WR)
        line = s.makefile().readline()
        return json.loads(line.strip()) if line else {}

# ---- Traffic driver ----
class UdpTraffic:
    def __init__(self, send_port:int, recv_port:int, out_events_path:str, rate_pps:int):
        self.send_addr = ("127.0.0.1", send_port)
        self.recv_addr = ("0.0.0.0", recv_port)
        self.rate_pps = rate_pps
        self.stop = threading.Event()
        self.out = open(out_events_path, "w")
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(self.recv_addr)
        self.sent = 0; self.rcvd = 0

    def sender(self):
        interval = 1.0 / max(self.rate_pps,1)
        seq = 0
        while not self.stop.is_set():
            payload = seq.to_bytes(4,"big") + int(time.time_ns()).to_bytes(8,"big")
            try:
                self.tx.sendto(payload, self.send_addr)
                self.out.write(json.dumps({"event":"send","seq":seq,"t_send_ns":time.time_ns()})+"\n")
                self.out.flush()
                self.sent += 1
            except Exception as e:
                self.out.write(json.dumps({"event":"send_error","err":str(e),"ts":ts()})+"\n"); self.out.flush()
            seq += 1
            time.sleep(interval)

    def receiver(self):
        self.rx.settimeout(0.2)
        while not self.stop.is_set():
            try:
                data, _ = self.rx.recvfrom(65535)
                now = time.time_ns()
                seq = int.from_bytes(data[:4],"big") if len(data) >= 4 else -1
                self.out.write(json.dumps({"event":"recv","seq":seq,"t_recv_ns":now})+"\n"); self.out.flush()
                self.rcvd += 1
            except socket.timeout:
                pass
            except Exception as e:
                self.out.write(json.dumps({"event":"recv_error","err":str(e),"ts":ts()})+"\n"); self.out.flush()

    def start(self):
        self.t1 = threading.Thread(target=self.sender, daemon=True); self.t1.start()
        self.t2 = threading.Thread(target=self.receiver, daemon=True); self.t2.start()

    def stop_and_close(self):
        self.stop.set()
        for t in (self.t1, self.t2): 
            if t.is_alive(): t.join(timeout=1.0)
        self.out.close()
        self.tx.close(); self.rx.close()

def wait_handshake(status_file, timeout=15):
    deadline = time.time()+timeout
    while time.time()<deadline:
        if os.path.exists(status_file):
            try:
                js = json.load(open(status_file))
                if js.get("state") in ("running","completed","ready"):
                    return True
            except Exception: pass
        time.sleep(0.3)
    return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gcs", required=True)
    ap.add_argument("--drone", required=True)
    ap.add_argument("--control-port", type=int, default=48080)
    ap.add_argument("--app-send-port", type=int, default=47001)
    ap.add_argument("--app-recv-port", type=int, default=47002)
    ap.add_argument("--duration", type=int, default=25, help="seconds per suite")
    ap.add_argument("--rate", type=int, default=100, help="pps")
    ap.add_argument("--outdir", default="logs/auto")
    ap.add_argument("--secrets-dir", default="secrets/matrix")
    ap.add_argument("--suites", nargs="*", default=SUITES)
    args = ap.parse_args()

    os.environ["DRONE_HOST"] = args.drone
    os.environ["GCS_HOST"] = args.gcs
    os.environ["ENABLE_PACKET_TYPE"] = "1"
    os.environ["STRICT_UDP_PEER_MATCH"] = "1"

    def sdir(s): p=f"{args.outdir}/{s}"; mkdirp(p); return p

    # Launch GCS proxy on first suite with manual control
    first = args.suites[0]
    key = f"{args.secrets_dir}/{first}/gcs_signing.key"
    status_file = f"{sdir(first)}/gcs_status.json"
    summary_file = f"{sdir(first)}/gcs_summary.json"
    gcs_log = open(f"{args.outdir}/gcs_{time.strftime('%Y%m%d-%H%M%S')}.log","w")
    gcs = subprocess.Popen([
        sys.executable,"-m","core.run_proxy","gcs",
        "--suite", first,
        "--gcs-secret-file", key,
        "--control-manual",
        "--status-file", status_file,
        "--json-out", summary_file
    ], stdin=subprocess.PIPE, stdout=gcs_log, stderr=subprocess.STDOUT, text=True, bufsize=1)

    # Ensure the drone follower is up; rotate monitors for the first suite
    try:
        ctl_send(args.drone, args.control_port, {"cmd":"ping"})
        ctl_send(args.drone, args.control_port, {"cmd":"mark","suite": first})
    except Exception as e:
        print(f"[WARN] control ping failed: {e}", file=sys.stderr)

    # Wait handshake / readiness
    ok = wait_handshake(status_file, timeout=20)
    print(f"[{ts()}] initial handshake ready? {ok}")

    # Traffic per suite
    summary_rows = []
    for idx, suite in enumerate(args.suites):
        if idx>0:
            # Rekey by writing suite id + newline to proxy stdin
            print(f"[{ts()}] rekey -> {suite}")
            gcs.stdin.write(suite + "\n"); gcs.stdin.flush()
            # rotate drone monitors to the new suite
            try:
                ctl_send(args.drone, args.control_port, {"cmd":"mark","suite": suite})
            except Exception as e:
                print(f"[WARN] mark failed: {e}", file=sys.stderr)
            # Switch output files for status/summary to keep per-suite artifacts
            status_file = f"{sdir(suite)}/gcs_status.json"
            summary_file = f"{sdir(suite)}/gcs_summary.json"
            # (run_proxy will keep writing to the originally provided files; if you
            #  prefer per-suite files from the proxy, relaunch per suite instead of rekeying.)

        # Start traffic
        events_path = f"{sdir(suite)}/gcs_events.jsonl"
        traf = UdpTraffic(args.app_send_port, args.app_recv_port, events_path, args.rate)
        start_ns = time.time_ns()
        traf.start()
        time.sleep(args.duration)
        traf.stop_and_close()
        end_ns = time.time_ns()

        # Read current proxy summary (best-effort)
        js = {}
        try:
            if os.path.exists(summary_file):
                js = json.load(open(summary_file))
        except Exception: pass

        summary_rows.append({
            "suite": suite,
            "duration_s": args.duration,
            "sent": traf.sent,
            "rcvd": traf.rcvd,
            "start_ns": start_ns,
            "end_ns": end_ns,
            "enc_out": js.get("enc_out",0),
            "enc_in": js.get("enc_in",0),
            "drops": js.get("drops",0),
            "rekeys_ok": js.get("rekeys_ok",0),
            "rekeys_fail": js.get("rekeys_fail",0),
        })
        print(f"[{ts()}] {suite}: sent={traf.sent} rcvd={traf.rcvd} enc_out={js.get('enc_out')} enc_in={js.get('enc_in')}")

    # Write CSV rollup
    csv_path = f"{args.outdir}/summary.csv"
    with open(csv_path,"w",newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        w.writeheader(); w.writerows(summary_rows)
    print(f"[{ts()}] wrote {csv_path}")

    # Clean shutdown
    try:
        ctl_send(args.drone, args.control_port, {"cmd":"stop"})
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

if __name__ == "__main__":
    main()
