"""Manual script: Launch GCS proxy for manual verification.
Run in terminal 1.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from core.suites import get_suite
from core.async_proxy import run_proxy
from core.config import CONFIG
from oqs.oqs import Signature

if __name__ == "__main__":
    suite = get_suite("cs-kyber768-aesgcm-dilithium3")
    print("Generating GCS signature keypair...")
    sig = Signature(suite["sig_name"])
    gcs_sig_public = sig.generate_keypair()
    print("="*60)
    print("🔐 GCS SIGNATURE PUBLIC KEY (use in drone script):")
    print(gcs_sig_public.hex())
    print("="*60)
    print("Starting GCS proxy...")
    run_proxy(
        role="gcs",
        suite=suite,
        cfg=CONFIG,
        gcs_sig_secret=sig,
        gcs_sig_public=None,
        stop_after_seconds=None,
    )
