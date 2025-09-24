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
    parser.add_argument("--gcs-pub-hex", 
                       help="GCS public key as hex string (required for drone role)")
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
    
    gcs_sig_secret: Optional[bytes] = None
    gcs_sig_public: Optional[bytes] = None
    
    if args.role == "gcs":
        # Generate fresh GCS signing keypair for testing
        try:
            sig = Signature(suite["sig_name"])
            gcs_sig_public = sig.generate_keypair()
            gcs_sig_secret = sig.export_secret_key()
            
            print("Generated GCS signing keypair:")
            print(f"Public key (hex): {gcs_sig_public.hex()}")
            print("Share this public key with the drone process.")
            print()
            
        except Exception as e:
            print(f"Error generating GCS keypair: {e}")
            sys.exit(1)
            
    elif args.role == "drone":
        if not args.gcs_pub_hex:
            print("Error: --gcs-pub-hex required for drone role")
            sys.exit(1)
            
        try:
            gcs_sig_public = bytes.fromhex(args.gcs_pub_hex)
        except ValueError:
            print("Error: Invalid hex string for --gcs-pub-hex")
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