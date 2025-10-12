"""
Tests for configuration validation and suite registry integrity.

Tests CONFIG completeness, types, and suite metadata without requiring crypto libraries.
"""

import struct
from unittest.mock import patch
import os

import pytest

from core.config import CONFIG, validate_config, _REQUIRED_KEYS
from core.suites import (
    SUITES,
    build_suite_id,
    enabled_kems,
    enabled_sigs,
    get_suite,
    header_ids_for_suite,
    list_suites,
    suite_bytes_for_hkdf,
)
from tools.auto import gcs_scheduler as scheduler


class TestConfig:
    """Test configuration validation and completeness."""
    
    def test_config_completeness_and_types(self):
        """Test CONFIG contains all required keys with correct types."""
        # Should validate without exception
        validate_config(CONFIG)
        
        # Check all required keys exist
        for key in _REQUIRED_KEYS:
            assert key in CONFIG, f"Missing required key: {key}"
        
        # Check types match expectations
        for key, expected_type in _REQUIRED_KEYS.items():
            value = CONFIG[key]
            assert isinstance(value, expected_type), \
                f"CONFIG[{key}] should be {expected_type.__name__}, got {type(value).__name__}"
    
    def test_wire_version_frozen(self):
        """Test WIRE_VERSION is frozen at 1."""
        assert CONFIG["WIRE_VERSION"] == 1
        
        # Test validation rejects other values
        bad_config = CONFIG.copy()
        bad_config["WIRE_VERSION"] = 2
        
        with pytest.raises(NotImplementedError, match="WIRE_VERSION.*must be 1"):
            validate_config(bad_config)
    
    def test_replay_window_minimum(self):
        """Test REPLAY_WINDOW has minimum value."""
        assert CONFIG["REPLAY_WINDOW"] >= 64
        
        # Test validation rejects too-small values
        bad_config = CONFIG.copy()
        bad_config["REPLAY_WINDOW"] = 32
        
        with pytest.raises(NotImplementedError, match="REPLAY_WINDOW.*must be >= 64"):
            validate_config(bad_config)

    def test_replay_window_maximum(self):
        """Test REPLAY_WINDOW upper bound is enforced."""
        bad_config = CONFIG.copy()
        bad_config["REPLAY_WINDOW"] = 9000

        with pytest.raises(NotImplementedError, match="REPLAY_WINDOW.*must be <= 8192"):
            validate_config(bad_config)
    
    def test_port_ranges(self):
        """Test all port values are in valid range."""
        port_keys = [k for k in CONFIG if "PORT" in k or k.endswith("_RX") or k.endswith("_TX")]
        
        for key in port_keys:
            port = CONFIG[key]
            assert 1 <= port <= 65535, f"Port {key} out of range: {port}"
    
    def test_missing_keys_rejected(self):
        """Test validation fails when required keys are missing."""
        incomplete_config = CONFIG.copy()
        del incomplete_config["TCP_HANDSHAKE_PORT"]
        
        with pytest.raises(NotImplementedError, match="CONFIG missing required keys"):
            validate_config(incomplete_config)
    
    def test_wrong_types_rejected(self):
        """Test validation fails for wrong data types."""
        bad_config = CONFIG.copy()
        bad_config["TCP_HANDSHAKE_PORT"] = "5800"  # String instead of int
        
        with pytest.raises(NotImplementedError, match="must be int, got str"):
            validate_config(bad_config)
    
    def test_invalid_port_ranges_rejected(self):
        """Test validation fails for invalid port ranges."""
        bad_config = CONFIG.copy()
        bad_config["TCP_HANDSHAKE_PORT"] = 70000  # Too high
        
        with pytest.raises(NotImplementedError, match="must be valid port"):
            validate_config(bad_config)
    
    def test_empty_hosts_rejected(self):
        """Test validation fails for empty host strings."""
        bad_config = CONFIG.copy()
        bad_config["DRONE_HOST"] = ""
        
        with pytest.raises(NotImplementedError, match="must be non-empty string"):
            validate_config(bad_config)

    def test_plaintext_hosts_must_be_loopback_by_default(self):
        """Test plaintext binding rejects non-loopback without override."""
        bad_config = CONFIG.copy()
        bad_config["DRONE_PLAINTEXT_HOST"] = "0.0.0.0"

        with pytest.raises(NotImplementedError, match="loopback address"):
            validate_config(bad_config)

    def test_plaintext_host_override_env(self, monkeypatch):
        """ALLOW_NON_LOOPBACK_PLAINTEXT env should permit non-loopback host."""
        bad_config = CONFIG.copy()
        bad_config["DRONE_PLAINTEXT_HOST"] = "0.0.0.0"
        monkeypatch.setenv("ALLOW_NON_LOOPBACK_PLAINTEXT", "1")

        # Should not raise now
        validate_config(bad_config)
    
    def test_env_overrides(self):
        """Test environment variable overrides work correctly."""
        with patch.dict(os.environ, {"TCP_HANDSHAKE_PORT": "6000", "DRONE_HOST": "192.168.1.100"}):
            # Re-import to trigger env override application
            import importlib
            import core.config
            importlib.reload(core.config)
            
            assert core.config.CONFIG["TCP_HANDSHAKE_PORT"] == 6000
            assert core.config.CONFIG["DRONE_HOST"] == "192.168.1.100"
            
            # Validation should still pass
            validate_config(core.config.CONFIG)
    
    def test_invalid_env_overrides_rejected(self):
        """Test invalid environment values are rejected."""
        with patch.dict(os.environ, {"TCP_HANDSHAKE_PORT": "invalid"}):
            with pytest.raises(NotImplementedError, match="Invalid int value"):
                import importlib
                import core.config
                importlib.reload(core.config)


