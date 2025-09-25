"""
Unified CLI entrypoint for the PQC drone-GCS proxy.

Supports subcommands:
- init-identity: Create persistent GCS signing identity
- gcs: Start GCS proxy (requires secret key by default)  
- drone: Start drone proxy (requires GCS public key)

Uses persistent file-based keys by default for production security.
"""

import sys
import argparse
import signal
import os
from pathlib import Path
from typing import Optional

from oqs.oqs import Signature
from core.config import CONFIG
from core.suites import get_suite
from core.async_proxy import run_proxy
from core.logging_utils import get_logger

logger = get_logger("pqc")


def signal_handler(signum, frame):
    """Handle interrupt signals gracefully."""
    print("\nReceived interrupt signal. Shutting down...")
    sys.exit(0)


def create_secrets_dir():
    """Create secrets directory if it doesn't exist."""
    secrets_dir = Path("secrets")
    secrets_dir.mkdir(exist_ok=True)
    return secrets_dir


def init_identity_command(args):
    """Create GCS signing identity and save to persistent files."""
    # Use custom output_dir if provided, otherwise default secrets directory
    if hasattr(args, 'output_dir') and args.output_dir:
        secrets_dir = Path(args.output_dir)
        secrets_dir.mkdir(parents=True, exist_ok=True)
    else:
        secrets_dir = create_secrets_dir()
    
    try:
        suite = get_suite(args.suite) if hasattr(args, 'suite') and args.suite else get_suite("cs-kyber768-aesgcm-dilithium3")
    except KeyError as e:
        print(f"Error: Unknown suite: {args.suite if hasattr(args, 'suite') else 'default'}")
        sys.exit(1)
    
    secret_path = secrets_dir / "gcs_signing.key"
    public_path = secrets_dir / "gcs_signing.pub"
    
    if secret_path.exists() or public_path.exists():
        print("Warning: Identity files already exist. Overwriting with a new keypair.")
    
    try:
        sig = Signature(suite["sig_name"])
        if hasattr(sig, 'export_secret_key'):
            gcs_sig_public = sig.generate_keypair()
            gcs_sig_secret = sig.export_secret_key()
            
            # Write files with appropriate permissions
            secret_path.write_bytes(gcs_sig_secret)
            public_path.write_bytes(gcs_sig_public)
            
            # Secure the secret file
            try:
                os.chmod(secret_path, 0o600)
            except Exception:
                pass  # Best effort on Windows
                
            print(f"Created GCS signing identity:")
            print(f"  Secret: {secret_path}")
            print(f"  Public: {public_path}")
            print(f"  Public key (hex): {gcs_sig_public.hex()}")
            return 0  # Success
            
        else:
            print("Error: oqs build lacks key import/export; use --ephemeral or upgrade oqs-python.")
            sys.exit(1)
            
    except Exception as e:
        print(f"Error creating identity: {e}")
        sys.exit(1)


