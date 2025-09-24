"""
Network transport proxy orchestration with TCP handshake and UDP encrypt/decrypt loops.

Implements the main proxy logic that:
1. Performs authenticated TCP handshake using PQC KEM + signatures  
2. Bridges plaintext UDP ⇄ encrypted UDP in both directions
3. Uses non-blocking I/O with selectors for single-threaded operation
"""

import socket
import selectors
import time
from typing import Optional, Dict, Tuple
from contextlib import contextmanager

from core.config import CONFIG
from core.suites import get_suite
from core.handshake import server_gcs_handshake, client_drone_handshake
from core.aead import Sender, Receiver


class ProxyCounters:
    """Simple counters for proxy statistics."""
    
    def __init__(self):
        self.ptx_out = 0      # plaintext packets sent out to app
        self.ptx_in = 0       # plaintext packets received from app  
        self.enc_out = 0      # encrypted packets sent to peer
        self.enc_in = 0       # encrypted packets received from peer
        self.drops = 0        # packets dropped (AEAD failures, replay, etc.)
    
    def to_dict(self) -> Dict[str, int]:
        return {
            "ptx_out": self.ptx_out,
            "ptx_in": self.ptx_in, 
            "enc_out": self.enc_out,
            "enc_in": self.enc_in,
            "drops": self.drops
        }


def _validate_config(cfg: dict) -> None:
    """Validate required configuration keys are present."""
    required_keys = [
        "TCP_HANDSHAKE_PORT", "UDP_DRONE_RX", "UDP_GCS_RX", 
        "DRONE_PLAINTEXT_TX", "DRONE_PLAINTEXT_RX",
        "GCS_PLAINTEXT_TX", "GCS_PLAINTEXT_RX", 
        "DRONE_HOST", "GCS_HOST", "REPLAY_WINDOW"
    ]
    
    for key in required_keys:
        if key not in cfg:
            raise NotImplementedError(f"CONFIG missing: {key}")


def _perform_handshake(role: str, suite: dict, gcs_sig_secret: Optional[bytes], gcs_sig_public: Optional[bytes], cfg: dict, stop_after_seconds: Optional[float] = None) -> Tuple[bytes, bytes, bytes, bytes, bytes]:
    """Perform TCP handshake and return derived keys and session_id."""
    
    if role == "gcs":
        if gcs_sig_secret is None:
            raise NotImplementedError("GCS signature secret not provided")
            
        # GCS server: bind and accept one connection
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(('0.0.0.0', cfg["TCP_HANDSHAKE_PORT"]))
        server_sock.listen(1)
        
        # Set timeout for accept() to prevent hanging when no drone connects
        timeout = stop_after_seconds if stop_after_seconds is not None else 30.0
        server_sock.settimeout(timeout)
        
        try:
            try:
                conn, addr = server_sock.accept()
                try:
                    result = server_gcs_handshake(conn, suite, gcs_sig_secret)
                    return result  # (k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id)
                finally:
                    conn.close()
            except socket.timeout:
                raise NotImplementedError("No drone connection received within timeout")
        finally:
            server_sock.close()
            
    elif role == "drone":
        if gcs_sig_public is None:
            raise NotImplementedError("GCS signature public key not provided")
            
        # Drone client: connect to GCS
        client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client_sock.connect((cfg["GCS_HOST"], cfg["TCP_HANDSHAKE_PORT"]))
            result = client_drone_handshake(client_sock, suite, gcs_sig_public)
            return result  # Already includes session_id
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
            enc_sock.bind(('0.0.0.0', cfg["UDP_DRONE_RX"]))
            enc_sock.setblocking(False)
            sockets['encrypted'] = enc_sock
            
            # Plaintext ingress - receive from local app
            ptx_in_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  
            ptx_in_sock.bind(('127.0.0.1', cfg["DRONE_PLAINTEXT_TX"]))
            ptx_in_sock.setblocking(False)
            sockets['plaintext_in'] = ptx_in_sock
            
            # Plaintext egress - send to local app (no bind needed)
            ptx_out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets['plaintext_out'] = ptx_out_sock
            
            # Peer addresses
            sockets['encrypted_peer'] = (cfg["GCS_HOST"], cfg["UDP_GCS_RX"])
            sockets['plaintext_peer'] = ('127.0.0.1', cfg["DRONE_PLAINTEXT_RX"])
            
        elif role == "gcs":
            # Encrypted socket - receive from Drone
            enc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            enc_sock.bind(('0.0.0.0', cfg["UDP_GCS_RX"]))
            enc_sock.setblocking(False) 
            sockets['encrypted'] = enc_sock
            
            # Plaintext ingress - receive from local app
            ptx_in_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ptx_in_sock.bind(('127.0.0.1', cfg["GCS_PLAINTEXT_TX"]))
            ptx_in_sock.setblocking(False)
            sockets['plaintext_in'] = ptx_in_sock
            
            # Plaintext egress - send to local app (no bind needed)
            ptx_out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets['plaintext_out'] = ptx_out_sock
            
            # Peer addresses
            sockets['encrypted_peer'] = (cfg["DRONE_HOST"], cfg["UDP_DRONE_RX"])
            sockets['plaintext_peer'] = ('127.0.0.1', cfg["GCS_PLAINTEXT_RX"])
        
        yield sockets
        
    finally:
        # Cleanup all sockets
        for sock in sockets.values():
            if isinstance(sock, socket.socket):
                try:
                    sock.close()
                except:
                    pass


