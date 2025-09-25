"""
Selectors-based network transport proxy.

Responsibilities:
1. Perform authenticated TCP handshake (PQC KEM + signature) using `core.handshake`.
2. Bridge plaintext UDP <-> encrypted UDP (AEAD framing) both directions.
3. Enforce replay window and per-direction sequence via `core.aead`.

Note: This module uses the low-level `selectors` stdlib facility—not `asyncio`—to
remain dependency-light and fully deterministic for test harnesses. The filename
is retained for backward compatibility; a future refactor may rename it to
`selector_proxy.py` and/or introduce an asyncio variant.
"""

from __future__ import annotations

import socket
import selectors
import threading
import time
import struct
from typing import Optional, Dict, Tuple
from contextlib import contextmanager

from core.config import CONFIG
from core.suites import SUITES, header_ids_for_suite
try:
    # Optional helper (if you implemented it)
    from core.suites import header_ids_from_names  # type: ignore
except Exception:
    header_ids_from_names = None  # type: ignore

from core.handshake import server_gcs_handshake, client_drone_handshake
from core.logging_utils import get_logger

from core.aead import (
    Sender,
    Receiver,
    HeaderMismatch,
    ReplayError,
    AeadAuthError,
    AeadIds,
)

from core.policy_engine import handle_control

logger = get_logger("pqc")


class ProxyCounters:
    """Simple counters for proxy statistics."""

    def __init__(self) -> None:
        self.ptx_out = 0      # plaintext packets sent out to app
        self.ptx_in = 0       # plaintext packets received from app
        self.enc_out = 0      # encrypted packets sent to peer
        self.enc_in = 0       # encrypted packets received from peer
        self.drops = 0        # total drops
        # Granular drop reasons
        self.drop_replay = 0
        self.drop_auth = 0
        self.drop_header = 0
        self.drop_session_epoch = 0
        self.drop_other = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "ptx_out": self.ptx_out,
            "ptx_in": self.ptx_in,
            "enc_out": self.enc_out,
            "enc_in": self.enc_in,
            "drops": self.drops,
            "drop_replay": self.drop_replay,
            "drop_auth": self.drop_auth,
            "drop_header": self.drop_header,
            "drop_session_epoch": self.drop_session_epoch,
            "drop_other": self.drop_other,
        }


def _dscp_to_tos(dscp: Optional[int]) -> Optional[int]:
    """Convert DSCP value to TOS byte for socket options."""
    if dscp is None:
        return None
    try:
        d = int(dscp)
        if 0 <= d <= 63:
            return d << 2  # DSCP occupies high 6 bits of TOS/Traffic Class
    except Exception:
        pass
    return None


def _parse_header_fields(
    expected_version: int,
    aead_ids: AeadIds,
    session_id: bytes,
    wire: bytes,
) -> Tuple[str, Optional[int]]:
    """
    Try to unpack the header and classify the most likely drop reason *without* AEAD work.
    Returns (reason, seq_if_available).
    """
    HEADER_STRUCT = "!BBBBB8sQB"
    HEADER_LEN = struct.calcsize(HEADER_STRUCT)
    if len(wire) < HEADER_LEN:
        return ("header_too_short", None)
    try:
        (version, kem_id, kem_param, sig_id, sig_param, sess, seq, epoch) = struct.unpack(
            HEADER_STRUCT, wire[:HEADER_LEN]
        )
    except struct.error:
        return ("header_unpack_error", None)
    if version != expected_version:
        return ("version_mismatch", seq)
    if (kem_id, kem_param, sig_id, sig_param) != (
        aead_ids.kem_id,
        aead_ids.kem_param,
        aead_ids.sig_id,
        aead_ids.sig_param,
    ):
        return ("crypto_id_mismatch", seq)
    if sess != session_id:
        return ("session_mismatch", seq)
    # If we got here, header matches; any decrypt failure that returns None is auth/tag failure.
    return ("auth_fail_or_replay", seq)


