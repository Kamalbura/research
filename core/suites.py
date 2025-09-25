"""
PQC cryptographic suite registry and algorithm ID mapping.

Single source of truth for all cryptographic suites with frozen header IDs.
No crypto imports - pure metadata and mapping logic only.
"""

from typing import Dict, Tuple
from types import MappingProxyType


# Frozen header ID mappings
# Suite registry - embed header ID bytes directly to avoid split mappings
_SUITES_MUTABLE = {
    "cs-kyber512-aesgcm-dilithium2": {
        "kem_name": "ML-KEM-512", "kem_id": 1, "kem_param_id": 1,
        "sig_name": "ML-DSA-44", "sig_id": 1, "sig_param_id": 1,
        "nist_level": "L1", "aead": "AES-256-GCM", "kdf": "HKDF-SHA256"
    },
    "cs-kyber768-aesgcm-dilithium3": {
        "kem_name": "ML-KEM-768", "kem_id": 1, "kem_param_id": 2,
        "sig_name": "ML-DSA-65", "sig_id": 1, "sig_param_id": 2,
        "nist_level": "L3", "aead": "AES-256-GCM", "kdf": "HKDF-SHA256"
    },
    "cs-kyber1024-aesgcm-dilithium5": {
        "kem_name": "ML-KEM-1024", "kem_id": 1, "kem_param_id": 3,
        "sig_name": "ML-DSA-87", "sig_id": 1, "sig_param_id": 3,
        "nist_level": "L5", "aead": "AES-256-GCM", "kdf": "HKDF-SHA256"
    },
    "cs-kyber768-aesgcm-falcon512": {
        "kem_name": "ML-KEM-768", "kem_id": 1, "kem_param_id": 2,
        "sig_name": "Falcon-512", "sig_id": 2, "sig_param_id": 1,
        "nist_level": "L3", "aead": "AES-256-GCM", "kdf": "HKDF-SHA256"
    },
    "cs-kyber1024-aesgcm-falcon1024": {
        "kem_name": "ML-KEM-1024", "kem_id": 1, "kem_param_id": 3,
        "sig_name": "Falcon-1024", "sig_id": 2, "sig_param_id": 2,
        "nist_level": "L5", "aead": "AES-256-GCM", "kdf": "HKDF-SHA256"
    },
    "cs-kyber512-aesgcm-sphincs128f_sha2": {
        "kem_name": "ML-KEM-512", "kem_id": 1, "kem_param_id": 1,
        "sig_name": "SLH-DSA-SHA2-128f", "sig_id": 3, "sig_param_id": 1,
        "nist_level": "L1", "aead": "AES-256-GCM", "kdf": "HKDF-SHA256"
    },
    "cs-kyber1024-aesgcm-sphincs256f_sha2": {
        "kem_name": "ML-KEM-1024", "kem_id": 1, "kem_param_id": 3,
        "sig_name": "SLH-DSA-SHA2-256f", "sig_id": 3, "sig_param_id": 2,
        "nist_level": "L5", "aead": "AES-256-GCM", "kdf": "HKDF-SHA256"
    }
}

# Immutable suite registry
SUITES = MappingProxyType({
    suite_id: MappingProxyType(suite_config)
    for suite_id, suite_config in _SUITES_MUTABLE.items()
})


def list_suites() -> Dict[str, Dict]:
    """Return all available suites as immutable mapping.
    
    Returns:
        Dictionary mapping suite IDs to suite configurations
    """
    return dict(SUITES)


def get_suite(suite_id: str) -> Dict:
    """Get suite configuration by ID.
    
    Args:
        suite_id: Suite identifier string
        
    Returns:
        Immutable suite configuration dictionary
        
    Raises:
        NotImplementedError: If suite_id not found in registry
    """
    if suite_id not in SUITES:
        raise NotImplementedError(f"unknown suite_id: {suite_id}")
    
    suite = SUITES[suite_id]
    
    # Validate suite has all required fields
    required_fields = {"kem_name", "sig_name", "aead", "kdf", "nist_level"}
    missing_fields = required_fields - set(suite.keys())
    if missing_fields:
        raise NotImplementedError(f"malformed suite {suite_id}: missing fields {missing_fields}")
    
    return dict(suite)  # Return mutable copy


def header_ids_for_suite(suite: Dict) -> Tuple[int, int, int, int]:
    """Return embedded header ID bytes for provided suite dict copy."""
    try:
        return (
            suite["kem_id"], suite["kem_param_id"],
            suite["sig_id"], suite["sig_param_id"],
        )
    except KeyError as e:
        raise NotImplementedError(f"suite missing embedded id field: {e}")


def suite_bytes_for_hkdf(suite: Dict) -> bytes:
    """Generate deterministic bytes from suite for HKDF info parameter.
    
    Args:
        suite: Suite configuration dictionary
        
    Returns:
        UTF-8 encoded suite ID bytes for use in HKDF info parameter
        
    Raises:
        NotImplementedError: If suite not found in registry
    """
    # Find suite ID by matching configuration
    for suite_id, stored_suite in SUITES.items():
        if dict(stored_suite) == suite:
            return suite_id.encode('utf-8')
    
    raise NotImplementedError("Suite configuration not found in registry")