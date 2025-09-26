#!/usr/bin/env python3
"""Bi-directional UDP probe to verify ports and paths end-to-end.

Run this on both hosts (GCS and Drone) at the same time. It will:
- Bind a local RX port and print every packet received (with source IP:port).
- Periodically send numbered messages to the peer's RX port.
- Log exactly which local ephemeral source port each message leaves from.

Defaults are taken from core.config.CONFIG for the encrypted path (UDP_GCS_RX/UDP_DRONE_RX)
so you can prove the tunnel ports themselves are reachable. You can target plaintext
ports as well with --mode plaintext.

Examples:
  # GCS side (listens on UDP_GCS_RX, sends to DRONE_HOST:UDP_DRONE_RX)
  python tools/udp_dual_probe.py --role gcs --mode encrypted

  # Drone side (listens on UDP_DRONE_RX, sends to GCS_HOST:UDP_GCS_RX)
  python tools/udp_dual_probe.py --role drone --mode encrypted

Stop with Ctrl+C.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from pathlib import Path

# Ensure repository root is on sys.path when executed directly
_HERE = Path(__file__).resolve()
for parent in (_HERE.parent, _HERE.parent.parent):
    try:
        if (parent / "core").exists():
            p = str(parent)
            if p not in sys.path:
                sys.path.insert(0, p)
            break
    except Exception:
        pass

from core.config import CONFIG


def build_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Bi-directional UDP probe")
    ap.add_argument("--role", choices=["gcs", "drone"], required=True)
    ap.add_argument("--mode", choices=["encrypted", "plaintext"], default="encrypted")
    ap.add_argument("--interval", type=float, default=1.0, help="Seconds between sends")
    ap.add_argument("--count", type=int, default=10, help="Messages to send before exit (0 = infinite)")
    return ap.parse_args()


essential = {
    "gcs": {
        "encrypted_rx": int(CONFIG["UDP_GCS_RX"]),
        "plaintext_tx": int(CONFIG["GCS_PLAINTEXT_TX"]),
        "plaintext_rx": int(CONFIG["GCS_PLAINTEXT_RX"]),
        "peer_host": CONFIG["DRONE_HOST"],
        "peer_encrypted_rx": int(CONFIG["UDP_DRONE_RX"]),
    },
    "drone": {
        "encrypted_rx": int(CONFIG["UDP_DRONE_RX"]),
        "plaintext_tx": int(CONFIG["DRONE_PLAINTEXT_TX"]),
        "plaintext_rx": int(CONFIG["DRONE_PLAINTEXT_RX"]),
        "peer_host": CONFIG["GCS_HOST"],
        "peer_encrypted_rx": int(CONFIG["UDP_GCS_RX"]),
    },
}


def run_probe(role: str, mode: str, interval: float, count: int) -> None:
    cfg = essential[role]

    if mode == "encrypted":
        local_rx_port = cfg["encrypted_rx"]
        peer_host = cfg["peer_host"]
        peer_rx_port = cfg["peer_encrypted_rx"]
        label = "ENC"
    else:
        # plaintext runs on loopback only for each host
        local_rx_port = cfg["plaintext_rx"]
        peer_host = "127.0.0.1"
        peer_rx_port = cfg["plaintext_tx"]
        label = "PTX"

    print(f"[{role.upper()}][{label}] RX bind on 0.0.0.0:{local_rx_port}")
    print(f"[{role.upper()}][{label}] Will send to {peer_host}:{peer_rx_port}")

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("0.0.0.0", local_rx_port))
    rx.settimeout(0.2)

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # For visibility, bind tx to an ephemeral port so we know the source
    tx.bind(("0.0.0.0", 0))

    stop = threading.Event()

    def receiver() -> None:
        while not stop.is_set():
            try:
                data, addr = rx.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            ts = time.strftime("%H:%M:%S")
            print(f"[{role.upper()}][{label}][{ts}] RX {len(data)}B from {addr[0]}:{addr[1]} :: {data[:64]!r}")

    t = threading.Thread(target=receiver, daemon=True)
    t.start()

    try:
        i = 0
        while count == 0 or i < count:
            i += 1
            ts = time.strftime("%H:%M:%S")
            try:
                # Print the local source address/port before sending
                src_host, src_port = tx.getsockname()
                payload = f"{label}_MSG_{i}@{ts}".encode()
                tx.sendto(payload, (peer_host, peer_rx_port))
                print(f"[{role.upper()}][{label}][{ts}] TX {len(payload)}B from {src_host}:{src_port} -> {peer_host}:{peer_rx_port}")
            except Exception as exc:
                print(f"[{role.upper()}][{label}] TX error: {exc}")
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        t.join(timeout=0.3)
        try:
            rx.close()
            tx.close()
        except OSError:
            pass


if __name__ == "__main__":
    args = build_args()
    run_probe(args.role, args.mode, args.interval, args.count)
