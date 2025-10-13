#!/usr/bin/env python3
"""Backfill handshake_*_mJ fields in logs/auto/gcs/summary.csv.

This script copies values from kem_keygen_mJ/kem_encaps_mJ/kem_decap_mJ/sig_sign_mJ/sig_verify_mJ
into handshake_kem_keygen_mJ/handshake_kem_encap_mJ/handshake_kem_decap_mJ/handshake_sig_sign_mJ/handshake_sig_verify_mJ
when the handshake-prefixed fields are missing or empty.
"""
from __future__ import annotations

import csv
from pathlib import Path


def backfill(csv_path: Path) -> int:
    rows = []
    changed = 0
    with csv_path.open('r', encoding='utf-8', newline='') as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        for r in reader:
            # do backfill
            for src, dst in [
                ('kem_keygen_mJ', 'handshake_kem_keygen_mJ'),
                ('kem_encaps_mJ', 'handshake_kem_encap_mJ'),
                ('kem_decap_mJ', 'handshake_kem_decap_mJ'),
                ('sig_sign_mJ', 'handshake_sig_sign_mJ'),
                ('sig_verify_mJ', 'handshake_sig_verify_mJ'),
            ]:
                srcval = (r.get(src) or '').strip()
                dstval = (r.get(dst) or '').strip()
                if not dstval and srcval:
                    r[dst] = srcval
                    changed += 1
            rows.append(r)

    if changed:
        # ensure fieldnames include dst fields
        for extra in ['handshake_kem_keygen_mJ','handshake_kem_encap_mJ','handshake_kem_decap_mJ','handshake_sig_sign_mJ','handshake_sig_verify_mJ']:
            if extra not in fieldnames:
                fieldnames.append(extra)
        with csv_path.open('w', encoding='utf-8', newline='') as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)

    return changed


def main() -> None:
    p = Path('logs/auto/gcs/summary.csv')
    if not p.exists():
        print('summary.csv not found:', p)
        return
    c = backfill(p)
    print('backfilled handshake_*_mJ fields (cells written):', c)


if __name__ == '__main__':
    main()