def run_proxy(*, role: str, suite: dict, cfg: dict,
              gcs_sig_secret: Optional[bytes] = None,
              gcs_sig_public: Optional[bytes] = None,
              stop_after_seconds: Optional[float] = None
              ) -> Dict[str, int]:
    """
    Start a blocking proxy process for `role` in {"drone","gcs"}.

    - Performs TCP handshake (server on GCS, client on Drone).
    - Bridges plaintext UDP <-> encrypted UDP in both directions.
    - Returns a dict of simple counters on clean exit:
      {"ptx_out": int, "ptx_in": int, "enc_out": int, "enc_in": int, "drops": int}

    Required cfg keys:
      TCP_HANDSHAKE_PORT, UDP_DRONE_RX, UDP_GCS_RX, DRONE_PLAINTEXT_TX, DRONE_PLAINTEXT_RX,
      GCS_PLAINTEXT_TX, GCS_PLAINTEXT_RX, DRONE_HOST, GCS_HOST, REPLAY_WINDOW

    Security constraints:
      - Header used as AAD (enforced by core.aead.Sender/Receiver).
      - 12-byte counter IVs (from core.aead).
      - Replay window enforced (from core.aead).
    """
    
    # Validate inputs
    if role not in {"drone", "gcs"}:
        raise ValueError(f"Invalid role: {role}")
    
    _validate_config(cfg)
    
    counters = ProxyCounters()
    start_time = time.time()
    
    # Perform handshake and get session keys
    handshake_result = _perform_handshake(role, suite, gcs_sig_secret, gcs_sig_public, cfg, stop_after_seconds)
    k_d2g, k_g2d, nseed_d2g, nseed_g2d, session_id = handshake_result
    
    # Setup AEAD endpoints based on role
    from core.suites import header_ids_for_suite
    from core.aead import AeadIds
    
    header_ids = header_ids_for_suite(suite)
    aead_ids = AeadIds(*header_ids)
    
    if role == "drone":
        sender = Sender(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, k_d2g)
        receiver = Receiver(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, k_g2d, cfg["REPLAY_WINDOW"])
    else:  # gcs
        sender = Sender(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, k_g2d)
        receiver = Receiver(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, k_d2g, cfg["REPLAY_WINDOW"])
    
    # Setup UDP sockets and run main loop
    with _setup_sockets(role, cfg) as sockets:
        selector = selectors.DefaultSelector()
        
        # Register sockets for reading
        selector.register(sockets['encrypted'], selectors.EVENT_READ, data='encrypted')
        selector.register(sockets['plaintext_in'], selectors.EVENT_READ, data='plaintext_in')
        
        try:
            while True:
                # Check stop condition
                if stop_after_seconds is not None:
                    if time.time() - start_time >= stop_after_seconds:
                        break
                
                # Poll for ready sockets with timeout
                events = selector.select(timeout=0.1)
                
                for key, mask in events:
                    sock = key.fileobj
                    data_type = key.data
                    
                    if data_type == 'plaintext_in':
                        # Plaintext ingress: encrypt and forward
                        try:
                            payload, addr = sock.recvfrom(2048)
                            if not payload:
                                continue
                                
                            counters.ptx_in += 1
                            
                            # Encrypt payload
                            wire = sender.encrypt(payload)
                            
                            # Send to encrypted peer
                            try:
                                sockets['encrypted'].sendto(wire, sockets['encrypted_peer'])
                                counters.enc_out += 1
                            except socket.error:
                                counters.drops += 1
                                
                        except socket.error:
                            continue
                    
                    elif data_type == 'encrypted':
                        # Encrypted ingress: decrypt and forward
                        try:
                            wire, addr = sock.recvfrom(2048)
                            if not wire:
                                continue
                                
                            counters.enc_in += 1
                            
                            # Decrypt payload
                            try:
                                plaintext = receiver.unpack(wire)
                                if plaintext is None:
                                    # Replay or tampered packet
                                    counters.drops += 1
                                    continue
                            except Exception:
                                # AEAD failure
                                counters.drops += 1
                                continue
                                
                            # Forward to plaintext peer
                            try:
                                sockets['plaintext_out'].sendto(plaintext, sockets['plaintext_peer'])
                                counters.ptx_out += 1
                            except socket.error:
                                counters.drops += 1
                                
                        except socket.error:
                            continue
                            
        except KeyboardInterrupt:
            pass
        finally:
            selector.close()
    
    return counters.to_dict()