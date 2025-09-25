"""Tests for hardening features: rate limit    def test_capacity_limits(self):
        \"\"\"Test that capacity is respected.\"\"\"
        bucket = _TokenBucket(capacity=1, refill_per_sec=0.1)  # Very slow refill
        
        # First request allowed
        assert bucket.allow(\"192.168.1.100\") is True
        
        # Second request blocked (capacity = 1)
        assert bucket.allow(\"192.168.1.100\") is False
        assert bucket.allow(\"192.168.1.100\") is False, and epoch guard."""

import pytest
import time
import struct
import os
from unittest.mock import Mock, patch

from core.async_proxy import _TokenBucket, _parse_header_fields
from core.aead import Sender, Receiver, AeadIds
from core.config import CONFIG
from core.suites import get_suite, header_ids_for_suite


class TestTokenBucket:
    """Test the per-IP rate limiter."""
    
    def test_initial_burst_allowed(self):
        """Test that initial requests up to burst limit are allowed."""
        bucket = _TokenBucket(capacity=3, refill_per_sec=1.0)
        
        # First 3 requests should be allowed
        assert bucket.allow("192.168.1.100") is True
        assert bucket.allow("192.168.1.100") is True  
        assert bucket.allow("192.168.1.100") is True
        
        # Fourth request should be blocked
        assert bucket.allow("192.168.1.100") is False
    
    def test_rate_limiting_per_ip(self):
        """Test that different IPs have independent rate limits."""
        bucket = _TokenBucket(capacity=2, refill_per_sec=1.0)
        
        # Exhaust tokens for first IP
        assert bucket.allow("192.168.1.100") is True
        assert bucket.allow("192.168.1.100") is True
        assert bucket.allow("192.168.1.100") is False
        
        # Second IP should still have full capacity
        assert bucket.allow("192.168.1.101") is True
        assert bucket.allow("192.168.1.101") is True
        assert bucket.allow("192.168.1.101") is False
    
    def test_capacity_limits(self):
        """Test that tokens are refilled over time."""
        with patch('time.monotonic') as mock_time:
            mock_time.return_value = 1000.0
            bucket = _TokenBucket(capacity=2, refill_per_sec=2.0)  # 2 tokens/sec = 0.5 sec per token
            
            # Exhaust tokens
            assert bucket.allow("192.168.1.100") is True  # uses 1 token, 1 remaining
            assert bucket.allow("192.168.1.100") is True  # uses 1 token, 0 remaining
            assert bucket.allow("192.168.1.100") is False # no tokens left

            # After 0.6 seconds (should refill 0.6 * 2.0 = 1.2 tokens, capped at capacity)
            mock_time.return_value = 1000.6
            assert bucket.allow("192.168.1.100") is True  # should have 1+ tokens after refill
            assert bucket.allow("192.168.1.100") is False  # Back to empty