class TestSuites:
    """Test suite registry integrity and header ID mapping."""
    
    def test_suite_catalog_cross_product(self):
        """Test registry spans curated KEM × SIG pairs across all AEAD options."""
        suites = list_suites()

        pairs_to_aeads = {}
        for suite in suites.values():
            pair = (suite["kem_name"], suite["sig_name"])
            pairs_to_aeads.setdefault(pair, set()).add(suite["aead"])

        expected_aeads = {"AES-256-GCM", "ChaCha20-Poly1305", "ASCON-128"}

        assert len(pairs_to_aeads) == 15
        for aeads in pairs_to_aeads.values():
            assert aeads == expected_aeads

        assert len(suites) == len(pairs_to_aeads) * len(expected_aeads)
    
    def test_suite_fields_complete(self):
        """Test each suite has all required fields."""
        required_fields = {"kem_name", "sig_name", "aead", "aead_token", "kdf", "nist_level"}

        for suite_id in list_suites():
            suite = get_suite(suite_id)
            assert set(suite.keys()) >= required_fields | {"suite_id"}, \
                f"Suite {suite_id} missing required fields"
            
            # Check field types
            assert isinstance(suite["kem_name"], str)
            assert isinstance(suite["sig_name"], str) 
            assert isinstance(suite["aead"], str)
            assert isinstance(suite["aead_token"], str)
            assert isinstance(suite["kdf"], str)
            assert isinstance(suite["nist_level"], str)
    
    def test_header_ids_unique(self):
        """Test header IDs only collide for identical KEM/SIG pairs."""
        header_map = {}
        
        for suite_id in list_suites():
            suite = get_suite(suite_id)
            header_tuple = header_ids_for_suite(suite)
            assert len(header_tuple) == 4, f"Header tuple should have 4 elements for {suite_id}"
            
            # Check all elements are integers in valid range
            for i, id_val in enumerate(header_tuple):
                assert isinstance(id_val, int), f"Header ID {i} should be int for {suite_id}"
                assert 1 <= id_val <= 255, f"Header ID {i} out of byte range for {suite_id}"

            kem_sig_pair = (suite["kem_name"], suite["sig_name"])
            previous_pair = header_map.setdefault(header_tuple, kem_sig_pair)
            assert (
                previous_pair == kem_sig_pair
            ), "Header tuples should only collide for identical KEM/SIG pairs"
    
    def test_specific_suite_mappings(self):
        """Test specific expected header ID mappings."""
        # Test a few key suites have expected header IDs
        cases = [
            ("cs-mlkem768-aesgcm-mldsa65", "cs-kyber768-aesgcm-dilithium3", (1, 2, 1, 2)),
            ("cs-mlkem512-aesgcm-falcon512", "cs-kyber512-aesgcm-falcon512", (1, 1, 2, 1)),
            ("cs-mlkem1024-aesgcm-sphincs256fsha2", "cs-kyber1024-aesgcm-sphincs256f_sha2", (1, 3, 3, 2)),
        ]

        for suite_id, legacy_id, expected_ids in cases:
            suite = get_suite(suite_id)
            legacy_suite = get_suite(legacy_id)
            actual_ids = header_ids_for_suite(suite)
            legacy_ids_tuple = header_ids_for_suite(legacy_suite)

            assert actual_ids == expected_ids, (
                f"Suite {suite_id} should map to {expected_ids}, got {actual_ids}"
            )
            assert legacy_ids_tuple == expected_ids, (
                f"Legacy alias {legacy_id} should map to {expected_ids}, got {legacy_ids_tuple}"
            )

        extra_suite = get_suite("cs-classicmceliece348864-aesgcm-sphincs128fsha2")
        assert header_ids_for_suite(extra_suite) == (3, 1, 3, 1)
    
    def test_registry_immutability(self):
        """Test that returned suite dicts cannot mutate the registry."""
        original_suite = get_suite("cs-kyber768-aesgcm-dilithium3")
        original_kem = original_suite["kem_name"]
        
        # Try to modify the returned dict
        original_suite["kem_name"] = "MODIFIED"
        
        # Get fresh copy and verify registry wasn't affected
        fresh_suite = get_suite("cs-kyber768-aesgcm-dilithium3") 
        assert fresh_suite["kem_name"] == original_kem, \
            "Registry should not be mutated by modifying returned dict"
    
    def test_unknown_suite_rejected(self):
        """Test that unknown suite IDs raise NotImplementedError."""
        with pytest.raises(NotImplementedError, match="unknown suite_id: fake-suite"):
            get_suite("fake-suite")

    def test_build_suite_id_synonyms(self):
        """build_suite_id should accept synonym inputs."""

        suite_id = build_suite_id("Kyber768", "aesgcm", "Dilithium3")
        assert suite_id == "cs-mlkem768-aesgcm-mldsa65"

        suite = get_suite(suite_id)
        assert suite["kem_name"] == "ML-KEM-768"
        assert suite["sig_name"] == "ML-DSA-65"

    def test_suite_bytes_for_hkdf_matches_canonical_id(self):
        """suite_bytes_for_hkdf should return canonical identifier bytes."""

        legacy_suite = get_suite("cs-kyber768-aesgcm-dilithium3")
        canonical_suite = get_suite("cs-mlkem768-aesgcm-mldsa65")

        assert suite_bytes_for_hkdf(legacy_suite) == b"cs-mlkem768-aesgcm-mldsa65"
        assert suite_bytes_for_hkdf(canonical_suite) == b"cs-mlkem768-aesgcm-mldsa65"

    def test_enabled_helper_functions(self, monkeypatch):
        """enabled_kems/sigs should surface oqs capability lists."""

        monkeypatch.setattr(
            "core.suites._safe_get_enabled_kem_mechanisms",
            lambda: ["ML-KEM-512", "ML-KEM-768"],
        )
        monkeypatch.setattr(
            "core.suites._safe_get_enabled_sig_mechanisms",
            lambda: ["ML-DSA-44", "Falcon-512"],
        )

        assert enabled_kems() == ("ML-KEM-512", "ML-KEM-768")
        assert enabled_sigs() == ("ML-DSA-44", "Falcon-512")
    
    def test_header_version_stability(self):
        """Test header packing stability across all suites."""
        from core.config import CONFIG
        
        for suite_id in list_suites():
            suite = get_suite(suite_id)
            kem_id, kem_param_id, sig_id, sig_param_id = header_ids_for_suite(suite)
            
            # Build sample header tuple
            header_tuple = (
                CONFIG["WIRE_VERSION"],  # version
                kem_id,                  # kem_id  
                kem_param_id,           # kem_param
                sig_id,                 # sig_id
                sig_param_id,           # sig_param
                b"\x01" * 8,           # session_id (8 bytes)
                1,                      # seq (8 bytes as uint64)
                0                       # epoch (1 byte)
            )
            
            # Pack with struct - should be exactly 22 bytes
            # Format: version(1) + kem_id(1) + kem_param(1) + sig_id(1) + sig_param(1) + session_id(8) + seq(8) + epoch(1)  
            packed = struct.pack("!BBBBB8sQB", *header_tuple)
            assert len(packed) == 22, f"Packed header should be 22 bytes for {suite_id}, got {len(packed)}"
    
    def test_nist_levels_valid(self):
        """Test NIST security levels are valid."""
        valid_levels = {"L1", "L3", "L5"}
        
        for suite_id in list_suites():
            suite = get_suite(suite_id)
            level = suite["nist_level"]
            assert level in valid_levels, f"Invalid NIST level '{level}' in suite {suite_id}"
    
    def test_aead_kdf_consistency(self):
        """Test AEAD and KDF are consistent across suites."""
        allowed_aeads = {"AES-256-GCM", "ChaCha20-Poly1305", "ASCON-128"}

        for suite_id in list_suites():
            suite = get_suite(suite_id)
            assert suite["aead"] in allowed_aeads, f"Suite {suite_id} should use allowed AEAD"
            assert suite["kdf"] == "HKDF-SHA256", f"Suite {suite_id} should use HKDF-SHA256"


