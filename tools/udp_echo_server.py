#!/usr/bin/env python3
r"""Simple UDP echo server to test network connectivity.

This replaces the complex pktmon capture with a basic UDP listener that
will tell us definitively if packets are reaching the Windows machine.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

# Ensure repository root is on sys.path when executed directly
_HERE = Path(__file__).resolve()
for parent in (_HERE.parent.parent.parent, _HERE.parent.parent):
    try:
        if (parent / "core").exists():
            parent_str = str(parent)
            if parent_str not in sys.path:
                sys.path.insert(0, parent_str)
            break
    except Exception:
        pass

from core.config import CONFIG


def main() -> None:
    parser = argparse.ArgumentParser(description="UDP Echo Server for Firewall Testing")
    parser.add_argument("--port", type=int, default=CONFIG["UDP_GCS_RX"], help="UDP port to listen on")
    parser.add_argument("--host", default="0.0.0.0", help="Host IP to bind to")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout in seconds")
    args = parser.parse_args()

    print(f"--- UDP Echo Server ---")
    print(f"🚀 Listening for UDP packets on {args.host}:{args.port} for {args.timeout} seconds...")
    print(f"Send a packet from the Pi with: echo 'TEST' | nc -u -w1 <GCS_IP> {args.port}")

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind((args.host, args.port))
            s.settimeout(args.timeout)
            
            while True:
                try:
                    data, addr = s.recvfrom(2048)
                    timestamp = time.strftime("%H:%M:%S")
                    print(f"\n✅ [{timestamp}] Received '{data.decode()}' from {addr[0]}:{addr[1]}")
                    
                    # Echo back
                    s.sendto(b"ECHO:" + data, addr)
                    print(f"🚀 Echoed back to sender")
                except socket.timeout:
                    print("\n⏰ Timeout reached. No packets received.")
                    break
                except KeyboardInterrupt:
                    print("\n🛑 Stopped by user.")
                    break

        except Exception as e:
            print(f"\n❌ FAILED: {e}")
            return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())