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

import hashlib
import json
import queue
import socket
import selectors
import struct
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from core.config import CONFIG
from core.suites import SUITES, get_suite, header_ids_for_suite, list_suites
try:
    # Optional helper (if you implemented it)
    from core.suites import header_ids_from_names  # type: ignore
except Exception:
    header_ids_from_names = None  # type: ignore

from core.handshake import HandshakeVerifyError, client_drone_handshake, server_gcs_handshake
from core.logging_utils import get_logger

from core.aead import (
    AeadAuthError,
    AeadIds,
    HeaderMismatch,
    Receiver,
    ReplayError,
    Sender,
)

from core.policy_engine import (
    ControlResult,
    ControlState,
    create_control_state,
    handle_control,
    record_rekey_result,
    request_prepare,
)

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
        self.drop_src_addr = 0
        self.rekeys_ok = 0
        self.rekeys_fail = 0
        self.last_rekey_ms = 0
        self.last_rekey_suite: Optional[str] = None
        self.handshake_metrics: Dict[str, object] = {}
        self._primitive_templates = {
            "count": 0,
            "total_ns": 0,
            "min_ns": None,
            "max_ns": 0,
            "total_in_bytes": 0,
            "total_out_bytes": 0,
        }
        self.primitive_metrics: Dict[str, Dict[str, object]] = {
            "aead_encrypt": dict(self._primitive_templates),
            "aead_decrypt_ok": dict(self._primitive_templates),
            "aead_decrypt_fail": dict(self._primitive_templates),
        }

    @staticmethod
    def _ns_to_ms(value: object) -> float:
        try:
            ns = float(value)
        except (TypeError, ValueError):
            return 0.0
        if ns <= 0.0:
            return 0.0
        return round(ns / 1_000_000.0, 6)

    def _part_b_metrics(self) -> Dict[str, object]:
        handshake = self.handshake_metrics
        if not isinstance(handshake, dict) or not handshake:
            return {}

        primitives = handshake.get("primitives") or {}
        if not isinstance(primitives, dict):
            primitives = {}

        kem = primitives.get("kem") if isinstance(primitives.get("kem"), dict) else {}
        sig = primitives.get("signature") if isinstance(primitives.get("signature"), dict) else {}
        artifacts = handshake.get("artifacts") if isinstance(handshake.get("artifacts"), dict) else {}

        summary: Dict[str, object] = {}

        def _emit(prefix: str, source: Dict[str, object], key: str, legacy_key: Optional[str] = None) -> None:
            ns_value = source.get(key)
            ms_value = self._ns_to_ms(ns_value)
            summary[f"{prefix}_max_ms"] = ms_value
            summary[f"{prefix}_avg_ms"] = ms_value
            if legacy_key:
                summary[legacy_key] = ms_value

        _emit("kem_keygen", kem, "keygen_ns", "kem_keygen_ms")
        _emit("kem_encaps", kem, "encap_ns", "kem_encaps_ms")
        _emit("kem_decaps", kem, "decap_ns", "kem_decap_ms")
        _emit("sig_sign", sig, "sign_ns", "sig_sign_ms")
        _emit("sig_verify", sig, "verify_ns", "sig_verify_ms")

        summary["pub_key_size_bytes"] = int(
            kem.get("public_key_bytes")
            or artifacts.get("public_key_bytes")
            or 0
        )
        summary["ciphertext_size_bytes"] = int(kem.get("ciphertext_bytes", 0) or 0)
        summary["sig_size_bytes"] = int(
            sig.get("signature_bytes")
            or artifacts.get("signature_bytes")
            or 0
        )
        summary["shared_secret_size_bytes"] = int(kem.get("shared_secret_bytes", 0) or 0)

        def _avg_ns_for(key: str) -> float:
            stats = self.primitive_metrics.get(key)
            if not isinstance(stats, dict):
                return 0.0
            count = int(stats.get("count", 0) or 0)
            total_ns = int(stats.get("total_ns", 0) or 0)
            if count <= 0 or total_ns <= 0:
                return 0.0
            return total_ns / max(count, 1)

        summary["aead_encrypt_avg_ms"] = self._ns_to_ms(_avg_ns_for("aead_encrypt"))
        summary["aead_decrypt_avg_ms"] = self._ns_to_ms(_avg_ns_for("aead_decrypt_ok"))
        summary["aead_encrypt_ms"] = summary["aead_encrypt_avg_ms"]
        summary["aead_decrypt_ms"] = summary["aead_decrypt_avg_ms"]

        summary["rekey_ms"] = self._ns_to_ms(handshake.get("handshake_total_ns"))

        total_ns = 0
        for key in ("keygen_ns", "encap_ns", "decap_ns"):
            value = kem.get(key)
            if isinstance(value, (int, float)) and value > 0:
                total_ns += int(value)
        for key in ("sign_ns", "verify_ns"):
            value = sig.get(key)
            if isinstance(value, (int, float)) and value > 0:
                total_ns += int(value)
        summary["primitive_total_ms"] = self._ns_to_ms(total_ns)

        return summary

    def to_dict(self) -> Dict[str, object]:
        def _serialize(stats: Dict[str, object]) -> Dict[str, object]:
            return {
                "count": int(stats.get("count", 0) or 0),
                "total_ns": int(stats.get("total_ns", 0) or 0),
                "min_ns": int(stats.get("min_ns") or 0),
                "max_ns": int(stats.get("max_ns", 0) or 0),
                "total_in_bytes": int(stats.get("total_in_bytes", 0) or 0),
                "total_out_bytes": int(stats.get("total_out_bytes", 0) or 0),
            }

        result = {
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
            "drop_src_addr": self.drop_src_addr,
            "rekeys_ok": self.rekeys_ok,
            "rekeys_fail": self.rekeys_fail,
            "last_rekey_ms": self.last_rekey_ms,
            "last_rekey_suite": self.last_rekey_suite or "",
            "handshake_metrics": self.handshake_metrics,
            "primitive_metrics": {name: _serialize(stats) for name, stats in self.primitive_metrics.items()},
        }

        part_b = self._part_b_metrics()
        if part_b:
            result["part_b_metrics"] = part_b
            for key, value in part_b.items():
                result.setdefault(key, value)

        return result

    def _update_primitive(self, key: str, duration_ns: int, in_bytes: int, out_bytes: int) -> None:
        stats = self.primitive_metrics.setdefault(key, dict(self._primitive_templates))
        stats["count"] = int(stats.get("count", 0) or 0) + 1
        stats["total_ns"] = int(stats.get("total_ns", 0) or 0) + max(0, int(duration_ns))
        current_min = stats.get("min_ns")
        if current_min in (None, 0) or (isinstance(current_min, int) and duration_ns < current_min):
            stats["min_ns"] = max(0, int(duration_ns))
        current_max = stats.get("max_ns", 0) or 0
        if duration_ns > current_max:
            stats["max_ns"] = max(0, int(duration_ns))
        stats["total_in_bytes"] = int(stats.get("total_in_bytes", 0) or 0) + max(0, int(in_bytes))
        stats["total_out_bytes"] = int(stats.get("total_out_bytes", 0) or 0) + max(0, int(out_bytes))

    def record_encrypt(self, duration_ns: int, plaintext_bytes: int, ciphertext_bytes: int) -> None:
        self._update_primitive("aead_encrypt", duration_ns, plaintext_bytes, ciphertext_bytes)

    def record_decrypt_ok(self, duration_ns: int, ciphertext_bytes: int, plaintext_bytes: int) -> None:
        self._update_primitive("aead_decrypt_ok", duration_ns, ciphertext_bytes, plaintext_bytes)

    def record_decrypt_fail(self, duration_ns: int, ciphertext_bytes: int) -> None:
        self._update_primitive("aead_decrypt_fail", duration_ns, ciphertext_bytes, 0)


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
    gcs_sig_secret: Optional[object],
    gcs_sig_public: Optional[bytes],
    cfg: dict,
    stop_after_seconds: Optional[float] = None,
    ready_event: Optional[threading.Event] = None,
) -> Tuple[
    bytes,
    bytes,
    bytes,
    bytes,
    bytes,
    Optional[str],
    Optional[str],
    Tuple[str, int],
    Dict[str, object],
]:
    """Perform TCP handshake and return keys, session details, and authenticated peer address."""
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
        deadline: Optional[float] = None
        if stop_after_seconds is not None:
            deadline = time.monotonic() + stop_after_seconds

        gate = _TokenBucket(
            cfg.get("HANDSHAKE_RL_BURST", 5),
            cfg.get("HANDSHAKE_RL_REFILL_PER_SEC", 1),
        )

        try:
            try:
                while True:
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise socket.timeout
                        server_sock.settimeout(max(0.01, remaining))
                    else:
                        server_sock.settimeout(timeout)

                    conn, addr = server_sock.accept()
                    try:
                        ip, _port = addr
                        allowed_ips = {str(cfg["DRONE_HOST"])}
                        allowlist = cfg.get("DRONE_HOST_ALLOWLIST", []) or []
                        if isinstance(allowlist, (list, tuple, set)):
                            for entry in allowlist:
                                allowed_ips.add(str(entry))
                        else:
                            allowed_ips.add(str(allowlist))
                        if ip not in allowed_ips:
                            logger.warning(
                                "Rejected handshake from unauthorized IP",
                                extra={"role": role, "expected": sorted(allowed_ips), "received": ip},
                            )
                            conn.close()
                            continue

                        if not gate.allow(ip):
                            try:
                                conn.settimeout(0.2)
                                conn.sendall(b"\x00")
                            except Exception:
                                pass
                            finally:
                                conn.close()
                            logger.warning(
                                "Handshake rate-limit drop",
                                extra={"role": role, "ip": ip},
                            )
                            continue

                        try:
                            result = server_gcs_handshake(conn, suite, gcs_sig_secret)
                        except HandshakeVerifyError:
                            logger.warning(
                                "Rejected drone handshake with failed authentication",
                                extra={"role": role, "expected": cfg["DRONE_HOST"], "received": ip},
                            )
                            continue
                        # Support either 5-tuple or 7-tuple
                        metrics_payload: Dict[str, object] = {}
                        if len(result) >= 7:
                            k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id, kem_name, sig_name = result[:7]
                            if len(result) >= 8 and isinstance(result[7], dict):
                                metrics_payload = result[7]
                        else:
                            k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id = result
                            kem_name = sig_name = None
                        if not metrics_payload:
                            metrics_payload = {}
                        peer_addr = (ip, cfg["UDP_DRONE_RX"])
                        return (
                            k_d2g,
                            k_g2d,
                            nseed_d2g,
                            nseed_g2d,
                            session_id,
                            kem_name,
                            sig_name,
                            peer_addr,
                            metrics_payload,
                        )
                    finally:
                        try:
                            conn.close()
                        except Exception:
                            pass
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
            peer_ip, _peer_port = client_sock.getpeername()
            result = client_drone_handshake(client_sock, suite, gcs_sig_public)
            metrics_payload: Dict[str, object] = {}
            if len(result) >= 7:
                k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id, kem_name, sig_name = result[:7]
                if len(result) >= 8 and isinstance(result[7], dict):
                    metrics_payload = result[7]
            else:
                k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id = result
                kem_name = sig_name = None
            if not metrics_payload:
                metrics_payload = {}
            peer_addr = (peer_ip, cfg["UDP_GCS_RX"])
            return (
                k_d2g,
                k_g2d,
                nseed_d2g,
                nseed_g2d,
                session_id,
                kem_name,
                sig_name,
                peer_addr,
                metrics_payload,
            )
        finally:
            client_sock.close()
    else:
        raise ValueError(f"Invalid role: {role}")


