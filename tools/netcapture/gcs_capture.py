#!/usr/bin/env python3
r"""Windows-oriented capture helper for the GCS host.

Usage examples
--------------
Collect a 30s capture of handshake + encrypted ports into ``captures\gcs``::

    python tools/netcapture/gcs_capture.py --duration 30 --out captures/gcs

The script prefers ``pktmon`` (ships with Windows 10 2004+) and falls back to
``netsh trace``.  It tries to add filters for the PQC handshake and encrypted
UDP ports defined in ``core.config.CONFIG`` so the traces stay focused.

Outputs
-------
* ``<out>.etl``        Raw ETW capture (always produced)
* ``<out>.pcapng``     Packet capture (when ``pktmon`` is available)
* ``<out>.log``        Text summary (when ``pktmon`` is available)

Prerequisites
-------------
* Run from an elevated PowerShell / Command Prompt (admin rights).
* ``pktmon`` or ``netsh`` must be available in ``PATH`` (Windows built-ins).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
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


def run(cmd: Iterable[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and proc.returncode != 0:
        raise CaptureError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout}")
    return proc


def ensure_windows() -> None:
    if sys.platform != "win32":
        raise SystemExit("gcs_capture.py is intended for Windows hosts only")


def pktmon_available() -> bool:
    return shutil.which("pktmon") is not None


def netsh_available() -> bool:
    return shutil.which("netsh") is not None


def build_default_output(out_base: Path) -> tuple[Path, Path, Path]:
    etl = out_base.with_suffix(".etl")
    pcap = out_base.with_suffix(".pcapng")
    log = out_base.with_suffix(".log")
    return etl, pcap, log


def run_pktmon(out_base: Path, duration: int) -> list[Path]:
    etl, pcap, log = build_default_output(out_base)

    # Reset previous state to keep output predictable
    run(["pktmon", "stop"], check=False)
    run(["pktmon", "reset"], check=False)

    # Apply lightweight port filters so we only capture the PQC traffic
    filter_ports = sorted({HANDSHAKE_PORT, *ENCRYPTED_PORTS})
    for port in filter_ports:
        run(["pktmon", "filter", "add", "--port", str(port)])

    run(["pktmon", "start", "--etw", "--capture"])
    time.sleep(duration)
    run(["pktmon", "stop"])

    temp_etl = Path("PktMon.etl")
    if temp_etl.exists():
        temp_etl.replace(etl)
    else:
        raise CaptureError("pktmon did not produce PktMon.etl")

    run(["pktmon", "format", str(etl), "-o", str(pcap)])
    run(["pktmon", "format", str(etl), "-o", str(log), "--text"])

    run(["pktmon", "reset"], check=False)
    return [etl, pcap, log]


def run_netsh(out_base: Path, duration: int) -> list[Path]:
    if not netsh_available():
        raise CaptureError("Neither pktmon nor netsh is available; cannot capture")

    etl, _, _ = build_default_output(out_base)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir) / "trace"
        run(
            [
                "netsh",
                "trace",
                "start",
                "capture=yes",
                "tracefile=" + str(tmp),
                "report=no",
                "maxsize=512",
            ]
        )
        time.sleep(duration)
        run(["netsh", "trace", "stop"])
        raw = tmp.with_suffix(".etl")
        if raw.exists():
            raw.replace(etl)
        else:
            raise CaptureError("netsh trace did not produce an .etl file")
    return [etl]


def main() -> None:
    ensure_windows()

    ap = argparse.ArgumentParser(description="Capture handshake/encrypted traffic on the GCS host")
    ap.add_argument("--duration", type=int, default=20, help="Capture duration in seconds (default: 20)")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("captures/gcs_capture"),
        help="Output file base name (extensions added automatically)",
    )
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)

    try:
        if pktmon_available():
            produced = run_pktmon(args.out, args.duration)
        else:
            produced = run_netsh(args.out, args.duration)
    except CaptureError as exc:
        print(f"\n❌ Capture failed: {exc}\n", file=sys.stderr)
        raise SystemExit(2) from exc

    print("\n✅ Capture complete. Generated files:")
    for path in produced:
        print(f"  • {path}")
    print(
        "\nTip: start this capture, then launch the proxy. Stop the proxy and re-run the capture if you need multiple segments."
    )


if __name__ == "__main__":
    main()
