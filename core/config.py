"""
Core configuration constants for PQC drone-GCS secure proxy.

Single source of truth for all network ports, hosts, and runtime parameters.
"""

import os
from typing import Dict, Any


# Default configuration - all required keys with correct types
CONFIG = {
    # Handshake (TCP)
    "TCP_HANDSHAKE_PORT": 5800,

    # Encrypted UDP data-plane (network)
    "UDP_DRONE_RX": 5810,   # drone binds here; GCS sends here
    "UDP_GCS_RX": 5811,     # gcs binds here; Drone sends here

    # Plaintext UDP (local loopback to apps/FC)
    "DRONE_PLAINTEXT_TX": 14550,  # app→drone-proxy (to encrypt out)
    "DRONE_PLAINTEXT_RX": 14551,  # drone-proxy→app (after decrypt)
    "GCS_PLAINTEXT_TX": 14551,    # app→gcs-proxy
    "GCS_PLAINTEXT_RX": 14550,    # gcs-proxy→app

    # Hosts
    "DRONE_HOST": "127.0.0.1",
    "GCS_HOST": "127.0.0.1",

    # Crypto/runtime
    "REPLAY_WINDOW": 1024,
    "WIRE_VERSION": 1,      # header version byte (frozen)

    # --- Optional hardening / QoS knobs (NOT required; safe defaults) ---
    # Limit TCP handshake attempts accepted per IP at the GCS (server) side.
    # Model: token bucket; BURST tokens max, refilling at REFILL_PER_SEC tokens/sec.
    "HANDSHAKE_RL_BURST": 5,
    "HANDSHAKE_RL_REFILL_PER_SEC": 1,

    # Mark encrypted UDP with DSCP EF (46) to prioritize on WMM-enabled APs.
    # Set to None to disable. Implementation multiplies by 4 to form TOS.
    "ENCRYPTED_DSCP": 46,

    # Feature flag: if True, proxy prefixes app->proxy plaintext with 1 byte packet type.
    # 0x01 = MAVLink/data (forward to local app); 0x02 = control (route to policy engine).
    # When False (default), proxy passes bytes unchanged (backward compatible).
    "ENABLE_PACKET_TYPE": False,
}


# Required keys with their expected types
_REQUIRED_KEYS = {
    "TCP_HANDSHAKE_PORT": int,
    "UDP_DRONE_RX": int,
    "UDP_GCS_RX": int,
    "DRONE_PLAINTEXT_TX": int,
    "DRONE_PLAINTEXT_RX": int,
    "GCS_PLAINTEXT_TX": int,
    "GCS_PLAINTEXT_RX": int,
    "DRONE_HOST": str,
    "GCS_HOST": str,
    "REPLAY_WINDOW": int,
    "WIRE_VERSION": int,
}

# Keys that can be overridden by environment variables
_ENV_OVERRIDABLE = {
    "TCP_HANDSHAKE_PORT",
    "UDP_DRONE_RX", 
    "UDP_GCS_RX",
    "DRONE_PLAINTEXT_TX",  # Added for testing/benchmarking flexibility
    "DRONE_PLAINTEXT_RX",  # Added for testing/benchmarking flexibility  
    "GCS_PLAINTEXT_TX",    # Added for testing/benchmarking flexibility
    "GCS_PLAINTEXT_RX",    # Added for testing/benchmarking flexibility
    "DRONE_HOST",
    "GCS_HOST"
}


def validate_config(cfg: Dict[str, Any]) -> None:
    """
    Ensure all required keys exist with correct types/ranges.
    Raise NotImplementedError("<reason>") on any violation.
    No return value on success.
    """
    # Check all required keys exist
    missing_keys = set(_REQUIRED_KEYS.keys()) - set(cfg.keys())
    if missing_keys:
        raise NotImplementedError(f"CONFIG missing required keys: {', '.join(sorted(missing_keys))}")
    
    # Check types for all keys
    for key, expected_type in _REQUIRED_KEYS.items():
        value = cfg[key]
        if not isinstance(value, expected_type):
            raise NotImplementedError(f"CONFIG[{key}] must be {expected_type.__name__}, got {type(value).__name__}")
    
    # Validate port ranges
    for key in _REQUIRED_KEYS:
        if key.endswith("_PORT") or key.endswith("_RX") or key.endswith("_TX"):
            port = cfg[key]
            if not (1 <= port <= 65535):
                raise NotImplementedError(f"CONFIG[{key}] must be valid port (1-65535), got {port}")
    
    # Validate specific constraints
    if cfg["WIRE_VERSION"] != 1:
        raise NotImplementedError(f"CONFIG[WIRE_VERSION] must be 1 (frozen), got {cfg['WIRE_VERSION']}")
    
    if cfg["REPLAY_WINDOW"] < 64:
        raise NotImplementedError(f"CONFIG[REPLAY_WINDOW] must be >= 64, got {cfg['REPLAY_WINDOW']}")
    
    # Validate hosts are valid strings (basic check)
    for host_key in ["DRONE_HOST", "GCS_HOST"]:
        host = cfg[host_key]
        if not host or not isinstance(host, str):
            raise NotImplementedError(f"CONFIG[{host_key}] must be non-empty string, got {repr(host)}")
    
    # Optional keys are intentionally not required; do light validation if present
    if "ENCRYPTED_DSCP" in cfg and cfg["ENCRYPTED_DSCP"] is not None:
        if not (0 <= int(cfg["ENCRYPTED_DSCP"]) <= 63):
            raise NotImplementedError("CONFIG[ENCRYPTED_DSCP] must be 0..63 or None")


def _apply_env_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Apply environment variable overrides to config."""
    result = cfg.copy()
    
    for key in _ENV_OVERRIDABLE:
        env_var = key
        if env_var in os.environ:
            env_value = os.environ[env_var]
            expected_type = _REQUIRED_KEYS[key]
            
            try:
                if expected_type == int:
                    result[key] = int(env_value)
                elif expected_type == str:
                    result[key] = str(env_value)
                else:
                    raise NotImplementedError(f"Unsupported type for env override: {expected_type}")
            except ValueError:
                raise NotImplementedError(f"Invalid {expected_type.__name__} value for {env_var}: {env_value}")
    
    return result


# Apply environment overrides and validate
CONFIG = _apply_env_overrides(CONFIG)
validate_config(CONFIG)