@contextmanager
def _setup_sockets(role: str, cfg: dict, *, encrypted_peer: Optional[Tuple[str, int]] = None):
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
            ptx_in_sock.bind((cfg["DRONE_PLAINTEXT_HOST"], cfg["DRONE_PLAINTEXT_TX"]))
            ptx_in_sock.setblocking(False)
            sockets["plaintext_in"] = ptx_in_sock

            # Plaintext egress - send to local app (no bind needed)
            ptx_out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets["plaintext_out"] = ptx_out_sock

            # Peer addresses
            sockets["encrypted_peer"] = encrypted_peer or (cfg["GCS_HOST"], cfg["UDP_GCS_RX"])
            sockets["plaintext_peer"] = (cfg["DRONE_PLAINTEXT_HOST"], cfg["DRONE_PLAINTEXT_RX"])

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
            ptx_in_sock.bind((cfg["GCS_PLAINTEXT_HOST"], cfg["GCS_PLAINTEXT_TX"]))
            ptx_in_sock.setblocking(False)
            sockets["plaintext_in"] = ptx_in_sock

            # Plaintext egress - send to local app (no bind needed)
            ptx_out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets["plaintext_out"] = ptx_out_sock

            # Peer addresses
            sockets["encrypted_peer"] = encrypted_peer or (cfg["DRONE_HOST"], cfg["UDP_DRONE_RX"])
            sockets["plaintext_peer"] = (cfg["GCS_PLAINTEXT_HOST"], cfg["GCS_PLAINTEXT_RX"])
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


