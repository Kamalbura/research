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
import json
import time
import logging
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

from core.config import CONFIG
from core.suites import get_suite, build_suite_id
from core.logging_utils import get_logger, configure_file_logger

logger = get_logger("pqc")


def _format_duration_ns(ns: int) -> str:
    """Return human readable representation for a duration in nanoseconds."""

    if ns < 0:
        ns = 0
    if ns >= 1_000_000_000:
        seconds = ns / 1_000_000_000.0
        return f"{seconds:.3f} s"
    if ns >= 1_000_000:
        millis = ns / 1_000_000.0
        return f"{millis:.3f} ms"
    if ns >= 1_000:
        micros = ns / 1_000.0
        return f"{micros:.3f} µs"
    return f"{ns} ns"


def _ns_to_ms(value: object) -> float:
    try:
        ns = float(value)
    except (TypeError, ValueError):
        return 0.0
    if ns <= 0.0:
        return 0.0
    return round(ns / 1_000_000.0, 6)


def _flatten_part_b_metrics(handshake_metrics: Dict[str, object]) -> Dict[str, object]:
    """Derive flattened Part B primitive metrics from the handshake payload."""

    if not isinstance(handshake_metrics, dict):
        return {}

    primitives = handshake_metrics.get("primitives") or {}
    if not isinstance(primitives, dict):
        primitives = {}

    kem_metrics = primitives.get("kem") if isinstance(primitives.get("kem"), dict) else {}
    sig_metrics = primitives.get("signature") if isinstance(primitives.get("signature"), dict) else {}

    artifacts = handshake_metrics.get("artifacts") if isinstance(handshake_metrics.get("artifacts"), dict) else {}

    flat: Dict[str, object] = {}

    flat["kem_keygen_ms"] = _ns_to_ms(kem_metrics.get("keygen_ns"))
    flat["kem_encaps_ms"] = _ns_to_ms(kem_metrics.get("encap_ns"))
    flat["kem_decap_ms"] = _ns_to_ms(kem_metrics.get("decap_ns"))
    flat["sig_sign_ms"] = _ns_to_ms(sig_metrics.get("sign_ns"))
    flat["sig_verify_ms"] = _ns_to_ms(sig_metrics.get("verify_ns"))

    flat["pub_key_size_bytes"] = int(
        kem_metrics.get("public_key_bytes")
        or artifacts.get("public_key_bytes")
        or 0
    )
    flat["ciphertext_size_bytes"] = int(kem_metrics.get("ciphertext_bytes", 0) or 0)
    flat["sig_size_bytes"] = int(
        sig_metrics.get("signature_bytes")
        or artifacts.get("signature_bytes")
        or 0
    )
    flat["shared_secret_size_bytes"] = int(kem_metrics.get("shared_secret_bytes", 0) or 0)

    total_ns = 0
    for key in ("keygen_ns", "encap_ns", "decap_ns"):
        value = kem_metrics.get(key)
        if isinstance(value, (int, float)) and value > 0:
            total_ns += int(value)
    for key in ("sign_ns", "verify_ns"):
        value = sig_metrics.get(key)
        if isinstance(value, (int, float)) and value > 0:
            total_ns += int(value)
    flat["primitive_total_ms"] = _ns_to_ms(total_ns)

    return flat


def _augment_part_b_metrics(counters: Dict[str, object]) -> None:
    """Inject flattened primitive timing/size metrics into counter payload."""

    if not isinstance(counters, dict):  # defensive guard
        return

    handshake_payload = counters.get("handshake_metrics")
    flat_metrics = _flatten_part_b_metrics(handshake_payload) if isinstance(handshake_payload, dict) else {}
    if not flat_metrics:
        return

    for key, value in flat_metrics.items():
        # Avoid clobbering values already populated upstream (e.g., tests)
        counters.setdefault(key, value)


