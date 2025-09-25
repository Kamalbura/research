"""Manual script: Launch Drone proxy for manual verification.
Run in terminal 2 with GCS public key from terminal 1.

Usage:
  python manual/run_drone_proxy.py <GCS_PUBLIC_KEY_HEX>
  or set GCS_PUBLIC_KEY environment variable
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from core.suites import get_suite
from core.async_proxy import run_proxy
from core.config import CONFIG

def get_gcs_public_key():
    """Get GCS public key from command line or environment.

    Exits if not provided to avoid insecure random fallback.
    """
    if len(sys.argv) > 1:
        try:
            return bytes.fromhex(sys.argv[1])
        except ValueError:
            print("❌ Provided public key hex is invalid.")
            sys.exit(1)
    env_key = os.environ.get('GCS_PUBLIC_KEY')
    if env_key:
        try:
            return bytes.fromhex(env_key)
        except ValueError:
            print("❌ GCS_PUBLIC_KEY environment variable is not valid hex.")
            sys.exit(1)
    print("❌ ERROR: GCS public key required!")
    print("")
    print("Usage:")
    print(f"  python {sys.argv[0]} <GCS_PUBLIC_KEY_HEX>")
    print("  or set GCS_PUBLIC_KEY environment variable")
    print("")
    print("Get the key from the GCS proxy output (terminal 1)")
    sys.exit(1)

if __name__ == "__main__":
    gcs_sig_public = get_gcs_public_key()
    suite = get_suite("cs-kyber768-aesgcm-dilithium3")
    print(f"Using GCS public key (first 32 hex chars): {gcs_sig_public.hex()[:32]}...")
    print("Starting Drone proxy...")
    run_proxy(
        role="drone",
        suite=suite,
        cfg=CONFIG,
        gcs_sig_secret=None,
        gcs_sig_public=gcs_sig_public,
        stop_after_seconds=None,
    )
