"""Shared helpers for traffic generators that exercise the plaintext sides of the PQC proxy."""
from __future__ import annotations

import json
import os
import selectors
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Literal, Tuple

from core.config import CONFIG

Role = Literal["gcs", "drone"]


def _timestamp() -> str:
    """Return an ISO-8601 timestamp with UTC timezone."""
    return datetime.now(timezone.utc).isoformat()


def load_ports_and_hosts(role: Role) -> Dict[str, object]:
    """Return resolved host/port information for the given role.

    All values originate from ``core.config.CONFIG`` after environment overrides
    have been applied. The returned dictionary contains:

    ``local_listen_ip`` – interface to bind UDP receivers (default ``0.0.0.0``).
    ``tx_addr`` – tuple of (host, port) for sending plaintext to the local proxy.
    ``rx_bind`` – tuple for binding the UDP receive socket.
    ``peer_role`` – the opposite role string.
    """

    role_upper = role.upper()
    peer_role = "drone" if role == "gcs" else "gcs"

    host_key_tx = f"{role_upper}_PLAINTEXT_HOST"
    host_key_rx = host_key_tx
    tx_port_key = f"{role_upper}_PLAINTEXT_TX"
    rx_port_key = f"{role_upper}_PLAINTEXT_RX"

    tx_host = CONFIG[host_key_tx]
    rx_host = CONFIG[host_key_rx]
    tx_port = CONFIG[tx_port_key]
    rx_port = CONFIG[rx_port_key]

    return {
        "local_listen_ip": os.environ.get("PQC_TRAFFIC_LISTEN_IP", "0.0.0.0"),
        "tx_addr": (tx_host, tx_port),
        "rx_bind": (os.environ.get("PQC_TRAFFIC_BIND_HOST", rx_host), rx_port),
        "peer_role": peer_role,
        "role_host": tx_host,
    }


def open_udp_socket(rx_bind: Tuple[str, int]) -> socket.socket:
    """Create a non-blocking UDP socket bound to ``rx_bind``."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass  # Not supported on all platforms (e.g., Windows prior to 10)
    sock.bind(rx_bind)
    sock.setblocking(False)
    return sock


def ndjson_logger(path: Path) -> Tuple[Callable[[Dict[str, object]], None], Callable[[], None]]:
    """Return a simple NDJSON logger factory returning (log_fn, close_fn)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fp = path.open("a", encoding="utf-8")

    def log(event: Dict[str, object]) -> None:
        payload = {"ts": _timestamp(), **event}
        fp.write(json.dumps(payload, separators=(",", ":")) + "\n")
        fp.flush()

    def close() -> None:
        fp.flush()
        os.fsync(fp.fileno())
        fp.close()

    return log, close


class TokenBucket:
    """Simple token bucket rate limiter."""

    def __init__(self, rate_per_sec: float) -> None:
        self.rate = max(rate_per_sec, 0.0)
        self.tokens = 0.0
        self.last = time.monotonic()

    def consume(self, now: float) -> bool:
        if self.rate <= 0:
            return True
        self.tokens = min(self.rate, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


def configured_selector(sock: socket.socket) -> selectors.BaseSelector:
    sel = selectors.DefaultSelector()
    sel.register(sock, selectors.EVENT_READ)
    return sel
*** End of File***