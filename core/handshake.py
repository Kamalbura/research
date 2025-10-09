from dataclasses import dataclass, field
import hashlib
import hmac
import os
import struct
import time
from typing import Dict, Optional
from core.config import CONFIG
from core.suites import get_suite
from core.logging_utils import get_logger
from oqs.oqs import KeyEncapsulation, Signature

logger = get_logger("pqc")

class HandshakeFormatError(Exception):
    pass

class HandshakeVerifyError(Exception):
    pass

@dataclass(frozen=True)
class ServerHello:
    version: int
    kem_name: bytes
    sig_name: bytes
    session_id: bytes
    kem_pub: bytes
    signature: bytes
    challenge: bytes
    metrics: Optional[Dict[str, object]] = None

@dataclass
class ServerEphemeral:
    kem_name: str
    sig_name: str
    session_id: bytes
    kem_obj: object  # oqs.KeyEncapsulation instance
    challenge: bytes
    metrics: Dict[str, object] = field(default_factory=dict)

def build_server_hello(
    suite_id: str,
    server_sig_obj,
    *,
    metrics: Optional[Dict[str, object]] = None,
):
    suite = get_suite(suite_id)
    if not suite:
        raise NotImplementedError("suite_id not found")
    version = CONFIG["WIRE_VERSION"]
    kem_name = suite["kem_name"].encode("utf-8")
    sig_name = suite["sig_name"].encode("utf-8")
    if not kem_name or not sig_name:
        raise NotImplementedError("kem_name/sig_name empty")
    if not isinstance(server_sig_obj, Signature):
        raise NotImplementedError("server_sig_obj must be oqs.Signature")
    session_id = os.urandom(8)
    challenge = os.urandom(8)
    metrics_ref = metrics if metrics is not None else {}
    metrics_ref.setdefault("role", "gcs")
    metrics_ref.setdefault("suite_id", suite_id)
    metrics_ref.setdefault("kem_name", suite["kem_name"])
    metrics_ref.setdefault("sig_name", suite["sig_name"])
    primitives = metrics_ref.setdefault("primitives", {})
    kem_metrics = primitives.setdefault("kem", {})
    sig_metrics = primitives.setdefault("signature", {})
    artifacts = metrics_ref.setdefault("artifacts", {})

    keygen_wall_start = time.time_ns()
    keygen_perf_start = time.perf_counter_ns()
    kem_obj = KeyEncapsulation(kem_name.decode("utf-8"))
    kem_pub = kem_obj.generate_keypair()
    keygen_perf_end = time.perf_counter_ns()
    keygen_wall_end = time.time_ns()
    kem_metrics["keygen_ns"] = keygen_perf_end - keygen_perf_start
    kem_metrics["keygen_wall_start_ns"] = keygen_wall_start
    kem_metrics["keygen_wall_end_ns"] = keygen_wall_end
    kem_metrics["public_key_bytes"] = len(kem_pub)
    # Include negotiated wire version as first byte of transcript to prevent downgrade
    transcript = (
        struct.pack("!B", version)
        + b"|pq-drone-gcs:v1|"
        + session_id
        + b"|"
        + kem_name
        + b"|"
        + sig_name
        + b"|"
        + kem_pub
        + b"|"
        + challenge
    )
    sign_wall_start = time.time_ns()
    sign_perf_start = time.perf_counter_ns()
    signature = server_sig_obj.sign(transcript)
    sign_perf_end = time.perf_counter_ns()
    sign_wall_end = time.time_ns()
    sig_metrics["sign_ns"] = sign_perf_end - sign_perf_start
    sig_metrics["sign_wall_start_ns"] = sign_wall_start
    sig_metrics["sign_wall_end_ns"] = sign_wall_end
    sig_metrics["signature_bytes"] = len(signature)
    wire = struct.pack("!B", version)
    wire += struct.pack("!H", len(kem_name)) + kem_name
    wire += struct.pack("!H", len(sig_name)) + sig_name
    wire += session_id
    wire += challenge
    wire += struct.pack("!I", len(kem_pub)) + kem_pub
    wire += struct.pack("!H", len(signature)) + signature
    artifacts["server_hello_bytes"] = len(wire)
    artifacts.setdefault("challenge_bytes", len(challenge))
    ephemeral = ServerEphemeral(
        kem_name=kem_name.decode("utf-8"),
        sig_name=sig_name.decode("utf-8"),
        session_id=session_id,
        kem_obj=kem_obj,
        challenge=challenge,
        metrics=metrics_ref,
    )
    return wire, ephemeral

