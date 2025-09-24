"""
Tests for AES-GCM AEAD framing and header authentication.
"""

import os
import struct

import pytest

from core.suites import get_suite
from core.aead import Sender, Receiver, HDR_FMT, HDR_LEN, IV_LEN


class TestAEADFraming:
    """Test AEAD packet framing with header authentication."""
    
    @pytest.fixture
    def suite(self):
        """Default test suite."""
        return get_suite("cs-kyber768-aesgcm-dilithium3")
    
    @pytest.fixture
    def test_key(self):
        """Generate test AES key."""
        return os.urandom(32)
    
    @pytest.fixture
    def test_session_id(self):
        """Generate test session ID."""
        return os.urandom(8)
    
    def test_encrypt_decrypt_round_trip(self, suite, test_key, test_session_id):
        """Test basic encrypt/decrypt functionality."""
        sender = Sender(test_key, suite, test_session_id, epoch=0)
        receiver = Receiver(test_key, window=1024)
        
        # Test various payload sizes
        payloads = [
            b"small",
            b"medium payload with some data",
            b"large payload " * 100,
            b"",  # empty payload
            os.urandom(1024)  # random data
        ]
        
        for payload in payloads:
            # Encrypt
            wire = sender.pack(payload)
            
            # Decrypt  
            decrypted = receiver.unpack(wire)
            
            assert decrypted == payload
    
    def test_header_format_and_authentication(self, suite, test_key, test_session_id):
        """Test header format and AAD authentication."""
        sender = Sender(test_key, suite, test_session_id, epoch=5)
        receiver = Receiver(test_key)
        
        payload = b"test payload"
        wire = sender.pack(payload)
        
        # Verify header structure
        assert len(wire) >= HDR_LEN + IV_LEN + len(payload) + 16  # header + iv + payload + tag
        
        # Parse header
        hdr = wire[:HDR_LEN]
        fields = struct.unpack(HDR_FMT, hdr)
        version, kem_id, kem_param, sig_id, sig_param, session_id, seq, epoch = fields
        
        # Verify header contents
        assert version == 1
        assert kem_id == suite["kem_id"]
        assert kem_param == suite["kem_param"] 
        assert sig_id == suite["sig_id"]
        assert sig_param == suite["sig_param"]
        assert session_id == test_session_id
        assert seq == 0  # first packet
        assert epoch == 5
        
        # Verify decryption works
        decrypted = receiver.unpack(wire)
        assert decrypted == payload
    
    def test_header_tampering_fails(self, suite, test_key, test_session_id):
        """Test that header tampering causes decryption failure."""
        sender = Sender(test_key, suite, test_session_id, epoch=0)
        receiver = Receiver(test_key)
        
        payload = b"test payload"
        wire = sender.pack(payload)
        
        # Test tampering each header field
        for offset in range(HDR_LEN):
            tampered = bytearray(wire)
            tampered[offset] ^= 0x01  # flip one bit
            tampered_wire = bytes(tampered)
            
            # Decryption should fail
            decrypted = receiver.unpack(tampered_wire)
            assert decrypted is None
    
    def test_sequence_counter_increments(self, suite, test_key, test_session_id):
        """Test that sequence counter increments correctly."""
        sender = Sender(test_key, suite, test_session_id)
        receiver = Receiver(test_key)
        
        payloads = [b"packet1", b"packet2", b"packet3"]
        
        for i, payload in enumerate(payloads):
            wire = sender.pack(payload)
            
            # Check sequence number in header
            hdr = wire[:HDR_LEN]
            fields = struct.unpack(HDR_FMT, hdr)
            seq = fields[6]
            assert seq == i
            
            # Verify decryption
            decrypted = receiver.unpack(wire)
            assert decrypted == payload
    
    def test_nonce_uniqueness(self, suite, test_key, test_session_id):
        """Test that nonces are unique and deterministic."""
        sender1 = Sender(test_key, suite, test_session_id)
        sender2 = Sender(test_key, suite, test_session_id)
        
        # Same payload, different senders at same sequence
        payload = b"test"
        
        wire1 = sender1.pack(payload)
        wire2 = sender2.pack(payload)
        
        # Extract IVs
        iv1 = wire1[HDR_LEN:HDR_LEN+IV_LEN]
        iv2 = wire2[HDR_LEN:HDR_LEN+IV_LEN]
        
        # IVs should be identical (deterministic from sequence)
        assert iv1 == iv2
        
        # But advance one sender
        wire3 = sender1.pack(payload)
        iv3 = wire3[HDR_LEN:HDR_LEN+IV_LEN]
        
        # Now IV should be different
        assert iv1 != iv3
    
    def test_key_separation(self, suite, test_session_id):
        """Test that different keys cannot decrypt each other's packets."""
        key1 = os.urandom(32)
        key2 = os.urandom(32)
        
        sender1 = Sender(key1, suite, test_session_id)
        sender2 = Sender(key2, suite, test_session_id)
        receiver1 = Receiver(key1)
        receiver2 = Receiver(key2)
        
        payload = b"secret message"
        
        # Encrypt with key1
        wire1 = sender1.pack(payload)
        
        # Should decrypt with receiver1 but not receiver2
        assert receiver1.unpack(wire1) == payload
        assert receiver2.unpack(wire1) is None
        
        # Encrypt with key2  
        wire2 = sender2.pack(payload)
        
        # Should decrypt with receiver2 but not receiver1
        assert receiver2.unpack(wire2) == payload
        
        # receiver1 cannot decrypt receiver2's packet
        assert receiver1.unpack(wire2) is None
        
        # But receiver1 still works with its own packet (if not already processed)
        # Note: This will be None because wire1 was already processed above
        # Let's create a fresh packet for receiver1
        wire1_fresh = sender1.pack(payload)
        assert receiver1.unpack(wire1_fresh) == payload
    
    def test_malformed_packets(self, test_key):
        """Test handling of malformed packets."""
        receiver = Receiver(test_key)
        
        # Too short packets
        assert receiver.unpack(b"") is None
        assert receiver.unpack(b"short") is None
        assert receiver.unpack(b"x" * 10) is None
        
        # Minimum size but invalid
        min_size = HDR_LEN + IV_LEN + 16
        assert receiver.unpack(b"x" * min_size) is None
        
        # Invalid header format
        bad_header = b"x" * HDR_LEN + b"y" * IV_LEN + b"z" * 16  
        assert receiver.unpack(bad_header) is None
    
    def test_invalid_key_sizes(self, suite, test_session_id):
        """Test error handling for invalid key sizes."""
        # Wrong key sizes should raise ValueError
        with pytest.raises(ValueError, match="Key must be 32 bytes"):
            Sender(b"wrong_size_key", suite, test_session_id)
        
        with pytest.raises(ValueError, match="Key must be 32 bytes"):
            Receiver(b"wrong_size_key")
    
    def test_invalid_session_id_size(self, suite, test_key):
        """Test error handling for invalid session ID size."""
        with pytest.raises(ValueError, match="Session ID must be 8 bytes"):
            Sender(test_key, suite, b"wrong_size")