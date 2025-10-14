from __future__ import annotations

"""Lightweight UDP heartbeat channel for passive/active DDOS signalling.

Design:
- Pre-encrypted, fixed-size payload blobs are stored on disk (generated offline)
- Sender transmits one blob every interval seconds to a configured host:port
- Receiver validates payloads by constant-time compare against an allowlist
- Stop after N consecutive send failures; expose last status for dataset fusion

This module is intentionally decoupled from core/ transport to avoid any wire
compatibility changes. Integrate from schedulers or tools/ as an auxiliary
channel. Do not log secrets or payload bytes.
"""

import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


def _load_blobs(path: Path) -> list[bytes]:
    blobs: list[bytes] = []
    if not path.exists():
        return blobs
    for child in sorted(path.glob("*.bin")):
        try:
            data = child.read_bytes()
            if data:
                blobs.append(data)
        except Exception:
            continue
    return blobs


@dataclass
class HeartbeatConfig:
    host: str
    port: int
    interval_s: float = 2.0
    retry_limit: int = 5
    payload_dir: Optional[Path] = None  # Directory containing pre-encrypted .bin files


class HeartbeatSender:
    def __init__(self, cfg: HeartbeatConfig) -> None:
        self.cfg = cfg
        self._blobs = _load_blobs(cfg.payload_dir) if cfg.payload_dir else []
        self._last_ok = False
        self._consecutive_failures = 0
        self._idx = 0

    @property
    def last_ok(self) -> bool:
        return self._last_ok

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def send_once(self) -> bool:
        payload = self._select_payload()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.5)
                s.sendto(payload, (self.cfg.host, self.cfg.port))
            self._last_ok = True
            self._consecutive_failures = 0
            return True
        except Exception:
            self._last_ok = False
            self._consecutive_failures += 1
            return False

    def run(self, stop_time: Optional[float] = None) -> None:
        while True:
            if stop_time is not None and time.time() >= stop_time:
                break
            ok = self.send_once()
            if not ok and self._consecutive_failures >= self.cfg.retry_limit:
                break
            time.sleep(max(0.05, self.cfg.interval_s))

    def _select_payload(self) -> bytes:
        if self._blobs:
            blob = self._blobs[self._idx % len(self._blobs)]
            self._idx += 1
            return blob
        # Fallback: zero-filled minimal payload (non-secret)
        return b"\x00" * 16


class HeartbeatReceiver:
    def __init__(self, host: str, port: int, allowlist: Optional[Iterable[bytes]] = None) -> None:
        self.host = host
        self.port = port
        self.allow = tuple(allowlist or ())
        self.last_recv_ts: Optional[float] = None
        self.last_valid: bool = False

    def listen_once(self, timeout_s: float = 0.5) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind((self.host, self.port))
                s.settimeout(timeout_s)
                data, _addr = s.recvfrom(4096)
        except Exception:
            self.last_valid = False
            return False
        self.last_recv_ts = time.time()
        if not self.allow:
            # If no allowlist provided, accept any non-empty payload
            self.last_valid = bool(data)
            return self.last_valid
        for ref in self.allow:
            # Constant-time compare by length + XOR reduction
            if len(ref) == len(data):
                acc = 0
                for a, b in zip(ref, data):
                    acc |= a ^ b
                if acc == 0:
                    self.last_valid = True
                    return True
        self.last_valid = False
        return False
