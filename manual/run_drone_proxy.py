""""Manual script: Launch Drone proxy for manual verification.
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
    """Get GCS public key from command line or environment."""
    if len(sys.argv) > 1:
        return bytes.fromhex(sys.argv[1])
    
    env_key = os.environ.get('GCS_PUBLIC_KEY')
    if env_key:
        return bytes.fromhex(env_key)
    
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
    
    print(f"Using GCS public key: {gcs_sig_public.hex()[:32]}...")
    print("Starting Drone proxy...")
    
    run_proxy(
        role="drone",
        suite=suite,
        cfg=CONFIG,
        gcs_sig_secret=None,
        gcs_sig_public=gcs_sig_public,  # Real public key, not random bytes!
        stop_after_seconds=None,
    )Launch Drone proxy for manual verification.
Run in terminal 2.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from core.suites import get_suite
from core.async_proxy import run_proxy
from core.config import CONFIG

if __name__ == "__main__":
    suite = get_suite("cs-kyber768-aesgcm-dilithium3")
    run_proxy(
        role="drone",
        suite=suite,
        cfg=CONFIG,
        gcs_sig_secret=None,
        gcs_sig_public=os.urandom(64),
        stop_after_seconds=None,
    )