def parse_and_verify_server_hello(
    wire: bytes,
    expected_version: int,
    server_sig_pub: bytes,
    *,
    metrics: Optional[Dict[str, object]] = None,
) -> ServerHello:
    try:
        offset = 0
        version = wire[offset]
        offset += 1
        if version != expected_version:
            raise HandshakeFormatError("bad wire version")
        kem_name_len = struct.unpack_from("!H", wire, offset)[0]
        offset += 2
        kem_name = wire[offset:offset+kem_name_len]
        offset += kem_name_len
        sig_name_len = struct.unpack_from("!H", wire, offset)[0]
        offset += 2
        sig_name = wire[offset:offset+sig_name_len]
        offset += sig_name_len
        session_id = wire[offset:offset+8]
        offset += 8
        challenge = wire[offset:offset+8]
        offset += 8
        kem_pub_len = struct.unpack_from("!I", wire, offset)[0]
        offset += 4
        kem_pub = wire[offset:offset+kem_pub_len]
        offset += kem_pub_len
        sig_len = struct.unpack_from("!H", wire, offset)[0]
        offset += 2
        signature = wire[offset:offset+sig_len]
        offset += sig_len
    except Exception:
        raise HandshakeFormatError("malformed server hello")
    transcript = (
        struct.pack("!B", version)
        + b"|pq-drone-gcs:v1|"
        + session_id
        + b"|"
        + kem_name
        + b"|"
        + sig_name
        + b"|"
        + kem_pub
        + b"|"
        + challenge
    )
    metrics_ref = metrics
    if metrics_ref is not None:
        metrics_ref.setdefault("role", metrics_ref.get("role", "drone"))
        primitives = metrics_ref.setdefault("primitives", {})
        sig_metrics = primitives.setdefault("signature", {})
    else:
        sig_metrics = None
    sig = None
    try:
        verify_wall_start = time.time_ns() if sig_metrics is not None else None
        verify_perf_start = time.perf_counter_ns() if sig_metrics is not None else None
        sig = Signature(sig_name.decode("utf-8"))
        if not sig.verify(transcript, signature, server_sig_pub):
            raise HandshakeVerifyError("bad signature")
        if sig_metrics is not None and verify_perf_start is not None and verify_wall_start is not None:
            verify_perf_end = time.perf_counter_ns()
            verify_wall_end = time.time_ns()
            sig_metrics["verify_ns"] = verify_perf_end - verify_perf_start
            sig_metrics["verify_wall_start_ns"] = verify_wall_start
            sig_metrics["verify_wall_end_ns"] = verify_wall_end
            sig_metrics["signature_bytes"] = len(signature)
    except HandshakeVerifyError:
        raise
    except Exception:
        raise HandshakeVerifyError("signature verification failed")
    finally:
        if sig is not None and hasattr(sig, "free"):
            try:
                sig.free()
            except Exception:
                pass
    return ServerHello(
        version=version,
        kem_name=kem_name,
        sig_name=sig_name,
        session_id=session_id,
        kem_pub=kem_pub,
        signature=signature,
        challenge=challenge,
        metrics=metrics_ref,
    )

def _drone_psk_bytes() -> bytes:
    psk_hex = CONFIG.get("DRONE_PSK", "")
    try:
        psk = bytes.fromhex(psk_hex)
    except ValueError as exc:
        raise NotImplementedError(f"Invalid DRONE_PSK hex: {exc}")
    if len(psk) != 32:
        raise NotImplementedError("DRONE_PSK must decode to 32 bytes")
    return psk


