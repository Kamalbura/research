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

import json
import queue
import socket
import selectors
import sys
import threading
import time
import struct
from typing import Optional, Dict, Tuple
from contextlib import contextmanager

from core.config import CONFIG
from core.suites import SUITES, header_ids_for_suite, get_suite, list_suites
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

from core.policy_engine import (
    ControlState,
    ControlResult,
    create_control_state,
    handle_control,
    request_prepare,
    record_rekey_result,
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
        self.rekeys_ok = 0
        self.rekeys_fail = 0
        self.last_rekey_ms = 0
        self.last_rekey_suite: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
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
            "rekeys_ok": self.rekeys_ok,
            "rekeys_fail": self.rekeys_fail,
            "last_rekey_ms": self.last_rekey_ms,
            "last_rekey_suite": self.last_rekey_suite or "",
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
    if role == "drone":
        sender = Sender(CONFIG["WIRE_VERSION"], ids, session_id, 0, k_d2g)
        receiver = Receiver(CONFIG["WIRE_VERSION"], ids, session_id, 0, k_g2d, cfg["REPLAY_WINDOW"])
    else:
        sender = Sender(CONFIG["WIRE_VERSION"], ids, session_id, 0, k_g2d)
        receiver = Receiver(CONFIG["WIRE_VERSION"], ids, session_id, 0, k_d2g, cfg["REPLAY_WINDOW"])
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
    gcs_sig_secret: Optional[bytes] = None,
    gcs_sig_public: Optional[bytes] = None,
    stop_after_seconds: Optional[float] = None,
    manual_control: bool = False,
    quiet: bool = False,
    ready_event: Optional[threading.Event] = None,
) -> Dict[str, object]:
    """
    Start a blocking proxy process for `role` in {"drone","gcs"}.

    Performs the TCP handshake, bridges plaintext/encrypted UDP, and processes
    in-band control messages for rekey negotiation. Returns counters on clean exit.
    """
    if role not in {"drone", "gcs"}:
        raise ValueError(f"Invalid role: {role}")

    _validate_config(cfg)

    counters = ProxyCounters()
    start_time = time.time()

    k_d2g, k_g2d, _nseed_d2g, _nseed_g2d, session_id, kem_name, sig_name = _perform_handshake(
        role, suite, gcs_sig_secret, gcs_sig_public, cfg, stop_after_seconds, ready_event
    )

    suite_id = suite.get("suite_id")
    if not suite_id:
        try:
            suite_id = next((sid for sid, s in SUITES.items() if dict(s) == suite), "unknown")
        except Exception:
            suite_id = "unknown"

    logger.info(
        "PQC handshake completed successfully",
        extra={
            "suite_id": suite_id,
            "peer_role": ("drone" if role == "gcs" else "gcs"),
            "session_id": session_id.hex(),
        },
    )

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
            try:
                new_suite = get_suite(target_suite_id)
            except NotImplementedError as exc:
                with context_lock:
                    current_suite = active_context["suite"]
                counters.rekeys_fail += 1
                record_rekey_result(control_state, rid, current_suite, success=False)
                logger.warning(
                    "Control rekey rejected: unknown suite",
                    extra={"role": role, "suite_id": target_suite_id, "rid": rid, "error": str(exc)},
                )
                with rekey_guard:
                    active_rekeys.discard(rid)
                return

            try:
                timeout = cfg.get("REKEY_HANDSHAKE_TIMEOUT", 20.0)
                rk_result = _perform_handshake(role, new_suite, gcs_sig_secret, gcs_sig_public, cfg, timeout)
                if len(rk_result) >= 7:
                    new_k_d2g, new_k_g2d, _nd1, _nd2, new_session_id, new_kem_name, new_sig_name = rk_result[:7]
                else:
                    new_k_d2g, new_k_g2d, _nd1, _nd2, new_session_id = rk_result
                    new_kem_name = new_sig_name = None

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
                        }
                    )

                counters.rekeys_ok += 1
                counters.last_rekey_ms = int(time.time() * 1000)
                counters.last_rekey_suite = new_suite["suite_id"]
                record_rekey_result(control_state, rid, new_suite["suite_id"], success=True)
                logger.info(
                    "Control rekey successful",
                    extra={"role": role, "suite_id": new_suite["suite_id"], "rid": rid, "session_id": new_session_id.hex()},
                )
            except Exception as exc:
                with context_lock:
                    current_suite = active_context["suite"]
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

    with _setup_sockets(role, cfg) as sockets:
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
                            payload, _addr = sock.recvfrom(2048)
                            if not payload:
                                continue
                            counters.ptx_in += 1

                            payload_out = (b"\x01" + payload) if cfg.get("ENABLE_PACKET_TYPE") else payload
                            with context_lock:
                                current_sender = active_context["sender"]
                            wire = current_sender.encrypt(payload_out)

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

                            with context_lock:
                                current_receiver = active_context["receiver"]

                            try:
                                plaintext = current_receiver.decrypt(wire)
                                if plaintext is None:
                                    reason, _seq = _parse_header_fields(
                                        CONFIG["WIRE_VERSION"], current_receiver.ids, current_receiver.session_id, wire
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
                            except NotImplementedError as exc:
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
                                counters.drops += 1
                                counters.drop_other += 1
                                logger.warning(
                                    "Decrypt failed (other)",
                                    extra={"role": role, "error": str(exc), "wire_len": len(wire)},
                                )
                                continue

                            try:
                                out_bytes = plaintext
                                if cfg.get("ENABLE_PACKET_TYPE") and plaintext:
                                    ptype = plaintext[0]
                                    if ptype == 0x01:
                                        out_bytes = plaintext[1:]
                                    elif ptype == 0x02:
                                        try:
                                            control_json = json.loads(plaintext[1:].decode("utf-8"))
                                        except (UnicodeDecodeError, json.JSONDecodeError):
                                            counters.drops += 1
                                            counters.drop_other += 1
                                            continue
                                        result = handle_control(control_json, role, control_state)
                                        for note in result.notes:
                                            if note.startswith("prepare_fail"):
                                                counters.rekeys_fail += 1
                                        for payload in result.send:
                                            control_state.outbox.put(payload)
                                        if result.start_handshake:
                                            suite_next, rid = result.start_handshake
                                            _launch_rekey(suite_next, rid)
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
            if manual_stop:
                manual_stop.set()
                for thread in manual_threads:
                    thread.join(timeout=0.5)

    return counters.to_dict()
