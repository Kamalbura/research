"""
PQC cryptographic suite registry and algorithm ID mapping.

Single source of truth for all cryptographic suites with frozen header IDs.
No crypto imports - pure metadata and mapping logic only.
"""

from typing import Dict, Tuple
from types import MappingProxyType


# Frozen header ID mappings
_KEM_IDS = {
    "ML-KEM-512": (1, 1),   # (kem_id, kem_param)
    "ML-KEM-768": (1, 2),
    "ML-KEM-1024": (1, 3),
}

_SIG_IDS = {
    # ML-DSA (Dilithium)
    "ML-DSA-44": (1, 1),    # (sig_id, sig_param)  
    "ML-DSA-65": (1, 2),
    "ML-DSA-87": (1, 3),
    # Falcon
    "Falcon-512": (2, 1),
    "Falcon-1024": (2, 2),
    # SLH-DSA (SPHINCS+) - SHA2 variants only
    "SLH-DSA-SHA2-128f": (3, 1),
    "SLH-DSA-SHA2-256f": (3, 2),
}

# Suite registry - exactly 7 suites as specified
_SUITES_MUTABLE = {
    "cs-kyber512-aesgcm-dilithium2": {
        "kem_name": "ML-KEM-512",
        "kem_param": 512,
        "sig_name": "ML-DSA-44", 
        "sig_param": 44,
        "nist_level": "L1",
        "aead": "AES-256-GCM",
        "kdf": "HKDF-SHA256"
    },
    "cs-kyber768-aesgcm-dilithium3": {
        "kem_name": "ML-KEM-768",
        "kem_param": 768,
        "sig_name": "ML-DSA-65",
        "sig_param": 65, 
        "nist_level": "L3",
        "aead": "AES-256-GCM",
        "kdf": "HKDF-SHA256"
    },
    "cs-kyber1024-aesgcm-dilithium5": {
        "kem_name": "ML-KEM-1024",
        "kem_param": 1024,
        "sig_name": "ML-DSA-87",
        "sig_param": 87,
        "nist_level": "L5", 
        "aead": "AES-256-GCM",
        "kdf": "HKDF-SHA256"
    },
    "cs-kyber768-aesgcm-falcon512": {
        "kem_name": "ML-KEM-768",
        "kem_param": 768,
        "sig_name": "Falcon-512",
        "sig_param": 512,
        "nist_level": "L3",
        "aead": "AES-256-GCM", 
        "kdf": "HKDF-SHA256"
    },
    "cs-kyber1024-aesgcm-falcon1024": {
        "kem_name": "ML-KEM-1024",
        "kem_param": 1024,
        "sig_name": "Falcon-1024",
        "sig_param": 1024,
        "nist_level": "L5",
        "aead": "AES-256-GCM",
        "kdf": "HKDF-SHA256"
    },
    "cs-kyber512-aesgcm-sphincs128f_sha2": {
        "kem_name": "ML-KEM-512",
        "kem_param": 512,
        "sig_name": "SLH-DSA-SHA2-128f",
        "sig_param": "SHA2-128f",
        "nist_level": "L1",
        "aead": "AES-256-GCM",
        "kdf": "HKDF-SHA256"
    },
    "cs-kyber1024-aesgcm-sphincs256f_sha2": {
        "kem_name": "ML-KEM-1024", 
        "kem_param": 1024,
        "sig_name": "SLH-DSA-SHA2-256f",
        "sig_param": "SHA2-256f",
        "nist_level": "L5",
        "aead": "AES-256-GCM",
        "kdf": "HKDF-SHA256"
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
    """
    Map suite to (kem_id, kem_param_id, sig_id, sig_param_id) as specified.
    
    Args:
        suite: Suite configuration dictionary
        
    Returns:
        4-tuple of header ID bytes: (kem_id, kem_param_id, sig_id, sig_param_id)
        
    Raises:
        NotImplementedError: If the suite uses unknown IDs/params
    """
    kem_name = suite.get("kem_name")
    sig_name = suite.get("sig_name")
    
    if kem_name not in _KEM_IDS:
        raise NotImplementedError(f"unknown KEM name: {kem_name}")
    
    if sig_name not in _SIG_IDS:
        raise NotImplementedError(f"unknown signature name: {sig_name}")
    
    kem_id, kem_param_id = _KEM_IDS[kem_name]
    sig_id, sig_param_id = _SIG_IDS[sig_name]
    
    return (kem_id, kem_param_id, sig_id, sig_param_id)


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