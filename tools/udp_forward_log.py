#!/usr/bin/env python3
"""UDP forwarder that logs PQC header metadata while keeping traffic flowing."""

from __future__ import annotations

import argparse
import socket
import struct
import time

HEADER_STRUCT = "!BBBBB8sQB"
HEADER_LEN = struct.calcsize(HEADER_STRUCT)


def parse_header(data: bytes):
    if len(data) < HEADER_LEN:
        return None
    try:
        version, kem_id, kem_param, sig_id, sig_param, session_id, seq, epoch = struct.unpack(
            HEADER_STRUCT, data[:HEADER_LEN]
        )
        return {
            "version": version,
            "kem": (kem_id, kem_param),
            "sig": (sig_id, sig_param),
            "session_id": session_id.hex(),
            "seq": seq,
            "epoch": epoch,
        }
    except Exception:
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="UDP forwarder with PQC header logging")
    parser.add_argument("--listen", required=True, help="host:port to bind (e.g., 0.0.0.0:46012)")
    parser.add_argument("--forward", required=True, help="host:port to forward to (e.g., 127.0.0.1:56012)")
    parser.add_argument("--label", default="tap", help="Log label for output prefix")
    return parser


def parse_host_port(value: str) -> tuple[str, int]:
    host, port_str = value.rsplit(":", 1)
    return host, int(port_str)


def main() -> None:
    args = build_parser().parse_args()
    listen_host, listen_port = parse_host_port(args.listen)
    forward_host, forward_port = parse_host_port(args.forward)

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        rx.bind((listen_host, listen_port))
        print(f"[{args.label}] listening on {listen_host}:{listen_port} -> forwarding to {forward_host}:{forward_port}")

        while True:
            data, addr = rx.recvfrom(65535)
            meta = parse_header(data)
            ts = time.strftime("%H:%M:%S")
            if meta:
                print(
                    f"[{ts}][{args.label}] {len(data)}B from {addr[0]}:{addr[1]} "
                    f"hdr={{'version': {meta['version']}, 'kem': {meta['kem']}, 'sig': {meta['sig']}, "
                    f"'session_id': '{meta['session_id']}', 'seq': {meta['seq']}, 'epoch': {meta['epoch']}}}"
                )
            else:
                print(
                    f"[{ts}][{args.label}] {len(data)}B from {addr[0]}:{addr[1]} hdr=? first16={data[:16].hex()}"
                )
            tx.sendto(data, (forward_host, forward_port))
    except KeyboardInterrupt:
        pass
    finally:
        rx.close()
        tx.close()


if __name__ == "__main__":
    main()