def client_encapsulate(server_hello: ServerHello, *, metrics: Optional[Dict[str, object]] = None):
    kem = None
    try:
        kem = KeyEncapsulation(server_hello.kem_name.decode("utf-8"))
        metrics_ref = metrics if metrics is not None else getattr(server_hello, "metrics", None)
        encap_wall_start = time.time_ns() if metrics_ref is not None else None
        encap_perf_start = time.perf_counter_ns() if metrics_ref is not None else None
        kem_ct, shared_secret = kem.encap_secret(server_hello.kem_pub)
        if metrics_ref is not None and encap_perf_start is not None and encap_wall_start is not None:
            encap_perf_end = time.perf_counter_ns()
            encap_wall_end = time.time_ns()
            primitives = metrics_ref.setdefault("primitives", {})
            kem_metrics = primitives.setdefault("kem", {})
            kem_metrics["encap_ns"] = encap_perf_end - encap_perf_start
            kem_metrics["encap_wall_start_ns"] = encap_wall_start
            kem_metrics["encap_wall_end_ns"] = encap_wall_end
            kem_metrics["ciphertext_bytes"] = len(kem_ct)
            kem_metrics.setdefault("shared_secret_bytes", len(shared_secret))
        return kem_ct, shared_secret
    except Exception:
        raise NotImplementedError("client_encapsulate failed")
    finally:
        if kem is not None and hasattr(kem, "free"):
            try:
                kem.free()
            except Exception:
                pass


def server_decapsulate(
    ephemeral: ServerEphemeral,
    kem_ct: bytes,
    *,
    metrics: Optional[Dict[str, object]] = None,
):
    kem_obj = getattr(ephemeral, "kem_obj", None)
    try:
        if kem_obj is None:
            raise NotImplementedError("server_decapsulate missing kem_obj")
        metrics_ref = metrics if metrics is not None else getattr(ephemeral, "metrics", None)
        decap_wall_start = time.time_ns() if metrics_ref is not None else None
        decap_perf_start = time.perf_counter_ns() if metrics_ref is not None else None
        shared_secret = kem_obj.decap_secret(kem_ct)
        if metrics_ref is not None and decap_perf_start is not None and decap_wall_start is not None:
            decap_perf_end = time.perf_counter_ns()
            decap_wall_end = time.time_ns()
            primitives = metrics_ref.setdefault("primitives", {})
            kem_metrics = primitives.setdefault("kem", {})
            kem_metrics["decap_ns"] = decap_perf_end - decap_perf_start
            kem_metrics["decap_wall_start_ns"] = decap_wall_start
            kem_metrics["decap_wall_end_ns"] = decap_wall_end
            kem_metrics.setdefault("ciphertext_bytes", len(kem_ct))
            kem_metrics.setdefault("shared_secret_bytes", len(shared_secret))
        return shared_secret
    except Exception:
        raise NotImplementedError("server_decapsulate failed")
    finally:
        if kem_obj is not None and hasattr(kem_obj, "free"):
            try:
                kem_obj.free()
            except Exception:
                pass
        if hasattr(ephemeral, "kem_obj"):
            ephemeral.kem_obj = None


def derive_transport_keys(
    role: str,
    session_id: bytes,
    kem_name: bytes,
    sig_name: bytes,
    shared_secret: bytes,
    *,
    metrics: Optional[Dict[str, object]] = None,
):
    if role not in {"client", "server"}:
        raise NotImplementedError("invalid role")
    if not (isinstance(session_id, bytes) and len(session_id) == 8):
        raise NotImplementedError("session_id must be 8 bytes")
    if not kem_name or not sig_name:
        raise NotImplementedError("kem_name/sig_name empty")
    try:
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        raise NotImplementedError("cryptography not available")
    metrics_ref = metrics
    derive_wall_start = time.time_ns() if metrics_ref is not None else None
    derive_perf_start = time.perf_counter_ns() if metrics_ref is not None else None
    info = b"pq-drone-gcs:kdf:v1|" + session_id + b"|" + kem_name + b"|" + sig_name
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=b"pq-drone-gcs|hkdf|v1",
        info=info,
    )
    okm = hkdf.derive(shared_secret)
    if metrics_ref is not None and derive_perf_start is not None and derive_wall_start is not None:
        derive_perf_end = time.perf_counter_ns()
        derive_wall_end = time.time_ns()
        prefix = "server" if role == "server" else "client"
        metrics_ref[f"kdf_{prefix}_ns"] = derive_perf_end - derive_perf_start
        metrics_ref[f"kdf_{prefix}_wall_start_ns"] = derive_wall_start
        metrics_ref[f"kdf_{prefix}_wall_end_ns"] = derive_wall_end
    key_d2g = okm[:32]
    key_g2d = okm[32:64]

    if role == "client":
        # Drone acts as client; return (send_to_gcs, receive_from_gcs).
        return key_d2g, key_g2d
    else:  # server == GCS
        # GCS perspective: send_to_drone first, receive_from_drone second.
        return key_g2d, key_d2g
