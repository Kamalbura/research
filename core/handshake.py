from dataclasses import dataclass
import os
import struct
from core.config import CONFIG
from core.suites import get_suite
from oqs.oqs import KeyEncapsulation, Signature

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

@dataclass
class ServerEphemeral:
    kem_name: str
    sig_name: str
    session_id: bytes
    kem_obj: object  # oqs.KeyEncapsulation instance

def build_server_hello(suite_id: str, server_sig_obj):
    suite = get_suite(suite_id)
    if not suite:
        raise NotImplementedError("suite_id not found")
    version = CONFIG["WIRE_VERSION"]
    kem_name = suite["kem_name"].encode()
    sig_name = suite["sig_name"].encode()
    if not kem_name or not sig_name:
        raise NotImplementedError("kem_name/sig_name empty")
    if not isinstance(server_sig_obj, Signature):
        raise NotImplementedError("server_sig_obj must be oqs.Signature")
    session_id = os.urandom(8)
    kem_obj = KeyEncapsulation(kem_name.decode())
    kem_pub = kem_obj.generate_keypair()
    transcript = b"pq-drone-gcs:v1|" + session_id + b"|" + kem_name + b"|" + sig_name + b"|" + kem_pub
    signature = server_sig_obj.sign(transcript)
    wire = struct.pack("!B", version)
    wire += struct.pack("!H", len(kem_name)) + kem_name
    wire += struct.pack("!H", len(sig_name)) + sig_name
    wire += session_id
    wire += struct.pack("!I", len(kem_pub)) + kem_pub
    wire += struct.pack("!H", len(signature)) + signature
    ephemeral = ServerEphemeral(
        kem_name=kem_name.decode(),
        sig_name=sig_name.decode(),
        session_id=session_id,
        kem_obj=kem_obj
    )
    return wire, ephemeral

def parse_and_verify_server_hello(wire: bytes, expected_version: int, server_sig_pub: bytes) -> ServerHello:
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
    transcript = b"pq-drone-gcs:v1|" + session_id + b"|" + kem_name + b"|" + sig_name + b"|" + kem_pub
    try:
        sig = Signature(sig_name.decode())
        if not sig.verify(transcript, signature, server_sig_pub):
            raise HandshakeVerifyError("bad signature")
    except HandshakeVerifyError:
        raise
    except Exception:
        raise HandshakeVerifyError("signature verification failed")
    return ServerHello(
        version=version,
        kem_name=kem_name,
        sig_name=sig_name,
        session_id=session_id,
        kem_pub=kem_pub,
        signature=signature
    )

def client_encapsulate(server_hello: ServerHello):
    try:
        kem = KeyEncapsulation(server_hello.kem_name.decode())
        kem_ct, shared_secret = kem.encap_secret(server_hello.kem_pub)
        return kem_ct, shared_secret
    except Exception:
        raise NotImplementedError("client_encapsulate failed")

def server_decapsulate(ephemeral: ServerEphemeral, kem_ct: bytes):
    try:
        shared_secret = ephemeral.kem_obj.decap_secret(kem_ct)
        return shared_secret
    except Exception:
        raise NotImplementedError("server_decapsulate failed")

def derive_transport_keys(role: str, session_id: bytes, kem_name: bytes, sig_name: bytes, shared_secret: bytes):
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
    info = b"pq-drone-gcs:kdf:v1|" + session_id + b"|" + kem_name + b"|" + sig_name
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=None,
        info=info
    )
    okm = hkdf.derive(shared_secret)
    key_send = okm[:32]
    key_recv = okm[32:64]
    if role == "client":
        return key_send, key_recv
    else:
        return key_recv, key_send
def server_gcs_handshake(conn, suite, gcs_sig_secret):
    # Minimal handshake logic for GCS server role
    # This should perform the server-side handshake and return derived keys and session_id
    # For demonstration, return dummy values (replace with real logic as needed)
    import os
    k_d2g = os.urandom(32)
    k_g2d = os.urandom(32) 
    nseed_d2g = os.urandom(32)
    nseed_g2d = os.urandom(32)
    session_id = os.urandom(8)
    return k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id

