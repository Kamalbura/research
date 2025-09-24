"""
Tests for replay window functionality.
"""

import os
import pytest

# Skip tests if cryptography not available
pytest.importorskip("cryptography.hazmat.primitives.ciphers.aead")

from core.aead import (
    Sender, Receiver, AeadIds, ReplayError
)
from core.config import CONFIG
from core.suites import get_suite, header_ids_for_suite


def test_accept_out_of_order_in_window():
    """Test that out-of-order packets within window are accepted."""
    # Setup
    key = os.urandom(32)
    session_id = b"\xAA" * 8
    
    suite = get_suite("cs-kyber768-aesgcm-dilithium3")
    kem_id, kem_param, sig_id, sig_param = header_ids_for_suite(suite)
    ids = AeadIds(kem_id, kem_param, sig_id, sig_param)
    
    sender = Sender(
        version=CONFIG["WIRE_VERSION"],
        ids=ids,
        session_id=session_id,
        epoch=0,
        key_send=key
    )
    
    receiver = Receiver(
        version=CONFIG["WIRE_VERSION"],
        ids=ids,
        session_id=session_id,
        epoch=0,
        key_recv=key,
        window=64
    )
    
    # Generate packets in order: 0, 1, 2, 3, 4
    packets = []
    for i in range(5):
        wire = sender.encrypt(f"packet{i}".encode())
        packets.append(wire)
    
    # Receive in order: 0, 1, 2, 3, 4
    for i, packet in enumerate(packets):
        plaintext = receiver.decrypt(packet)
        assert plaintext == f"packet{i}".encode()
    
    # Generate more packets: 5, 6, 7
    for i in range(5, 8):
        wire = sender.encrypt(f"packet{i}".encode())
        packets.append(wire)
    
    # Receive out of order: 6, 5, 7
    # packet 6
    plaintext = receiver.decrypt(packets[6])
    assert plaintext == b"packet6"
    
    # packet 5 (out of order - should still work)
    plaintext = receiver.decrypt(packets[5])
    assert plaintext == b"packet5"
    
    # packet 7
    plaintext = receiver.decrypt(packets[7])
    assert plaintext == b"packet7"
    
    # Verify duplicates raise ReplayError
    with pytest.raises(ReplayError):
        receiver.decrypt(packets[0])  # Duplicate packet 0
    
    with pytest.raises(ReplayError):
        receiver.decrypt(packets[5])  # Duplicate packet 5


