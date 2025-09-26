"""
End-to-end tests for the PQC proxy network transport.

Tests the complete flow: TCP handshake -> UDP encrypt/decrypt bridging on localhost.
"""

import socket
import threading
import time
import os
from unittest.mock import patch

import pytest
from oqs.oqs import Signature

from core.config import CONFIG
from core.suites import get_suite
from core.async_proxy import run_proxy


def _alloc_port(sock_type=socket.SOCK_STREAM) -> int:
    """Reserve an available loopback port for tests."""
    with socket.socket(socket.AF_INET, sock_type) as sock:
        if sock_type == socket.SOCK_DGRAM:
            sock.bind(("127.0.0.1", 0))
        else:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
        return sock.getsockname()[1]


class TestEndToEndProxy:
    """End-to-end proxy tests on localhost."""
    
    @pytest.fixture
    def suite(self):
        """Default test suite."""
        return get_suite("cs-kyber768-aesgcm-dilithium3")
    
    @pytest.fixture
    def gcs_keypair(self, suite):
        """Generate GCS signature keypair."""
        sig = Signature(suite["sig_name"])
        gcs_sig_public = sig.generate_keypair()
        # Return the signature object itself, not the exported secret
        # This matches our updated handshake security requirements
        return gcs_sig_public, sig
    
    def test_bidirectional_plaintext_forwarding(self, suite, gcs_keypair):
        """Test happy path: bidirectional UDP forwarding through encrypted tunnel."""
        gcs_sig_public, gcs_sig_object = gcs_keypair
        
        # Create synchronization event to eliminate race conditions
        gcs_ready_event = threading.Event()
        
        # Reserve dedicated ports to avoid clashes with running proxies
        handshake_port = _alloc_port()
        udp_gcs_rx = _alloc_port(socket.SOCK_DGRAM)
        udp_drone_rx = _alloc_port(socket.SOCK_DGRAM)

        # Use different ports for test to avoid conflicts
        test_config = CONFIG.copy()
        test_config.update({
            "TCP_HANDSHAKE_PORT": handshake_port,
            "UDP_GCS_RX": udp_gcs_rx,
            "UDP_DRONE_RX": udp_drone_rx,
            "DRONE_PLAINTEXT_TX": 15550,  # Apps send to drone proxy here
            "DRONE_PLAINTEXT_RX": 15551,  # Apps receive from drone proxy here
            "GCS_PLAINTEXT_TX": 15552,    # Apps send to GCS proxy here  
            "GCS_PLAINTEXT_RX": 15553,    # Apps receive from GCS proxy here
            "DRONE_HOST": "127.0.0.1",    # Force loopback for encrypted peer
            "GCS_HOST": "127.0.0.1",      # Force loopback for handshake/peer
        })
        
        # Storage for proxy results
        gcs_counters = None
        drone_counters = None
        gcs_error = None
        drone_error = None
        
        def run_gcs_proxy():
            nonlocal gcs_counters, gcs_error
            try:
                gcs_counters = run_proxy(
                    role="gcs",
                    suite=suite,
                    cfg=test_config,
                    gcs_sig_secret=gcs_sig_object,  # Pass signature object
                    gcs_sig_public=None,
                    stop_after_seconds=3.0,  # Increased timeout
                    ready_event=gcs_ready_event  # Signal when ready
                )
            except Exception as e:
                gcs_error = e
        
        def run_drone_proxy():
            nonlocal drone_counters, drone_error
            try:
                # Wait for GCS to be ready instead of arbitrary sleep
                if not gcs_ready_event.wait(timeout=5):
                    raise TimeoutError("GCS proxy failed to start within timeout")
                
                drone_counters = run_proxy(
                    role="drone", 
                    suite=suite,
                    cfg=test_config,
                    gcs_sig_secret=None,
                    gcs_sig_public=gcs_sig_public,
                    stop_after_seconds=3.0  # Increased timeout
                )
            except Exception as e:
                drone_error = e
        
        # Start receiver sockets first
        received_at_gcs = None
        received_at_drone = None
        
        def receive_at_gcs():
            nonlocal received_at_gcs
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
                    receiver.bind(('127.0.0.1', test_config["GCS_PLAINTEXT_RX"]))
                    receiver.settimeout(2.5)  # Increased timeout
                    data, addr = receiver.recvfrom(1024)
                    received_at_gcs = data
            except (socket.timeout, OSError):
                pass
        
        def receive_at_drone():
            nonlocal received_at_drone
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
                    receiver.bind(('127.0.0.1', test_config["DRONE_PLAINTEXT_RX"]))
                    receiver.settimeout(2.5)  # Increased timeout
                    data, addr = receiver.recvfrom(1024)
                    received_at_drone = data
            except (socket.timeout, OSError):
                pass
        
        # Start receiver threads first
        gcs_recv_thread = threading.Thread(target=receive_at_gcs)
        drone_recv_thread = threading.Thread(target=receive_at_drone)
        
        gcs_recv_thread.start()
        drone_recv_thread.start()
        
        # Small delay to let receivers start
        time.sleep(0.1)
        
        # Start proxy threads
        gcs_thread = threading.Thread(target=run_gcs_proxy)
        drone_thread = threading.Thread(target=run_drone_proxy)
        
        gcs_thread.start()
        drone_thread.start()
        
        # Allow handshake to complete
        time.sleep(0.7)
        
        # Test drone -> gcs forwarding
        drone_to_gcs_data = b"Hello from drone"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(drone_to_gcs_data, ('127.0.0.1', test_config["DRONE_PLAINTEXT_TX"]))
        
        # Small delay
        time.sleep(0.1)
        
        # Test gcs -> drone forwarding  
        gcs_to_drone_data = b"Hello from GCS"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(gcs_to_drone_data, ('127.0.0.1', test_config["GCS_PLAINTEXT_TX"]))
        
        # Wait for everything to complete
        gcs_recv_thread.join(timeout=2.0)
        drone_recv_thread.join(timeout=2.0)
        
        gcs_thread.join(timeout=3.0)
        drone_thread.join(timeout=3.0)
        
        # Check for proxy errors
        if gcs_error:
            raise gcs_error
        if drone_error:
            raise drone_error
        
        # Verify counters exist (proxies ran)
        assert gcs_counters is not None
        assert drone_counters is not None
        
        # Assert successful forwarding both directions
        assert received_at_gcs is not None, "GCS did not receive data from drone"
        assert received_at_gcs == drone_to_gcs_data, (
            f"Mismatch drone->GCS: expected {drone_to_gcs_data!r} got {received_at_gcs!r}"
        )
        assert received_at_drone is not None, "Drone did not receive data from GCS"
        assert received_at_drone == gcs_to_drone_data, (
            f"Mismatch GCS->drone: expected {gcs_to_drone_data!r} got {received_at_drone!r}"
        )

        # Basic sanity on counters (at least one packet each direction was processed)
        assert gcs_counters["enc_in"] >= 1
        assert drone_counters["enc_in"] >= 1
    
    def test_tampered_packet_dropped(self, suite, gcs_keypair):
        """Test that tampered encrypted packets are dropped."""
        gcs_sig_public, gcs_sig_secret = gcs_keypair
        
        # We'll test packet tampering by directly testing the AEAD receiver
        from core.aead import Sender, Receiver, AeadIds
        from core.suites import header_ids_for_suite
        
        # Create sender and receiver with same key
        key = os.urandom(32)
        session_id = os.urandom(8)
        
        header_ids = header_ids_for_suite(suite)
        aead_ids = AeadIds(*header_ids)
        sender = Sender(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, key)
        receiver = Receiver(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, key, 1024)
        
        # Create a valid packet
        original_payload = b"test payload"
        wire = sender.encrypt(original_payload)
        
        # Verify original packet decrypts correctly
        decrypted = receiver.decrypt(wire)
        assert decrypted == original_payload
        
        # Tamper with the header (flip one byte)
        tampered_wire = bytearray(wire)
        tampered_wire[5] ^= 0x01  # Flip a bit in the header
        tampered_wire = bytes(tampered_wire)
        
        # Create fresh receiver to avoid replay detection
        receiver2 = Receiver(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, key, 1024)
        
        # Tampered packet should be dropped
        decrypted_tampered = receiver2.decrypt(tampered_wire)
        assert decrypted_tampered is None
    
    def test_replay_packet_dropped(self, suite, gcs_keypair):
        """Test that replayed packets are dropped."""
        from core.aead import Sender, Receiver, AeadIds
        from core.suites import header_ids_for_suite
        
        # Create sender and receiver
        key = os.urandom(32)
        session_id = os.urandom(8)
        
        header_ids = header_ids_for_suite(suite)
        aead_ids = AeadIds(*header_ids)
        sender = Sender(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, key)
        receiver = Receiver(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, key, 1024)
        
        # Send first packet
        payload = b"original packet"
        wire = sender.encrypt(payload)
        
        # First decryption should succeed
        decrypted1 = receiver.decrypt(wire)
        assert decrypted1 == payload
        
        # Replay same packet - should be dropped
        decrypted2 = receiver.decrypt(wire)
        assert decrypted2 is None
    
    def test_missing_config_keys(self):
        """Test that missing config keys raise NotImplementedError."""
        incomplete_config = {
            "TCP_HANDSHAKE_PORT": 5800,
            # Missing other required keys
        }
        
        suite = get_suite("cs-kyber768-aesgcm-dilithium3")
        
        with pytest.raises(NotImplementedError, match="CONFIG missing"):
            run_proxy(
                role="gcs",
                suite=suite,
                cfg=incomplete_config,
                gcs_sig_secret=b"fake_secret",
                stop_after_seconds=0.1
            )
    
    def test_missing_gcs_secret(self, suite):
        """Test that GCS role requires signature secret."""
        with pytest.raises(NotImplementedError, match="GCS signature secret not provided"):
            run_proxy(
                role="gcs",
                suite=suite,
                cfg=CONFIG,
                gcs_sig_secret=None,  # Missing secret
                stop_after_seconds=0.1
            )
    
    def test_missing_gcs_public_key(self, suite):
        """Test that drone role requires GCS public key.""" 
        with pytest.raises(NotImplementedError, match="GCS signature public key not provided"):
            run_proxy(
                role="drone",
                suite=suite,
                cfg=CONFIG,
                gcs_sig_public=None,  # Missing public key
                stop_after_seconds=0.1
            )