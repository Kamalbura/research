#!/usr/bin/env python3
"""
Drone follower/loopback + control server.

- Starts the drone proxy (core.run_proxy drone) pinned to an initial suite
  and the matching GCS public key from secrets/matrix/<suite>/gcs_signing.pub.
- Runs a UDP echo server bound to DRONE_APP_RECV_PORT that bounces any payload
  back to the sender (the local proxy), creating a true encrypted loopback path.
- Exposes a small TCP JSON control API so the GCS scheduler can:
    * ping / status
    * rotate per-suite log files ("mark") when the GCS rekeys
    * start/stop perf & pidstat tied to the proxy PID
- Writes all artifacts under logs/auto/<suite>/ on the Pi.

Run:
  python -m tools.auto.drone_follower \
    --gcs 192.168.0.101 --drone 192.168.0.102 \
    --control-port 48080 \
    --initial-suite cs-mlkem768-aesgcm-mldsa65 \
    --app-recv-port 47004 \
    --outdir logs/auto \
    --secrets-dir secrets/matrix
"""
import argparse, json, os, socket, threading, time, subprocess, sys, pathlib, signal, shlex

# ---------- Config defaults (override by CLI) ----------
DEFAULT_CONTROL_PORT = 48080
DEFAULT_APP_RECV_PORT = 47004   # drone side plaintext recv port (through proxy)
PERF_EVENTS = "task-clock,cycles,instructions,cache-misses,context-switches"

# ---------- Helpers ----------
def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def mkdirp(p):
    pathlib.Path(p).mkdir(parents=True, exist_ok=True)

def popen(cmd, **kw):
    print(f"[{ts()}] exec: {cmd}", flush=True)
    return subprocess.Popen(cmd, **kw)

def killtree(p: subprocess.Popen):
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

# ---------- UDP Echo (loopback) ----------
class UdpEcho(threading.Thread):
    def __init__(self, port, stop_event):
        super().__init__(daemon=True)
        self.port = port
        self.stop_event = stop_event
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", self.port))

    def run(self):
        print(f"[{ts()}] UDP echo listening on 0.0.0.0:{self.port}", flush=True)
        self.sock.settimeout(0.5)
        while not self.stop_event.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
                # Echo to sender (local proxy)
                self.sock.sendto(data, addr)
            except socket.timeout:
                pass
            except Exception as e:
                print(f"[{ts()}] UDP echo error: {e}", flush=True)

# ---------- Perf/Pidstat ----------
class Monitors:
    def __init__(self):
        self.perf = None
        self.pidstat = None

    def start(self, pid:int, outdir:str, suite:str):
        mkdirp(outdir)
        # perf stat (1s) -> CSV-like
        perf_cmd = f"perf stat -I 1000 -e {PERF_EVENTS} -p {pid} --log-fd 1"
        self.perf = popen(shlex.split(perf_cmd), stdout=open(f"{outdir}/perf_{suite}.csv","w"), stderr=subprocess.STDOUT)
        # pidstat (1s)
        self.pidstat = popen(["pidstat","-hlur","-p",str(pid),"1"], stdout=open(f"{outdir}/pidstat_{suite}.txt","w"), stderr=subprocess.STDOUT)

    def rotate(self, pid:int, outdir:str, suite:str):
        self.stop()
        self.start(pid, outdir, suite)

    def stop(self):
        killtree(self.perf); self.perf = None
        killtree(self.pidstat); self.pidstat = None

