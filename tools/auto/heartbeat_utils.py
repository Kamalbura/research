from __future__ import annotations
import os
import hmac
import hashlib
import base64
import struct
import time
from typing import Optional, Tuple

# Configuration - override via env if needed
HEARTBEAT_KEY_ENV = "HEARTBEAT_KEY"
HEARTBEAT_INFO = b"ddos-heartbeat|v1"
HEARTBEAT_TAG_BYTES = 16
HEARTBEAT_TYPE = 1
HEARTBEAT_EPOCH_SECONDS = int(os.getenv("HEARTBEAT_EPOCH_SECONDS", "4"))
HEARTBEAT_WINDOW_STEPS_TOLERANCE = int(os.getenv("HEARTBEAT_WINDOW_STEPS_TOLERANCE", "1"))


def _derive_key_from_env_or_secret(session_secret: Optional[bytes]) -> bytes:
    env_key = os.environ.get(HEARTBEAT_KEY_ENV)
    if env_key:
        env_key = env_key.strip()
        try:
            return bytes.fromhex(env_key)
        except Exception:
            try:
                return base64.b64decode(env_key)
            except Exception:
                return env_key.encode("utf-8")
    if session_secret:
        prk = hmac.new(b"", session_secret, hashlib.sha256).digest()
        okm = hmac.new(prk, HEARTBEAT_INFO + b"\x01", hashlib.sha256).digest()
        return okm
    fallback = b"local-unsafe-heartbeat-key-default"
    return hashlib.sha256(fallback).digest()


def make_heartbeat_payload(session_secret: Optional[bytes] = None, epoch_time: Optional[int] = None, hb_type: int = HEARTBEAT_TYPE) -> bytes:
    if epoch_time is None:
        epoch_time = int(time.time())
    step = int(epoch_time) // HEARTBEAT_EPOCH_SECONDS
    key = _derive_key_from_env_or_secret(session_secret)
    header = struct.pack("!BQ", hb_type & 0xFF, int(step) & 0xFFFFFFFFFFFFFFFF)
    tag = hmac.new(key, header, hashlib.sha256).digest()[:HEARTBEAT_TAG_BYTES]
    return header + tag


def verify_heartbeat_payload(payload: bytes, session_secret: Optional[bytes] = None, allow_window: int = HEARTBEAT_WINDOW_STEPS_TOLERANCE) -> Tuple[bool, Optional[int], Optional[int]]:
    min_len = 1 + 8 + HEARTBEAT_TAG_BYTES
    if not payload or len(payload) < min_len:
        return False, None, None
    try:
        hb_type = payload[0]
        step = struct.unpack("!Q", payload[1:9])[0]
        recv_tag = payload[9:9 + HEARTBEAT_TAG_BYTES]
    except Exception:
        return False, None, None
    key = _derive_key_from_env_or_secret(session_secret)
    header = payload[:9]
    expected_full = hmac.new(key, header, hashlib.sha256).digest()[:HEARTBEAT_TAG_BYTES]
    ok = hmac.compare_digest(recv_tag, expected_full)
    if ok:
        return True, int(hb_type), int(step)
    # check nearby steps for skew
    for delta in range(1, max(1, allow_window) + 1):
        for s in (step - delta, step + delta):
            if s < 0:
                continue
            hdr = struct.pack("!BQ", hb_type & 0xFF, int(s) & 0xFFFFFFFFFFFFFFFF)
            if hmac.compare_digest(hmac.new(key, hdr, hashlib.sha256).digest()[:HEARTBEAT_TAG_BYTES], recv_tag):
                return True, int(hb_type), int(s)
    return False, int(hb_type), int(step)


def payload_to_b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def b64_to_payload(b64: str) -> bytes:
    return base64.b64decode(b64)
