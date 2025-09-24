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
        window=64,
        strict_mode=True
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
        window=64,
        strict_mode=True
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