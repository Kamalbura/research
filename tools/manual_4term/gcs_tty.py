#!/usr/bin/env python3
"""Minimal interactive CLI for GCS plaintext tunnel."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading

from core.config import CONFIG

MAX_PAYLOAD = 4096


def ensure_newline(payload: bytes) -> bytes:
    if payload.endswith(b"\n"):
        return payload
    return payload + b"\n"


def truncate_payload(payload: bytes) -> bytes:
    if len(payload) <= MAX_PAYLOAD:
        return payload
    trimmed = payload[:MAX_PAYLOAD]
    if trimmed[-1:] != b"\n":
        trimmed = trimmed[:-1] + b"\n"
    return trimmed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interactive GCS plaintext console")
    parser.add_argument("--host", default=CONFIG.get("GCS_HOST", "127.0.0.1"), help="Destination host for plaintext commands")
    parser.add_argument("--tx-port", type=int, default=CONFIG["GCS_PLAINTEXT_TX"], help="Port to send plaintext commands")
    parser.add_argument("--rx-port", type=int, default=CONFIG["GCS_PLAINTEXT_RX"], help="Port receiving telemetry lines")
    parser.add_argument("--expect", type=int, default=0, help="Exit automatically after receiving N lines")
    parser.add_argument("--verbose", action="store_true", help="Enable debug output to stderr")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    done = threading.Event()
    recv_count = 0
    recv_lock = threading.Lock()

    rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx_sock.bind(("0.0.0.0", args.rx_port))
    rx_sock.settimeout(0.1)

    tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def debug(msg: str) -> None:
        if args.verbose:
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()

    def reader() -> None:
        nonlocal recv_count
        while not done.is_set():
            try:
                data, _ = rx_sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                continue
            trimmed = data[:MAX_PAYLOAD]
            text = trimmed.decode("utf-8", errors="replace")
            if not text.endswith("\n"):
                text += "\n"
            sys.stdout.write(text)
            sys.stdout.flush()
            if args.expect:
                with recv_lock:
                    recv_count += 1
                    if recv_count >= args.expect:
                        done.set()
                        os._exit(0)
        try:
            rx_sock.close()
        except OSError:
            pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    try:
        for line in sys.stdin:
            if done.is_set():
                break
            encoded = ensure_newline(line.encode("utf-8", errors="replace"))
            encoded = truncate_payload(encoded)
            try:
                tx_sock.sendto(encoded, (args.host, args.tx_port))
            except OSError as exc:
                debug(f"sendto failed: {exc}")
                break
    except KeyboardInterrupt:
        pass
    finally:
        done.set()
        try:
            tx_sock.close()
        except OSError:
            pass
        thread.join(timeout=0.2)


if __name__ == "__main__":
    main()