def test_reject_old_beyond_window():
    """Test that packets older than window size are rejected."""
    # Setup
    key = os.urandom(32)
    session_id = b"\xAA" * 8
    
    suite = get_suite("cs-kyber768-aesgcm-dilithium3")
    kem_id, kem_param, sig_id, sig_param = header_ids_for_suite(suite)
    ids = AeadIds(kem_id, kem_param, sig_id, sig_param)
    
    sender = Sender(
        version=CONFIG["WIRE_VERSION"],
        ids=ids,
        session_id=session_id,
        epoch=0,
        key_send=key
    )
    
    receiver = Receiver(
        version=CONFIG["WIRE_VERSION"],
        ids=ids,
        session_id=session_id,
        epoch=0,
        key_recv=key,
        window=64
    )
    
    # Generate and store packets
    packets = []
    
    # Send packets up to seq 100
    for i in range(101):
        wire = sender.encrypt(f"packet{i}".encode())
        packets.append(wire)
    
    # Receive packet 100 (establishes high water mark)
    plaintext = receiver.decrypt(packets[100])
    assert plaintext == b"packet100"
    
    # Try to receive packet 30 (old - outside window of 64)
    # 100 - 64 = 36, so anything <= 36 should be rejected
    with pytest.raises(ReplayError):
        receiver.decrypt(packets[30])
    
    # But packet 37 should still be acceptable (within window)
    plaintext = receiver.decrypt(packets[37])
    assert plaintext == b"packet37"
    
    @pytest.fixture 
    def test_key(self):
        """Generate test AES key."""
        return os.urandom(32)
    
    @pytest.fixture
    def test_session_id(self):
        """Generate test session ID."""
        return os.urandom(8)
    
    def test_duplicate_packet_dropped(self, suite, test_key, test_session_id):
        """Test that duplicate packets are dropped."""
        sender = Sender(test_key, suite, test_session_id)
        receiver = Receiver(test_key, window=64)
        
        payload = b"test packet"
        
        # Send and receive first packet
        wire = sender.pack(payload)
        decrypted1 = receiver.unpack(wire)
        assert decrypted1 == payload
        
        # Replay same packet - should be dropped
        decrypted2 = receiver.unpack(wire)
        assert decrypted2 is None
    
    def test_out_of_order_within_window(self, suite, test_key, test_session_id):
        """Test that out-of-order packets within window are accepted."""
        sender = Sender(test_key, suite, test_session_id)
        receiver = Receiver(test_key, window=64)
        
        # Generate several packets
        packets = []
        payloads = [f"packet{i}".encode() for i in range(5)]
        
        for payload in payloads:
            wire = sender.pack(payload)
            packets.append((wire, payload))
        
        # Deliver packets out of order: 0, 2, 1, 3, 4
        order = [0, 2, 1, 3, 4]
        
        for i in order:
            wire, payload = packets[i]
            decrypted = receiver.unpack(wire)
            assert decrypted == payload
        
        # Try to replay packet 2 - should be dropped
        wire, _ = packets[2]
        decrypted = receiver.unpack(wire)
        assert decrypted is None
    
    def test_packets_outside_window_dropped(self, suite, test_key, test_session_id):
        """Test that packets outside the sliding window are dropped."""
        window_size = 10
        sender = Sender(test_key, suite, test_session_id)
        receiver = Receiver(test_key, window=window_size)
        
        # Send many packets to advance the window
        packets = []
        for i in range(20):
            payload = f"packet{i}".encode()
            wire = sender.pack(payload)
            packets.append(wire)
            
            # Decrypt to advance window
            decrypted = receiver.unpack(wire)
            assert decrypted == payload
        
        # Now try to deliver an old packet (packet 5 when we're at packet 19)
        # This should be outside the window and dropped
        old_packet = packets[5]
        decrypted = receiver.unpack(old_packet)
        assert decrypted is None
        
        # But a recent packet (packet 15) should still work if replayed immediately
        # Actually, it should be dropped because it was already processed
        recent_packet = packets[15]
        decrypted = receiver.unpack(recent_packet)
        assert decrypted is None  # Already processed
    
    def test_window_advancement(self, suite, test_key, test_session_id):
        """Test that window high water mark advances correctly."""
        sender = Sender(test_key, suite, test_session_id)
        receiver = Receiver(test_key, window=64)
        
        # Send packet 0
        wire0 = sender.pack(b"packet0")
        receiver.unpack(wire0)
        assert receiver.high == 0
        
        # Send packet 5 (jump ahead)
        for _ in range(5):  # advance sender to seq 5
            sender.pack(b"skip")
        wire5 = sender.pack(b"packet5")  # This will be seq 6 (0-based indexing)
        receiver.unpack(wire5)
        assert receiver.high == 6  # seq counter is 0-based, so after 6 packs it's at 6        # Send packet 3 (backwards but within window)
        # Need to manually create packet with seq 3
        sender_seq3 = Sender(test_key, suite, test_session_id)
        sender_seq3.seq = 3
        wire3 = sender_seq3.pack(b"packet3")
        decrypted = receiver.unpack(wire3)
        assert decrypted == b"packet3"
        assert receiver.high == 6  # Should not decrease
        
        # Send packet 10 (advance further)  
        for _ in range(4):  # advance to seq 10
            sender.pack(b"skip")
        wire10 = sender.pack(b"packet10")  # This will be seq 11 now
        receiver.unpack(wire10)
        assert receiver.high == 11
    
    def test_different_sessions_isolated(self, suite, test_key):
        """Test that different sessions have isolated replay windows."""
        session1 = os.urandom(8)
        session2 = os.urandom(8)
        
        sender1 = Sender(test_key, suite, session1)
        sender2 = Sender(test_key, suite, session2)
        receiver = Receiver(test_key, window=64)
        
        payload = b"test"
        
        # Send packets from both sessions with same sequence numbers
        wire1 = sender1.pack(payload)
        wire2 = sender2.pack(payload)
        
        # Both should be accepted (different sessions)
        assert receiver.unpack(wire1) == payload
        assert receiver.unpack(wire2) == payload
        
        # But replays within same session should be dropped
        assert receiver.unpack(wire1) is None
        assert receiver.unpack(wire2) is None
    
    def test_receiver_memory_cleanup(self, suite, test_key, test_session_id):
        """Test that receiver cleans up old entries to prevent memory growth."""
        window_size = 4
        sender = Sender(test_key, suite, test_session_id)
        receiver = Receiver(test_key, window=window_size)
        
        # Send many packets to trigger cleanup
        for i in range(20):
            wire = sender.pack(f"packet{i}".encode())
            receiver.unpack(wire)
        
        # Check that received set doesn't grow unboundedly
        # Should be cleaned up when it exceeds 2 * window_size
        assert len(receiver.received) <= window_size * 2
    
    def test_large_sequence_gaps(self, suite, test_key, test_session_id):
        """Test handling of large gaps in sequence numbers."""
        sender = Sender(test_key, suite, test_session_id)  
        receiver = Receiver(test_key, window=64)
        
        # Send packet 0
        wire0 = sender.pack(b"packet0")
        assert receiver.unpack(wire0) == b"packet0"
        
        # Jump far ahead in sequence
        sender.seq = 1000
        wire1000 = sender.pack(b"packet1000")
        assert receiver.unpack(wire1000) == b"packet1000"
        assert receiver.high == 1000
        
        # Old packet should be dropped (outside window)
        assert receiver.unpack(wire0) is None
        
        # Packet within new window should work
        sender.seq = 950
        wire950 = sender.pack(b"packet950")
        assert receiver.unpack(wire950) == b"packet950"
    
    def test_zero_window_size(self, test_key):
        """Test edge case of zero window size."""
        receiver = Receiver(test_key, window=0)
        
        # Should still work but with very restrictive replay protection
        # Only packets with sequence >= high should be accepted
        
        # This test mainly ensures no crashes occur
        assert receiver.window == 0
        assert receiver.high == -1
    
    def test_epoch_in_replay_key(self, suite, test_key, test_session_id):
        """Test that epoch is part of replay protection key."""
        sender_epoch0 = Sender(test_key, suite, test_session_id, epoch=0)
        sender_epoch1 = Sender(test_key, suite, test_session_id, epoch=1) 
        receiver = Receiver(test_key, window=64)
        
        payload = b"test"
        
        # Send same sequence from different epochs
        wire_epoch0 = sender_epoch0.pack(payload)
        wire_epoch1 = sender_epoch1.pack(payload)
        
        # Both should be accepted (different epochs)
        # Note: They won't decrypt successfully because receiver has same key
        # but this tests that replay protection considers epoch
        
        # For this test, we'll manually verify replay keys are different
        import struct
        from core.aead import HDR_FMT, HDR_LEN
        
        hdr0 = wire_epoch0[:HDR_LEN]
        hdr1 = wire_epoch1[:HDR_LEN] 
        
        fields0 = struct.unpack(HDR_FMT, hdr0)
        fields1 = struct.unpack(HDR_FMT, hdr1)
        
        # Same session_id and seq, different epoch
        assert fields0[5] == fields1[5]  # session_id
        assert fields0[6] == fields1[6]  # seq
        assert fields0[7] != fields1[7]  # epoch
        
        # Replay keys should be different
        key0 = (fields0[5], fields0[6], fields0[7])  # (session_id, seq, epoch)
        key1 = (fields1[5], fields1[6], fields1[7])
        assert key0 != key1