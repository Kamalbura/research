#!/usr/bin/env python3
"""Linux-oriented capture helper for the drone host (Raspberry Pi).

Usage::

    python tools/netcapture/drone_capture.py --iface wlan0 --duration 30 --out captures/drone

The script shells out to ``tcpdump`` (ubiquitous on Linux) and applies
BPF filters for the PQC handshake TCP port and encrypted UDP ports defined in
``core.config.CONFIG``.  The resulting ``.pcap`` can be inspected with Wireshark
on any workstation.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

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

HANDSHAKE_PORT = int(CONFIG["TCP_HANDSHAKE_PORT"])
ENCRYPTED_PORTS = [int(CONFIG["UDP_GCS_RX"]), int(CONFIG["UDP_DRONE_RX"])]


class CaptureError(RuntimeError):
    pass


def ensure_linux() -> None:
    if sys.platform.startswith("win"):
        raise SystemExit("drone_capture.py is intended for Linux hosts only")


def tcpdump_available() -> bool:
    return shutil.which("tcpdump") is not None


def build_filter() -> str:
    ports = {HANDSHAKE_PORT, *ENCRYPTED_PORTS}
    clauses = []
    for port in sorted(ports):
        clauses.append(f"port {port}")
    return " or ".join(clauses)


def run_tcpdump(iface: str, pcap_path: Path, duration: int) -> None:
    if not tcpdump_available():
        raise CaptureError("tcpdump not found in PATH; install it (sudo apt install tcpdump)")

    bpf = build_filter()
    cmd: Iterable[str] = (
        "tcpdump",
        "-i",
        iface,
        "-w",
        str(pcap_path),
        "-G",
        str(duration),
        "-W",
        "1",
        "-n",
        bpf,
    )
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise CaptureError(f"tcpdump failed ({proc.returncode})\n{proc.stdout}")


def main() -> None:
    ensure_linux()

    ap = argparse.ArgumentParser(description="Capture handshake/encrypted traffic on the drone host")
    ap.add_argument("--iface", required=True, help="Network interface to capture (e.g., wlan0, eth0)")
    ap.add_argument("--duration", type=int, default=20, help="Capture duration in seconds (default: 20)")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("captures/drone.pcap"),
        help="Output pcap path (default: captures/drone.pcap)",
    )
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)

    try:
        run_tcpdump(args.iface, args.out, args.duration)
    except CaptureError as exc:
        print(f"\n❌ Capture failed: {exc}\n", file=sys.stderr)
        raise SystemExit(2) from exc

    print("\n✅ Capture complete:")
    print(f"  • {args.out}")
    print("\nTip: start this capture, then launch the proxy. Stop the proxy when you have enough packets, or rerun the capture for another segment.")


if __name__ == "__main__":
    main()