def client_drone_handshake(client_sock, suite, gcs_sig_public):
    # Minimal handshake logic for drone client role
    # This should perform the client-side handshake and return derived keys and session_id
    # For demonstration, return dummy values (replace with real logic as needed)
    import os
    k_d2g = os.urandom(32)
    k_g2d = os.urandom(32)
    nseed_d2g = os.urandom(32)
    nseed_g2d = os.urandom(32)
    session_id = os.urandom(8)
    return k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id


from dataclasses import dataclass
from core.config import CONFIG
from core.suites import get_suite
from oqs.oqs import KeyEncapsulation, Signature

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

@dataclass
class ServerEphemeral:
    kem_name: str
    sig_name: str
    session_id: bytes
    kem_obj: object  # oqs.KeyEncapsulation instance


import os
import struct

def build_server_hello(suite_id: str, server_sig_obj):
    suite = get_suite(suite_id)
    if not suite:
        raise NotImplementedError("suite_id not found")
    version = CONFIG["WIRE_VERSION"]
    kem_name = suite["kem_name"].encode()
    sig_name = suite["sig_name"].encode()
    if not kem_name or not sig_name:
        raise NotImplementedError("kem_name or sig_name empty")
    session_id = os.urandom(8)
    kem_obj = KeyEncapsulation(suite["kem_name"])
    kem_pub = kem_obj.generate_keypair()
    transcript = b"pq-drone-gcs:v1|" + session_id + b"|" + kem_name + b"|" + sig_name + b"|" + kem_pub
    signature = server_sig_obj.sign(transcript)
    wire = struct.pack(
        "!B H {}s H {}s 8s I {}s H {}s".format(
            len(kem_name), len(sig_name), len(kem_pub), len(signature)
        ),
        version,
        len(kem_name), kem_name,
        len(sig_name), sig_name,
        session_id,
        len(kem_pub), kem_pub,
        len(signature), signature
    )
    eph = ServerEphemeral(
        kem_name=suite["kem_name"],
        sig_name=suite["sig_name"],
        session_id=session_id,
        kem_obj=kem_obj
    )
    return wire, eph


def parse_and_verify_server_hello(wire: bytes, expected_version: int, server_sig_pub: bytes):
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
    transcript = b"pq-drone-gcs:v1|" + session_id + b"|" + kem_name + b"|" + sig_name + b"|" + kem_pub
    try:
        sig = Signature(sig_name.decode())
        if not sig.verify(transcript, signature, server_sig_pub):
            raise HandshakeVerifyError("bad signature")
    except HandshakeVerifyError:
        raise
    except Exception:
        raise HandshakeVerifyError("signature verification failed")
    return ServerHello(
        version=version,
        kem_name=kem_name,
        sig_name=sig_name,
        session_id=session_id,
        kem_pub=kem_pub,
        signature=signature
    )


def client_encapsulate(server_hello: ServerHello):
    try:
        kem = KeyEncapsulation(server_hello.kem_name.decode())
        kem_ct, shared_secret = kem.encap_secret(server_hello.kem_pub)
        return kem_ct, shared_secret
    except Exception:
        raise NotImplementedError("client_encapsulate failed")

def server_decapsulate(ephemeral: ServerEphemeral, kem_ct: bytes):
    try:
        shared_secret = ephemeral.kem_obj.decap_secret(kem_ct)
        return shared_secret
    except Exception:
        raise NotImplementedError("server_decapsulate failed")

def derive_transport_keys(role: str, session_id: bytes, kem_name: bytes, sig_name: bytes, shared_secret: bytes):
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
    info = b"pq-drone-gcs:kdf:v1|" + session_id + b"|" + kem_name + b"|" + sig_name
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=None,
        info=info
    )
    keymat = hkdf.derive(shared_secret)
    key_send = keymat[:32]
    key_recv = keymat[32:64]
    if role == "client":
        return key_send, key_recv
    else:
        return key_recv, key_send