class _TokenBucket:
    """Per-IP rate limiter using token bucket algorithm."""
    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        self.capacity = max(1, capacity)
        self.refill = max(0.01, float(refill_per_sec))
        self.tokens: Dict[str, float] = {}      # ip -> tokens
        self.last: Dict[str, float] = {}        # ip -> last timestamp

    def allow(self, ip: str) -> bool:
        """Check if request from IP should be allowed."""
        now = time.monotonic()
        t = self.tokens.get(ip, self.capacity)
        last = self.last.get(ip, now)
        # refill
        t = min(self.capacity, t + (now - last) * self.refill)
        self.last[ip] = now
        if t >= 1.0:
            t -= 1.0
            self.tokens[ip] = t
            return True
        self.tokens[ip] = t
        return False


def _validate_config(cfg: dict) -> None:
    """Validate required configuration keys are present."""
    required_keys = [
        "TCP_HANDSHAKE_PORT", "UDP_DRONE_RX", "UDP_GCS_RX",
        "DRONE_PLAINTEXT_TX", "DRONE_PLAINTEXT_RX",
        "GCS_PLAINTEXT_TX", "GCS_PLAINTEXT_RX",
        "DRONE_HOST", "GCS_HOST", "REPLAY_WINDOW",
    ]
    for key in required_keys:
        if key not in cfg:
            raise NotImplementedError(f"CONFIG missing: {key}")


def _perform_handshake(
    role: str,
    suite: dict,
    gcs_sig_secret: Optional[bytes],
    gcs_sig_public: Optional[bytes],
    cfg: dict,
    stop_after_seconds: Optional[float] = None,
    ready_event: Optional[threading.Event] = None,
) -> Tuple[bytes, bytes, bytes, bytes, bytes, Optional[str], Optional[str]]:
    """Perform TCP handshake and return derived keys, session_id, and optionally kem/sig names."""
    if role == "gcs":
        if gcs_sig_secret is None:
            raise NotImplementedError("GCS signature secret not provided")

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("0.0.0.0", cfg["TCP_HANDSHAKE_PORT"]))
        server_sock.listen(32)

        if ready_event:
            ready_event.set()

        timeout = stop_after_seconds if stop_after_seconds is not None else 30.0
        server_sock.settimeout(timeout)

        gate = _TokenBucket(
            cfg.get("HANDSHAKE_RL_BURST", 5),
            cfg.get("HANDSHAKE_RL_REFILL_PER_SEC", 1),
        )

        try:
            try:
                conn, addr = server_sock.accept()
                try:
                    ip, _port = addr
                    if not gate.allow(ip):
                        try:
                            conn.settimeout(0.2)
                            conn.sendall(b"\x00")
                        except Exception:
                            pass
                        finally:
                            conn.close()
                        raise NotImplementedError("Handshake rate-limit: too many attempts")

                    result = server_gcs_handshake(conn, suite, gcs_sig_secret)
                    # Support either 5-tuple or 7-tuple
                    if len(result) >= 7:
                        k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id, kem_name, sig_name = result[:7]
                    else:
                        k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id = result
                        kem_name = sig_name = None
                    return k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id, kem_name, sig_name
                finally:
                    conn.close()
            except socket.timeout:
                raise NotImplementedError("No drone connection received within timeout")
        finally:
            server_sock.close()

    elif role == "drone":
        if gcs_sig_public is None:
            raise NotImplementedError("GCS signature public key not provided")

        client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client_sock.connect((cfg["GCS_HOST"], cfg["TCP_HANDSHAKE_PORT"]))
            result = client_drone_handshake(client_sock, suite, gcs_sig_public)
            if len(result) >= 7:
                k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id, kem_name, sig_name = result[:7]
            else:
                k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id = result
                kem_name = sig_name = None
            return k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id, kem_name, sig_name
        finally:
            client_sock.close()
    else:
        raise ValueError(f"Invalid role: {role}")