class TestDropClassifier:
    """Test the drop reason classification."""
    
    def test_header_too_short(self):
        """Test classification of truncated packets."""
        aead_ids = Mock()
        reason, seq = _parse_header_fields(1, aead_ids, b"session1", b"short")
        assert reason == "header_too_short"
        assert seq is None
    
    def test_version_mismatch(self):
        """Test classification of version mismatch."""
        aead_ids = Mock()
        aead_ids.kem_id = 1
        aead_ids.kem_param = 2
        aead_ids.sig_id = 1
        aead_ids.sig_param = 2
        
        # Build valid header but wrong version
        header = struct.pack("!BBBBB8sQB", 99, 1, 2, 1, 2, b"session1", 42, 0)
        reason, seq = _parse_header_fields(1, aead_ids, b"session1", header)
        assert reason == "version_mismatch"
        assert seq == 42
    
    def test_crypto_id_mismatch(self):
        """Test classification of crypto ID mismatch."""
        aead_ids = Mock()
        aead_ids.kem_id = 1
        aead_ids.kem_param = 2
        aead_ids.sig_id = 1
        aead_ids.sig_param = 2
        
        # Build header with wrong crypto IDs
        header = struct.pack("!BBBBB8sQB", 1, 99, 2, 1, 2, b"session1", 42, 0)
        reason, seq = _parse_header_fields(1, aead_ids, b"session1", header)
        assert reason == "crypto_id_mismatch"
        assert seq == 42
    
    def test_session_mismatch(self):
        """Test classification of session mismatch."""
        aead_ids = Mock()
        aead_ids.kem_id = 1
        aead_ids.kem_param = 2
        aead_ids.sig_id = 1  
        aead_ids.sig_param = 2
        
        # Build header with wrong session ID
        header = struct.pack("!BBBBB8sQB", 1, 1, 2, 1, 2, b"badsess1", 42, 0)
        reason, seq = _parse_header_fields(1, aead_ids, b"session1", header)
        assert reason == "session_mismatch"
        assert seq == 42
    
    def test_valid_header_classified_as_auth_fail(self):
        """Test that valid header is classified as auth failure."""
        aead_ids = Mock()
        aead_ids.kem_id = 1
        aead_ids.kem_param = 2
        aead_ids.sig_id = 1
        aead_ids.sig_param = 2
        
        # Build completely valid header
        header = struct.pack("!BBBBB8sQB", 1, 1, 2, 1, 2, b"session1", 42, 0)
        reason, seq = _parse_header_fields(1, aead_ids, b"session1", header)
        assert reason == "auth_fail_or_replay"
        assert seq == 42


class TestEpochGuard:
    """Test the epoch wrap safety guard."""
    
    def test_sender_epoch_wrap_forbidden(self):
        """Test that sender epoch wrap at 255 is forbidden."""
        key = os.urandom(32)
        session_id = os.urandom(8)
        suite = get_suite("cs-kyber768-aesgcm-dilithium3")
        header_ids = header_ids_for_suite(suite)
        aead_ids = AeadIds(*header_ids)
        
        sender = Sender(CONFIG["WIRE_VERSION"], aead_ids, session_id, 255, key)
        
        with pytest.raises(NotImplementedError, match="epoch wrap forbidden"):
            sender.bump_epoch()
    
    def test_receiver_epoch_wrap_forbidden(self):
        """Test that receiver epoch wrap at 255 is forbidden."""
        key = os.urandom(32)
        session_id = os.urandom(8)
        suite = get_suite("cs-kyber768-aesgcm-dilithium3")
        header_ids = header_ids_for_suite(suite)
        aead_ids = AeadIds(*header_ids)
        
        receiver = Receiver(CONFIG["WIRE_VERSION"], aead_ids, session_id, 255, key, 1024)
        
        with pytest.raises(NotImplementedError, match="epoch wrap forbidden"):
            receiver.bump_epoch()
    
    def test_normal_epoch_bump_allowed(self):
        """Test that normal epoch increments work fine."""
        key = os.urandom(32)
        session_id = os.urandom(8)
        suite = get_suite("cs-kyber768-aesgcm-dilithium3")
        header_ids = header_ids_for_suite(suite)
        aead_ids = AeadIds(*header_ids)
        
        sender = Sender(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, key)
        receiver = Receiver(CONFIG["WIRE_VERSION"], aead_ids, session_id, 0, key, 1024)
        
        # Should work fine for normal values
        for epoch in range(5):
            sender.bump_epoch()
            receiver.bump_epoch()
            assert sender.epoch == epoch + 1
            assert receiver.epoch == epoch + 1
            assert sender._seq == 0  # Sequence reset
    
    def test_epoch_254_to_255_allowed(self):
        """Test that epoch 254 -> 255 is allowed (it's the wrap that's forbidden)."""
        key = os.urandom(32)
        session_id = os.urandom(8)
        suite = get_suite("cs-kyber768-aesgcm-dilithium3")
        header_ids = header_ids_for_suite(suite)
        aead_ids = AeadIds(*header_ids)
        
        sender = Sender(CONFIG["WIRE_VERSION"], aead_ids, session_id, 254, key)
        
        # This should work (254 -> 255)
        sender.bump_epoch()
        assert sender.epoch == 255
        
        # But this should fail (255 -> 0)
        with pytest.raises(NotImplementedError, match="epoch wrap forbidden"):
            sender.bump_epoch()