def gcs_command(args):
    """Start GCS proxy."""
    try:
        suite = get_suite(args.suite)
    except KeyError as e:
        print(f"Error: Unknown suite: {args.suite}")
        sys.exit(1)
    
    gcs_sig_secret = None
    gcs_sig_public = None
    
    if args.ephemeral:
        print("⚠️  WARNING: Using EPHEMERAL keys - not suitable for production!")
        print("⚠️  Key will be lost when process exits.")
        print()
        
        # Generate ephemeral keypair
        sig = Signature(suite["sig_name"])
        gcs_sig_public = sig.generate_keypair()
        gcs_sig_secret = sig
        print("Generated ephemeral GCS signing keypair:")
        print(f"Public key (hex): {gcs_sig_public.hex()}")
        print("Provide this to the drone via --gcs-pub-hex or --peer-pubkey-file")
        print()
        
    else:
        # Load persistent key
        if args.gcs_secret_file:
            secret_path = Path(args.gcs_secret_file)
        else:
            secret_path = Path("secrets/gcs_signing.key")
            
        if not secret_path.exists():
            print(f"Error: Secret key file not found: {secret_path}")
            print("Run 'python -m core.run_proxy init-identity' to create one,")
            print("or use --ephemeral for development only.")
            sys.exit(1)
            
        secret_bytes = None
        try:
            secret_bytes = secret_path.read_bytes()
        except Exception as exc:
            print(f"Error reading secret key file: {exc}")
            sys.exit(1)

        load_errors = []
        imported_public: Optional[bytes] = None
        load_method: Optional[str] = None

        try:
            primary_sig = Signature(suite["sig_name"])
        except Exception as exc:
            load_errors.append(f"Signature ctor failed: {exc}")
            primary_sig = None  # type: ignore

        if primary_sig is not None and hasattr(primary_sig, "import_secret_key"):
            try:
                imported_public = primary_sig.import_secret_key(secret_bytes)
                gcs_sig_secret = primary_sig
                load_method = "import_secret_key"
            except Exception as exc:
                load_errors.append(f"import_secret_key failed: {exc}")

        if gcs_sig_secret is None:
            try:
                fallback_sig = Signature(suite["sig_name"], secret_key=secret_bytes)
                gcs_sig_secret = fallback_sig
                load_method = "ctor_secret_key"
            except TypeError as exc:
                load_errors.append(f"ctor secret_key unsupported: {exc}")
            except Exception as exc:
                load_errors.append(f"ctor secret_key failed: {exc}")

        if gcs_sig_secret is None:
            print("Error: oqs build lacks usable key import. Tried import_secret_key and constructor fallback without success.")
            if load_errors:
                print("Details:")
                for err in load_errors:
                    print(f"  - {err}")
            print("Consider running with --ephemeral or upgrading oqs-python/liboqs with key import support.")
            sys.exit(1)

        print("Loaded GCS signing key from file.")
        if load_method == "ctor_secret_key":
            print("Using constructor-based fallback because import/export APIs are unavailable.")

        gcs_sig_public = imported_public
        if gcs_sig_public is None:
            public_candidates = []
            if secret_path.suffix:
                public_candidates.append(secret_path.with_suffix(".pub"))
            public_candidates.append(secret_path.parent / "gcs_signing.pub")
            seen = set()
            for candidate in public_candidates:
                key = str(candidate.resolve()) if candidate.exists() else str(candidate)
                if key in seen:
                    continue
                seen.add(key)
                if candidate.exists():
                    try:
                        gcs_sig_public = candidate.read_bytes()
                        print(f"Loaded public key from {candidate}.")
                    except Exception as exc:
                        load_errors.append(f"public key read failed ({candidate}): {exc}")
                    break

        if gcs_sig_public is not None:
            print(f"Public key (hex): {gcs_sig_public.hex()}")
        else:
            print("Warning: Could not locate public key file for display. Ensure the drone has the matching public key.")
        print()
    
    try:
        print(f"Starting GCS proxy with suite {args.suite}")
        if args.stop_seconds:
            print(f"Will auto-stop after {args.stop_seconds} seconds")
        print()
        
        counters = run_proxy(
            role="gcs",
            suite=suite,
            cfg=CONFIG,
            gcs_sig_secret=gcs_sig_secret,
            gcs_sig_public=None,
            stop_after_seconds=args.stop_seconds
        )
        
        # Log final counters as JSON
        logger.info("GCS proxy shutdown", extra={"counters": counters})
        
        print("GCS proxy stopped. Final counters:")
        for key, value in counters.items():
            print(f"  {key}: {value}")
            
    except KeyboardInterrupt:
        print("\nGCS proxy stopped by user.")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def drone_command(args):
    """Start drone proxy."""
    try:
        suite = get_suite(args.suite)
    except KeyError as e:
        print(f"Error: Unknown suite: {args.suite}")
        sys.exit(1)
    
    # Get GCS public key
    gcs_sig_public = None
    
    try:
        if args.peer_pubkey_file:
            pub_path = Path(args.peer_pubkey_file)
            if not pub_path.exists():
                raise FileNotFoundError(f"Public key file not found: {pub_path}")
            gcs_sig_public = pub_path.read_bytes()
        elif args.gcs_pub_hex:
            gcs_sig_public = bytes.fromhex(args.gcs_pub_hex)
        else:
            # Try default location
            default_pub = Path("secrets/gcs_signing.pub")
            if default_pub.exists():
                gcs_sig_public = default_pub.read_bytes()
                print(f"Using GCS public key from: {default_pub}")
            else:
                raise ValueError("No GCS public key provided. Use --peer-pubkey-file, --gcs-pub-hex, or ensure secrets/gcs_signing.pub exists.")
                
    except Exception as e:
        print(f"Error loading GCS public key: {e}")
        sys.exit(1)
    
    try:
        print(f"Starting drone proxy with suite {args.suite}")
        if args.stop_seconds:
            print(f"Will auto-stop after {args.stop_seconds} seconds")
        print()
        
        counters = run_proxy(
            role="drone",
            suite=suite,
            cfg=CONFIG,
            gcs_sig_secret=None,
            gcs_sig_public=gcs_sig_public,
            stop_after_seconds=args.stop_seconds
        )
        
        # Log final counters as JSON
        logger.info("Drone proxy shutdown", extra={"counters": counters})
        
        print("Drone proxy stopped. Final counters:")
        for key, value in counters.items():
            print(f"  {key}: {value}")
            
    except KeyboardInterrupt:
        print("\nDrone proxy stopped by user.")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def main():
    """Main CLI entrypoint with subcommands."""
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, signal_handler)
    
    parser = argparse.ArgumentParser(description="PQC Drone-GCS Secure Proxy")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # init-identity subcommand
    init_parser = subparsers.add_parser('init-identity', 
                                       help='Create persistent GCS signing identity')
    init_parser.add_argument("--suite", default="cs-kyber768-aesgcm-dilithium3",
                            help="Cryptographic suite ID (default: cs-kyber768-aesgcm-dilithium3)")
    init_parser.add_argument("--output-dir", 
                            help="Directory for key files (default: secrets/)")
    
    # gcs subcommand
    gcs_parser = subparsers.add_parser('gcs', help='Start GCS proxy')
    gcs_parser.add_argument("--suite", required=True,
                           help="Cryptographic suite ID (e.g., cs-kyber768-aesgcm-dilithium3)")
    gcs_parser.add_argument("--gcs-secret-file",
                           help="Path to GCS secret key file (default: secrets/gcs_signing.key)")
    gcs_parser.add_argument("--ephemeral", action='store_true',
                           help="Use ephemeral keys (development only - prints warning)")
    gcs_parser.add_argument("--stop-seconds", type=float,
                           help="Auto-stop after N seconds (for testing)")
    
    # drone subcommand
    drone_parser = subparsers.add_parser('drone', help='Start drone proxy')
    drone_parser.add_argument("--suite", required=True,
                             help="Cryptographic suite ID (e.g., cs-kyber768-aesgcm-dilithium3)")
    drone_parser.add_argument("--peer-pubkey-file",
                             help="Path to GCS public key file (default: secrets/gcs_signing.pub)")
    drone_parser.add_argument("--gcs-pub-hex",
                             help="GCS public key as hex string")
    drone_parser.add_argument("--stop-seconds", type=float,
                             help="Auto-stop after N seconds (for testing)")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
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
    
    # Route to appropriate command handler
    if args.command == 'init-identity':
        init_identity_command(args)
    elif args.command == 'gcs':
        gcs_command(args)
    elif args.command == 'drone':
        drone_command(args)


if __name__ == "__main__":
    main()