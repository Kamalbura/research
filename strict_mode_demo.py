#!/usr/bin/env python3
"""
Demonstration of strict_mode behavior in PQC AEAD layer
"""
import os
from core.aead import Sender, Receiver, HeaderMismatch, AeadAuthError, ReplayError, AeadIds
from core.config import CONFIG
from core.suites import get_suite, header_ids_for_suite

def demo_strict_mode():
    """Show the difference between strict_mode=True and strict_mode=False"""
    print("🔒 PQC AEAD Strict Mode Demonstration\n")
    
    # Setup
    key = os.urandom(32)
    session_id = b"\xAA" * 8
    suite = get_suite("cs-kyber768-aesgcm-dilithium3")
    header_ids = header_ids_for_suite(suite)
    aead_ids = AeadIds(*header_ids)
    
    sender = Sender(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, key)
    
    # Create receivers in both modes
    receiver_strict = Receiver(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, key, 64, strict_mode=True)
    receiver_silent = Receiver(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, key, 64, strict_mode=False)
    
    # Valid packet
    valid_packet = sender.encrypt(b"test payload")
    print("✅ Valid packet decryption:")
    print(f"  Strict mode: {receiver_strict.decrypt(valid_packet)}")
    print(f"  Silent mode: {receiver_silent.decrypt(valid_packet)}\n")
    
    # Test 1: Header tampering
    print("🚨 Test 1: Header Tampering")
    tampered = bytearray(valid_packet)
    tampered[1] ^= 0x01  # Flip bit in kem_id
    tampered = bytes(tampered)
    
    try:
        result = receiver_strict.decrypt(tampered)
        print(f"  Strict mode: {result}")
    except HeaderMismatch as e:
        print(f"  Strict mode: 💥 HeaderMismatch: {e}")
    
    result = receiver_silent.decrypt(tampered)
    print(f"  Silent mode: {result} (fails silently)\n")
    
    # Test 2: Replay attack
    print("🚨 Test 2: Replay Attack")
    # Reset receivers for clean replay test
    receiver_strict_2 = Receiver(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, key, 64, strict_mode=True)
    receiver_silent_2 = Receiver(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, key, 64, strict_mode=False)
    
    valid_packet_2 = sender.encrypt(b"replay test")
    
    # First decryption (should work)
    receiver_strict_2.decrypt(valid_packet_2)
    receiver_silent_2.decrypt(valid_packet_2)
    
    # Replay attempt
    try:
        result = receiver_strict_2.decrypt(valid_packet_2)
        print(f"  Strict mode: {result}")
    except ReplayError as e:
        print(f"  Strict mode: 💥 ReplayError: {e}")
    
    result = receiver_silent_2.decrypt(valid_packet_2)
    print(f"  Silent mode: {result} (fails silently)\n")
    
    # Test 3: Wrong epoch (always silent for security)
    print("🚨 Test 3: Wrong Epoch (Always Silent)")
    receiver_epoch1 = Receiver(CONFIG["WIRE_VERSION"], aead_ids, session_id, 1, key, 64, strict_mode=True)
    sender_epoch0 = Sender(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, key)
    epoch_packet = sender_epoch0.encrypt(b"wrong epoch")
    
    result = receiver_epoch1.decrypt(epoch_packet)
    print(f"  Strict mode: {result} (always silent for rekeying security)")
    
    print("\n🎯 Summary:")
    print("  • strict_mode=True: Raises exceptions for debugging/testing")
    print("  • strict_mode=False: Returns None silently (production)")
    print("  • Epoch/Session mismatches: Always silent for security")

if __name__ == "__main__":
    demo_strict_mode()