@contextmanager
def _setup_sockets(role: str, cfg: dict):
    """Setup and cleanup all UDP sockets for the proxy."""
    sockets = {}
    try:
        if role == "drone":
            # Encrypted socket - receive from GCS
            enc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            enc_sock.bind(("0.0.0.0", cfg["UDP_DRONE_RX"]))
            enc_sock.setblocking(False)
            tos = _dscp_to_tos(cfg.get("ENCRYPTED_DSCP"))
            if tos is not None:
                try:
                    enc_sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
                except Exception:
                    pass
            sockets["encrypted"] = enc_sock

            # Plaintext ingress - receive from local app
            ptx_in_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ptx_in_sock.bind(("127.0.0.1", cfg["DRONE_PLAINTEXT_TX"]))
            ptx_in_sock.setblocking(False)
            sockets["plaintext_in"] = ptx_in_sock

            # Plaintext egress - send to local app (no bind needed)
            ptx_out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets["plaintext_out"] = ptx_out_sock

            # Peer addresses
            sockets["encrypted_peer"] = (cfg["GCS_HOST"], cfg["UDP_GCS_RX"])
            sockets["plaintext_peer"] = ("127.0.0.1", cfg["DRONE_PLAINTEXT_RX"])

        elif role == "gcs":
            # Encrypted socket - receive from Drone
            enc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            enc_sock.bind(("0.0.0.0", cfg["UDP_GCS_RX"]))
            enc_sock.setblocking(False)
            tos = _dscp_to_tos(cfg.get("ENCRYPTED_DSCP"))
            if tos is not None:
                try:
                    enc_sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)
                except Exception:
                    pass
            sockets["encrypted"] = enc_sock

            # Plaintext ingress - receive from local app
            ptx_in_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ptx_in_sock.bind(("127.0.0.1", cfg["GCS_PLAINTEXT_TX"]))
            ptx_in_sock.setblocking(False)
            sockets["plaintext_in"] = ptx_in_sock

            # Plaintext egress - send to local app (no bind needed)
            ptx_out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets["plaintext_out"] = ptx_out_sock

            # Peer addresses
            sockets["encrypted_peer"] = (cfg["DRONE_HOST"], cfg["UDP_DRONE_RX"])
            sockets["plaintext_peer"] = ("127.0.0.1", cfg["GCS_PLAINTEXT_RX"])
        else:
            raise ValueError(f"Invalid role: {role}")

        yield sockets
    finally:
        for sock in list(sockets.values()):
            if isinstance(sock, socket.socket):
                try:
                    sock.close()
                except Exception:
                    pass


