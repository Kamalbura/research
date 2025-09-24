"""
Authenticated PQC handshake protocol implementation.

Provides client (drone) and server (GCS) handshake functions with KEM key exchange,
signature-based authentication, and HKDF key derivation.
"""

import os
import socket
import struct
from typing import Tuple, Dict

from oqs.oqs import KeyEncapsulation, Signature, MechanismNotSupportedError
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .suites import suite_bytes_for_hkdf


class HandshakeError(Exception):
    """Base exception for handshake protocol errors."""
    pass


class SignatureVerifyError(HandshakeError):
    """Signature verification failed during handshake."""
    pass


def _pack_len_bytes(prefix_fmt: str, b: bytes) -> bytes:
    """Pack length-prefixed bytes with given format.
    
    Args:
        prefix_fmt: struct format for length (e.g., "!H" for uint16)
        b: bytes to pack
        
    Returns:
        Packed bytes with length prefix
    """
    return struct.pack(prefix_fmt, len(b)) + b


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Receive exactly n bytes from socket.
    
    Args:
        sock: Socket to receive from
        n: Number of bytes to receive
        
    Returns:
        Received bytes
        
    Raises:
        HandshakeError: If connection closed or insufficient data
    """
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise HandshakeError("Connection closed during handshake")
        data += chunk
    return data


def _recv_len_prefixed(sock: socket.socket, fmt: str) -> bytes:
    """Receive length-prefixed bytes with given format.
    
    Args:
        sock: Socket to receive from  
        fmt: struct format for length (e.g., "!H" for uint16)
        
    Returns:
        Received bytes without length prefix
    """
    len_size = struct.calcsize(fmt)
    len_bytes = _recv_exact(sock, len_size)
    length = struct.unpack(fmt, len_bytes)[0]
    return _recv_exact(sock, length)


def _derive_keys(shared_secret: bytes, session_id: bytes, suite: Dict) -> Tuple[bytes, bytes, bytes, bytes]:
    """Derive session keys using HKDF-SHA256.
    
    Args:
        shared_secret: 32-byte KEM shared secret
        session_id: 8-byte session identifier
        suite: Suite configuration dictionary
        
    Returns:
        Tuple of (k_d2g, k_g2d, nseed_d2g, nseed_g2d)
    """
    # per FIPS-203 Kyber shared secret length is 32 bytes
    if len(shared_secret) != 32:
        raise HandshakeError(f"Invalid shared secret length: {len(shared_secret)}")
    
    suite_bytes = suite_bytes_for_hkdf(suite)
    
    # HKDF-Expand for each key with distinct info
    def expand_key(purpose: str, length: int) -> bytes:
        info = b"pq-drone-gcs:" + purpose.encode('ascii') + b":" + session_id + suite_bytes
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=length,
            salt=None,  # Use default salt
            info=info,
        )
        return hkdf.derive(shared_secret)
    
    k_d2g = expand_key("d2g:key", 32)
    k_g2d = expand_key("g2d:key", 32) 
    nseed_d2g = expand_key("d2g:nonce", 8)
    nseed_g2d = expand_key("g2d:nonce", 8)
    
    return (k_d2g, k_g2d, nseed_d2g, nseed_g2d)


def client_drone_handshake(sock: socket.socket, suite: Dict, gcs_sig_pub: bytes) -> Tuple[bytes, bytes, bytes, bytes, bytes]:
    """Execute drone (client) side of handshake protocol.
    
    Args:
        sock: Connected TCP socket to GCS
        suite: Suite configuration dictionary
        gcs_sig_pub: GCS signature public key bytes
        
    Returns:
        Tuple of (k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id)
        
    Raises:
        HandshakeError: On protocol or crypto errors
        SignatureVerifyError: On signature verification failure
    """
    try:
        # Initialize KEM for this suite
        kem = KeyEncapsulation(suite["kem_name"])
        sig = Signature(suite["sig_name"])
    except (RuntimeError, MechanismNotSupportedError) as e:
        raise NotImplementedError(f"Suite not supported by local oqs build: {e}")
    
    # Receive GCS hello: session_id(8), kem_name_len|kem_name, gcs_kem_pub_len|gcs_kem_pub, signature_len|signature
    session_id = _recv_exact(sock, 8)
    kem_name = _recv_len_prefixed(sock, "!H")
    gcs_kem_pub = _recv_len_prefixed(sock, "!I")
    signature = _recv_len_prefixed(sock, "!I")
    
    # Verify KEM name matches suite
    expected_kem = suite["kem_name"].encode('ascii')
    if kem_name != expected_kem:
        raise HandshakeError(f"KEM mismatch: expected {expected_kem}, got {kem_name}")
    
    # Construct transcript for signature verification
    # T = session_id || kem_name || gcs_kem_pub
    transcript = session_id + kem_name + gcs_kem_pub
    
    # Verify GCS signature over transcript
    try:
        if not sig.verify(transcript, signature, gcs_sig_pub):
            raise SignatureVerifyError("GCS signature verification failed")
    except Exception as e:
        raise SignatureVerifyError(f"GCS signature verification failed: {e}")
    
    # Encapsulate against GCS KEM public key
    try:
        ciphertext, shared_secret = kem.encap_secret(gcs_kem_pub)
    except Exception as e:
        raise HandshakeError(f"KEM encapsulation failed: {e}")
    
    # Send ciphertext to GCS
    sock.sendall(_pack_len_bytes("!I", ciphertext))
    
    # Derive session keys
    keys = _derive_keys(shared_secret, session_id, suite)
    return keys + (session_id,)


def server_gcs_handshake(conn: socket.socket, suite: Dict, gcs_sig_secret: bytes) -> Tuple[bytes, bytes, bytes, bytes, bytes]:
    """Execute GCS (server) side of handshake protocol.
    
    Args:
        conn: Connected TCP socket to drone
        suite: Suite configuration dictionary  
        gcs_sig_secret: GCS signature secret key bytes
        
    Returns:
        Tuple of (k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id)
        
    Raises:
        HandshakeError: On protocol or crypto errors
        NotImplementedError: If signature secret not provided
    """
    if not gcs_sig_secret:
        raise NotImplementedError("GCS signature secret not provided")
    
    try:
        # Initialize KEM and signature for this suite
        kem = KeyEncapsulation(suite["kem_name"])
        sig = Signature(suite["sig_name"])
    except (RuntimeError, MechanismNotSupportedError) as e:
        raise NotImplementedError(f"Suite not supported by local oqs build: {e}")
    
    # Generate ephemeral KEM keypair
    gcs_kem_pub = kem.generate_keypair()
    
    # Generate session ID
    session_id = os.urandom(8)
    
    # Prepare KEM name
    kem_name = suite["kem_name"].encode('ascii')
    
    # Construct transcript for signing
    # T = session_id || kem_name || gcs_kem_pub  
    transcript = session_id + kem_name + gcs_kem_pub
    
    # Sign transcript with GCS long-term key  
    try:
        # Create signature object with provided secret key
        sig_obj = Signature(suite["sig_name"], secret_key=gcs_sig_secret)
        signature = sig_obj.sign(transcript)
    except Exception as e:
        raise HandshakeError(f"Signature generation failed: {e}")
    
    # Send GCS hello: session_id(8), kem_name_len|kem_name, gcs_kem_pub_len|gcs_kem_pub, signature_len|signature
    # Note: We don't send sig_pub as it's pre-provisioned on drone
    conn.sendall(session_id)
    conn.sendall(_pack_len_bytes("!H", kem_name))
    conn.sendall(_pack_len_bytes("!I", gcs_kem_pub))
    conn.sendall(_pack_len_bytes("!I", signature))
    
    # Receive drone response: ct_len|ct
    ciphertext = _recv_len_prefixed(conn, "!I")
    
    # Decapsulate to recover shared secret
    try:
        shared_secret = kem.decap_secret(ciphertext)
    except Exception as e:
        raise HandshakeError(f"KEM decapsulation failed: {e}")
    
    # Derive session keys
    keys = _derive_keys(shared_secret, session_id, suite)
    
    return keys + (session_id,)