def server_gcs_handshake(conn, suite, gcs_sig_secret):
    """Authenticated GCS side handshake.

    Requires a ready oqs.Signature object (with generated key pair). Fails fast if not.
    """
    from oqs.oqs import Signature
    import struct

    conn.settimeout(10.0)

    if not isinstance(gcs_sig_secret, Signature):
        raise ValueError("gcs_sig_secret must be an oqs.Signature object with a loaded keypair")

    # Resolve suite_id by matching suite dict
    suite_id = None
    from core.suites import SUITES
    for sid, s in SUITES.items():
        if dict(s) == suite:
            suite_id = sid
            break
    if suite_id is None:
        raise ValueError("suite not found in registry")

    handshake_metrics: Dict[str, object] = {
        "role": "gcs",
        "suite_id": suite_id,
        "kem_name": suite.get("kem_name"),
        "sig_name": suite.get("sig_name"),
    }
    handshake_wall_start = time.time_ns()
    handshake_perf_start = time.perf_counter_ns()
    hello_wire, ephemeral = build_server_hello(suite_id, gcs_sig_secret, metrics=handshake_metrics)
    handshake_metrics["handshake_wall_start_ns"] = handshake_wall_start
    artifacts = handshake_metrics.setdefault("artifacts", {})
    artifacts.setdefault("server_hello_bytes", len(hello_wire))
    conn.sendall(struct.pack("!I", len(hello_wire)) + hello_wire)

    # Receive KEM ciphertext
    ct_len_bytes = b""
    while len(ct_len_bytes) < 4:
        chunk = conn.recv(4 - len(ct_len_bytes))
        if not chunk:
            raise ConnectionError("Connection closed reading ciphertext length")
        ct_len_bytes += chunk
    ct_len = struct.unpack("!I", ct_len_bytes)[0]
    kem_ct = b""
    while len(kem_ct) < ct_len:
        chunk = conn.recv(ct_len - len(kem_ct))
        if not chunk:
            raise ConnectionError("Connection closed reading ciphertext")
        kem_ct += chunk
    primitives = handshake_metrics.setdefault("primitives", {})
    kem_metrics = primitives.setdefault("kem", {})
    kem_metrics.setdefault("ciphertext_bytes", len(kem_ct))

    tag_len = hashlib.sha256().digest_size
    tag = b""
    while len(tag) < tag_len:
        chunk = conn.recv(tag_len - len(tag))
        if not chunk:
            raise ConnectionError("Connection closed reading drone authentication tag")
        tag += chunk
    artifacts["auth_tag_bytes"] = len(tag)

    expected_tag = hmac.new(_drone_psk_bytes(), hello_wire, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        peer_ip = "unknown"
        try:
            peer_info = conn.getpeername()
            if isinstance(peer_info, tuple) and peer_info:
                peer_ip = str(peer_info[0])
            elif isinstance(peer_info, str) and peer_info:
                peer_ip = peer_info
        except (OSError, ValueError):
            peer_ip = "unknown"
        logger.warning(
            "Rejected drone handshake with bad authentication tag",
            extra={"role": "gcs", "expected_peer": CONFIG["DRONE_HOST"], "received": peer_ip},
        )
        raise HandshakeVerifyError("drone authentication failed")

    shared_secret = server_decapsulate(ephemeral, kem_ct, metrics=handshake_metrics)
    key_send, key_recv = derive_transport_keys(
        "server",
        ephemeral.session_id,
        ephemeral.kem_name.encode("utf-8"),
        ephemeral.sig_name.encode("utf-8"),
        shared_secret,
        metrics=handshake_metrics,
    )
    handshake_metrics["handshake_wall_end_ns"] = time.time_ns()
    handshake_metrics["handshake_total_ns"] = time.perf_counter_ns() - handshake_perf_start
    return (
        key_recv,
        key_send,
        b"",
        b"",
        ephemeral.session_id,
        ephemeral.kem_name,
        ephemeral.sig_name,
        handshake_metrics,
    )

def client_drone_handshake(client_sock, suite, gcs_sig_public):
    # Real handshake implementation with MANDATORY signature verification
    import struct
    
    # Add socket timeout to prevent hanging
    client_sock.settimeout(10.0)
    
    handshake_metrics: Dict[str, object] = {
        "role": "drone",
        "suite_id": suite.get("suite_id") if isinstance(suite, dict) else None,
        "kem_name": suite.get("kem_name") if isinstance(suite, dict) else None,
        "sig_name": suite.get("sig_name") if isinstance(suite, dict) else None,
    }
    handshake_wall_start = time.time_ns()
    handshake_perf_start = time.perf_counter_ns()

    # Receive server hello with length prefix
    hello_len_bytes = b""
    while len(hello_len_bytes) < 4:
        chunk = client_sock.recv(4 - len(hello_len_bytes))
        if not chunk:
            raise NotImplementedError("Connection closed reading hello length")
        hello_len_bytes += chunk
        
    hello_len = struct.unpack("!I", hello_len_bytes)[0]
    hello_wire = b""
    while len(hello_wire) < hello_len:
        chunk = client_sock.recv(hello_len - len(hello_wire))
        if not chunk:
            raise NotImplementedError("Connection closed reading hello")
        hello_wire += chunk
    artifacts = handshake_metrics.setdefault("artifacts", {})
    artifacts["server_hello_bytes"] = len(hello_wire)

    # Parse and VERIFY server hello - NO BYPASS ALLOWED
    # This is critical for security - verification failure must abort
    hello = parse_and_verify_server_hello(
        hello_wire,
        CONFIG["WIRE_VERSION"],
        gcs_sig_public,
        metrics=handshake_metrics,
    )

    expected_kem = suite.get("kem_name") if isinstance(suite, dict) else None
    expected_sig = suite.get("sig_name") if isinstance(suite, dict) else None
    negotiated_kem = hello.kem_name.decode("utf-8") if isinstance(hello.kem_name, bytes) else hello.kem_name
    negotiated_sig = hello.sig_name.decode("utf-8") if isinstance(hello.sig_name, bytes) else hello.sig_name
    if expected_kem and negotiated_kem != expected_kem:
        logger.error(
            "Suite mismatch",
            extra={
                "expected_kem": expected_kem,
                "expected_sig": expected_sig,
                "negotiated_kem": negotiated_kem,
                "negotiated_sig": negotiated_sig,
            },
        )
        raise HandshakeVerifyError(
            f"Downgrade attempt detected: expected {expected_kem}, got {negotiated_kem}"
        )
    if expected_sig and negotiated_sig != expected_sig:
        logger.error(
            "Suite mismatch",
            extra={
                "expected_kem": expected_kem,
                "expected_sig": expected_sig,
                "negotiated_kem": negotiated_kem,
                "negotiated_sig": negotiated_sig,
            },
        )
        raise HandshakeVerifyError(
            f"Downgrade attempt detected: expected {expected_sig}, got {negotiated_sig}"
        )

    # Encapsulate and send KEM ciphertext + authentication tag
    kem_ct, shared_secret = client_encapsulate(hello, metrics=handshake_metrics)
    primitives = handshake_metrics.setdefault("primitives", {})
    kem_metrics = primitives.setdefault("kem", {})
    kem_metrics.setdefault("ciphertext_bytes", len(kem_ct))
    kem_metrics.setdefault("shared_secret_bytes", len(shared_secret))
    tag = hmac.new(_drone_psk_bytes(), hello_wire, hashlib.sha256).digest()
    client_sock.sendall(struct.pack("!I", len(kem_ct)) + kem_ct + tag)
    artifacts["auth_tag_bytes"] = len(tag)
    
    # Derive transport keys
    key_send, key_recv = derive_transport_keys(
        "client",
        hello.session_id,
        hello.kem_name,
        hello.sig_name,
        shared_secret,
        metrics=handshake_metrics,
    )

    handshake_metrics["handshake_wall_start_ns"] = handshake_wall_start
    handshake_metrics["handshake_wall_end_ns"] = time.time_ns()
    handshake_metrics["handshake_total_ns"] = time.perf_counter_ns() - handshake_perf_start

    # Return in expected format (nonce seeds are unused)
    return (
        key_send,
        key_recv,
        b"",
        b"",
        hello.session_id,
        hello.kem_name.decode() if isinstance(hello.kem_name, bytes) else hello.kem_name,
        hello.sig_name.decode() if isinstance(hello.sig_name, bytes) else hello.sig_name,
        handshake_metrics,
    )

