#!/usr/bin/env python3
"""Fixed clean exporter (safe). See tools/export_summary_fields.py which is
currently corrupted; this file is a deterministic replacement used for
verification and later promotion.
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


def try_fill_from_power(row: Dict[str, str]) -> None:
    s = row.get('monitor_artifact_paths') or ''
    candidates = []
    for k in ('power_summary_path', 'power_csv_path'):
        v = row.get(k)
        if v:
            candidates.append(Path(v))
    for tok in s.replace("\\'", "'").replace(',', ' ').split():
        tok = tok.strip('"').strip("'")
        if tok.endswith('.json') or tok.endswith('.csv'):
            candidates.append(Path(tok))

    for p in candidates:
        if not p.exists():
            alt = Path('logs/auto/gcs') / p.name
            if alt.exists():
                p = alt
            else:
                continue
        if p.suffix.lower() == '.json':
            try:
                d = json.loads(p.read_text(encoding='utf-8'))
            except Exception:
                continue
            for k in ('power_avg_w', 'power_energy_j', 'power_duration_s'):
                if d.get(k) is not None and not row.get(k):
                    row[k] = str(d.get(k))
            return
        if p.suffix.lower() == '.csv':
            try:
                with p.open('r', encoding='utf-8', newline='') as fh:
                    rdr = csv.DictReader(fh)
                    first = next(rdr, None)
                    if first:
                        for k in ('power_avg_w', 'power_energy_j'):
                            if first.get(k) and not row.get(k):
                                row[k] = first.get(k)
                        return
            except Exception:
                continue


def _find_power_paths(row: Dict[str, str]) -> List[Path]:
    """Return list of candidate power CSV/JSON paths (existing) for this row."""
    s = row.get('monitor_artifact_paths') or ''
    candidates: List[Path] = []
    for k in ('power_summary_path', 'power_csv_path'):
        v = row.get(k)
        if v:
            candidates.append(Path(v))
    for tok in s.replace("\\'", "'").replace(',', ' ').split():
        tok = tok.strip('"').strip("'")
        if tok.endswith('.json') or tok.endswith('.csv'):
            candidates.append(Path(tok))

    existing: List[Path] = []
    for p in candidates:
        if p.exists():
            existing.append(p)
            continue
        alt = Path('logs/auto/gcs') / p.name
        if alt.exists():
            existing.append(alt)
    return existing


def _integrate_energy_csv(csv_path: Path, start_ns: int, end_ns: int) -> float:
    """Integrate power_w from csv_path between [start_ns, end_ns). Returns energy in joules."""
    if not csv_path.exists():
        return 0.0
    total_j = 0.0
    try:
        with csv_path.open('r', encoding='utf-8', newline='') as fh:
            rdr = csv.DictReader(fh)
            prev_ts = None
            prev_p = None
            for row in rdr:
                try:
                    ts = int(row.get('timestamp_ns') or 0)
                    p = float(row.get('power_w') or 0.0)
                except Exception:
                    continue
                # skip samples entirely before interval
                if prev_ts is None:
                    prev_ts = ts
                    prev_p = p
                    continue
                # trapezoid between prev and ts
                seg_start = max(prev_ts, start_ns)
                seg_end = min(ts, end_ns)
                if seg_end > seg_start:
                    dt_s = (seg_end - seg_start) / 1e9
                    # approximate by average power over segment
                    avg_p = (prev_p + p) / 2.0
                    total_j += avg_p * dt_s
                prev_ts = ts
                prev_p = p
                # early exit if we've passed end
                if ts >= end_ns:
                    break
    except Exception:
        return 0.0
    return total_j


def fill_measured_energy_from_power(row: Dict[str, str]) -> None:
    """Populate handshake/rekey energy fields only if measurable from power traces.

    This function will NOT estimate from averages; it only integrates power
    trace CSVs or proportionally uses summary JSON when the power trace covers
    the requested window.
    """
    def tof(k: str):
        v = row.get(k)
        if v in (None, ''):
            return 0
        try:
            return int(float(v))
        except Exception:
            return 0

    # determine handshake window (prefer explicit energy window fields)
    hs0 = tof('handshake_energy_start_ns') or tof('handshake_wall_start_ns') or 0
    hs1 = tof('handshake_energy_end_ns') or tof('handshake_wall_end_ns') or 0

    # determine rekey window
    rk0 = tof('rekey_energy_start_ns') or tof('rekey_mark_ns') or 0
    rk1 = tof('rekey_energy_end_ns') or tof('rekey_ok_ns') or 0

    if not any((hs0 and hs1 and hs1 > hs0, rk0 and rk1 and rk1 > rk0)):
        # nothing measurable
        return

    candidates = _find_power_paths(row)
    if not candidates:
        return

    # prefer csv paths over json for integration
    csv_paths = [p for p in candidates if p.suffix.lower() == '.csv']
    json_paths = [p for p in candidates if p.suffix.lower() == '.json']

    # helper to try compute energy for a window
    def compute_window_energy(start_ns: int, end_ns: int) -> float:
        # try CSV integration first
        for cp in csv_paths:
            ej = _integrate_energy_csv(cp, start_ns, end_ns)
            if ej and ej > 0.0:
                return ej
        # fallback to proportion of summary JSON energy_j if it fully covers the window
        for jp in json_paths:
            try:
                d = json.loads(jp.read_text(encoding='utf-8'))
            except Exception:
                continue
            pstart = int(float(d.get('start_ns') or 0))
            pend = int(float(d.get('end_ns') or 0))
            total_j = float(d.get('energy_j') or 0.0)
            if pstart and pend and pend > pstart and start_ns >= pstart and end_ns <= pend and total_j > 0:
                # proportionally allocate
                return total_j * ((end_ns - start_ns) / (pend - pstart))
        return 0.0

    if hs0 and hs1 and hs1 > hs0 and (not row.get('handshake_energy_mJ') or float(row.get('handshake_energy_mJ') or 0) <= 0):
        ej = compute_window_energy(hs0, hs1)
        if ej and ej > 0.0:
            row['handshake_energy_mJ'] = f"{(ej*1000):.3f}"
            row['handshake_energy_error'] = 'measured_from_power'

    if rk0 and rk1 and rk1 > rk0 and (not row.get('rekey_energy_mJ') or float(row.get('rekey_energy_mJ') or 0) <= 0):
        ej = compute_window_energy(rk0, rk1)
        if ej and ej > 0.0:
            row['rekey_energy_mJ'] = f"{(ej*1000):.3f}"
            row['rekey_energy_error'] = 'measured_from_power'


def estimate_energy_from_power(row: Dict[str, str]) -> None:
    def tof(k: str):
        v = row.get(k)
        if v in (None, ''):
            return None
        try:
            return float(v)
        except Exception:
            return None

    avg = tof('power_avg_w')
    if avg is None:
        total_j = tof('power_energy_j')
        dur = tof('power_duration_s')
        if total_j is not None and dur and dur > 0:
            avg = total_j / dur
    if avg is None:
        return

    try:
        hs0 = int(row.get('handshake_wall_start_ns') or 0)
        hs1 = int(row.get('handshake_wall_end_ns') or 0)
    except Exception:
        hs0 = hs1 = 0
    if hs0 and hs1 and hs1 > hs0 and (not row.get('handshake_energy_mJ') or float(row.get('handshake_energy_mJ') or 0) <= 0):
        dur_s = (hs1 - hs0) / 1e9
        ej = avg * dur_s
        row['handshake_energy_mJ'] = f"{(ej*1000):.3f}"
        row['handshake_energy_error'] = 'estimated_from_power'

    try:
        rk0 = int(row.get('rekey_mark_ns') or 0)
        rk1 = int(row.get('rekey_ok_ns') or 0)
    except Exception:
        rk0 = rk1 = 0
    if rk0 and rk1 and rk1 > rk0 and (not row.get('rekey_energy_mJ') or float(row.get('rekey_energy_mJ') or 0) <= 0):
        dur_s = (rk1 - rk0) / 1e9
        ej = avg * dur_s
        row['rekey_energy_mJ'] = f"{(ej*1000):.3f}"
        row['rekey_energy_error'] = 'estimated_from_power'


def write_outputs(rows: List[Dict[str, str]]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_csv = OUT_DIR / 'final_records.csv'
    out_json = OUT_DIR / 'final_records.json'
    fields = sorted({k for r in rows for k in r.keys()})
    with out_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(fields)
        for r in rows:
            w.writerow([r.get(f, '') for f in fields])
    out_json.write_text(json.dumps(rows, indent=2), encoding='utf-8')


def main() -> None:
    if not CANONICAL_CSV.exists():
        print('Canonical CSV missing:', CANONICAL_CSV)
        return
    rows = load_rows(CANONICAL_CSV)
    for row in rows:
        try_fill_from_power(row)
        # Only populate handshake/rekey from measured power traces. Do NOT estimate.
        fill_measured_energy_from_power(row)
    write_outputs(rows)
    print('Wrote outputs to', OUT_DIR)


if __name__ == '__main__':
    main()
