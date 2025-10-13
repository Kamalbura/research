#!/usr/bin/env python3
"""Check presence of rekey and handshake energy metrics in the scheduler summary CSV.

Usage: python -m tools.check_energy_summary --summary-csv logs/auto/gcs/summary.csv [--run-id run_123]
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import List, Optional


def parse_args():
    p = argparse.ArgumentParser(description="Check rekey and handshake energy presence in summary CSV")
    p.add_argument("--summary-csv", type=Path, default=Path("logs/auto/gcs/summary.csv"))
    p.add_argument("--run-id", type=str, default=None)
    return p.parse_args()


def read_rows(path: Path) -> List[dict]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader)


def matches_run(row: dict, run_id: str) -> bool:
    if not run_id:
        return True
    for v in row.values():
        try:
            if isinstance(v, str) and run_id in v:
                return True
        except Exception:
            continue
    # last resort: stringify whole row
    return run_id in str(row)


def summarize(rows: List[dict], run_id: Optional[str] = None) -> None:
    if run_id:
        sel = [r for r in rows if matches_run(r, run_id)]
        print(f"Rows matching run '{run_id}': {len(sel)}")
        if not sel:
            print("No CSV rows matched the requested run id. (This explains missing metrics in reports relying on summary.csv)")
            return
    else:
        sel = rows

    total = len(sel)
    rekey_present = 0
    handshake_present = 0
    rekey_values = []
    handshake_values = []
    for r in sel:
        re = (r.get("rekey_energy_mJ") or "").strip()
        he = (r.get("handshake_energy_mJ") or "").strip()
        if re:
            try:
                float(re)
                rekey_present += 1
                rekey_values.append(re)
            except ValueError:
                # could be '0.0' or 'ERR' etc. Count non-empty as present
                rekey_present += 1
                rekey_values.append(re)
        if he:
            try:
                float(he)
                handshake_present += 1
                handshake_values.append(he)
            except ValueError:
                handshake_present += 1
                handshake_values.append(he)

    print(f"Total rows considered: {total}")
    print(f"Rows with rekey_energy_mJ present: {rekey_present} ({(rekey_present/total*100) if total else 0:.1f}%)")
    if rekey_values:
        sample = rekey_values[:5]
        print("Sample rekey values:", ", ".join(sample))
    print(f"Rows with handshake_energy_mJ present: {handshake_present} ({(handshake_present/total*100) if total else 0:.1f}%)")
    if handshake_values:
        sample = handshake_values[:5]
        print("Sample handshake values:", ", ".join(sample))


def main():
    args = parse_args()
    rows = read_rows(args.summary_csv)
    if not rows:
        print(f"No rows found in {args.summary_csv}")
        return
    summarize(rows, args.run_id)


if __name__ == '__main__':
    main()
