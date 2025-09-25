#!/usr/bin/env python3
"""Generate and persist a post-quantum GCS identity (signature keypair).

Usage:
  python tools/generate_identity.py --suite cs-kyber768-aesgcm-dilithium3 --out-dir keys

Outputs:
  <out-dir>/gcs_sig_public.bin
  <out-dir>/gcs_sig_secret.bin

Security:
  - Secret key file is written with 0o600 permissions where supported.
  - Fails fast on any error; never substitutes random bytes.
"""
import argparse, os, sys, stat
from pathlib import Path
from oqs.oqs import Signature
from core.suites import get_suite


def write_file(path: Path, data: bytes, secret: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if secret:
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass  # best effort on non-POSIX


def main():
    ap = argparse.ArgumentParser(description="Generate PQC signature identity keypair")
    ap.add_argument("--suite", required=True, help="Suite ID (must correspond to desired signature algorithm)")
    ap.add_argument("--out-dir", default="identity", help="Output directory for key files")
    args = ap.parse_args()

    try:
        suite = get_suite(args.suite)
    except Exception as e:
        print(f"Error: unknown suite '{args.suite}': {e}")
        sys.exit(2)

    sig_alg = suite["sig_name"]
    try:
        sig = Signature(sig_alg)
        pub = sig.generate_keypair()
        secret = sig.export_secret_key()
    except Exception as e:
        print(f"Failed to generate signature keypair for {sig_alg}: {e}")
        sys.exit(1)

    out_dir = Path(args.out_dir).resolve()
    write_file(out_dir / "gcs_sig_public.bin", pub, secret=False)
    write_file(out_dir / "gcs_sig_secret.bin", secret, secret=True)

    print("Generated PQC signature identity:")
    print(f"  Signature algorithm : {sig_alg}")
    print(f"  Public key (hex)    : {pub.hex()}")
    print(f"  Public key file     : {out_dir / 'gcs_sig_public.bin'}")
    print(f"  Secret key file     : {out_dir / 'gcs_sig_secret.bin'} (mode 600 if supported)")
    print("\nDistribute the public key to drone nodes; keep the secret key private.")

if __name__ == "__main__":
    main()
