#!/usr/bin/env python3
"""Generate per-suite signing identities for matrix tests.

Creates `gcs_signing.key`/`gcs_signing.pub` pairs under
`secrets/matrix/<safe_suite>/` so both the GCS and drone proxies can
reuse deterministic file locations during automated matrix runs.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from core.suites import list_suites

REPO_ROOT = Path(__file__).resolve().parents[1]


def safe_suite_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)


def ensure_identity(suite: str, out_root: Path, *, force: bool = False) -> None:
    safe = safe_suite_name(suite)
    suite_dir = out_root / safe
    secret_path = suite_dir / "gcs_signing.key"
    public_path = suite_dir / "gcs_signing.pub"

    if not force and secret_path.exists() and public_path.exists():
        print(f"[keys] Reusing existing signing identity for {suite} ({suite_dir})")
        return

    print(f"[keys] Generating signing identity for {suite} -> {suite_dir}")
    suite_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "core.run_proxy",
        "init-identity",
        "--suite",
        suite,
        "--output-dir",
        str(suite_dir),
    ]
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    if result.returncode != 0:
        raise SystemExit(f"Failed to generate signing identity for {suite}")

    if not secret_path.exists() or not public_path.exists():
        raise SystemExit(f"Generated signing identity for {suite} is missing files in {suite_dir}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare signing identities for matrix tests")
    parser.add_argument(
        "--suite",
        action="append",
        help="Suite ID to generate (may be provided multiple times). Defaults to all registered suites.",
    )
    parser.add_argument(
        "--out-root",
        default=str(REPO_ROOT / "secrets" / "matrix"),
        help="Output directory for matrix key material (default: secrets/matrix)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate identities even if files already exist",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.suite:
        suites = list(dict.fromkeys(args.suite))
    else:
        suites = list(list_suites().keys())

    out_root = Path(args.out_root).expanduser()
    if not out_root.is_absolute():
        out_root = (REPO_ROOT / out_root).resolve()
    else:
        out_root.mkdir(parents=True, exist_ok=True)

    out_root.mkdir(parents=True, exist_ok=True)

    for suite in suites:
        ensure_identity(suite, out_root, force=args.force)

    print(f"[keys] Complete. Generated {len(suites)} suites in {out_root}")


if __name__ == "__main__":
    main()
