"""Drone-side simulator for manual quad-terminal tests.

Generates telemetry frames towards the drone proxy and prints any
commands received from the GCS proxy.
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from typing import List

_TELEMETRY_FRAMES: List[str] = [
    "TELEM:POS:37.7749,-122.4194,ALT=120",
    "TELEM:ATT:ROLL=1.2,PITCH=-0.3,YAW=90",
    "TELEM:VEL:N=5.1,E=0.4,D=-0.2",
    "TELEM:BAT:V=23.9,I=12.3,SOC=87",
]

_BUFFER_SIZE = 2048


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate a drone autopilot connected to the proxy")
    parser.add_argument("--send-port", type=int, required=True, help="Port where drone proxy listens for plaintext telemetry")
    parser.add_argument("--recv-port", type=int, required=True, help="Port where drone proxy delivers decrypted commands")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback host for both directions (default: %(default)s)")
    parser.add_argument("--interval", type=float, default=1.5, help="Seconds between telemetry frames (default: %(default)s)")
    parser.add_argument("--loop", action="store_true", help="Loop telemetry frames forever (default: stop after one pass)")
    return parser.parse_args()


def telemetry_loop(sock: socket.socket, host: str, port: int, interval: float, loop: bool, shutdown: threading.Event) -> None:
    print(f"[DRONE] Sending telemetry to {host}:{port}")
    while not shutdown.is_set():
        for frame in _TELEMETRY_FRAMES:
            try:
                payload = frame.encode("utf-8")
                sock.sendto(payload, (host, port))
                timestamp = time.strftime("%H:%M:%S")
                print(f"[DRONE] {timestamp} -> {frame}")
            except OSError as exc:
                print(f"[DRONE] Send error: {exc}")
                shutdown.set()
                break
            if shutdown.wait(interval):
                break
        if not loop:
            break
    print("[DRONE] Telemetry loop stopped")


def command_loop(sock: socket.socket, shutdown: threading.Event) -> None:
    print("[DRONE] Listening for decrypted commands...")
    sock.settimeout(0.5)
    while not shutdown.is_set():
        try:
            data, addr = sock.recvfrom(_BUFFER_SIZE)
        except socket.timeout:
            continue
        except OSError as exc:
            if not shutdown.is_set():
                print(f"[DRONE] Receive error: {exc}")
            break
        timestamp = time.strftime("%H:%M:%S")
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = data.hex()
        print(f"[DRONE] {timestamp} <- {text} (from {addr[0]}:{addr[1]})")
    print("[DRONE] Command listener stopped")


def main() -> None:
    args = parse_args()

    shutdown = threading.Event()

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as send_sock, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as recv_sock:
        recv_sock.bind(("0.0.0.0", args.recv_port))

        sender = threading.Thread(target=telemetry_loop, args=(send_sock, args.host, args.send_port, args.interval, args.loop, shutdown), daemon=True)
        receiver = threading.Thread(target=command_loop, args=(recv_sock, shutdown), daemon=True)

        sender.start()
        receiver.start()

        print("[DRONE] Autopilot simulator running. Press Ctrl+C to exit.")
        try:
            while sender.is_alive() or receiver.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[DRONE] Interrupt received, shutting down")
            shutdown.set()
            sender.join(timeout=1.0)
            receiver.join(timeout=1.0)


if __name__ == "__main__":
    main()
