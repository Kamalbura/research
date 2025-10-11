#!/usr/bin/env python3
"""Simple telemetry collector that appends incoming JSON lines to a log file."""

from __future__ import annotations

import argparse
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path


def _ensure_core_importable() -> Path:
    root = Path(__file__).resolve().parents[2]
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    try:
        __import__("core")
    except ModuleNotFoundError as exc:  # pragma: no cover - safety belt
        raise RuntimeError(
            f"Unable to import 'core'; repo root {root} missing from sys.path."
        ) from exc
    return root


ROOT = _ensure_core_importable()

from core.config import CONFIG


def _default_host() -> str:
    return CONFIG.get("GCS_TELEMETRY_HOST", "0.0.0.0")


def _default_port() -> int:
    return int(CONFIG.get("GCS_TELEMETRY_PORT", 52080))


class _TelemetryServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler, output_file: Path) -> None:
        super().__init__(server_address, handler)
        self.output_file = output_file
        self.output_lock = threading.Lock()
        self.output_file.parent.mkdir(parents=True, exist_ok=True)

    def append(self, text: str) -> None:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        entry = f"{timestamp} {text}\n"
        with self.output_lock:
            with self.output_file.open("a", encoding="utf-8") as fh:
                fh.write(entry)

    def log(self, message: str) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        print(f"[{ts}] telemetry_collector {message}")


class _TelemetryHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: _TelemetryServer = self.server  # type: ignore[assignment]
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        server.log(f"connection from {peer}")
        try:
            with self.request.makefile("r", encoding="utf-8", buffering=1) as reader:
                for line in reader:
                    line = line.rstrip("\r\n")
                    if not line:
                        continue
                    server.append(line)
        except (ConnectionResetError, ConnectionAbortedError):
            server.log(f"connection to {peer} closed abruptly")
        except Exception as exc:  # pragma: no cover - diagnostic only
            server.log(f"connection to {peer} error: {exc}")
        finally:
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            self.request.close()
            server.log(f"connection from {peer} closed")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Append-only telemetry collector")
    parser.add_argument("--host", default=_default_host(), help="Host/interface to bind")
    parser.add_argument("--port", type=int, default=_default_port(), help="TCP port to bind")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "logs" / "auto" / "telemetry" / "telemetry.log",
        help="File to append telemetry lines",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    server = _TelemetryServer((args.host, args.port), _TelemetryHandler, args.output)
    server.log(f"listening on {args.host}:{args.port}")
    server.log(f"appending to {args.output}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.log("shutting down")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