def _compute_aead_ids(suite: dict, kem_name: Optional[str], sig_name: Optional[str]) -> AeadIds:
    if kem_name and sig_name and header_ids_from_names:
        ids_tuple = header_ids_from_names(kem_name, sig_name)  # type: ignore
    else:
        ids_tuple = header_ids_for_suite(suite)
    return AeadIds(*ids_tuple)


def _build_sender_receiver(
    role: str,
    ids: AeadIds,
    session_id: bytes,
    k_d2g: bytes,
    k_g2d: bytes,
    cfg: dict,
):
    aead_token = cfg.get("SUITE_AEAD_TOKEN")
    if aead_token is None:
        raise NotImplementedError("SUITE_AEAD_TOKEN missing from proxy config context")

    if role == "drone":
        sender = Sender(CONFIG["WIRE_VERSION"], ids, session_id, 0, k_d2g, aead_token=aead_token)
        receiver = Receiver(
            CONFIG["WIRE_VERSION"],
            ids,
            session_id,
            0,
            k_g2d,
            cfg["REPLAY_WINDOW"],
            aead_token=aead_token,
        )
    else:
        sender = Sender(CONFIG["WIRE_VERSION"], ids, session_id, 0, k_g2d, aead_token=aead_token)
        receiver = Receiver(
            CONFIG["WIRE_VERSION"],
            ids,
            session_id,
            0,
            k_d2g,
            cfg["REPLAY_WINDOW"],
            aead_token=aead_token,
        )
    return sender, receiver


