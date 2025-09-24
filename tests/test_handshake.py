"""
Tests for authenticated PQC handshake protocol.
"""

import socket
import threading
import time
from unittest.mock import patch

import pytest
from oqs.oqs import Signature, KeyEncapsulation

from core.suites import get_suite
from core.handshake import (
    client_drone_handshake, 
    server_gcs_handshake,
    HandshakeError,
    SignatureVerifyError
)


class TestHandshake:
    """Test authenticated handshake protocol."""
    
    @pytest.fixture
    def suite(self):
        """Default test suite."""
        return get_suite("cs-kyber768-aesgcm-dilithium3")
    
    @pytest.fixture
    def gcs_keypair(self, suite):
        """Generate GCS signature keypair."""
        sig = Signature(suite["sig_name"])
        gcs_sig_pub = sig.generate_keypair()
        gcs_sig_secret = sig.export_secret_key()
        return gcs_sig_pub, gcs_sig_secret
    
    def test_successful_handshake(self, suite, gcs_keypair):
        """Test successful handshake between drone and GCS."""
        gcs_sig_pub, gcs_sig_secret = gcs_keypair
        
        # Create socket pair for communication
        server_sock, client_sock = socket.socketpair()
        
        # Storage for handshake results
        drone_keys = None
        gcs_keys = None
        gcs_session_id = None
        drone_error = None
        gcs_error = None
        
        def run_gcs():
            nonlocal gcs_keys, gcs_session_id, gcs_error
            try:
                result = server_gcs_handshake(server_sock, suite, gcs_sig_secret)
                gcs_keys = result[:4]  # (k_d2g, k_g2d, nseed_d2g, nseed_g2d)
                gcs_session_id = result[4]  # session_id
            except Exception as e:
                gcs_error = e
            finally:
                server_sock.close()
        
        def run_drone():
            nonlocal drone_keys, drone_error
            try:
                drone_keys = client_drone_handshake(client_sock, suite, gcs_sig_pub)
            except Exception as e:
                drone_error = e
            finally:
                client_sock.close()
        
        # Run handshake concurrently
        gcs_thread = threading.Thread(target=run_gcs)
        drone_thread = threading.Thread(target=run_drone)
        
        gcs_thread.start()
        drone_thread.start()
        
        gcs_thread.join(timeout=5.0)
        drone_thread.join(timeout=5.0)
        
        # Check for errors
        if gcs_error:
            raise gcs_error
        if drone_error:
            raise drone_error
        
        # Verify handshake completed
        assert drone_keys is not None
        assert gcs_keys is not None
        assert gcs_session_id is not None
        
        # Verify both sides derived identical keys (first 4 elements)
        assert drone_keys[:4] == gcs_keys[:4]
        
        # Verify key properties  
        k_d2g, k_g2d, nseed_d2g, nseed_g2d = drone_keys[:4]
        assert len(k_d2g) == 32
        assert len(k_g2d) == 32
        assert len(nseed_d2g) == 8
        assert len(nseed_g2d) == 8
        
        # Keys should be different from each other
        assert k_d2g != k_g2d
        assert nseed_d2g != nseed_g2d
        
        # Session ID should be 8 bytes
        assert len(gcs_session_id) == 8
        
        # Drone should also have session_id 
        drone_session_id = drone_keys[4]
        assert len(drone_session_id) == 8
        assert drone_session_id == gcs_session_id
    
    def test_signature_verification_failure(self, suite, gcs_keypair):
        """Test handshake failure when signature is tampered."""
        gcs_sig_pub, gcs_sig_secret = gcs_keypair
        
        # Create socket pair
        server_sock, client_sock = socket.socketpair()
        
        drone_error = None
        gcs_started = threading.Event()
        
        def run_gcs():
            gcs_started.set()
            try:
                server_gcs_handshake(server_sock, suite, gcs_sig_secret)
            except Exception:
                pass  # Expected to fail when client disconnects
            finally:
                server_sock.close()
        
        def run_drone_with_tampered_signature():
            nonlocal drone_error
            try:
                # Start GCS in background
                gcs_thread = threading.Thread(target=run_gcs)
                gcs_thread.start()
                gcs_started.wait(timeout=1.0)
                
                # Manually receive and tamper with signature
                from core.handshake import _recv_len_prefixed, _recv_exact
                
                # Receive normal handshake data
                session_id = _recv_exact(client_sock, 8)
                kem_name = _recv_len_prefixed(client_sock, "!H") 
                gcs_kem_pub = _recv_len_prefixed(client_sock, "!I")
                signature = _recv_len_prefixed(client_sock, "!I")
                
                # Tamper with signature (flip one byte)
                tampered_sig = bytearray(signature)
                tampered_sig[0] ^= 0x01
                tampered_sig = bytes(tampered_sig)
                
                # Verify KEM name
                expected_kem = suite["kem_name"].encode('ascii')
                if kem_name != expected_kem:
                    raise HandshakeError(f"KEM mismatch")
                
                # Construct transcript
                transcript = session_id + kem_name + gcs_kem_pub
                
                # Try to verify tampered signature
                sig = Signature(suite["sig_name"])
                if not sig.verify(transcript, tampered_sig, gcs_sig_pub):
                    raise SignatureVerifyError("GCS signature verification failed")
                
            except SignatureVerifyError as e:
                drone_error = e
            except Exception as e:
                drone_error = e
            finally:
                client_sock.close()
        
        run_drone_with_tampered_signature()
        
        # Should get signature verification error
        assert isinstance(drone_error, SignatureVerifyError)
    
    def test_unsupported_suite(self):
        """Test error handling for unsupported crypto suite."""
        # Create fake suite with unsupported algorithms
        fake_suite = {
            "kem_name": "FAKE-KEM-999",
            "sig_name": "FAKE-SIG-999", 
            "aead": "AES-256-GCM",
            "kdf": "HKDF-SHA256",
            "nist_level": "L1"
        }
        
        server_sock, client_sock = socket.socketpair()
        
        with pytest.raises(NotImplementedError, match="Suite not supported"):
            server_gcs_handshake(server_sock, fake_suite, b"fake_secret")
        
        server_sock.close()
        client_sock.close()
    
    def test_missing_gcs_secret(self, suite):
        """Test error when GCS signature secret not provided."""
        server_sock, client_sock = socket.socketpair()
        
        with pytest.raises(NotImplementedError, match="GCS signature secret not provided"):
            server_gcs_handshake(server_sock, suite, b"")
        
        server_sock.close() 
        client_sock.close()
    
    def test_kem_name_mismatch(self, suite, gcs_keypair):
        """Test handshake failure on KEM algorithm mismatch."""
        gcs_sig_pub, gcs_sig_secret = gcs_keypair
        
        # Create different suite for client
        client_suite = get_suite("cs-kyber512-aesgcm-dilithium2")
        
        server_sock, client_sock = socket.socketpair()
        
        drone_error = None
        
        def run_gcs():
            try:
                server_gcs_handshake(server_sock, suite, gcs_sig_secret)
            except Exception:
                pass  # Expected to fail
            finally:
                server_sock.close()
        
        def run_drone():
            nonlocal drone_error
            try:
                client_drone_handshake(client_sock, client_suite, gcs_sig_pub)
            except Exception as e:
                drone_error = e
            finally:
                client_sock.close()
        
        gcs_thread = threading.Thread(target=run_gcs)
        drone_thread = threading.Thread(target=run_drone)
        
        gcs_thread.start()
        drone_thread.start()
        
        gcs_thread.join(timeout=5.0)
        drone_thread.join(timeout=5.0)
        
        # Should get KEM mismatch error
        assert isinstance(drone_error, HandshakeError)
        assert "KEM mismatch" in str(drone_error)