# ---------- Control Server ----------
class ControlServer(threading.Thread):
    """
    Simple line-delimited JSON over TCP.
    Requests:
      {"cmd":"ping"}
      {"cmd":"status"}
      {"cmd":"mark","suite":"cs-..."}              -> rotate monitor files to new suite
      {"cmd":"stop"}                               -> stop monitors (keeps proxy+echo)
    Replies:
      {"ok":true, ...} or {"ok":false,"error":"..."}
    """
    def __init__(self, host, port, state):
        super().__init__(daemon=True)
        self.host, self.port = host, port
        self.state = state
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(5)

    def run(self):
        print(f"[{ts()}] Control server on {self.host}:{self.port}", flush=True)
        while not self.state["stop_event"].is_set():
            try:
                self.sock.settimeout(0.5)
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def send(self, conn, obj):
        conn.sendall((json.dumps(obj)+"\n").encode())

    def handle(self, conn: socket.socket):
        f = conn.makefile("rwb", buffering=0)
        try:
            line = f.readline()
            if not line:
                return
            req = json.loads(line.decode().strip() or "{}")
            cmd = req.get("cmd")
            if cmd == "ping":
                self.send(conn, {"ok": True, "ts": ts()})
            elif cmd == "status":
                p = self.state["proxy"]
                self.send(conn, {
                    "ok": True,
                    "suite": self.state["suite"],
                    "proxy_pid": (p.pid if p else None),
                    "running": (p and p.poll() is None),
                })
            elif cmd == "mark":
                suite = req.get("suite")
                if not suite:
                    self.send(conn, {"ok": False, "error": "missing suite"})
                else:
                    self.state["suite"] = suite
                    pid = self.state["proxy"].pid if self.state["proxy"] and self.state["proxy"].poll() is None else None
                    if pid is None:
                        self.send(conn, {"ok": False, "error": "proxy not running"})
                    else:
                        self.state["mon"].rotate(pid, self.state["suite_outdir"](suite), suite)
                        self.send(conn, {"ok": True, "rotated": suite})
            elif cmd == "stop":
                self.state["mon"].stop()
                self.send(conn, {"ok": True})
            else:
                self.send(conn, {"ok": False, "error": f"unknown cmd {cmd}"})
        except Exception as e:
            try: self.send(conn, {"ok": False, "error": str(e)})
            except Exception: pass
        finally:
            try: conn.close()
            except: pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gcs", required=True, help="GCS host ip")
    ap.add_argument("--drone", required=True, help="Drone host ip (this Pi)")
    ap.add_argument("--control-port", type=int, default=DEFAULT_CONTROL_PORT)
    ap.add_argument("--initial-suite", required=True)
    ap.add_argument("--secrets-dir", default="secrets/matrix")
    ap.add_argument("--app-recv-port", type=int, default=DEFAULT_APP_RECV_PORT)
    ap.add_argument("--outdir", default="logs/auto")
    args = ap.parse_args()

    os.environ["DRONE_HOST"] = args.drone
    os.environ["GCS_HOST"] = args.gcs
    os.environ["ENABLE_PACKET_TYPE"] = "1"
    os.environ["STRICT_UDP_PEER_MATCH"] = "1"

    stop_event = threading.Event()

    def suite_outdir(suite): 
        p = f"{args.outdir}/{suite}"
        mkdirp(p); return p

    # Start drone proxy for initial suite
    pub = f"{args.secrets_dir}/{args.initial_suite}/gcs_signing.pub"
    if not os.path.exists(pub):
        print(f"ERROR: missing pubkey {pub}", file=sys.stderr); sys.exit(2)
    drone_sum = f"{suite_outdir(args.initial_suite)}/drone_summary.json"
    log = open(f"{args.outdir}/drone_{time.strftime('%Y%m%d-%H%M%S')}.log","w")
    proxy = popen([
        sys.executable, "-m", "core.run_proxy", "drone",
        "--suite", args.initial_suite,
        "--peer-pubkey-file", pub,
        "--json-out", drone_sum
    ], stdout=log, stderr=subprocess.STDOUT)

    # Start monitors & echo
    mon = Monitors()
    time.sleep(1)
    if proxy.poll() is None:
        mon.start(proxy.pid, suite_outdir(args.initial_suite), args.initial_suite)
    echo = UdpEcho(args.app_recv_port, stop_event); echo.start()

    # Control server
    state = {
        "proxy": proxy,
        "suite": args.initial_suite,
        "suite_outdir": suite_outdir,
        "mon": mon,
        "stop_event": stop_event,
    }
    ctl = ControlServer("0.0.0.0", args.control_port, state); ctl.start()

    # Wait forever / until SIGINT
    try:
        while True:
            time.sleep(0.5)
            if proxy.poll() is not None:
                print(f"[{ts()}] proxy exited with {proxy.returncode}", flush=True)
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        mon.stop()
        killtree(proxy)

if __name__ == "__main__":
    main()
