#!/usr/bin/env python3
"""Quick matrix-key sanity checker.

Scans `secrets/` (or a supplied path) to ensure each suite directory
contains both `gcs_signing.key` and `gcs_signing.pub`, and that every
suite name maps to a registered entry in `core.suites`.

Exit status is 0 when all checks pass, otherwise 1.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.suites import get_suite

MISSING_KEY = "gcs_signing.key"
MISSING_PUB = "gcs_signing.pub"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify GCS key matrix layout")
    parser.add_argument(
        "--secrets-dir",
        type=Path,
        default=Path("secrets"),
        help="Root directory containing gcs_signing.* and matrix/ (default: secrets)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only emit failures; stay silent on success.",
    )
    return parser.parse_args()


def _check_suite_dir(path: Path) -> list[str]:
    failures: list[str] = []
    key_path = path / MISSING_KEY
    pub_path = path / MISSING_PUB
    if not key_path.exists():
        failures.append(f"missing {key_path}")
    if not pub_path.exists():
        failures.append(f"missing {pub_path}")
    return failures


def _validate_suite_name(path: Path) -> str | None:
    try:
        get_suite(path.name)
    except NotImplementedError:
        return f"directory {path.name} is not a registered suite"
    return None


def main() -> int:
    args = _parse_args()
    secrets_dir: Path = args.secrets_dir

    if not secrets_dir.exists():
        print(f"[ERROR] secrets directory not found: {secrets_dir}", file=sys.stderr)
        return 1

    matrix_dir = secrets_dir / "matrix"
    if not matrix_dir.exists():
        print(f"[ERROR] matrix directory not found: {matrix_dir}", file=sys.stderr)
        return 1

    failures: list[str] = []

    for suite_dir in sorted(p for p in matrix_dir.iterdir() if p.is_dir()):
        maybe_err = _validate_suite_name(suite_dir)
        if maybe_err:
            failures.append(maybe_err)
        failures.extend(_check_suite_dir(suite_dir))

    if failures:
        print("[FAIL] matrix verification found issues:")
        for item in failures:
            print(f"  - {item}")
        return 1

    if not args.quiet:
        print("[OK] all suite directories have gcs_signing.key and gcs_signing.pub")
    return 0


if __name__ == "__main__":
    sys.exit(main())
