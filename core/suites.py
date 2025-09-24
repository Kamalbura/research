"""
PQC cryptographic suite registry and algorithm ID mapping.

Provides suite definitions with NIST security levels and header encoding functions.
"""

from typing import Dict

# Suite registry with algorithm mappings
SUITES = {
    "cs-kyber512-aesgcm-dilithium2": {
        "kem": "ML-KEM-512", "kem_id": 1, "kem_param": 1,
        "sig": "ML-DSA-44", "sig_id": 1, "sig_param": 1,
        "aead": "AES-256-GCM", "nist_level": 1
    },
    "cs-kyber768-aesgcm-dilithium3": {
        "kem": "ML-KEM-768", "kem_id": 1, "kem_param": 2,
        "sig": "ML-DSA-65", "sig_id": 1, "sig_param": 2,
        "aead": "AES-256-GCM", "nist_level": 3
    },
    "cs-kyber1024-aesgcm-dilithium5": {
        "kem": "ML-KEM-1024", "kem_id": 1, "kem_param": 3,
        "sig": "ML-DSA-87", "sig_id": 1, "sig_param": 3,
        "aead": "AES-256-GCM", "nist_level": 5
    },
    "cs-kyber768-aesgcm-falcon512": {
        "kem": "ML-KEM-768", "kem_id": 1, "kem_param": 2,
        "sig": "Falcon-512", "sig_id": 2, "sig_param": 1,
        "aead": "AES-256-GCM", "nist_level": 3
    },
    "cs-kyber1024-aesgcm-falcon1024": {
        "kem": "ML-KEM-1024", "kem_id": 1, "kem_param": 3,
        "sig": "Falcon-1024", "sig_id": 2, "sig_param": 2,
        "aead": "AES-256-GCM", "nist_level": 5
    },
    "cs-kyber768-chacha20-sphincs128f": {
        "kem": "ML-KEM-768", "kem_id": 1, "kem_param": 2,
        "sig": "SLH-DSA-SHA2-128f", "sig_id": 3, "sig_param": 1,
        "aead": "AES-256-GCM", "nist_level": 3
    }
}


def get_suite(suite_id: str) -> Dict:
    """Get suite configuration by ID.
    
    Args:
        suite_id: Suite identifier string
        
    Returns:
        Suite configuration dictionary
        
    Raises:
        KeyError: If suite_id not found in registry
    """
    if suite_id not in SUITES:
        raise KeyError(f"Unknown suite: {suite_id}")
    return SUITES[suite_id]


def suite_header_bytes(suite: Dict) -> bytes:
    """Generate header prefix bytes from suite configuration.
    
    Args:
        suite: Suite configuration dictionary
        
    Returns:
        5-byte header prefix: version(1) | kem_id(1) | kem_param(1) | sig_id(1) | sig_param(1)
    """
    version = 1
    return bytes([
        version,
        suite["kem_id"],
        suite["kem_param"], 
        suite["sig_id"],
        suite["sig_param"]
    ])


def suite_bytes_for_hkdf(suite: Dict) -> bytes:
    """Generate canonical suite identifier for HKDF info parameter.
    
    Args:
        suite: Suite configuration dictionary
        
    Returns:
        ASCII-encoded suite string like b"ML-KEM-768|ML-DSA-65|AES-256-GCM"
    """
    suite_str = f"{suite['kem']}|{suite['sig']}|{suite['aead']}"
    return suite_str.encode('ascii')