def _pretty_print_counters(counters: Dict[str, object]) -> None:
    """Display counters with special handling for nested metrics."""

    scalar_items = []
    handshake_payload: Optional[Dict[str, object]] = None
    primitive_payload: Optional[Dict[str, Dict[str, object]]] = None

    for key, value in counters.items():
        if key == "handshake_metrics" and isinstance(value, dict):
            handshake_payload = value  # defer printing until after scalars
            continue
        if key == "primitive_metrics" and isinstance(value, dict):
            primitive_payload = value  # defer printing until after scalars
            continue
        scalar_items.append((key, value))

    for key, value in sorted(scalar_items, key=lambda item: item[0]):
        print(f"  {key}: {value}")

    if handshake_payload:
        print("  handshake_metrics:")
        for key, value in sorted(handshake_payload.items(), key=lambda item: item[0]):
            print(f"    {key}: {value}")

    if primitive_payload:
        print("  primitive_metrics:")
        for name, stats in sorted(primitive_payload.items(), key=lambda item: item[0]):
            if not isinstance(stats, dict):
                print(f"    {name}: {stats}")
                continue
            count = int(stats.get("count", 0) or 0)
            total_ns = int(stats.get("total_ns", 0) or 0)
            avg_ns = total_ns // count if count > 0 else 0
            min_ns = int(stats.get("min_ns", 0) or 0)
            max_ns = int(stats.get("max_ns", 0) or 0)
            total_in = int(stats.get("total_in_bytes", 0) or 0)
            total_out = int(stats.get("total_out_bytes", 0) or 0)
            print(
                "    "
                f"{name}: count={count}, avg={_format_duration_ns(avg_ns)}, "
                f"min={_format_duration_ns(min_ns)}, max={_format_duration_ns(max_ns)}, "
                f"in_bytes={total_in}, out_bytes={total_out}"
            )

def _require_signature_class():
    """Lazily import oqs Signature and provide a friendly error if missing."""

    try:
        from oqs.oqs import Signature  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via CLI
        if exc.name in {"oqs", "oqs.oqs"}:
            print(
                "Error: oqs-python is required for cryptographic operations. "
                "Install it with 'pip install oqs-python' or activate the project environment."
            )
            sys.exit(1)
        raise

    return Signature


def _require_run_proxy():
    """Import run_proxy only when needed, surfacing helpful guidance on failure."""

    try:
        from core.async_proxy import run_proxy as _run_proxy  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via CLI
        if exc.name in {"oqs", "oqs.oqs"}:
            print(
                "Error: oqs-python is required to start the proxy. "
                "Install it with 'pip install oqs-python' or activate the project environment."
            )
            sys.exit(1)
        raise

    return _run_proxy


def _build_matrix_secret_loader(
    *,
    suite_id: Optional[str],
    default_secret_path: Optional[Path],
    initial_secret: Optional[object],
    signature_cls,
    matrix_dir: Optional[Path] = None,
) -> Callable[[Dict[str, object]], object]:
    """Return loader that fetches per-suite signing secrets from disk.

    The loader prefers a suite-specific directory under `secrets/matrix/` and falls
    back to the primary secret path when targeting the initial suite. Results are
    cached per suite and guarded with a lock because rekeys may run in background
    threads.
    """

    lock = threading.Lock()
    cache: Dict[str, object] = {}
    if suite_id and initial_secret is not None and isinstance(initial_secret, signature_cls):
        cache[suite_id] = initial_secret

    matrix_secrets_dir = matrix_dir or Path("secrets/matrix")

    def instantiate(secret_bytes: bytes, sig_name: str):
        errors = []
        sig_obj = None
        try:
            sig_obj = signature_cls(sig_name)
        except Exception as exc:  # pragma: no cover - depends on oqs build
            errors.append(f"Signature ctor failed: {exc}")
            sig_obj = None

        if sig_obj is not None and hasattr(sig_obj, "import_secret_key"):
            try:
                sig_obj.import_secret_key(secret_bytes)
                return sig_obj
            except Exception as exc:
                errors.append(f"import_secret_key failed: {exc}")

        try:
            return signature_cls(sig_name, secret_key=secret_bytes)
        except TypeError as exc:
            errors.append(f"ctor secret_key unsupported: {exc}")
        except Exception as exc:  # pragma: no cover - defensive logging only
            errors.append(f"ctor secret_key failed: {exc}")

        detail = "; ".join(errors) if errors else "unknown error"
        raise RuntimeError(f"Unable to load signature secret: {detail}")

    def load_secret_for_suite(target_suite: Dict[str, object]):
        target_suite_id = target_suite.get("suite_id") if isinstance(target_suite, dict) else None
        if not target_suite_id:
            raise RuntimeError("Suite dictionary missing suite_id")

        with lock:
            cached = cache.get(target_suite_id)
            if cached is not None:
                return cached

        candidates = []
        if default_secret_path and suite_id and target_suite_id == suite_id:
            candidates.append(default_secret_path)
        candidates.append(matrix_secrets_dir / target_suite_id / "gcs_signing.key")

        seen: Dict[str, None] = {}
        for candidate in candidates:
            candidate_path = candidate.expanduser()
            key = str(candidate_path.resolve()) if candidate_path.exists() else str(candidate_path)
            if key in seen:
                continue
            seen[key] = None
            if not candidate_path.exists():
                continue
            try:
                secret_bytes = candidate_path.read_bytes()
            except Exception as exc:
                raise RuntimeError(f"Failed to read GCS secret key {candidate_path}: {exc}") from exc
            try:
                sig_obj = instantiate(secret_bytes, target_suite["sig_name"])  # type: ignore[index]
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to load GCS secret key {candidate_path} for suite {target_suite_id}: {exc}"
                ) from exc
            with lock:
                cache[target_suite_id] = sig_obj
            return sig_obj

        raise FileNotFoundError(f"No GCS signing secret key found for suite {target_suite_id}")

    return load_secret_for_suite


