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
        from core.suites import header_ids_for_suite
        from core.config import CONFIG
        from core.aead import AeadIds
        header_ids = header_ids_for_suite(suite)
        aead_ids = AeadIds(*header_ids)
        sender_epoch0 = Sender(CONFIG["WIRE_VERSION"], aead_ids, test_session_id, 0, key_epoch0)
        sender_epoch1 = Sender(CONFIG["WIRE_VERSION"], aead_ids, test_session_id, 1, key_epoch1)
        
        # Receivers for different epochs
        receiver_epoch0 = Receiver(CONFIG["WIRE_VERSION"], aead_ids, test_session_id, 0, key_epoch0, 64)
        receiver_epoch1 = Receiver(CONFIG["WIRE_VERSION"], aead_ids, test_session_id, 1, key_epoch1, 64)
        
        payload = b"test message"
        
        # Encrypt with epoch 0
        wire_epoch0 = sender_epoch0.encrypt(payload)
        
        # Encrypt with epoch 1
        wire_epoch1 = sender_epoch1.encrypt(payload)        # Each receiver should only decrypt its own epoch's packets
        assert receiver_epoch0.decrypt(wire_epoch0) == payload
        assert receiver_epoch0.decrypt(wire_epoch1) is None  # Wrong key
        
        assert receiver_epoch1.decrypt(wire_epoch1) == payload  
        assert receiver_epoch1.decrypt(wire_epoch0) is None  # Wrong key
    
    def test_epoch_in_header(self, suite, test_session_id):
        """Test that epoch is correctly encoded in packet header."""
        key = os.urandom(32)
        
        # Test various epoch values
        epochs = [0, 1, 5, 255]
        
        from core.suites import header_ids_for_suite
        from core.config import CONFIG
        from core.aead import AeadIds
        header_ids = header_ids_for_suite(suite)
        aead_ids = AeadIds(*header_ids)
        
        for epoch in epochs:
            sender = Sender(CONFIG["WIRE_VERSION"], aead_ids, test_session_id, epoch, key)
            receiver = Receiver(CONFIG["WIRE_VERSION"], aead_ids, test_session_id, epoch, key, 64)
            
            payload = f"epoch {epoch} packet".encode()
            wire = sender.encrypt(payload)
            
            # Verify header contains correct epoch
            import struct
            from core.aead import HEADER_STRUCT
            
            hdr = wire[:struct.calcsize(HEADER_STRUCT)]
            fields = struct.unpack(HEADER_STRUCT, hdr)
            header_epoch = fields[7]  # epoch is last field
            
            assert header_epoch == epoch
            
            # Verify decryption works
            decrypted = receiver.decrypt(wire)
            assert decrypted == payload
    
    def test_sequence_reset_on_epoch_change(self, suite, test_session_id):
        """Test that sequence counters reset when epoch changes."""
        key_epoch0 = os.urandom(32)
        key_epoch1 = os.urandom(32)
        
        # Start with epoch 0, send some packets
        from core.suites import header_ids_for_suite
        from core.config import CONFIG
        from core.aead import AeadIds
        header_ids = header_ids_for_suite(suite)
        aead_ids = AeadIds(*header_ids)
        sender_epoch0 = Sender(CONFIG["WIRE_VERSION"], aead_ids, test_session_id, 0, key_epoch0)
        
        # Send packets to advance sequence
        for i in range(5):
            wire = sender_epoch0.encrypt(f"packet {i}".encode())
            
        # Sequence should be at 5
        assert sender_epoch0.seq == 5
        
        # Simulate rekey: new sender with epoch 1 should reset sequence 
        aead_ids1 = AeadIds(*header_ids)
        sender_epoch1 = Sender(CONFIG["WIRE_VERSION"], aead_ids1, test_session_id, 1, key_epoch1)
        
        # New sender should start at sequence 0
        assert sender_epoch1.seq == 0
        
        # Verify first packet has seq=0 in header
        wire = sender_epoch1.encrypt(b"first packet new epoch")
        
        import struct
        from core.aead import HEADER_STRUCT
        
        hdr = wire[:struct.calcsize(HEADER_STRUCT)]  
        fields = struct.unpack(HEADER_STRUCT, hdr)
        seq = fields[6]
        epoch = fields[7]
        
        assert seq == 0
        assert epoch == 1
    
    def test_replay_protection_across_epochs(self, suite, test_session_id):
        """Test that replay protection is isolated between epochs."""
        key_epoch0 = os.urandom(32) 
        key_epoch1 = os.urandom(32)
        
        # Senders for different epochs
        from core.suites import header_ids_for_suite
        from core.config import CONFIG
        from core.aead import AeadIds
        header_ids = header_ids_for_suite(suite)
        aead_ids0 = AeadIds(*header_ids)
        aead_ids1 = AeadIds(*header_ids)
        sender_epoch0 = Sender(CONFIG["WIRE_VERSION"], aead_ids0, test_session_id, 0, key_epoch0)
        sender_epoch1 = Sender(CONFIG["WIRE_VERSION"], aead_ids1, test_session_id, 1, key_epoch1)
        
        # Single receiver that will handle both epochs
        # (In reality, receiver would switch keys during rekey)
        receiver = Receiver(CONFIG["WIRE_VERSION"], aead_ids0, test_session_id, 0, key_epoch0, 64)
        
        payload = b"test"
        
        # Send packet in epoch 0
        wire_epoch0 = sender_epoch0.encrypt(payload)
        assert receiver.decrypt(wire_epoch0) == payload
        
        # Replay same packet - should be blocked
        assert receiver.decrypt(wire_epoch0) is None
        
        # Send packet with same sequence but different epoch
        # This won't decrypt (wrong key) but tests replay key isolation
        wire_epoch1 = sender_epoch1.encrypt(payload)
        assert receiver.decrypt(wire_epoch1) is None  # Wrong key
        
        # Switch receiver to epoch 1 key
        receiver_epoch1 = Receiver(CONFIG["WIRE_VERSION"], aead_ids1, test_session_id, 1, key_epoch1, 64)
        
        # Now epoch 1 packet should work
        assert receiver_epoch1.decrypt(wire_epoch1) == payload
        
        # And replay should be blocked within epoch 1
        assert receiver_epoch1.decrypt(wire_epoch1) is None
        
        # But epoch 0 packet should still be blocked by wrong key
        assert receiver_epoch1.decrypt(wire_epoch0) is None
    
    def test_epoch_overflow_handling(self, suite, test_session_id):
        """Test handling of epoch values near overflow boundary."""
        key = os.urandom(32)
        
        # Test max epoch value (255 for single byte)
        from core.suites import header_ids_for_suite
        from core.config import CONFIG
        from core.aead import AeadIds
        header_ids = header_ids_for_suite(suite)
        aead_ids = AeadIds(*header_ids)
        sender_max = Sender(CONFIG["WIRE_VERSION"], aead_ids, test_session_id, 255, key)
        receiver = Receiver(CONFIG["WIRE_VERSION"], aead_ids, test_session_id, 255, key, 64)
        
        payload = b"max epoch test"
        wire = sender_max.encrypt(payload)
        
        # Should work normally
        assert receiver.decrypt(wire) == payload
        
        # Verify epoch in header
        import struct  
        from core.aead import HEADER_STRUCT, HEADER_LEN
        
        hdr = wire[:HEADER_LEN]
        fields = struct.unpack(HEADER_STRUCT, hdr)
        assert fields[7] == 255
    
    def test_concurrent_epochs(self, suite, test_session_id):
        """Test scenario with overlapping epochs during rekey transition."""
        key_old = os.urandom(32)
        key_new = os.urandom(32)
        
        # Simulate ongoing communication in old epoch
        from core.suites import header_ids_for_suite
        from core.config import CONFIG
        from core.aead import AeadIds
        header_ids = header_ids_for_suite(suite)
        aead_ids_old = AeadIds(*header_ids)
        aead_ids_new = AeadIds(*header_ids)
        sender_old = Sender(CONFIG["WIRE_VERSION"], aead_ids_old, test_session_id, 5, key_old)
        receiver_old = Receiver(CONFIG["WIRE_VERSION"], aead_ids_old, test_session_id, 5, key_old, 64)
        
        # Send some packets in old epoch
        for i in range(3):
            wire = sender_old.encrypt(f"old epoch packet {i}".encode())
            decrypted = receiver_old.decrypt(wire)
            assert decrypted == f"old epoch packet {i}".encode()
        
        # Start new epoch
        sender_new = Sender(CONFIG["WIRE_VERSION"], aead_ids_new, test_session_id, 6, key_new) 
        receiver_new = Receiver(CONFIG["WIRE_VERSION"], aead_ids_new, test_session_id, 6, key_new, 64)
        
        # Send packets in new epoch (sequence starts over)
        for i in range(3):
            wire = sender_new.encrypt(f"new epoch packet {i}".encode())
            decrypted = receiver_new.decrypt(wire)
            assert decrypted == f"new epoch packet {i}".encode()
        
        # Old receiver can't decrypt new packets
        wire_new = sender_new.encrypt(b"test")
        assert receiver_old.decrypt(wire_new) is None
        
        # New receiver can't decrypt old packets  
        wire_old = sender_old.encrypt(b"test")
        assert receiver_new.decrypt(wire_old) is None
    
    def test_same_key_different_epochs(self, suite, test_session_id):
        """Test that same key with different epochs creates different ciphertexts."""
        key = os.urandom(32)
        
        # Same key, different epochs
        from core.suites import header_ids_for_suite
        from core.config import CONFIG
        from core.aead import AeadIds
        header_ids = header_ids_for_suite(suite)
        aead_ids0 = AeadIds(*header_ids)
        aead_ids1 = AeadIds(*header_ids)
        sender_epoch0 = Sender(CONFIG["WIRE_VERSION"], aead_ids0, test_session_id, 0, key)
        sender_epoch1 = Sender(CONFIG["WIRE_VERSION"], aead_ids1, test_session_id, 1, key)
        receiver = Receiver(CONFIG["WIRE_VERSION"], aead_ids0, test_session_id, 0, key, 64)
        
        payload = b"identical payload"
        
        # Encrypt same payload with same key but different epochs
        wire_epoch0 = sender_epoch0.encrypt(payload)
        wire_epoch1 = sender_epoch1.encrypt(payload)
        
        # Ciphertexts should be different (different headers -> different AAD)
        assert wire_epoch0 != wire_epoch1
        
        # Only matching epoch should decrypt correctly
        assert receiver.decrypt(wire_epoch0) == payload
        assert receiver.decrypt(wire_epoch1) is None  # Wrong epoch
        
        # Verify different epochs in headers
        import struct
        from core.aead import HEADER_STRUCT, HEADER_LEN
        
        hdr0 = wire_epoch0[:HEADER_LEN]
        hdr1 = wire_epoch1[:HEADER_LEN]
        
        fields0 = struct.unpack(HEADER_STRUCT, hdr0)
        fields1 = struct.unpack(HEADER_STRUCT, hdr1)
        
        assert fields0[7] == 0  # epoch 0
        assert fields1[7] == 1  # epoch 1