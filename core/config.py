"""
Core configuration constants for PQC drone-GCS secure proxy.

Single source of truth for all network ports, hosts, and runtime parameters.
"""

import os
from ipaddress import ip_address
from typing import Dict, Any


# Baseline host defaults reused throughout the configuration payload.
_DEFAULT_DRONE_HOST = "192.168.1.139"
_DEFAULT_GCS_HOST = "192.168.1.207"


# Default configuration - all required keys with correct types
CONFIG = {
    # Handshake (TCP)
    "TCP_HANDSHAKE_PORT": 46000,

    # Encrypted UDP data-plane (network)
    "UDP_DRONE_RX": 46012,   # drone binds here; GCS sends here
    "UDP_GCS_RX": 46011,     # gcs binds here; Drone sends here

    # Plaintext UDP (local loopback to apps/FC)
    "DRONE_PLAINTEXT_TX": 47003,  # app→drone-proxy (to encrypt out)
    "DRONE_PLAINTEXT_RX": 47004,  # drone-proxy→app (after decrypt)
    "GCS_PLAINTEXT_TX": 47001,    # app→gcs-proxy
    "GCS_PLAINTEXT_RX": 47002,    # gcs-proxy→app
    "DRONE_PLAINTEXT_HOST": "127.0.0.1",
    "GCS_PLAINTEXT_HOST": "127.0.0.1",

    # Hosts
    "DRONE_HOST": _DEFAULT_DRONE_HOST,
    "GCS_HOST": _DEFAULT_GCS_HOST,

    # Pre-shared key (hex) for drone authentication during handshake.
    # Default is a placeholder; override in production via environment variable.
    "DRONE_PSK": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",

    # Crypto/runtime
    "REPLAY_WINDOW": 1024,
    "WIRE_VERSION": 1,      # header version byte (frozen)
    # Allow slower suites to finish the rekey handshake without timing out
    "REKEY_HANDSHAKE_TIMEOUT": 45.0,

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
    "ENABLE_PACKET_TYPE": True,

    # Enforce strict matching of encrypted UDP peer IP/port with the authenticated handshake peer.
    # Disable (set to False) only when operating behind NAT where source ports may differ.
    "STRICT_UDP_PEER_MATCH": True,

    # Log real session IDs only when explicitly enabled (default False masks them to hashes).
    "LOG_SESSION_ID": False,

    # --- Simple automation defaults (tools/auto/*_simple.py) ---
    "DRONE_CONTROL_HOST": "0.0.0.0",
    "DRONE_CONTROL_PORT": 48080,
    "SIMPLE_VERIFY_TIMEOUT_S": 5.0,
    "SIMPLE_PACKETS_PER_SUITE": 1,
    "SIMPLE_PACKET_DELAY_S": 0.0,
    "SIMPLE_SUITE_DWELL_S": 0.0,
    "SIMPLE_INITIAL_SUITE": "cs-mlkem768-aesgcm-mldsa65",

    # Automation defaults for tools/auto orchestration scripts
    "AUTO_DRONE": {
        # Session IDs default to "<prefix>_<unix>" unless DRONE_SESSION_ID env overrides
        "session_prefix": "run",
        # Optional explicit initial suite override (None -> discover from secrets/config)
        "initial_suite": None,
        # Enable follower monitors (perf/pidstat/psutil) by default
        "monitors_enabled": True,
        # Apply CPU governor tweaks unless disabled
        "cpu_optimize": True,
        # Enable telemetry publisher back to the scheduler
        "telemetry_enabled": True,
        # Optional explicit telemetry host/port (None -> derive from CONTROL_HOST defaults)
        "telemetry_host": None,
        "telemetry_port": 52080,
        # Override monitoring output base directory (None -> DEFAULT_MONITOR_BASE)
        "monitor_output_base": None,
        # Optional environment exports applied before creating the power monitor
        "power_env": {
            # Maintain 1 kHz sampling by default; backend remains auto unless overridden
            "DRONE_POWER_BACKEND": "ina219",
            "DRONE_POWER_SAMPLE_HZ": "1000",
            "INA219_I2C_BUS": "1",
            "INA219_ADDR": "0x40",
            "INA219_SHUNT_OHM": "0.1",
        },
    },

    "AUTO_GCS": {
        # Session IDs default to "<prefix>_<unix>" unless GCS_SESSION_ID env overrides
        "session_prefix": "run",
        # Traffic profile: "blast", "constant", "mavproxy", or "saturation"
        "traffic": "constant",
        # Duration for active traffic window per suite (seconds)
        "duration_s": 45.0,
        # Delay after rekey before starting traffic (seconds)
        "pre_gap_s": 1.0,
        # Delay between suites (seconds)
        "inter_gap_s": 15.0,
        # UDP payload size (bytes) for blaster calculations
        "payload_bytes": 256,
        # Sample every Nth send/receive event (0 disables)
        "event_sample": 100,
        # Number of full passes across suite list
        "passes": 1,
        # Explicit packets-per-second override; 0 means best-effort
        "rate_pps": 0,
        # Optional bandwidth target in Mbps (converted to PPS if > 0)
        "bandwidth_mbps": 0.0,
        # Max rate explored during saturation sweeps (Mbps)
        "max_rate_mbps": 200.0,
        # Optional ordered suite subset (None -> all suites from core.suites, including ChaCha20-Poly1305 and ASCON variants)
        "suites": None,
        # Launch local GCS proxy under scheduler control
        "launch_proxy": True,
        # Enable local proxy monitors (perf/pidstat/psutil)
        "monitors_enabled": True,
        # Start telemetry collector on the scheduler side
        "telemetry_enabled": True,
        # Bind/port for telemetry collector (defaults to CONFIG values)
        "telemetry_bind_host": "0.0.0.0",
        "telemetry_port": 52080,
        # Emit combined Excel workbook when run completes
        "export_combined_excel": True,
        # Optional post-run fetch of drone artifacts (logs, power captures)
        "post_fetch": {
            "enabled": True,
            "host": None,
            "username": "dev",
            "password": "kamal123",
            "port": 22,
            "logs_remote": "~/research/logs/auto/drone",
            "logs_local": "logs/auto",
            "output_remote": "~/research/output/drone",
            "output_local": "output/drone",
        },
        "post_report": {
            "enabled": True,
            "script": "tools/report_constant_run.py",
            "output_dir": "output/gcs",
            "table_name": "run_summary_table.md",
            "text_name": "run_suite_summaries.txt",
        },
    },
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
    "DRONE_PLAINTEXT_HOST": str,
    "GCS_PLAINTEXT_HOST": str,
    "REPLAY_WINDOW": int,
    "WIRE_VERSION": int,
    "ENABLE_PACKET_TYPE": bool,
    "STRICT_UDP_PEER_MATCH": bool,
    "LOG_SESSION_ID": bool,
    "DRONE_PSK": str,
    "REKEY_HANDSHAKE_TIMEOUT": float,
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
    "ENABLE_PACKET_TYPE",
    "STRICT_UDP_PEER_MATCH",
    "LOG_SESSION_ID",
    "DRONE_PSK",
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
        if key == "REKEY_HANDSHAKE_TIMEOUT":
            if not isinstance(value, (int, float)):
                raise NotImplementedError(
                    f"CONFIG[{key}] must be float seconds, got {type(value).__name__}"
                )
            continue
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
    if cfg["REPLAY_WINDOW"] > 8192:
        raise NotImplementedError(f"CONFIG[REPLAY_WINDOW] must be <= 8192, got {cfg['REPLAY_WINDOW']}")
    
    # Validate hosts are valid strings (basic check)
    for host_key in ["DRONE_HOST", "GCS_HOST"]:
        host = cfg[host_key]
        if not host or not isinstance(host, str):
            raise NotImplementedError(f"CONFIG[{host_key}] must be non-empty string, got {repr(host)}")
        try:
            ip_address(host)
        except ValueError as exc:
            raise NotImplementedError(f"CONFIG[{host_key}] must be a valid IP address: {exc}")

    # Loopback hosts for plaintext path may remain hostnames (e.g., 127.0.0.1).
    allow_non_loopback_plaintext = str(os.environ.get("ALLOW_NON_LOOPBACK_PLAINTEXT", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    for host_key in ["DRONE_PLAINTEXT_HOST", "GCS_PLAINTEXT_HOST"]:
        host = cfg[host_key]
        if not host or not isinstance(host, str):
            raise NotImplementedError(f"CONFIG[{host_key}] must be non-empty string, got {repr(host)}")
        if allow_non_loopback_plaintext:
            continue
        try:
            parsed = ip_address(host)
            if not parsed.is_loopback:
                raise NotImplementedError(
                    f"CONFIG[{host_key}] must be a loopback address unless ALLOW_NON_LOOPBACK_PLAINTEXT is set"
                )
        except ValueError:
            if host.lower() != "localhost":
                raise NotImplementedError(
                    f"CONFIG[{host_key}] must be loopback/localhost unless ALLOW_NON_LOOPBACK_PLAINTEXT is set"
                )
    
    # Optional keys are intentionally not required; do light validation if present
    if "ENCRYPTED_DSCP" in cfg and cfg["ENCRYPTED_DSCP"] is not None:
        if not (0 <= int(cfg["ENCRYPTED_DSCP"]) <= 63):
            raise NotImplementedError("CONFIG[ENCRYPTED_DSCP] must be 0..63 or None")

    psk = cfg.get("DRONE_PSK", "")
    try:
        psk_bytes = bytes.fromhex(psk)
    except ValueError:
        raise NotImplementedError("CONFIG[DRONE_PSK] must be a hex string")
    if len(psk_bytes) != 32:
        raise NotImplementedError("CONFIG[DRONE_PSK] must decode to 32 bytes")


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
                elif expected_type == bool:
                    lowered = str(env_value).strip().lower()
                    if lowered in {"1", "true", "yes", "on"}:
                        result[key] = True
                    elif lowered in {"0", "false", "no", "off"}:
                        result[key] = False
                    else:
                        raise ValueError(f"invalid boolean literal: {env_value}")
                elif expected_type == float:
                    result[key] = float(env_value)
                else:
                    raise NotImplementedError(f"Unsupported type for env override: {expected_type}")
            except ValueError:
                raise NotImplementedError(f"Invalid {expected_type.__name__} value for {env_var}: {env_value}")
    
    return result


# Apply environment overrides and validate
CONFIG = _apply_env_overrides(CONFIG)
validate_config(CONFIG)