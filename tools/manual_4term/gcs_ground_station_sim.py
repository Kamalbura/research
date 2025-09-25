"""Minimal GCS ground-station simulator for manual quad-terminal tests.

Sends a rotating set of high-level commands to the GCS proxy plaintext
port and prints any telemetry frames returned from the drone proxy via
GCS_PLAINTEXT_RX.
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from typing import List

_DEFAULT_COMMANDS: List[str] = [
    "CMD_ARM",
    "CMD_TAKEOFF_ALT_30",
    "CMD_SET_HEADING_090",
    "CMD_LOITER_HOLD",
    "CMD_RTL",
]

_BUFFER_SIZE = 2048


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate a GCS app talking to the proxy")
    parser.add_argument("--send-port", type=int, required=True, help="Port where GCS proxy listens for plaintext commands")
    parser.add_argument("--recv-port", type=int, required=True, help="Port where GCS proxy delivers decrypted telemetry")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback host for both directions (default: %(default)s)")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between commands (default: %(default)s)")
    parser.add_argument("--loop", action="store_true", help="Loop command list forever (default: stop after one pass)")
    return parser.parse_args()


def command_loop(sock: socket.socket, host: str, port: int, interval: float, loop: bool, shutdown: threading.Event) -> None:
    print(f"[GCS] Sending plaintext commands to {host}:{port}")
    while not shutdown.is_set():
        for command in _DEFAULT_COMMANDS:
            try:
                payload = command.encode("utf-8")
                sock.sendto(payload, (host, port))
                timestamp = time.strftime("%H:%M:%S")
                print(f"[GCS] {timestamp} -> {command}")
            except OSError as exc:
                print(f"[GCS] Send error: {exc}")
                shutdown.set()
                break
            if shutdown.wait(interval):
                break
        if not loop:
            break
    print("[GCS] Command loop stopped")


def telemetry_loop(sock: socket.socket, shutdown: threading.Event) -> None:
    print("[GCS] Listening for decrypted telemetry...")
    sock.settimeout(0.5)
    while not shutdown.is_set():
        try:
            data, addr = sock.recvfrom(_BUFFER_SIZE)
        except socket.timeout:
            continue
        except OSError as exc:
            if not shutdown.is_set():
                print(f"[GCS] Receive error: {exc}")
            break
        timestamp = time.strftime("%H:%M:%S")
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = data.hex()
        print(f"[GCS] {timestamp} <- {text} (from {addr[0]}:{addr[1]})")
    print("[GCS] Telemetry listener stopped")


def main() -> None:
    args = parse_args()

    shutdown = threading.Event()

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as send_sock, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as recv_sock:
        recv_sock.bind(("0.0.0.0", args.recv_port))

        sender = threading.Thread(target=command_loop, args=(send_sock, args.host, args.send_port, args.interval, args.loop, shutdown), daemon=True)
        receiver = threading.Thread(target=telemetry_loop, args=(recv_sock, shutdown), daemon=True)

        sender.start()
        receiver.start()

        print("[GCS] Ground-station simulator running. Press Ctrl+C to exit.")
        try:
            while sender.is_alive() or receiver.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[GCS] Interrupt received, shutting down")
            shutdown.set()
            sender.join(timeout=1.0)
            receiver.join(timeout=1.0)


if __name__ == "__main__":
    main()
