#!/usr/bin/env python3
"""
Drone follower (no args required):
- Starts the drone proxy with the initial suite.
- Runs a UDP echo (recv -> send ports defined in CONFIG).
- Exposes a tiny TCP JSON control API on CONFIG["DRONE_CONTROL_HOST"/"DRONE_CONTROL_PORT"].

All networking parameters are sourced from core.config.CONFIG.
"""

import json, os, socket, threading, subprocess, sys, time, pathlib, signal

from core.config import CONFIG

CONTROL_HOST = CONFIG.get("DRONE_CONTROL_HOST", "0.0.0.0")
CONTROL_PORT = CONFIG.get("DRONE_CONTROL_PORT", 48080)

APP_BIND_HOST = CONFIG.get("DRONE_PLAINTEXT_HOST", "127.0.0.1")
APP_SEND_HOST = CONFIG.get("DRONE_PLAINTEXT_HOST", "127.0.0.1")
APP_SEND_PORT = CONFIG.get("DRONE_PLAINTEXT_TX", 47003)
APP_RECV_PORT = CONFIG.get("DRONE_PLAINTEXT_RX", 47004)

SECRETS_DIR = "secrets/matrix"
OUTDIR = "logs/auto/drone"
INITIAL_SUITE = CONFIG.get("SIMPLE_INITIAL_SUITE", "cs-mlkem768-aesgcm-mldsa65")

pathlib.Path(OUTDIR).mkdir(parents=True, exist_ok=True)
pathlib.Path(f"{OUTDIR}/marks").mkdir(parents=True, exist_ok=True)

def ts(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def start_drone_proxy(suite: str):
    os.environ["DRONE_HOST"] = CONFIG["DRONE_HOST"]
    os.environ["GCS_HOST"] = CONFIG["GCS_HOST"]
    os.environ["ENABLE_PACKET_TYPE"] = "1" if CONFIG.get("ENABLE_PACKET_TYPE", True) else "0"
    os.environ["STRICT_UDP_PEER_MATCH"] = "1" if CONFIG.get("STRICT_UDP_PEER_MATCH", True) else "0"

    pub = f"{SECRETS_DIR}/{suite}/gcs_signing.pub"
    if not os.path.exists(pub):
        print(f"[follower] ERROR: missing {pub}", flush=True)
        sys.exit(2)

    status = f"{OUTDIR}/status.json"
    summary = f"{OUTDIR}/summary.json"
    log = open(f"{OUTDIR}/drone_{time.strftime('%Y%m%d-%H%M%S')}.log","w")
    print(f"[follower] launching drone proxy on suite {suite}", flush=True)
    p = subprocess.Popen([
        sys.executable,"-m","core.run_proxy","drone",
        "--suite", suite, "--peer-pubkey-file", pub,
        "--status-file", status, "--json-out", summary
    ], stdout=log, stderr=subprocess.STDOUT, text=True)
    return p

def udp_echo(stop_evt: threading.Event):
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind((APP_BIND_HOST, APP_RECV_PORT))
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"[follower] UDP echo up: recv:{APP_BIND_HOST}:{APP_RECV_PORT} -> send:{APP_SEND_HOST}:{APP_SEND_PORT}", flush=True)
    rx.settimeout(0.2)
    while not stop_evt.is_set():
        try:
            data, _ = rx.recvfrom(65535)
            tx.sendto(data, (APP_SEND_HOST, APP_SEND_PORT))
        except socket.timeout:
            pass
    rx.close(); tx.close()
    print("[follower] UDP echo stopped", flush=True)

def control_server(stop_evt: threading.Event):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((CONTROL_HOST, CONTROL_PORT)); srv.listen(8)
    print(f"[follower] control listening on {CONTROL_HOST}:{CONTROL_PORT}", flush=True)
    while not stop_evt.is_set():
        srv.settimeout(0.2)
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        with conn:
            line = conn.makefile().readline()
            try:
                req = json.loads(line.strip()) if line else {}
            except Exception:
                req = {}
            resp = {"ok": True}
            if req.get("cmd") == "ping":
                resp = {"ok": True, "ts": ts()}
            elif req.get("cmd") == "mark":
                suite = req.get("suite","unknown")
                marker = f"{OUTDIR}/marks/{int(time.time())}_{suite}.json"
                with open(marker,"w") as f:
                    json.dump({"ts":ts(),"suite":suite}, f)
                resp = {"ok": True, "marked": suite}
            elif req.get("cmd") == "stop":
                stop_evt.set()
                resp = {"ok": True, "stopping": True}
            else:
                resp = {"ok": False, "error": "unknown_cmd"}
            conn.sendall((json.dumps(resp)+"\n").encode())
    srv.close()

def main():
    stop_evt = threading.Event()
    # start proxy once, GCS will rekey from there
    proxy = start_drone_proxy(INITIAL_SUITE)

    t_echo = threading.Thread(target=udp_echo, args=(stop_evt,), daemon=True)
    t_ctl  = threading.Thread(target=control_server, args=(stop_evt,), daemon=True)
    t_echo.start(); t_ctl.start()

    try:
        while not stop_evt.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    stop_evt.set()
    try:
        proxy.send_signal(signal.SIGTERM)
    except Exception:
        pass

if __name__ == "__main__":
    main()