def _build_matrix_public_loader(
    *,
    suite_id: Optional[str],
    default_public_path: Optional[Path],
    initial_public: Optional[bytes],
    matrix_dir: Optional[Path] = None,
) -> Callable[[Dict[str, object]], bytes]:
    """Return loader that fetches per-suite GCS signing public keys from disk."""

    lock = threading.Lock()
    cache: Dict[str, bytes] = {}
    if suite_id and initial_public is not None:
        cache[suite_id] = initial_public

    matrix_public_dir = matrix_dir or Path("secrets/matrix")

    def load_public_for_suite(target_suite: Dict[str, object]) -> bytes:
        target_suite_id = target_suite.get("suite_id") if isinstance(target_suite, dict) else None
        if not target_suite_id:
            raise RuntimeError("Suite dictionary missing suite_id")

        with lock:
            cached = cache.get(target_suite_id)
            if cached is not None:
                return cached

        candidates = []
        if default_public_path and suite_id and target_suite_id == suite_id:
            candidates.append(default_public_path)
        candidates.append(matrix_public_dir / target_suite_id / "gcs_signing.pub")

        seen: Dict[str, None] = {}
        for candidate in candidates:
            candidate_path = candidate.expanduser()
            key = str(candidate_path.resolve()) if candidate_path.exists() else str(candidate_path)
            if key in seen:
                continue
            seen[key] = None
            if not candidate_path.exists():
                continue
            try:
                public_bytes = candidate_path.read_bytes()
            except Exception as exc:
                raise RuntimeError(f"Failed to read GCS public key {candidate_path}: {exc}") from exc
            with lock:
                cache[target_suite_id] = public_bytes
            return public_bytes

        raise FileNotFoundError(f"No GCS signing public key found for suite {target_suite_id}")

    return load_public_for_suite


def signal_handler(signum, frame):
    """Handle interrupt signals gracefully."""
    print("\nReceived interrupt signal. Shutting down...")
    sys.exit(0)


def create_secrets_dir():
    """Create secrets directory if it doesn't exist."""
    secrets_dir = Path("secrets")
    secrets_dir.mkdir(exist_ok=True)
    return secrets_dir


