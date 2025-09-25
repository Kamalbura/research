"""
No-arg runner entry point for wrappers.

Provides a thin interface that loads configuration and suite definitions
but deliberately requires key material injection from external sources.
"""

from core.config import CONFIG
from core.suites import get_suite
from core.async_proxy import run_proxy


def start(role: str, suite_id: str):
    """Launch the proxy for simple wrapper scripts.

    Security rules:
    - For GCS role: generate a fresh signature keypair per process (ephemeral) and print the public key to stdout
      so the operator can copy it to the drone side.
    - For Drone role: require the environment variable GCS_SIG_PUBLIC_HEX (or GCS_PUBLIC_KEY) to be set.
    - Never silently fall back to random bytes if key material is missing or malformed.
    """
    if role not in {"gcs", "drone"}:
        raise ValueError("role must be 'gcs' or 'drone'")

    suite = get_suite(suite_id)

    from oqs.oqs import Signature  # Local import to avoid dependency during pure metadata ops

    if role == "gcs":
        sig = Signature(suite["sig_name"])  # Generate fresh ephemeral signing key
        gcs_sig_public = sig.generate_keypair()
        print("=" * 60)
        print("GCS public signature key (hex) - provide to drone wrapper via env GCS_SIG_PUBLIC_HEX:")
        print(gcs_sig_public.hex())
        print("=" * 60)
        run_proxy(
            role="gcs",
            suite=suite,
            cfg=CONFIG,
            gcs_sig_secret=sig,  # pass signature object directly
            gcs_sig_public=None,
            stop_after_seconds=None,
        )
    else:  # drone
        import os, sys
        pub_hex = os.environ.get("GCS_SIG_PUBLIC_HEX") or os.environ.get("GCS_PUBLIC_KEY")
        if not pub_hex:
            print("❌ Missing GCS_SIG_PUBLIC_HEX (or GCS_PUBLIC_KEY) environment variable for drone role.")
            print("   Obtain the hex string printed by the GCS wrapper and export it, e.g.:")
            print("   PowerShell:  $env:GCS_SIG_PUBLIC_HEX='<hex>' ; python drone/wrappers/... .py")
            print("   Bash:        export GCS_SIG_PUBLIC_HEX='<hex>'; python drone/wrappers/... .py")
            sys.exit(2)
        try:
            gcs_sig_public = bytes.fromhex(pub_hex.strip())
        except ValueError:
            print("❌ Provided GCS_SIG_PUBLIC_HEX is not valid hex.")
            sys.exit(2)
        run_proxy(
            role="drone",
            suite=suite,
            cfg=CONFIG,
            gcs_sig_secret=None,
            gcs_sig_public=gcs_sig_public,
            stop_after_seconds=None,
        )