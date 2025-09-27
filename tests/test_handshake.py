
import socket
import struct
import threading

import pytest
pytest.importorskip("oqs.oqs")
pytest.importorskip("cryptography.hazmat.primitives.kdf.hkdf")
from core.handshake import (
    build_server_hello,
    parse_and_verify_server_hello,
    client_encapsulate,
    server_decapsulate,
    derive_transport_keys,
    HandshakeFormatError,
    HandshakeVerifyError,
    server_gcs_handshake,
)
from core.suites import get_suite
from core.config import CONFIG
from oqs.oqs import Signature

def test_handshake_happy_path():
    suite_id = "cs-kyber768-aesgcm-dilithium3"
    suite = get_suite(suite_id)
    sig = Signature("ML-DSA-65")
    pub = sig.generate_keypair()
    wire, eph = build_server_hello(suite_id, sig)
    hello = parse_and_verify_server_hello(wire, CONFIG["WIRE_VERSION"], pub)
    assert len(hello.challenge) == 8
    ct, ss_c = client_encapsulate(hello)
    ss_s = server_decapsulate(eph, ct)
    assert ss_c == ss_s
    cs, cr = derive_transport_keys("client", hello.session_id, hello.kem_name, hello.sig_name, ss_c)
    ss, sr = derive_transport_keys("server", hello.session_id, hello.kem_name, hello.sig_name, ss_s)
    assert cs == sr and cr == ss
    assert len(cs) == 32 and len(cr) == 32
    assert len(ss) == 32 and len(sr) == 32

def test_signature_failure():
    suite_id = "cs-kyber768-aesgcm-dilithium3"
    suite = get_suite(suite_id)
    sig = Signature("ML-DSA-65")
    pub = sig.generate_keypair()
    wire, eph = build_server_hello(suite_id, sig)
    offset = 1 + 2 + len(suite["kem_name"]) + 2 + len(suite["sig_name"]) + 8 + 8 + 4
    wire = bytearray(wire)
    wire[offset] ^= 0x01
    with pytest.raises(HandshakeVerifyError):
        parse_and_verify_server_hello(bytes(wire), CONFIG["WIRE_VERSION"], pub)

def test_format_failure_bad_version():
    suite_id = "cs-kyber768-aesgcm-dilithium3"
    suite = get_suite(suite_id)
    sig = Signature("ML-DSA-65")
    pub = sig.generate_keypair()
    wire, eph = build_server_hello(suite_id, sig)
    wire = bytearray(wire)
    wire[0] ^= 0xFF
    with pytest.raises(HandshakeFormatError):
        parse_and_verify_server_hello(bytes(wire), CONFIG["WIRE_VERSION"], pub)

def test_mismatched_role_kdf():
    suite_id = "cs-kyber768-aesgcm-dilithium3"
    suite = get_suite(suite_id)
    sig = Signature("ML-DSA-65")
    pub = sig.generate_keypair()
    wire, eph = build_server_hello(suite_id, sig)
    hello = parse_and_verify_server_hello(wire, CONFIG["WIRE_VERSION"], pub)
    ct, ss_c = client_encapsulate(hello)
    ss_s = server_decapsulate(eph, ct)
    cs, cr = derive_transport_keys("client", hello.session_id, hello.kem_name, hello.sig_name, ss_c)
    cs2, cr2 = derive_transport_keys("client", hello.session_id, hello.kem_name, hello.sig_name, ss_s)
    assert cs != cr2 and cr != cs2


def _recv_exact(sock, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise RuntimeError("unexpected EOF")
        chunks.extend(chunk)
    return bytes(chunks)


def test_gcs_rejects_bad_drone_auth():
    suite_id = "cs-kyber768-aesgcm-dilithium3"
    suite = get_suite(suite_id)
    sig = Signature(suite["sig_name"])
    sig.generate_keypair()

    srv, cli = socket.socketpair()

    def client_behavior() -> None:
        try:
            hello_len = struct.unpack("!I", _recv_exact(cli, 4))[0]
            _recv_exact(cli, hello_len)
            cli.sendall(struct.pack("!I", 0))
            cli.sendall(b"\x00" * 32)
        finally:
            cli.close()

    t = threading.Thread(target=client_behavior)
    t.start()
    try:
        with pytest.raises(HandshakeVerifyError):
            server_gcs_handshake(srv, suite, sig)
    finally:
        srv.close()
        t.join()