def write_json_report(json_path: Optional[str], payload: dict, *, quiet: bool = False) -> None:
    """Persist counters payload to JSON if a path is provided."""

    if not json_path:
        return

    try:
        path = Path(json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        if not quiet:
            print(f"Wrote JSON report to {path}")
    except Exception as exc:
        print(f"Warning: Failed to write JSON output to {json_path}: {exc}")


def _resolve_suite(args, role_label: str) -> dict:
    """Resolve suite via legacy --suite or new --kem/--aead/--sig components."""

    suite_arg = getattr(args, "suite", None)
    kem = getattr(args, "kem", None)
    sig = getattr(args, "sig", None)
    aead = getattr(args, "aead", None)

    if suite_arg and any(v is not None for v in (kem, sig, aead)):
        print("Error: --suite cannot be combined with --kem/--sig/--aead")
        sys.exit(1)

    try:
        if suite_arg:
            suite = get_suite(suite_arg)
        elif any(v is not None for v in (kem, sig, aead)):
            if not all(v is not None for v in (kem, sig, aead)):
                print("Error: --kem, --sig, and --aead must be provided together")
                sys.exit(1)
            suite_id = build_suite_id(kem, aead, sig)
            suite = get_suite(suite_id)
        else:
            print(f"Error: {role_label} requires --suite or --kem/--sig/--aead")
            sys.exit(1)
    except NotImplementedError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    # Normalize suite argument for downstream logging
    setattr(args, "suite", suite.get("suite_id", getattr(args, "suite", None)))
    return suite


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
    
    Signature = _require_signature_class()

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
    suite = _resolve_suite(args, "GCS proxy")
    suite_id = suite["suite_id"]
    
    Signature = _require_signature_class()
    proxy_runner = _require_run_proxy()

    gcs_sig_secret = None
    gcs_sig_public = None
    json_out_path = getattr(args, "json_out", None)
    quiet = getattr(args, "quiet", False)
    status_file = getattr(args, "status_file", None)
    primary_secret_path: Optional[Path] = None

    def info(msg: str) -> None:
        if not quiet:
            print(msg)
    
    if args.ephemeral:
        info("⚠️  WARNING: Using EPHEMERAL keys - not suitable for production!")
        info("⚠️  Key will be lost when process exits.")
        if not quiet:
            print()
        
        # Generate ephemeral keypair
        sig = Signature(suite["sig_name"])
        gcs_sig_public = sig.generate_keypair()
        gcs_sig_secret = sig
        info("Generated ephemeral GCS signing keypair:")
        if not quiet:
            print(f"Public key (hex): {gcs_sig_public.hex()}")
            print("Provide this to the drone via --gcs-pub-hex or --peer-pubkey-file")
            print()
        primary_secret_path = None
        
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

        info("Loaded GCS signing key from file.")
        if load_method == "ctor_secret_key":
            info("Using constructor-based fallback because import/export APIs are unavailable.")

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
                        info(f"Loaded public key from {candidate}.")
                    except Exception as exc:
                        load_errors.append(f"public key read failed ({candidate}): {exc}")
                    break

        if gcs_sig_public is not None and not quiet:
            print(f"Public key (hex): {gcs_sig_public.hex()}")
        elif gcs_sig_public is None and not quiet:
            print("Warning: Could not locate public key file for display. Ensure the drone has the matching public key.")
        if not quiet:
            print()
        primary_secret_path = secret_path
    load_secret_for_suite = _build_matrix_secret_loader(
        suite_id=suite_id,
        default_secret_path=primary_secret_path,
        initial_secret=gcs_sig_secret,
        signature_cls=Signature,
    )

    try:
        log_path = configure_file_logger("gcs", logger)
        if not quiet:
            print(f"Log file: {log_path}")

        info(f"Starting GCS proxy with suite {suite_id}")
        if args.stop_seconds:
            info(f"Will auto-stop after {args.stop_seconds} seconds")
        if not quiet:
            print()
        
        counters = proxy_runner(
            role="gcs",
            suite=suite,
            cfg=CONFIG,
            gcs_sig_secret=gcs_sig_secret,
            gcs_sig_public=None,
            stop_after_seconds=args.stop_seconds,
            manual_control=getattr(args, "control_manual", False),
            quiet=quiet,
            status_file=status_file,
            load_gcs_secret=load_secret_for_suite,
        )

        _augment_part_b_metrics(counters)

        # Log final counters as JSON
        logger.info("GCS proxy shutdown", extra={"counters": counters})
        
        if not quiet:
            print("GCS proxy stopped. Final counters:")
            _pretty_print_counters(counters)

        payload = {
            "role": "gcs",
            "suite": suite_id,
            "counters": counters,
            "ts_stop_ns": time.time_ns(),
        }
        write_json_report(json_out_path, payload, quiet=quiet)
            
    except KeyboardInterrupt:
        if not quiet:
            print("\nGCS proxy stopped by user.")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def drone_command(args):
    """Start drone proxy."""
    suite = _resolve_suite(args, "Drone proxy")
    suite_id = suite["suite_id"]
    
    proxy_runner = _require_run_proxy()

    # Get GCS public key
    gcs_sig_public = None
    json_out_path = getattr(args, "json_out", None)
    quiet = getattr(args, "quiet", False)
    status_file = getattr(args, "status_file", None)
    primary_public_path: Optional[Path] = None

    def info(msg: str) -> None:
        if not quiet:
            print(msg)
    
    try:
        if args.peer_pubkey_file:
            pub_path = Path(args.peer_pubkey_file)
            if not pub_path.exists():
                raise FileNotFoundError(f"Public key file not found: {pub_path}")
            gcs_sig_public = pub_path.read_bytes()
            primary_public_path = pub_path
        elif args.gcs_pub_hex:
            gcs_sig_public = bytes.fromhex(args.gcs_pub_hex)
        else:
            # Try default location
            default_pub = Path("secrets/gcs_signing.pub")
            if default_pub.exists():
                gcs_sig_public = default_pub.read_bytes()
                info(f"Using GCS public key from: {default_pub}")
                primary_public_path = default_pub
            else:
                raise ValueError("No GCS public key provided. Use --peer-pubkey-file, --gcs-pub-hex, or ensure secrets/gcs_signing.pub exists.")
                
    except Exception as e:
        print(f"Error loading GCS public key: {e}")
        sys.exit(1)
    
    try:
        log_path = configure_file_logger("drone", logger)
        if not quiet:
            print(f"Log file: {log_path}")

        info(f"Starting drone proxy with suite {suite_id}")
        if args.stop_seconds:
            info(f"Will auto-stop after {args.stop_seconds} seconds")
        if not quiet:
            print()

        load_public_for_suite = _build_matrix_public_loader(
            suite_id=suite_id,
            default_public_path=primary_public_path,
            initial_public=gcs_sig_public,
        )
        
        counters = proxy_runner(
            role="drone",
            suite=suite,
            cfg=CONFIG,
            gcs_sig_secret=None,
            gcs_sig_public=gcs_sig_public,
            stop_after_seconds=args.stop_seconds,
            manual_control=False,
            quiet=quiet,
            status_file=status_file,
            load_gcs_public=load_public_for_suite,
        )
        
        _augment_part_b_metrics(counters)

        # Log final counters as JSON
        logger.info("Drone proxy shutdown", extra={"counters": counters})
        
        if not quiet:
            print("Drone proxy stopped. Final counters:")
            _pretty_print_counters(counters)

        payload = {
            "role": "drone",
            "suite": suite_id,
            "counters": counters,
            "ts_stop_ns": time.time_ns(),
        }
        write_json_report(json_out_path, payload, quiet=quiet)
            
    except KeyboardInterrupt:
        if not quiet:
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
    gcs_parser.add_argument("--suite",
                           help="Cryptographic suite ID (e.g., cs-kyber768-aesgcm-dilithium3)")
    gcs_parser.add_argument("--kem",
                           help="KEM alias (e.g., ML-KEM-768, kyber768)")
    gcs_parser.add_argument("--aead",
                           help="AEAD alias (e.g., AES-GCM)")
    gcs_parser.add_argument("--sig",
                           help="Signature alias (e.g., ML-DSA-65, dilithium3)")
    gcs_parser.add_argument("--gcs-secret-file",
                           help="Path to GCS secret key file (default: secrets/gcs_signing.key)")
    gcs_parser.add_argument("--ephemeral", action='store_true',
                           help="Use ephemeral keys (development only - prints warning)")
    gcs_parser.add_argument("--stop-seconds", type=float,
                           help="Auto-stop after N seconds (for testing)")
    gcs_parser.add_argument("--quiet", action="store_true",
                           help="Suppress informational prints (warnings/errors still shown)")
    gcs_parser.add_argument("--json-out",
                           help="Optional path to write counters JSON on shutdown")
    gcs_parser.add_argument("--control-manual", action="store_true",
                           help="Enable interactive manual in-band rekey control thread")
    gcs_parser.add_argument("--status-file",
                           help="Path to write proxy status JSON updates (handshake/rekey)")
    
    # drone subcommand
    drone_parser = subparsers.add_parser('drone', help='Start drone proxy')
    drone_parser.add_argument("--suite",
                             help="Cryptographic suite ID (e.g., cs-kyber768-aesgcm-dilithium3)")
    drone_parser.add_argument("--kem",
                             help="KEM alias (e.g., ML-KEM-768, kyber768)")
    drone_parser.add_argument("--aead",
                             help="AEAD alias (e.g., AES-GCM)")
    drone_parser.add_argument("--sig",
                             help="Signature alias (e.g., ML-DSA-65, dilithium3)")
    drone_parser.add_argument("--peer-pubkey-file",
                             help="Path to GCS public key file (default: secrets/gcs_signing.pub)")
    drone_parser.add_argument("--gcs-pub-hex",
                             help="GCS public key as hex string")
    drone_parser.add_argument("--stop-seconds", type=float,
                             help="Auto-stop after N seconds (for testing)")
    drone_parser.add_argument("--quiet", action="store_true",
                              help="Suppress informational prints (warnings/errors still shown)")
    drone_parser.add_argument("--json-out",
                              help="Optional path to write counters JSON on shutdown")
    drone_parser.add_argument("--status-file",
                              help="Path to write proxy status JSON updates (handshake/rekey)")
    
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
        if getattr(args, "quiet", False):
            logger.setLevel(logging.WARNING)
        gcs_command(args)
    elif args.command == 'drone':
        if getattr(args, "quiet", False):
            logger.setLevel(logging.WARNING)
        drone_command(args)


if __name__ == "__main__":
    main()