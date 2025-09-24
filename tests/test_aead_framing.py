"""
Tests for AEAD framing functionality.
"""

import os
import pytest

# Skip tests if cryptography not available
pytest.importorskip("cryptography.hazmat.primitives.ciphers.aead")

from core.aead import (
    Sender, Receiver, AeadIds, HeaderMismatch, AeadAuthError, ReplayError,
    HEADER_LEN, IV_LEN
)
from core.config import CONFIG
from core.suites import get_suite, header_ids_for_suite


def test_round_trip_three_payloads():
    """Test round-trip encryption/decryption with 3 payload sizes."""
    # Setup common context
    key = os.urandom(32)
    session_id = b"\xAA" * 8
    
    # Get IDs from suite
    suite = get_suite("cs-kyber768-aesgcm-dilithium3")
    kem_id, kem_param, sig_id, sig_param = header_ids_for_suite(suite)
    ids = AeadIds(kem_id, kem_param, sig_id, sig_param)
    
    # Create sender and receiver
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
        window=CONFIG["REPLAY_WINDOW"]
    )
    
    # Test payloads: 0B, 64B, 1024B
    payloads = [b"", b"A" * 64, b"B" * 1024]
    
    for i, payload in enumerate(payloads):
        # Encrypt
        wire = sender.encrypt(payload)
        
        # Verify sender sequence increments
        assert sender._seq == i + 1
        
        # Decrypt
        decrypted = receiver.decrypt(wire)
        
        # Verify exact match
        assert decrypted == payload


def test_tamper_header_flip():
    """Test that flipping header bit raises HeaderMismatch without attempting AEAD."""
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
        window=CONFIG["REPLAY_WINDOW"]
    )
    
    # Encrypt one packet
    wire = sender.encrypt(b"test")
    
    # Flip 1 bit in header kem_id byte (byte 1)
    tampered = bytearray(wire)
    tampered[1] ^= 0x01  # Flip LSB of kem_id
    tampered = bytes(tampered)
    
    # Must raise HeaderMismatch without attempting AEAD
    with pytest.raises(HeaderMismatch):
        receiver.decrypt(tampered)


def test_tamper_ciphertext_tag():
    """Test that flipping ciphertext/tag bit raises AeadAuthError."""
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
        window=CONFIG["REPLAY_WINDOW"]
    )
    
    # Encrypt one packet
    wire = sender.encrypt(b"test")
    
    # Flip 1 bit in ciphertext/tag area (after header + IV)
    tampered = bytearray(wire)
    tamper_pos = HEADER_LEN + IV_LEN + 1  # First byte of ciphertext
    tampered[tamper_pos] ^= 0x01
    tampered = bytes(tampered)
    
    # Must raise AeadAuthError
    with pytest.raises(AeadAuthError):
        receiver.decrypt(tampered)


def test_nonce_reuse_replay():
    """Test that sending same wire bytes twice causes replay error on second attempt."""
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
        window=CONFIG["REPLAY_WINDOW"]
    )
    
    # Encrypt one packet
    wire = sender.encrypt(b"test")
    
    # First decrypt should succeed
    plaintext = receiver.decrypt(wire)
    assert plaintext == b"test"
    
    # Second decrypt of same wire should raise ReplayError
    with pytest.raises(ReplayError):
        receiver.decrypt(wire)


def test_epoch_bump():
    """Test that epoch bump allows successful communication and resets replay state."""
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
        window=CONFIG["REPLAY_WINDOW"]
    )
    
    # Send and decrypt one packet
    wire1 = sender.encrypt(b"before")
    plaintext1 = receiver.decrypt(wire1)
    assert plaintext1 == b"before"
    
    # Bump epoch on both sides
    sender.bump_epoch()
    receiver.bump_epoch()
    
    # Verify epochs incremented and sequence reset
    assert sender.epoch == 1
    assert receiver.epoch == 1
    assert sender._seq == 0  # Sequence should reset
    
    # Send another packet - should succeed with fresh replay state
    wire2 = sender.encrypt(b"after")
    plaintext2 = receiver.decrypt(wire2)
    assert plaintext2 == b"after"
    
    # Verify sequence started fresh
    assert sender._seq == 1