def test_filter_suites_for_follower_details():
    """filter_suites_for_follower should surface follower reasons."""
    suites = ["cs-mlkem768-aesgcm-mldsa65", "cs-test-ascon-suite"]
    capabilities = {
        "supported_suites": ["cs-mlkem768-aesgcm-mldsa65"],
        "unsupported_suites": [
            {
                "suite": "cs-test-ascon-suite",
                "reasons": ["aead_unavailable"],
                "details": {"aead_token": "ascon128", "aead_hint": "pyascon missing"},
            }
        ],
    }

    filtered, skipped = scheduler.filter_suites_for_follower(suites, capabilities)
    assert filtered == ["cs-mlkem768-aesgcm-mldsa65"]
    assert skipped and skipped[0]["suite"] == "cs-test-ascon-suite"
    assert skipped[0]["reason"] == "aead_unavailable"
    assert skipped[0]["details"]["aead_hint"] == "pyascon missing"


def test_expand_fetch_strategies_parsing():
    """_expand_fetch_strategies normalises comma-separated strategy strings."""
    expand = scheduler._expand_fetch_strategies
    assert expand("auto") == ["sftp", "scp"]
    assert expand("") == ["sftp", "scp"]
    assert expand("scp, rsync ,command") == ["scp", "rsync", "command"]
