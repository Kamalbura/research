"""
CLI entrypoint for testing the PQC drone-GCS proxy.

Generates ephemeral keys for testing purposes. Never writes secrets to disk.
This file exists only to make local development easy until wrappers/systemd are in place.
"""

import sys
import argparse
import signal
from typing import Optional

from oqs.oqs import Signature
from core.config import CONFIG
from core.suites import get_suite
from core.async_proxy import run_proxy


def signal_handler(signum, frame):
    """Handle interrupt signals gracefully."""
    print("\nReceived interrupt signal. Shutting down...")
    sys.exit(0)


def main():
    """Main CLI entrypoint."""
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal_handler)
    
    parser = argparse.ArgumentParser(description="PQC Drone-GCS Secure Proxy")
    parser.add_argument("--role", required=True, choices=["drone", "gcs"], 
                       help="Proxy role: drone or gcs")
    parser.add_argument("--suite", required=True, 
                       help="Cryptographic suite ID (e.g., cs-kyber768-aesgcm-dilithium3)")
    # Identity / keys
    parser.add_argument("--gcs-pub-hex",
                        help="GCS public key as hex string (drone role; alternative to --peer-pubkey-file)")
    parser.add_argument("--peer-pubkey-file",
                        help="Path to file containing GCS public key bytes (drone role)")
    parser.add_argument("--gcs-secret-file",
                        help="Path to file containing GCS signing secret key bytes (GCS role). If omitted, an ephemeral keypair is generated.")
    parser.add_argument("--stop-seconds", type=float,
                       help="Auto-stop after N seconds (for testing)")
    
    args = parser.parse_args()
    
    # Validate required CONFIG keys
    required_keys = [
        "TCP_HANDSHAKE_PORT", "UDP_DRONE_RX", "UDP_GCS_RX", 
        "DRONE_PLAINTEXT_TX", "DRONE_PLAINTEXT_RX",
        "GCS_PLAINTEXT_TX", "GCS_PLAINTEXT_RX", 
        "DRONE_HOST", "GCS_HOST", "REPLAY_WINDOW"
    ]
    
    missing_keys = [key for key in required_keys if key not in CONFIG]
    if missing_keys:
        print(f"Error: CONFIG missing required keys: {', '.join(missing_keys)}")
        sys.exit(1)
    
    try:
        suite = get_suite(args.suite)
    except KeyError as e:
        print(f"Error: Unknown suite: {args.suite}")
        sys.exit(1)
    
    gcs_sig_secret: Optional[object] = None  # Signature object for GCS role
    gcs_sig_public: Optional[bytes] = None   # Public key for drone role
    
    if args.role == "gcs":
        # Load persistent secret if provided; otherwise generate ephemeral.
        try:
            sig = Signature(suite["sig_name"])
            if args.gcs_secret_file:
                with open(args.gcs_secret_file, "rb") as f:
                    secret = f.read()
                # oqs-python exposes import/export on recent builds. Try import; fall back to generate if not available.
                if hasattr(sig, "import_secret_key"):
                    gcs_sig_public = sig.import_secret_key(secret)
                else:
                    # If import is not available in this build, fail clearly.
                    raise RuntimeError("This oqs build does not support import_secret_key; omit --gcs-secret-file to use an ephemeral keypair.")
                gcs_sig_secret = sig
                print("Loaded GCS signing key from file.")
            else:
                gcs_sig_public = sig.generate_keypair()
                gcs_sig_secret = sig
                print("Generated GCS signing keypair (ephemeral for this process):")
            print(f"Public key (hex): {gcs_sig_public.hex()}")
            print("Provide this to the drone via --gcs-pub-hex or --peer-pubkey-file")
            print()
        except Exception as e:
            print(f"Error preparing GCS keypair: {e}")
            sys.exit(1)
            
    elif args.role == "drone":
        # Accept either a file path or a hex string for the GCS public key.
        try:
            if args.peer_pubkey_file:
                with open(args.peer_pubkey_file, "rb") as f:
                    gcs_sig_public = f.read()
            elif args.gcs_pub_hex:
                gcs_sig_public = bytes.fromhex(args.gcs_pub_hex)
            else:
                raise ValueError("Missing --peer-pubkey-file or --gcs-pub-hex")
        except Exception as e:
            print(f"Error loading GCS public key: {e}")
            sys.exit(1)
    
    try:
        print(f"Starting {args.role} proxy with suite {args.suite}")
        if args.stop_seconds:
            print(f"Will auto-stop after {args.stop_seconds} seconds")
        print()
        
        counters = run_proxy(
            role=args.role,
            suite=suite,
            cfg=CONFIG,
            gcs_sig_secret=gcs_sig_secret,
            gcs_sig_public=gcs_sig_public,
            stop_after_seconds=args.stop_seconds
        )
        
        print("Proxy stopped. Final counters:")
        for key, value in counters.items():
            print(f"  {key}: {value}")
            
    except KeyboardInterrupt:
        print("\nProxy stopped by user.")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()