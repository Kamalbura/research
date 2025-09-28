#!/usr/bin/env python3
"""Check per-suite signing key/pub presence under secrets/matrix and print JSON.

Usage: python tools/check_matrix_keys.py
Outputs JSON to stdout: { suite: { has_key: bool, has_pub: bool, key_size: int|null, pub_size: int|null, pub_sha256: str|null } }
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys

from core.suites import list_suites


def sha256_hex(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    suites = list_suites()
    root = pathlib.Path('secrets') / 'matrix'
    out = {}
    for suite in suites.keys():
        d = root / suite
        key = d / 'gcs_signing.key'
        pub = d / 'gcs_signing.pub'
        rec = {
            'has_key': key.exists(),
            'has_pub': pub.exists(),
            'key_size': key.stat().st_size if key.exists() else None,
            'pub_size': pub.stat().st_size if pub.exists() else None,
            'pub_sha256': sha256_hex(pub) if pub.exists() else None,
        }
        out[suite] = rec

    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
