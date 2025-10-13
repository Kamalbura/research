#!/usr/bin/env python3
"""Clean exporter (alternate) to enrich canonical CSV rows with power data
and estimate handshake/rekey energy when necessary. Writes outputs to
output/gcs/final_records.* so we can verify specific runs even while the
original module is being repaired.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Dict, List


CANONICAL_CSV = Path("logs/auto/gcs/summary.csv")
OUT_DIR = Path("output/gcs")


def load_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _safe_load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _fill_from_power(row: Dict[str, str]) -> None:
    paths = []
    for k in ("power_summary_path", "power_csv_path"):
        v = row.get(k)
        if v:
            paths.append(Path(v))
    s = row.get('monitor_artifact_paths') or ''
    for tok in s.replace("\\'", "'").replace(',', ' ').split():
        tok = tok.strip('"').strip("'")
        if tok.endswith('.json') or tok.endswith('.csv'):
            paths.append(Path(tok))

    for p in paths:
        if not p.exists():
            alt = Path('logs/auto/gcs') / p.name
            if alt.exists():
                p = alt
            else:
                continue
        if p.suffix.lower() == '.json':
            d = _safe_load_json(p)
            if isinstance(d, dict):
                for k in ('power_avg_w', 'power_energy_j', 'power_duration_s'):
                    if d.get(k) is not None and not row.get(k):
                        row[k] = str(d.get(k))
                return
        if p.suffix.lower() == '.csv':
            try:
                with p.open('r', encoding='utf-8', newline='') as fh:
                    r = csv.DictReader(fh)
                    first = next(r, None)
                    if first:
                        for k in ('power_avg_w', 'power_energy_j'):
                            if first.get(k) and not row.get(k):
                                row[k] = first.get(k)
                        return
            except Exception:
                continue


def _estimate_energy(row: Dict[str, str]) -> None:
    def sf(k: str):
        v = row.get(k)
        if v in (None, ''):
            return None
        try:
            return float(v)
        except Exception:
            return None

    avg = sf('power_avg_w')
    if avg is None:
        total_j = sf('power_energy_j')
        dur_s = sf('power_duration_s')
        if total_j is not None and dur_s and dur_s > 0:
            avg = total_j / dur_s
    if avg is None:
        return

    def set_est(prefix: str, start_key: str, end_key: str):
        try:
            s0 = int(row.get(start_key) or 0)
            s1 = int(row.get(end_key) or 0)
        except Exception:
            return
        if s0 and s1 and s1 > s0:
            existing = sf(f"{prefix}_energy_mJ")
            if not existing or existing <= 0:
                dur = (s1 - s0) / 1e9
                ej = avg * dur
                row[f"{prefix}_energy_mJ"] = f"{(ej*1000):.3f}"
                row[f"{prefix}_energy_error"] = 'estimated_from_power'

    set_est('handshake', 'handshake_wall_start_ns', 'handshake_wall_end_ns')
    set_est('rekey', 'rekey_mark_ns', 'rekey_ok_ns')


def write_outputs(rows: List[Dict[str, str]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = OUT_DIR / 'final_records.csv'
    out_json = OUT_DIR / 'final_records.json'
    fields = set()
    for r in rows:
        fields.update(r.keys())
    fields = sorted(fields)
    with out_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(fields)
        for r in rows:
            w.writerow([r.get(f, '') for f in fields])
    out_json.write_text(json.dumps(rows, indent=2), encoding='utf-8')


def main() -> None:
    if not CANONICAL_CSV.exists():
        print('Canonical CSV not found:', CANONICAL_CSV)
        return
    rows = load_rows(CANONICAL_CSV)
    for row in rows:
        _fill_from_power(row)
        _estimate_energy(row)
    write_outputs(rows)
    print('Wrote outputs to', OUT_DIR)


if __name__ == '__main__':
    main()
