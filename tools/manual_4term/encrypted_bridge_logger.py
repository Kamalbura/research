"""Encrypted UDP bridge logger for manual 4-terminal testing.

Listens on two UDP ports (drone->GCS and GCS->drone), forwards the
packets to their true destinations, and prints concise metadata so you
can verify encrypted traffic is flowing in both directions.
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Tuple

_LOG_BYTES_DEFAULT = 32
_BUFFER_SIZE = 2048


@dataclass
class BridgeConfig:
    listen_addr: Tuple[str, int]
    forward_addr: Tuple[str, int]
    label: str


def _format_bytes(data: bytes, limit: int) -> str:
    clipped = data[:limit]
    hex_preview = clipped.hex()
    if len(data) > limit:
        return f"{hex_preview}... ({len(data)} bytes)"
    return f"{hex_preview} ({len(data)} bytes)"


def _bridge_loop(cfg: BridgeConfig, log_bytes: int, shutdown: threading.Event) -> None:
    packet_count = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as forwarder:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(cfg.listen_addr)
        listener.settimeout(0.5)

        print(f"[{cfg.label}] Listening on {cfg.listen_addr[0]}:{cfg.listen_addr[1]} -> forwarding to {cfg.forward_addr[0]}:{cfg.forward_addr[1]}")
        while not shutdown.is_set():
            try:
                data, addr = listener.recvfrom(_BUFFER_SIZE)
            except socket.timeout:
                continue
            except OSError as exc:
                if not shutdown.is_set():
                    print(f"[{cfg.label}] Socket error: {exc}")
                break

            packet_count += 1
            timestamp = time.strftime("%H:%M:%S")
            preview = _format_bytes(data, log_bytes)
            print(f"[{cfg.label}] {timestamp} #{packet_count} from {addr[0]}:{addr[1]} -> {preview}")

            try:
                forwarder.sendto(data, cfg.forward_addr)
            except OSError as exc:
                print(f"[{cfg.label}] Forward error: {exc}")
                break

        print(f"[{cfg.label}] Shutdown (processed {packet_count} packets)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Log encrypted packets while forwarding between proxies")
    parser.add_argument("--d2g-listen", type=int, required=True, help="Port to bind for Drone -> GCS traffic")
    parser.add_argument("--d2g-forward", required=True, help="host:port to forward Drone -> GCS packets")
    parser.add_argument("--g2d-listen", type=int, required=True, help="Port to bind for GCS -> Drone traffic")
    parser.add_argument("--g2d-forward", required=True, help="host:port to forward GCS -> Drone packets")
    parser.add_argument("--log-bytes", type=int, default=_LOG_BYTES_DEFAULT, help="Number of ciphertext bytes to preview (default: %(default)s)")
    return parser.parse_args()


def _parse_host_port(value: str) -> Tuple[str, int]:
    if ":" not in value:
        raise ValueError(f"Expected host:port, got '{value}'")
    host, port_str = value.rsplit(":", 1)
    return host, int(port_str)


def main() -> None:
    args = parse_args()

    try:
        d2g_forward = _parse_host_port(args.d2g_forward)
        g2d_forward = _parse_host_port(args.g2d_forward)
    except ValueError as exc:
        print(f"Argument error: {exc}")
        sys.exit(1)

    shutdown = threading.Event()
    bridges = [
        BridgeConfig(("0.0.0.0", args.d2g_listen), d2g_forward, "Drone->GCS"),
        BridgeConfig(("0.0.0.0", args.g2d_listen), g2d_forward, "GCS->Drone"),
    ]

    threads = [threading.Thread(target=_bridge_loop, args=(cfg, args.log_bytes, shutdown), daemon=True) for cfg in bridges]

    print("Starting encrypted bridge logger. Press Ctrl+C to stop.")
    for thread in threads:
        thread.start()

    try:
        while any(thread.is_alive() for thread in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nInterrupt received, shutting down...")
        shutdown.set()
        for thread in threads:
            thread.join(timeout=1.0)


if __name__ == "__main__":
    main()