def run_proxy(
    *,
    role: str,
    suite: dict,
    cfg: dict,
    gcs_sig_secret: Optional[bytes] = None,
    gcs_sig_public: Optional[bytes] = None,
    stop_after_seconds: Optional[float] = None,
    ready_event: Optional[threading.Event] = None,
) -> Dict[str, int]:
    """
    Start a blocking proxy process for `role` in {"drone","gcs"}.

    - Performs TCP handshake (server on GCS, client on Drone).
    - Bridges plaintext UDP <-> encrypted UDP in both directions.
    - Returns a dict of simple counters on clean exit:
      {"ptx_out": int, "ptx_in": int, "enc_out": int, "enc_in": int, "drops": int}
    """
    if role not in {"drone", "gcs"}:
        raise ValueError(f"Invalid role: {role}")

    _validate_config(cfg)

    counters = ProxyCounters()
    start_time = time.time()

    # Perform handshake and get session keys (+ optional kem/sig names)
    k_d2g, k_g2d, _nseed_d2g, _nseed_g2d, session_id, kem_name, sig_name = _perform_handshake(
        role, suite, gcs_sig_secret, gcs_sig_public, cfg, stop_after_seconds, ready_event
    )

    # Log successful handshake
    try:
        suite_id = next((sid for sid, s in SUITES.items() if dict(s) == suite), "unknown")
    except Exception:
        suite_id = "unknown"
    logger.info(
        "PQC handshake completed successfully",
        extra={"suite_id": suite_id, "peer_role": ("drone" if role == "gcs" else "gcs"), "session_id": session_id.hex()},
    )

    # Setup AEAD header IDs
    if kem_name and sig_name and header_ids_from_names:
        ids_tuple = header_ids_from_names(kem_name, sig_name)  # type: ignore
    else:
        ids_tuple = header_ids_for_suite(suite)
    aead_ids = AeadIds(*ids_tuple)

    # Role-based key directions
    if role == "drone":
        sender = Sender(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, k_d2g)
        receiver = Receiver(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, k_g2d, cfg["REPLAY_WINDOW"])
    else:  # gcs
        sender = Sender(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, k_g2d)
        receiver = Receiver(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, k_d2g, cfg["REPLAY_WINDOW"])

    # UDP bridge loop
    with _setup_sockets(role, cfg) as sockets:
        selector = selectors.DefaultSelector()
        selector.register(sockets["encrypted"], selectors.EVENT_READ, data="encrypted")
        selector.register(sockets["plaintext_in"], selectors.EVENT_READ, data="plaintext_in")

        try:
            while True:
                if stop_after_seconds is not None and (time.time() - start_time) >= stop_after_seconds:
                    break

                events = selector.select(timeout=0.1)
                for key, _mask in events:
                    sock = key.fileobj
                    data_type = key.data

                    if data_type == "plaintext_in":
                        # Plaintext ingress: encrypt and forward
                        try:
                            payload, _addr = sock.recvfrom(2048)
                            if not payload:
                                continue
                            counters.ptx_in += 1

                            payload_out = (b"\x01" + payload) if cfg.get("ENABLE_PACKET_TYPE") else payload
                            wire = sender.encrypt(payload_out)

                            try:
                                sockets["encrypted"].sendto(wire, sockets["encrypted_peer"])
                                counters.enc_out += 1
                            except socket.error:
                                counters.drops += 1
                        except socket.error:
                            continue

                    elif data_type == "encrypted":
                        try:
                            wire, _addr = sock.recvfrom(2048)
                            if not wire:
                                continue
                            counters.enc_in += 1

                            try:
                                plaintext = receiver.decrypt(wire)
                                if plaintext is None:
                                    reason, _seq = _parse_header_fields(
                                        CONFIG["WIRE_VERSION"], receiver.ids, receiver.session_id, wire
                                    )
                                    counters.drops += 1
                                    if reason in ("version_mismatch", "crypto_id_mismatch", "header_too_short", "header_unpack_error"):
                                        counters.drop_header += 1
                                    elif reason == "session_mismatch":
                                        counters.drop_session_epoch += 1
                                    else:
                                        counters.drop_auth += 1
                                    continue
                            except ReplayError:
                                counters.drops += 1
                                counters.drop_replay += 1
                                continue
                            except HeaderMismatch:
                                counters.drops += 1
                                counters.drop_header += 1
                                continue
                            except AeadAuthError:
                                counters.drops += 1
                                counters.drop_auth += 1
                                continue
                            except Exception:
                                counters.drops += 1
                                counters.drop_other += 1
                                continue

                            try:
                                out_bytes = plaintext
                                if cfg.get("ENABLE_PACKET_TYPE") and plaintext:
                                    ptype = plaintext[0]
                                    if ptype == 0x01:
                                        out_bytes = plaintext[1:]  # deliver to app
                                    elif ptype == 0x02:
                                        _ = handle_control(plaintext[1:])
                                        continue
                                    else:
                                        counters.drops += 1
                                        counters.drop_other += 1
                                        continue

                                sockets["plaintext_out"].sendto(out_bytes, sockets["plaintext_peer"])
                                counters.ptx_out += 1
                            except socket.error:
                                counters.drops += 1
                                counters.drop_other += 1
                        except socket.error:
                            continue
        except KeyboardInterrupt:
            pass
        finally:
            selector.close()

    return counters.to_dict()