def _launch_manual_console(control_state: ControlState, *, quiet: bool) -> Tuple[threading.Event, Tuple[threading.Thread, ...]]:
    suites_catalog = sorted(list_suites().keys())
    stop_event = threading.Event()

    def status_loop() -> None:
        last_line = ""
        while not stop_event.is_set():
            with control_state.lock:
                state = control_state.state
                suite_id = control_state.current_suite
            line = f"[{state}] {suite_id}"
            if line != last_line and not quiet:
                sys.stderr.write(f"\r{line:<80}")
                sys.stderr.flush()
                last_line = line
            time.sleep(0.5)
        if not quiet:
            sys.stderr.write("\r" + " " * 80 + "\r")
            sys.stderr.flush()

    def operator_loop() -> None:
        if not quiet:
            print("Manual control ready. Type a suite ID, 'list', 'status', or 'quit'.")
        while not stop_event.is_set():
            try:
                line = input("rekey> ")
            except EOFError:
                break
            if line is None:
                continue
            line = line.strip()
            if not line:
                continue
            lowered = line.lower()
            if lowered in {"quit", "exit"}:
                break
            if lowered == "list":
                if not quiet:
                    print("Available suites:")
                    for sid in suites_catalog:
                        print(f"  {sid}")
                continue
            if lowered == "status":
                with control_state.lock:
                    summary = f"state={control_state.state} suite={control_state.current_suite}"
                    if control_state.last_status:
                        summary += f" last_status={control_state.last_status}"
                if not quiet:
                    print(summary)
                continue
            try:
                target_suite = get_suite(line)
                rid = request_prepare(control_state, target_suite["suite_id"])
                if not quiet:
                    print(f"prepare queued for {target_suite['suite_id']} rid={rid}")
            except RuntimeError as exc:
                if not quiet:
                    print(f"Busy: {exc}")
            except Exception as exc:
                if not quiet:
                    print(f"Invalid suite: {exc}")

        stop_event.set()

    status_thread = threading.Thread(target=status_loop, daemon=True)
    operator_thread = threading.Thread(target=operator_loop, daemon=True)
    status_thread.start()
    operator_thread.start()
    return stop_event, (status_thread, operator_thread)


