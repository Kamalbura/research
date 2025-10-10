#!/usr/bin/env python3
"""
Verification script to check if all PQC and AEAD algorithms defined in
core/suites.py are available in the current Python environment.

Run this on both the GCS and the Drone to ensure consistent cryptographic support.
"""

import sys
from pathlib import Path

def _ensure_core_importable() -> Path:
    """Guarantee the repository root is on sys.path before importing core."""
    tools_dir = Path(__file__).resolve().parent
    repo_root = tools_dir.parent
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    try:
        __import__("core")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"Unable to import 'core'. Make sure this script is in your project root."
        ) from exc
    return repo_root

# --- Main Verification Logic ---
def run_checks() -> bool:
    """Checks all KEMs, Signatures, and AEADs against the installed libraries."""
    all_ok = True
    print("--- PQC UAV Crypto Verification ---")

    # --- 1. Check KEMs and Signatures (from liboqs) ---
    try:
        from oqs import get_enabled_kem_mechanisms, get_enabled_sig_mechanisms
        from core.suites import _KEM_REGISTRY, _SIG_REGISTRY

        enabled_kems = {name.lower() for name in get_enabled_kem_mechanisms()}
        enabled_sigs = {name.lower() for name in get_enabled_sig_mechanisms()}

        print("\n## Verifying Key Encapsulation Mechanisms (KEMs)...")
        for key, params in _KEM_REGISTRY.items():
            oqs_name = params["oqs_name"].lower()
            if oqs_name in enabled_kems:
                print(f"  [ OK ] {params['oqs_name']}")
            else:
                print(f"  [ MISSING ] {params['oqs_name']} (token: {key})")
                all_ok = False

        print("\n## Verifying Digital Signature Algorithms...")
        for key, params in _SIG_REGISTRY.items():
            oqs_name = params["oqs_name"].lower()
            if oqs_name in enabled_sigs:
                print(f"  [ OK ] {params['oqs_name']}")
            else:
                print(f"  [ MISSING ] {params['oqs_name']} (token: {key})")
                all_ok = False

    except ImportError:
        print("\n[ ERROR ] oqs-python library not found. Cannot verify KEMs or Signatures.")
        print("          Please install it with 'pip install oqs'.")
        return False
    except Exception as e:
        print(f"\n[ ERROR ] An unexpected error occurred while checking OQS: {e}")
        return False

    # --- 2. Check AEAD Ciphers (from cryptography) ---
    print("\n## Verifying AEAD Ciphers...")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
        from core.suites import _AEAD_REGISTRY

        try:
            import ascon  # type: ignore
        except ImportError:  # pragma: no cover - optional dependency
            ascon = None  # type: ignore

        aead_checks = {
            "aesgcm": (
                AESGCM,
                lambda cls: cls(b"\0" * 32),
            ),
            "chacha20poly1305": (
                ChaCha20Poly1305,
                lambda cls: cls(b"\0" * 32),
            ),
            "ascon128": (
                ascon,
                lambda module: module.encrypt(b"\0" * 16, b"\0" * 16, b"", b""),  # type: ignore[attr-defined]
            ),
        }

        for key, params in _AEAD_REGISTRY.items():
            checker = aead_checks.get(key)
            if not checker:
                print(f"  [ MISSING ] {params['display_name']} (token: {key}) - Unknown AEAD token.")
                all_ok = False
                continue

            impl, probe = checker
            if impl is None:
                print(f"  [ MISSING ] {params['display_name']} (token: {key}) - Dependency not installed.")
                all_ok = False
                continue

            try:
                probe(impl)
                print(f"  [ OK ] {params['display_name']}")
            except Exception as e:
                print(f"  [ ERROR ] {params['display_name']} - Probe failed: {e}")
                all_ok = False

    except ImportError:
        print("\n[ ERROR ] 'cryptography' library not found. Cannot verify AEADs.")
        print("          Please install it with 'pip install cryptography'.")
        return False

    # --- Final Summary ---
    print("\n--- Summary ---")
    if all_ok:
        print("✅ All required cryptographic primitives are available in this environment.")
    else:
        print("❌ One or more required primitives are MISSING. See details above.")

    return all_ok

if __name__ == "__main__":
    _ensure_core_importable()
    if not run_checks():
        sys.exit(1)