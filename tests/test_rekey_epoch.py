"""
Tests for epoch handling and rekeying functionality.
"""

import os

import pytest

from core.suites import get_suite  
from core.aead import Sender, Receiver


class TestRekeyEpoch:
    """Test epoch handling for rekeying scenarios."""
    
    @pytest.fixture
    def suite(self):
        """Default test suite."""
        return get_suite("cs-kyber768-aesgcm-dilithium3")
    
    @pytest.fixture
    def test_session_id(self):
        """Generate test session ID.""" 
        return os.urandom(8)
    
    def test_different_epochs_isolated(self, suite, test_session_id):
        """Test that packets from different epochs don't decrypt under wrong keys."""
        key_epoch0 = os.urandom(32)
        key_epoch1 = os.urandom(32)
        
        # Senders for different epochs
        sender_epoch0 = Sender(key_epoch0, suite, test_session_id, epoch=0)
        sender_epoch1 = Sender(key_epoch1, suite, test_session_id, epoch=1)
        
        # Receivers for different epochs
        receiver_epoch0 = Receiver(key_epoch0, window=64)
        receiver_epoch1 = Receiver(key_epoch1, window=64)
        
        payload = b"test message"
        
        # Encrypt with epoch 0
        wire_epoch0 = sender_epoch0.pack(payload)
        
        # Encrypt with epoch 1  
        wire_epoch1 = sender_epoch1.pack(payload)
        
        # Each receiver should only decrypt its own epoch's packets
        assert receiver_epoch0.unpack(wire_epoch0) == payload
        assert receiver_epoch0.unpack(wire_epoch1) is None  # Wrong key
        
        assert receiver_epoch1.unpack(wire_epoch1) == payload  
        assert receiver_epoch1.unpack(wire_epoch0) is None  # Wrong key
    
    def test_epoch_in_header(self, suite, test_session_id):
        """Test that epoch is correctly encoded in packet header."""
        key = os.urandom(32)
        
        # Test various epoch values
        epochs = [0, 1, 5, 255]
        
        for epoch in epochs:
            sender = Sender(key, suite, test_session_id, epoch=epoch)
            receiver = Receiver(key)
            
            payload = f"epoch {epoch} packet".encode()
            wire = sender.pack(payload)
            
            # Verify header contains correct epoch
            import struct
            from core.aead import HDR_FMT, HDR_LEN
            
            hdr = wire[:HDR_LEN]
            fields = struct.unpack(HDR_FMT, hdr)
            header_epoch = fields[7]  # epoch is last field
            
            assert header_epoch == epoch
            
            # Verify decryption works
            decrypted = receiver.unpack(wire)
            assert decrypted == payload
    
    def test_sequence_reset_on_epoch_change(self, suite, test_session_id):
        """Test that sequence counters reset when epoch changes."""
        key_epoch0 = os.urandom(32)
        key_epoch1 = os.urandom(32)
        
        # Start with epoch 0, send some packets
        sender_epoch0 = Sender(key_epoch0, suite, test_session_id, epoch=0)
        
        # Send packets to advance sequence
        for i in range(5):
            wire = sender_epoch0.pack(f"packet {i}".encode())
            
        # Sequence should be at 5
        assert sender_epoch0.seq == 5
        
        # Simulate rekey: new sender with epoch 1 should reset sequence 
        sender_epoch1 = Sender(key_epoch1, suite, test_session_id, epoch=1)
        
        # New sender should start at sequence 0
        assert sender_epoch1.seq == 0
        
        # Verify first packet has seq=0 in header
        wire = sender_epoch1.pack(b"first packet new epoch")
        
        import struct
        from core.aead import HDR_FMT, HDR_LEN
        
        hdr = wire[:HDR_LEN]  
        fields = struct.unpack(HDR_FMT, hdr)
        seq = fields[6]
        epoch = fields[7]
        
        assert seq == 0
        assert epoch == 1
    
    def test_replay_protection_across_epochs(self, suite, test_session_id):
        """Test that replay protection is isolated between epochs."""
        key_epoch0 = os.urandom(32) 
        key_epoch1 = os.urandom(32)
        
        # Senders for different epochs
        sender_epoch0 = Sender(key_epoch0, suite, test_session_id, epoch=0)
        sender_epoch1 = Sender(key_epoch1, suite, test_session_id, epoch=1)
        
        # Single receiver that will handle both epochs
        # (In reality, receiver would switch keys during rekey)
        receiver = Receiver(key_epoch0, window=64)
        
        payload = b"test"
        
        # Send packet in epoch 0
        wire_epoch0 = sender_epoch0.pack(payload)
        assert receiver.unpack(wire_epoch0) == payload
        
        # Replay same packet - should be blocked
        assert receiver.unpack(wire_epoch0) is None
        
        # Send packet with same sequence but different epoch
        # This won't decrypt (wrong key) but tests replay key isolation
        wire_epoch1 = sender_epoch1.pack(payload)
        assert receiver.unpack(wire_epoch1) is None  # Wrong key
        
        # Switch receiver to epoch 1 key
        receiver_epoch1 = Receiver(key_epoch1, window=64)
        
        # Now epoch 1 packet should work
        assert receiver_epoch1.unpack(wire_epoch1) == payload
        
        # And replay should be blocked within epoch 1
        assert receiver_epoch1.unpack(wire_epoch1) is None
        
        # But epoch 0 packet should still be blocked by wrong key
        assert receiver_epoch1.unpack(wire_epoch0) is None
    
    def test_epoch_overflow_handling(self, suite, test_session_id):
        """Test handling of epoch values near overflow boundary."""
        key = os.urandom(32)
        
        # Test max epoch value (255 for single byte)
        sender_max = Sender(key, suite, test_session_id, epoch=255)
        receiver = Receiver(key)
        
        payload = b"max epoch test"
        wire = sender_max.pack(payload)
        
        # Should work normally
        assert receiver.unpack(wire) == payload
        
        # Verify epoch in header
        import struct  
        from core.aead import HDR_FMT, HDR_LEN
        
        hdr = wire[:HDR_LEN]
        fields = struct.unpack(HDR_FMT, hdr)
        assert fields[7] == 255
    
    def test_concurrent_epochs(self, suite, test_session_id):
        """Test scenario with overlapping epochs during rekey transition."""
        key_old = os.urandom(32)
        key_new = os.urandom(32)
        
        # Simulate ongoing communication in old epoch
        sender_old = Sender(key_old, suite, test_session_id, epoch=5)
        receiver_old = Receiver(key_old)
        
        # Send some packets in old epoch
        for i in range(3):
            wire = sender_old.pack(f"old epoch packet {i}".encode())
            decrypted = receiver_old.unpack(wire)
            assert decrypted == f"old epoch packet {i}".encode()
        
        # Start new epoch
        sender_new = Sender(key_new, suite, test_session_id, epoch=6) 
        receiver_new = Receiver(key_new)
        
        # Send packets in new epoch (sequence starts over)
        for i in range(3):
            wire = sender_new.pack(f"new epoch packet {i}".encode())
            decrypted = receiver_new.unpack(wire)
            assert decrypted == f"new epoch packet {i}".encode()
        
        # Old receiver can't decrypt new packets
        wire_new = sender_new.pack(b"test")
        assert receiver_old.unpack(wire_new) is None
        
        # New receiver can't decrypt old packets  
        wire_old = sender_old.pack(b"test")
        assert receiver_new.unpack(wire_old) is None
    
    def test_same_key_different_epochs(self, suite, test_session_id):
        """Test that same key with different epochs creates different ciphertexts."""
        key = os.urandom(32)
        
        # Same key, different epochs
        sender_epoch0 = Sender(key, suite, test_session_id, epoch=0)
        sender_epoch1 = Sender(key, suite, test_session_id, epoch=1)
        receiver = Receiver(key)
        
        payload = b"identical payload"
        
        # Encrypt same payload with same key but different epochs
        wire_epoch0 = sender_epoch0.pack(payload)
        wire_epoch1 = sender_epoch1.pack(payload)
        
        # Ciphertexts should be different (different headers -> different AAD)
        assert wire_epoch0 != wire_epoch1
        
        # Both should decrypt correctly
        assert receiver.unpack(wire_epoch0) == payload
        assert receiver.unpack(wire_epoch1) == payload
        
        # Verify different epochs in headers
        import struct
        from core.aead import HDR_FMT, HDR_LEN
        
        hdr0 = wire_epoch0[:HDR_LEN]
        hdr1 = wire_epoch1[:HDR_LEN]
        
        fields0 = struct.unpack(HDR_FMT, hdr0)
        fields1 = struct.unpack(HDR_FMT, hdr1)
        
        assert fields0[7] == 0  # epoch 0
        assert fields1[7] == 1  # epoch 1