def run_proxy(
    *,
    role: str,
    suite: dict,
    cfg: dict,
    gcs_sig_secret: Optional[object] = None,
    gcs_sig_public: Optional[bytes] = None,
    stop_after_seconds: Optional[float] = None,
    manual_control: bool = False,
    quiet: bool = False,
    ready_event: Optional[threading.Event] = None,
    status_file: Optional[str] = None,
    load_gcs_secret: Optional[Callable[[Dict[str, object]], object]] = None,
    load_gcs_public: Optional[Callable[[Dict[str, object]], bytes]] = None,
) -> Dict[str, object]:
    """
    Start a blocking proxy process for `role` in {"drone","gcs"}.

    Performs the TCP handshake, bridges plaintext/encrypted UDP, and processes
    in-band control messages for rekey negotiation. Returns counters on clean exit.
    """
    if role not in {"drone", "gcs"}:
        raise ValueError(f"Invalid role: {role}")

    _validate_config(cfg)

    cfg = dict(cfg)
    cfg["SUITE_AEAD_TOKEN"] = suite.get("aead_token", "aesgcm")

    counters = ProxyCounters()
    counters_lock = threading.Lock()
    start_time = time.time()

    status_path: Optional[Path] = None
    if status_file:
        status_path = Path(status_file).expanduser()

    def write_status(payload: Dict[str, object]) -> None:
        if status_path is None:
            return
        try:
            status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = status_path.with_suffix(status_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            tmp_path.replace(status_path)
        except Exception as exc:
            logger.warning(
                "Failed to write status file",
                extra={"role": role, "error": str(exc), "path": str(status_path)},
            )

    if role == "drone" and gcs_sig_public is None:
        if load_gcs_public is None:
            raise NotImplementedError("GCS signature public key not provided (provide peer key or loader)")
        gcs_sig_public = load_gcs_public(suite)

    handshake_result = _perform_handshake(
        role, suite, gcs_sig_secret, gcs_sig_public, cfg, stop_after_seconds, ready_event
    )

    if len(handshake_result) >= 9:
        (
            k_d2g,
            k_g2d,
            _nseed_d2g,
            _nseed_g2d,
            session_id,
            kem_name,
            sig_name,
            peer_addr,
            handshake_metrics,
        ) = handshake_result
    else:
        (
            k_d2g,
            k_g2d,
            _nseed_d2g,
            _nseed_g2d,
            session_id,
            kem_name,
            sig_name,
            peer_addr,
        ) = handshake_result
        handshake_metrics = {}

    suite_id = suite.get("suite_id")
    if not suite_id:
        try:
            suite_id = next((sid for sid, s in SUITES.items() if dict(s) == suite), "unknown")
        except Exception:
            suite_id = "unknown"

    status_payload = {
        "status": "handshake_ok",
        "suite": suite_id,
        "session_id": session_id.hex(),
    }
    if handshake_metrics:
        status_payload["handshake_metrics"] = handshake_metrics
    write_status(status_payload)

    sess_display = (
        session_id.hex()
        if cfg.get("LOG_SESSION_ID", False)
        else hashlib.sha256(session_id).hexdigest()[:8] + "..."
    )

    with counters_lock:
        counters.handshake_metrics = dict(handshake_metrics) if handshake_metrics else {}

    logger.info(
        "PQC handshake completed successfully",
        extra={
            "suite_id": suite_id,
            "peer_role": ("drone" if role == "gcs" else "gcs"),
            "session_id": sess_display,
        },
    )

    # Periodically persist counters to the status file while the proxy runs.
    # This allows external automation (scheduler) to observe enc_in/enc_out
    # during long-running experiments without waiting for process exit.
    stop_status_writer = threading.Event()

    def _status_writer() -> None:
        while not stop_status_writer.is_set():
            try:
                with counters_lock:
                    payload = {
                        "status": "running",
                        "suite": suite_id,
                        "counters": counters.to_dict(),
                        "ts_ns": time.time_ns(),
                    }
                write_status(payload)
            except Exception:
                logger.debug("status writer failed", extra={"role": role})
            # sleep with event to allow quick shutdown
            stop_status_writer.wait(1.0)

    status_thread: Optional[threading.Thread] = None
    try:
        status_thread = threading.Thread(target=_status_writer, daemon=True)
        status_thread.start()
    except Exception:
        status_thread = None

    aead_ids = _compute_aead_ids(suite, kem_name, sig_name)
    sender, receiver = _build_sender_receiver(role, aead_ids, session_id, k_d2g, k_g2d, cfg)

    control_state = create_control_state(role, suite_id)
    context_lock = threading.RLock()
    active_context: Dict[str, object] = {
        "suite": suite_id,
        "suite_dict": suite,
        "session_id": session_id,
        "aead_ids": aead_ids,
        "sender": sender,
        "receiver": receiver,
        "peer_addr": peer_addr,
        "peer_match_strict": bool(cfg.get("STRICT_UDP_PEER_MATCH", True)),
    }

    active_rekeys: set[str] = set()
    rekey_guard = threading.Lock()

    if manual_control and role == "gcs" and not cfg.get("ENABLE_PACKET_TYPE"):
        logger.warning("ENABLE_PACKET_TYPE is disabled; control-plane packets may not be processed correctly.")

    manual_stop: Optional[threading.Event] = None
    manual_threads: Tuple[threading.Thread, ...] = ()
    if manual_control and role == "gcs":
        manual_stop, manual_threads = _launch_manual_console(control_state, quiet=quiet)

    def _launch_rekey(target_suite_id: str, rid: str) -> None:
        with rekey_guard:
            if rid in active_rekeys:
                return
            active_rekeys.add(rid)

        logger.info(
            "Control rekey negotiation started",
            extra={"role": role, "suite_id": target_suite_id, "rid": rid},
        )

        def worker() -> None:
            nonlocal gcs_sig_public
            try:
                new_suite = get_suite(target_suite_id)
                new_secret = None
                new_public: Optional[bytes] = None
                if role == "gcs" and load_gcs_secret is not None:
                    try:
                        new_secret = load_gcs_secret(new_suite)
                    except FileNotFoundError as exc:
                        with context_lock:
                            current_suite = active_context["suite"]
                        with counters_lock:
                            counters.rekeys_fail += 1
                        record_rekey_result(control_state, rid, current_suite, success=False)
                        logger.warning(
                            "Control rekey rejected: missing signing secret",
                            extra={
                                "role": role,
                                "suite_id": target_suite_id,
                                "rid": rid,
                                "error": str(exc),
                            },
                        )
                        with rekey_guard:
                            active_rekeys.discard(rid)
                        return
                    except Exception as exc:
                        with context_lock:
                            current_suite = active_context["suite"]
                        with counters_lock:
                            counters.rekeys_fail += 1
                        record_rekey_result(control_state, rid, current_suite, success=False)
                        logger.warning(
                            "Control rekey rejected: signing secret load failed",
                            extra={
                                "role": role,
                                "suite_id": target_suite_id,
                                "rid": rid,
                                "error": str(exc),
                            },
                        )
                        with rekey_guard:
                            active_rekeys.discard(rid)
                        return
            except NotImplementedError as exc:
                with context_lock:
                    current_suite = active_context["suite"]
                with counters_lock:
                    counters.rekeys_fail += 1
                record_rekey_result(control_state, rid, current_suite, success=False)
                logger.warning(
                    "Control rekey rejected: unknown suite",
                    extra={"role": role, "suite_id": target_suite_id, "rid": rid, "error": str(exc)},
                )
                with rekey_guard:
                    active_rekeys.discard(rid)
                return

            if role == "drone" and load_gcs_public is not None:
                try:
                    new_public = load_gcs_public(new_suite)
                except FileNotFoundError as exc:
                    with context_lock:
                        current_suite = active_context["suite"]
                    with counters_lock:
                        counters.rekeys_fail += 1
                    record_rekey_result(control_state, rid, current_suite, success=False)
                    logger.warning(
                        "Control rekey rejected: missing signing public key",
                        extra={
                            "role": role,
                            "suite_id": target_suite_id,
                            "rid": rid,
                            "error": str(exc),
                        },
                    )
                    with rekey_guard:
                        active_rekeys.discard(rid)
                    return
                except Exception as exc:
                    with context_lock:
                        current_suite = active_context["suite"]
                    with counters_lock:
                        counters.rekeys_fail += 1
                    record_rekey_result(control_state, rid, current_suite, success=False)
                    logger.warning(
                        "Control rekey rejected: signing public key load failed",
                        extra={
                            "role": role,
                            "suite_id": target_suite_id,
                            "rid": rid,
                            "error": str(exc),
                        },
                    )
                    with rekey_guard:
                        active_rekeys.discard(rid)
                    return

            prev_token: Optional[str] = cfg.get("SUITE_AEAD_TOKEN")
            try:
                timeout = cfg.get("REKEY_HANDSHAKE_TIMEOUT", 20.0)
                if role == "gcs" and new_secret is not None:
                    base_secret = new_secret
                else:
                    base_secret = gcs_sig_secret
                public_key = new_public if new_public is not None else gcs_sig_public
                if role == "drone" and public_key is None:
                    raise NotImplementedError("GCS public key not available for rekey")
                rk_result = _perform_handshake(role, new_suite, base_secret, public_key, cfg, timeout)
                if len(rk_result) >= 9:
                    (
                        new_k_d2g,
                        new_k_g2d,
                        _nd1,
                        _nd2,
                        new_session_id,
                        new_kem_name,
                        new_sig_name,
                        new_peer_addr,
                        new_handshake_metrics,
                    ) = rk_result
                else:
                    (
                        new_k_d2g,
                        new_k_g2d,
                        _nd1,
                        _nd2,
                        new_session_id,
                        new_kem_name,
                        new_sig_name,
                        new_peer_addr,
                    ) = rk_result
                    new_handshake_metrics = {}
                if new_handshake_metrics:
                    new_handshake_metrics = dict(new_handshake_metrics)

                cfg["SUITE_AEAD_TOKEN"] = new_suite.get("aead_token", "aesgcm")
                new_ids = _compute_aead_ids(new_suite, new_kem_name, new_sig_name)
                new_sender, new_receiver = _build_sender_receiver(
                    role, new_ids, new_session_id, new_k_d2g, new_k_g2d, cfg
                )

                with context_lock:
                    active_context.update(
                        {
                            "sender": new_sender,
                            "receiver": new_receiver,
                            "session_id": new_session_id,
                            "aead_ids": new_ids,
                            "suite": new_suite["suite_id"],
                            "suite_dict": new_suite,
                            "peer_addr": new_peer_addr,
                        }
                    )
                    sockets["encrypted_peer"] = new_peer_addr

                with counters_lock:
                    counters.rekeys_ok += 1
                    counters.last_rekey_ms = int(time.time() * 1000)
                    counters.last_rekey_suite = new_suite["suite_id"]
                    counters.handshake_metrics = dict(new_handshake_metrics) if new_handshake_metrics else {}
                if role == "drone" and new_public is not None:
                    gcs_sig_public = new_public
                record_rekey_result(control_state, rid, new_suite["suite_id"], success=True)
                status_payload = {
                    "status": "rekey_ok",
                    "new_suite": new_suite["suite_id"],
                    "session_id": new_session_id.hex(),
                }
                if new_handshake_metrics:
                    status_payload["handshake_metrics"] = new_handshake_metrics
                write_status(status_payload)
                new_sess_display = (
                    new_session_id.hex()
                    if cfg.get("LOG_SESSION_ID", False)
                    else hashlib.sha256(new_session_id).hexdigest()[:8] + "..."
                )
                logger.info(
                    "Control rekey successful",
                    extra={
                        "role": role,
                        "suite_id": new_suite["suite_id"],
                        "rid": rid,
                        "session_id": new_sess_display,
                    },
                )
            except Exception as exc:
                if prev_token is not None:
                    cfg["SUITE_AEAD_TOKEN"] = prev_token
                with context_lock:
                    current_suite = active_context["suite"]
                with counters_lock:
                    counters.rekeys_fail += 1
                record_rekey_result(control_state, rid, current_suite, success=False)
                logger.warning(
                    "Control rekey failed",
                    extra={"role": role, "suite_id": target_suite_id, "rid": rid, "error": str(exc)},
                )
            finally:
                with rekey_guard:
                    active_rekeys.discard(rid)

        threading.Thread(target=worker, daemon=True).start()

    with _setup_sockets(role, cfg, encrypted_peer=peer_addr) as sockets:
        selector = selectors.DefaultSelector()
        selector.register(sockets["encrypted"], selectors.EVENT_READ, data="encrypted")
        selector.register(sockets["plaintext_in"], selectors.EVENT_READ, data="plaintext_in")

        def send_control(payload: dict) -> None:
            body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            frame = b"\x02" + body
            with context_lock:
                current_sender = active_context["sender"]
            try:
                wire = current_sender.encrypt(frame)
            except Exception as exc:
                counters.drops += 1
                counters.drop_other += 1
                logger.warning("Failed to encrypt control payload", extra={"role": role, "error": str(exc)})
                return
            try:
                sockets["encrypted"].sendto(wire, sockets["encrypted_peer"])
                counters.enc_out += 1
            except socket.error as exc:
                counters.drops += 1
                counters.drop_other += 1
                logger.warning("Failed to send control payload", extra={"role": role, "error": str(exc)})

        try:
            while True:
                if stop_after_seconds is not None and (time.time() - start_time) >= stop_after_seconds:
                    break

                while True:
                    try:
                        control_payload = control_state.outbox.get_nowait()
                    except queue.Empty:
                        break
                    send_control(control_payload)

                events = selector.select(timeout=0.1)
                for key, _mask in events:
                    sock = key.fileobj
                    data_type = key.data

                    if data_type == "plaintext_in":
                        try:
                            payload, _addr = sock.recvfrom(16384)
                            if not payload:
                                continue
                            with counters_lock:
                                counters.ptx_in += 1

                            payload_out = (b"\x01" + payload) if cfg.get("ENABLE_PACKET_TYPE") else payload
                            with context_lock:
                                current_sender = active_context["sender"]
                            encrypt_start_ns = time.perf_counter_ns()
                            try:
                                wire = current_sender.encrypt(payload_out)
                            except Exception as exc:
                                encrypt_elapsed_ns = time.perf_counter_ns() - encrypt_start_ns
                                with counters_lock:
                                    counters.drops += 1
                                    counters.drop_other += 1
                                logger.warning(
                                    "Encrypt failed",
                                    extra={
                                        "role": role,
                                        "error": str(exc),
                                        "payload_len": len(payload_out),
                                    },
                                )
                                continue
                            encrypt_elapsed_ns = time.perf_counter_ns() - encrypt_start_ns
                            ciphertext_len = len(wire)
                            plaintext_len = len(payload_out)
                            with counters_lock:
                                counters.record_encrypt(encrypt_elapsed_ns, plaintext_len, ciphertext_len)

                            try:
                                sockets["encrypted"].sendto(wire, sockets["encrypted_peer"])
                                with counters_lock:
                                    counters.enc_out += 1
                            except socket.error:
                                with counters_lock:
                                    counters.drops += 1
                        except socket.error:
                            continue

                    elif data_type == "encrypted":
                        try:
                            wire, addr = sock.recvfrom(16384)
                            if not wire:
                                continue

                            with context_lock:
                                current_receiver = active_context["receiver"]
                                expected_peer = active_context.get("peer_addr")
                                strict_match = bool(active_context.get("peer_match_strict", True))

                            src_ip, src_port = addr
                            if expected_peer is not None:
                                exp_ip, exp_port = expected_peer  # type: ignore[misc]
                                mismatch = False
                                if strict_match:
                                    mismatch = src_ip != exp_ip or src_port != exp_port
                                else:
                                    mismatch = src_ip != exp_ip
                                if mismatch:
                                    with counters_lock:
                                        counters.drops += 1
                                        counters.drop_src_addr += 1
                                    logger.debug(
                                        "Dropped encrypted packet from unauthorized source",
                                        extra={"role": role, "expected": expected_peer, "received": addr},
                                    )
                                    continue

                            with counters_lock:
                                counters.enc_in += 1

                            cipher_len = len(wire)
                            decrypt_start_ns = time.perf_counter_ns()
                            try:
                                plaintext = current_receiver.decrypt(wire)
                            except ReplayError:
                                decrypt_elapsed_ns = time.perf_counter_ns() - decrypt_start_ns
                                with counters_lock:
                                    counters.drops += 1
                                    counters.drop_replay += 1
                                    counters.record_decrypt_fail(decrypt_elapsed_ns, cipher_len)
                                continue
                            except HeaderMismatch:
                                decrypt_elapsed_ns = time.perf_counter_ns() - decrypt_start_ns
                                with counters_lock:
                                    counters.drops += 1
                                    counters.drop_header += 1
                                    counters.record_decrypt_fail(decrypt_elapsed_ns, cipher_len)
                                continue
                            except AeadAuthError:
                                decrypt_elapsed_ns = time.perf_counter_ns() - decrypt_start_ns
                                with counters_lock:
                                    counters.drops += 1
                                    counters.drop_auth += 1
                                    counters.record_decrypt_fail(decrypt_elapsed_ns, cipher_len)
                                continue
                            except NotImplementedError as exc:
                                decrypt_elapsed_ns = time.perf_counter_ns() - decrypt_start_ns
                                with counters_lock:
                                    counters.drops += 1
                                    reason, _seq = _parse_header_fields(
                                        CONFIG["WIRE_VERSION"], current_receiver.ids, current_receiver.session_id, wire
                                    )
                                    if reason in (
                                        "version_mismatch",
                                        "crypto_id_mismatch",
                                        "header_too_short",
                                        "header_unpack_error",
                                    ):
                                        counters.drop_header += 1
                                    elif reason == "session_mismatch":
                                        counters.drop_session_epoch += 1
                                    else:
                                        counters.drop_auth += 1
                                    counters.record_decrypt_fail(decrypt_elapsed_ns, cipher_len)
                                logger.warning(
                                    "Decrypt failed (classified)",
                                    extra={
                                        "role": role,
                                        "reason": reason,
                                        "wire_len": len(wire),
                                        "error": str(exc),
                                    },
                                )
                                continue
                            except Exception as exc:
                                decrypt_elapsed_ns = time.perf_counter_ns() - decrypt_start_ns
                                with counters_lock:
                                    counters.drops += 1
                                    counters.drop_other += 1
                                    counters.record_decrypt_fail(decrypt_elapsed_ns, cipher_len)
                                logger.warning(
                                    "Decrypt failed (other)",
                                    extra={"role": role, "error": str(exc), "wire_len": len(wire)},
                                )
                                continue

                            decrypt_elapsed_ns = time.perf_counter_ns() - decrypt_start_ns
                            if plaintext is None:
                                with counters_lock:
                                    counters.drops += 1
                                    last_reason = current_receiver.last_error_reason()
                                    # Bug #7 fix: Proper error classification without redundancy
                                    if last_reason == "auth":
                                        counters.drop_auth += 1
                                    elif last_reason == "header":
                                        counters.drop_header += 1
                                    elif last_reason == "replay":
                                        counters.drop_replay += 1
                                    elif last_reason == "session":
                                        counters.drop_session_epoch += 1
                                    elif last_reason is None or last_reason == "unknown":
                                        # Only parse header if receiver didn't classify it
                                        reason, _seq = _parse_header_fields(
                                            CONFIG["WIRE_VERSION"],
                                            current_receiver.ids,
                                            current_receiver.session_id,
                                            wire,
                                        )
                                        if reason in (
                                            "version_mismatch",
                                            "crypto_id_mismatch",
                                            "header_too_short",
                                            "header_unpack_error",
                                        ):
                                            counters.drop_header += 1
                                        elif reason == "session_mismatch":
                                            counters.drop_session_epoch += 1
                                        elif reason == "auth_fail_or_replay":
                                            counters.drop_auth += 1
                                        else:
                                            counters.drop_other += 1
                                    else:
                                        # Unrecognized last_reason value
                                        counters.drop_other += 1
                                    counters.record_decrypt_fail(decrypt_elapsed_ns, cipher_len)
                                continue

                            plaintext_len = len(plaintext)
                            with counters_lock:
                                counters.record_decrypt_ok(decrypt_elapsed_ns, cipher_len, plaintext_len)

                            try:
                                if plaintext and plaintext[0] == 0x02:
                                    try:
                                        control_json = json.loads(plaintext[1:].decode("utf-8"))
                                    except (UnicodeDecodeError, json.JSONDecodeError):
                                        with counters_lock:
                                            counters.drops += 1
                                            counters.drop_other += 1
                                        continue
                                    result = handle_control(control_json, role, control_state)
                                    for note in result.notes:
                                        if note.startswith("prepare_fail"):
                                            with counters_lock:
                                                counters.rekeys_fail += 1
                                    for payload in result.send:
                                        control_state.outbox.put(payload)
                                    if result.start_handshake:
                                        suite_next, rid = result.start_handshake
                                        _launch_rekey(suite_next, rid)
                                    continue

                                if cfg.get("ENABLE_PACKET_TYPE") and plaintext:
                                    ptype = plaintext[0]
                                    if ptype == 0x01:
                                        out_bytes = plaintext[1:]
                                    else:
                                        with counters_lock:
                                            counters.drops += 1
                                            counters.drop_other += 1
                                        continue
                                else:
                                    out_bytes = plaintext

                                sockets["plaintext_out"].sendto(out_bytes, sockets["plaintext_peer"])
                                with counters_lock:
                                    counters.ptx_out += 1
                            except socket.error:
                                with counters_lock:
                                    counters.drops += 1
                                    counters.drop_other += 1
                        except socket.error:
                            continue
        except KeyboardInterrupt:
            pass
        finally:
            selector.close()
            if manual_stop:
                manual_stop.set()
                for thread in manual_threads:
                    thread.join(timeout=0.5)

        # Final status write and stop the status writer thread if running
        try:
            with counters_lock:
                write_status({
                    "status": "stopped",
                    "suite": suite_id,
                    "counters": counters.to_dict(),
                    "ts_ns": time.time_ns(),
                })
        except Exception:
            pass

        if 'stop_status_writer' in locals() and stop_status_writer is not None:
            try:
                stop_status_writer.set()
            except Exception:
                pass
        if 'status_thread' in locals() and status_thread is not None and status_thread.is_alive():
            try:
                status_thread.join(timeout=1.0)
            except Exception:
                pass

        return counters.to_dict()
