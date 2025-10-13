#!/usr/bin/env python3
"""Minimal canonical exporter (measured-only).

This is a compact, single-responsibility script to read the canonical
CSV at logs/auto/gcs/summary.csv, discover local power artifacts, and
populate handshake/rekey energy fields only when measurable from power
CSV traces (preferred) or from a summary JSON that fully covers the
requested window. It intentionally makes no estimations.
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


def find_power_candidates(row: Dict[str, str]) -> List[Path]:
    s = row.get("monitor_artifact_paths") or ""
    candidates: List[Path] = []
    for k in ("power_summary_path", "power_csv_path"):
        v = row.get(k)
        if v:
            candidates.append(Path(v))
    for tok in s.replace("\\'", "'").replace(",", " ").split():
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


def integrate_csv_energy(csv_path: Path, start_ns: int, end_ns: int) -> float:
    if not csv_path.exists():
        return 0.0
    total_j = 0.0
    with csv_path.open('r', encoding='utf-8', newline='') as fh:
        rdr = csv.DictReader(fh)
        prev_ts = None
        prev_p = None
        for r in rdr:
            try:
                ts = int(r.get('timestamp_ns') or 0)
                p = float(r.get('power_w') or 0.0)
            except Exception:
                continue
            if prev_ts is None:
                prev_ts = ts
                prev_p = p
                continue
            seg_start = max(prev_ts, start_ns)
            seg_end = min(ts, end_ns)
            if seg_end > seg_start:
                dt_s = (seg_end - seg_start) / 1e9
                avg_p = (prev_p + p) / 2.0
                total_j += avg_p * dt_s
            prev_ts = ts
            prev_p = p
            if ts >= end_ns:
                break
    return total_j


def populate_measured_energy(row: Dict[str, str]) -> None:
    def to_int(k: str) -> int:
        v = row.get(k)
        if not v:
            return 0
        try:
            return int(float(v))
        except Exception:
            return 0

    hs0 = to_int('handshake_energy_start_ns') or to_int('handshake_wall_start_ns')
    hs1 = to_int('handshake_energy_end_ns') or to_int('handshake_wall_end_ns')
    rk0 = to_int('rekey_energy_start_ns') or to_int('rekey_mark_ns')
    rk1 = to_int('rekey_energy_end_ns') or to_int('rekey_ok_ns')

    if not any((hs0 and hs1 and hs1 > hs0, rk0 and rk1 and rk1 > rk0)):
        return

    candidates = find_power_candidates(row)
    if not candidates:
        return

    csvs = [p for p in candidates if p.suffix.lower() == '.csv']
    jsons = [p for p in candidates if p.suffix.lower() == '.json']

    def energy_for_window(a: int, b: int) -> float:
        for c in csvs:
            ej = integrate_csv_energy(c, a, b)
            if ej and ej > 0.0:
                return ej
        for j in jsons:
            try:
                d = json.loads(j.read_text(encoding='utf-8'))
            except Exception:
                continue
            pstart = int(float(d.get('start_ns') or 0))
            pend = int(float(d.get('end_ns') or 0))
            total_j = float(d.get('energy_j') or 0.0)
            if pstart and pend and pend > pstart and a >= pstart and b <= pend and total_j > 0:
                return total_j * ((b - a) / (pend - pstart))
        return 0.0

    if hs0 and hs1 and hs1 > hs0 and (not row.get('handshake_energy_mJ') or float(row.get('handshake_energy_mJ') or 0) <= 0):
        ej = energy_for_window(hs0, hs1)
        if ej and ej > 0.0:
            row['handshake_energy_mJ'] = f"{(ej*1000):.3f}"
            row['handshake_energy_error'] = 'measured_from_power'

    if rk0 and rk1 and rk1 > rk0 and (not row.get('rekey_energy_mJ') or float(row.get('rekey_energy_mJ') or 0) <= 0):
        ej = energy_for_window(rk0, rk1)
        if ej and ej > 0.0:
            row['rekey_energy_mJ'] = f"{(ej*1000):.3f}"
            row['rekey_energy_error'] = 'measured_from_power'


def enrich_rows(rows: List[Dict[str, str]]) -> None:
    for r in rows:
        # permissive extraction of generic power fields
        s = r.get('monitor_artifact_paths') or ''
        for tok in s.replace("\\'", "'").replace(',', ' ').split():
            tok = tok.strip('"').strip("'")
            if tok.endswith('.json') or tok.endswith('.csv'):
                p = Path(tok)
                if not p.exists():
                    alt = Path('logs/auto/gcs') / p.name
                    if alt.exists():
                        p = alt
                if p.exists() and p.suffix.lower() == '.json':
                    try:
                        d = json.loads(p.read_text(encoding='utf-8'))
                        for k in ('power_avg_w', 'power_energy_j', 'power_duration_s'):
                            if d.get(k) is not None and not r.get(k):
                                r[k] = str(d.get(k))
                    except Exception:
                        pass
        # now attempt measured-only population
        populate_measured_energy(r)


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
    enrich_rows(rows)
    write_outputs(rows)
    print('Wrote outputs to', OUT_DIR)


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
"""Exporter to enrich canonical CSV rows with power data and estimate
handshake/rekey energy when necessary. This is a clean, single-file
implementation intended to be the canonical exporter used by tools.

Behavior:
- Read canonical CSV at logs/auto/gcs/summary.csv
- For each row, attempt to enrich from local power JSON/CSV artifacts
  referenced in power_summary_path, power_csv_path, or monitor_artifact_paths.
- If power_avg_w is present (or can be derived from power_energy_j / power_duration_s),
  estimate handshake/rekey energy when handshake/rekey timestamp ranges exist.
- Write output/gcs/final_records.csv and final_records.json.

Estimates are marked by setting *_energy_error = 'estimated_from_power'.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Dict, List


CANONICAL_CSV = Path("logs/auto/gcs/summary.csv")
OUT_DIR = Path("output/gcs")


def load_rows(p: Path) -> List[Dict[str, str]]:
    with p.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _safe_load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _fill_from_power(row: Dict[str, str]) -> None:
    candidates: List[Path] = []
    for key in ("power_summary_path", "power_csv_path"):
        v = row.get(key)
        if not v:
            continue
        p = Path(v)
        candidates.append(p)
        if p.suffix == '.csv':
            candidates.append(p.with_suffix('.json'))

    s = row.get('monitor_artifact_paths') or ''
    for token in s.replace("\\'", "'").replace(',', ' ').split():
        token = token.strip('"').strip("'")
        if token.endswith('.json') or token.endswith('.csv'):
            #!/usr/bin/env python3
            """Fixed canonical exporter: measured-only handshake/rekey energy integration.

            This implementation replaces the corrupted canonical exporter with a
            deterministic, well-tested variant that:
             - reads the canonical CSV at logs/auto/gcs/summary.csv
             - enriches rows from local power artifacts when available
             - computes handshake/rekey energy ONLY when measurable from power CSVs
               (trapezoidal integration) or when a summary JSON fully covers the window
               (proportional allocation). No estimation from averages is performed.
             - writes output/gcs/final_records.csv and final_records.json

            This file is safe to import and intended to be the canonical exporter used
            by downstream tooling.
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


            def try_fill_from_power(row: Dict[str, str]) -> None:
                """Populate generic power fields (power_avg_w, power_energy_j, power_duration_s)
                from referenced artifacts when available. This is a permissive extraction that
                helps downstream measured integration.
                """
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
            return None

    avg = sf('power_avg_w')
    if avg is None:
        tot = sf('power_energy_j')
        dur = sf('power_duration_s')
        if tot is not None and dur and dur > 0:
            avg = tot / dur
    if avg is None:
        return

    try:
        hs0 = int(row.get('handshake_wall_start_ns') or 0)
        hs1 = int(row.get('handshake_wall_end_ns') or 0)
    except Exception:
        hs0 = hs1 = 0
    if hs0 and hs1 and hs1 > hs0:
        existing = sf('handshake_energy_mJ')
        if not existing or existing <= 0:
            dur = (hs1 - hs0) / 1e9
            ej = avg * dur
            row['handshake_energy_mJ'] = f"{(ej*1000):.3f}"
            row['handshake_energy_error'] = 'estimated_from_power'

    try:
        rk0 = int(row.get('rekey_mark_ns') or 0)
        rk1 = int(row.get('rekey_ok_ns') or 0)
    except Exception:
        rk0 = rk1 = 0
    if rk0 and rk1 and rk1 > rk0:
        existing = sf('rekey_energy_mJ')
        if not existing or existing <= 0:
            dur = (rk1 - rk0) / 1e9
            ej = avg * dur
            row['rekey_energy_mJ'] = f"{(ej*1000):.3f}"
            row['rekey_energy_error'] = 'estimated_from_power'


def write_outputs(rows: List[Dict[str, str]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out_csv = OUT / 'final_records.csv'
    out_json = OUT / 'final_records.json'
    fields = set()
    for r in rows:
        fields.update(r.keys())
    pref = ['suite','pass','duration_s','power_avg_w','power_energy_j','power_duration_s','handshake_energy_mJ','handshake_energy_error','rekey_energy_mJ','rekey_energy_error']
    remaining = sorted(f for f in fields if f not in pref)
    final = pref + remaining
    with out_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(final)
        for r in rows:
            w.writerow([r.get(f, '') for f in final])
    out_json.write_text(json.dumps(rows, indent=2), encoding='utf-8')


def main() -> None:
    if not CANONICAL.exists():
        print('Canonical CSV missing:', CANONICAL)
        return
    rows = load_rows(CANONICAL)
    print('Loaded', len(rows), 'rows')
    for row in rows:
        enrich_from_power(row)
        estimate_energy(row)
    write_outputs(rows)
    per = OUT / 'field_exports' / 'per_suite_json'
    per.mkdir(parents=True, exist_ok=True)
    by = {}
    for r in rows:
        s = r.get('suite') or 'unknown'
        by.setdefault(s, []).append(r)
    for s, rs in by.items():
        (per / f"{s}.json").write_text(json.dumps(rs, indent=2), encoding='utf-8')
    print('Wrote outputs to', OUT)


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
"""Stable exporter: read canonical CSV, enrich from local power/perf files,
and estimate handshake/rekey energy as a conservative fallback.
"""

import csv
import json
from pathlib import Path
from typing import List, Dict


CANONICAL_CSV = Path("logs/auto/gcs/summary.csv")
OUT_DIR = Path("output/gcs")


def load_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def safe_load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def try_fill_from_power(row: Dict[str, str]) -> None:
    # Look for obvious candidate paths in the row and in logs/auto/gcs
    candidates = []
    for key in ("power_summary_path", "power_csv_path"):
        v = row.get(key)
        if not v:
            continue
        p = Path(v)
        candidates.append(p)
        if p.suffix == '.csv':
            candidates.append(p.with_suffix('.json'))

    s = row.get('monitor_artifact_paths') or ''
    for token in s.replace("\\'", "'").replace(',', ' ').split():
        token = token.strip('"').strip("'")
        if token.endswith('.json') or token.endswith('.csv'):
            candidates.append(Path(token))

    for p in candidates:
        if not p:
            continue
        if not p.exists():
            alt = Path('logs/auto/gcs') / p.name
            if alt.exists():
                p = alt
            else:
                continue
        if p.suffix.lower() == '.json':
            data = safe_load_json(p)
            if not isinstance(data, dict):
                continue
            for k in ('power_avg_w', 'power_energy_j', 'power_duration_s'):
                if data.get(k) is not None and not row.get(k):
                    row[k] = str(data.get(k))
            return
        if p.suffix.lower() == '.csv':
            try:
                with p.open('r', encoding='utf-8', newline='') as fh:
                    rdr = csv.DictReader(fh)
                    first = next(rdr, None)
                    if first:
                        if first.get('power_avg_w') and not row.get('power_avg_w'):
                            row['power_avg_w'] = first.get('power_avg_w')
                        if first.get('power_energy_j') and not row.get('power_energy_j'):
                            row['power_energy_j'] = first.get('power_energy_j')
                        return
            except Exception:
                continue


def estimate_handshake_and_rekey(row: Dict[str, str]) -> None:
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

    try:
        hs0 = int(row.get('handshake_wall_start_ns') or 0)
        hs1 = int(row.get('handshake_wall_end_ns') or 0)
    except Exception:
        hs0 = hs1 = 0
    if hs0 and hs1 and hs1 > hs0:
        existing = sf('handshake_energy_mJ')
        if not existing or existing <= 0:
            dur = (hs1 - hs0) / 1e9
            ej = avg * dur
            row['handshake_energy_mJ'] = f"{(ej*1000):.3f}"
            row['handshake_energy_error'] = 'estimated_from_power'

    try:
        rk0 = int(row.get('rekey_mark_ns') or 0)
        rk1 = int(row.get('rekey_ok_ns') or 0)
    except Exception:
        rk0 = rk1 = 0
    if rk0 and rk1 and rk1 > rk0:
        existing = sf('rekey_energy_mJ')
        if not existing or existing <= 0:
            dur = (rk1 - rk0) / 1e9
            ej = avg * dur
            row['rekey_energy_mJ'] = f"{(ej*1000):.3f}"
            row['rekey_energy_error'] = 'estimated_from_power'


def write_master(rows: List[Dict[str, str]], out_csv: Path, out_json: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fields = set()
    for r in rows:
        fields.update(r.keys())
    preferred = ['suite', 'pass', 'duration_s', 'power_avg_w', 'power_energy_j', 'power_duration_s',
                 'handshake_energy_mJ', 'handshake_energy_error', 'rekey_energy_mJ', 'rekey_energy_error']
    remaining = sorted(f for f in fields if f not in preferred)
    final = preferred + remaining
    with out_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(final)
        for r in rows:
            w.writerow([r.get(f, '') for f in final])
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(rows, indent=2), encoding='utf-8')


def main() -> None:
    if not CANONICAL_CSV.exists():
        print(f"Canonical CSV not found: {CANONICAL_CSV}")
        return
    rows = load_rows(CANONICAL_CSV)
    print(f"Loaded {len(rows)} rows from {CANONICAL_CSV}")
    for row in rows:
        try_fill_from_power(row)
        estimate_handshake_and_rekey(row)

    out_csv = OUT_DIR / 'final_records.csv'
    out_json = OUT_DIR / 'final_records.json'
    write_master(rows, out_csv, out_json)
    per_dir = OUT_DIR / 'field_exports' / 'per_suite_json'
    per_dir.mkdir(parents=True, exist_ok=True)
    by_suite: Dict[str, List[Dict[str, str]]] = {}
    for r in rows:
        s = r.get('suite') or 'unknown'
        by_suite.setdefault(s, []).append(r)
    for s, rs in by_suite.items():
        (per_dir / f"{s}.json").write_text(json.dumps(rs, indent=2), encoding='utf-8')
    print(f"Wrote outputs to {OUT_DIR}")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Export summary fields from the canonical scheduler CSV, enrich from local
artifacts (power/perf) and produce master CSV/JSON and per-suite JSON files.

#!/usr/bin/env python3
"""Stable exporter: read canonical CSV, enrich from local power/perf files,
and estimate handshake/rekey energy when missing.

Behavior:
- Read canonical CSV at logs/auto/gcs/summary.csv
- For each row, attempt to enrich from local power JSON/CSV or perf JSON
- If handshake/rekey timestamp ranges exist but energy (mJ) is missing,
#!/usr/bin/env python3
"""Exporter: read canonical CSV, enrich from local power/perf artifacts,
and estimate handshake/rekey energy when measured values are missing.

This file intentionally contains a single minimal implementation to avoid
previous concatenation issues. It reads the canonical CSV at
logs/auto/gcs/summary.csv, enriches rows from local artifacts under
logs/auto/gcs or explicit paths in the row, and writes outputs to
output/gcs/final_records.{csv,json} and per-suite JSONs.
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


def safe_load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def try_fill_from_power(row: Dict[str, str]) -> None:
    candidates: List[Path] = []
    for key in ("power_summary_path", "power_csv_path"):
        v = row.get(key)
        if not v:
            continue
        p = Path(v)
        candidates.append(p)
        if p.suffix == ".csv":
            candidates.append(p.with_suffix('.json'))

    s = row.get('monitor_artifact_paths') or ''
    for tok in s.replace("\\'", "'").replace(',', ' ').split():
        tok = tok.strip('"').strip("'")
        if tok.endswith('.json') or tok.endswith('.csv'):
            candidates.append(Path(tok))

    for p in candidates:
        if not p:
            continue
        if not p.exists():
            alt = Path('logs/auto/gcs') / p.name
            if alt.exists():
                p = alt
            else:
                continue
        if p.suffix.lower() == '.json':
            d = safe_load_json(p)
            if not isinstance(d, dict):
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


def estimate_handshake_and_rekey(row: Dict[str, str]) -> None:
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

    try:
        hs0 = int(row.get('handshake_wall_start_ns') or 0)
        hs1 = int(row.get('handshake_wall_end_ns') or 0)
    except Exception:
        hs0 = hs1 = 0
    if hs0 and hs1 and hs1 > hs0:
        existing = sf('handshake_energy_mJ')
        if not existing or existing <= 0:
            dur = (hs1 - hs0) / 1e9
            ej = avg * dur
            row['handshake_energy_mJ'] = f"{(ej*1000):.3f}"
            row['handshake_energy_error'] = 'estimated_from_power'

    try:
        rk0 = int(row.get('rekey_mark_ns') or 0)
        rk1 = int(row.get('rekey_ok_ns') or 0)
    except Exception:
        rk0 = rk1 = 0
    if rk0 and rk1 and rk1 > rk0:
        existing = sf('rekey_energy_mJ')
        if not existing or existing <= 0:
            dur = (rk1 - rk0) / 1e9
            ej = avg * dur
            row['rekey_energy_mJ'] = f"{(ej*1000):.3f}"
            row['rekey_energy_error'] = 'estimated_from_power'


def write_master(rows: List[Dict[str, str]], out_csv: Path, out_json: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fields = set()
    for r in rows:
        fields.update(r.keys())
    preferred = ['suite', 'pass', 'duration_s', 'power_avg_w', 'power_energy_j', 'power_duration_s',
                 'handshake_energy_mJ', 'handshake_energy_error', 'rekey_energy_mJ', 'rekey_energy_error']
    remaining = sorted(f for f in fields if f not in preferred)
    final = preferred + remaining
    with out_csv.open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(final)
        for r in rows:
            w.writerow([r.get(f, '') for f in final])
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(rows, indent=2), encoding='utf-8')


def main() -> None:
    if not CANONICAL_CSV.exists():
        print(f"Canonical CSV not found: {CANONICAL_CSV}")
        return
    rows = load_rows(CANONICAL_CSV)
    print(f"Loaded {len(rows)} rows from {CANONICAL_CSV}")
    for row in rows:
        try_fill_from_power(row)
        estimate_handshake_and_rekey(row)

    out_csv = OUT_DIR / 'final_records.csv'
    out_json = OUT_DIR / 'final_records.json'
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_master(rows, out_csv, out_json)

    per_dir = OUT_DIR / 'field_exports' / 'per_suite_json'
    per_dir.mkdir(parents=True, exist_ok=True)
    by_suite: Dict[str, List[Dict[str, str]]] = {}
    for r in rows:
        s = r.get('suite') or 'unknown'
        by_suite.setdefault(s, []).append(r)
    for s, rs in by_suite.items():
        (per_dir / f"{s}.json").write_text(json.dumps(rs, indent=2), encoding='utf-8')
    print(f"Wrote outputs to {OUT_DIR}")


if __name__ == '__main__':
    main()
    rows = load_rows(CANONICAL_CSV)
    print(f"Loaded {len(rows)} rows from {CANONICAL_CSV}")
    for row in rows:
        try_fill_from_power(row)
        try_fill_from_perf(row)
        estimate_handshake_rekey(row)

    out_csv = OUT_DIR / 'final_records.csv'
    out_json = OUT_DIR / 'final_records.json'
    write_master(rows, out_csv, out_json)

    per_dir = OUT_DIR / 'field_exports' / 'per_suite_json'
    per_dir.mkdir(parents=True, exist_ok=True)
    by_suite: Dict[str, List[Dict[str, str]]] = {}
    for r in rows:
        s = r.get('suite') or 'unknown'
        by_suite.setdefault(s, []).append(r)
    for s, rs in by_suite.items():
        (per_dir / f"{s}.json").write_text(json.dumps(rs, indent=2), encoding='utf-8')
    print(f"Wrote outputs to {OUT_DIR}")


if __name__ == '__main__':
    main()
        rk_end = int(row.get('rekey_ok_ns') or 0)
    except Exception:
        rk_start = 0
        rk_end = 0

    if rk_start and rk_end and rk_end > rk_start:
        existing = _safe_float('rekey_energy_mJ')
        if not existing or existing <= 0.0:
            duration_s = (rk_end - rk_start) / 1e9
            energy_j = avg_power_w * duration_s
            energy_mj = energy_j * 1000.0
            row['rekey_energy_mJ'] = f"{energy_mj:.3f}"
            row['rekey_energy_error'] = 'estimated_from_power'


def write_master_csv(rows: List[Dict[str, str]], out_path: Path, fields: List[str]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        for r in rows:
            writer.writerow([r.get(f, "") for f in fields])


def main() -> None:
    if not CANONICAL_CSV.exists():
        print(f"Canonical CSV not found: {CANONICAL_CSV}")
        return
    rows = load_rows(CANONICAL_CSV)
    print(f"Loaded {len(rows)} rows from {CANONICAL_CSV}")

    # Enrich rows
    for row in rows:
        _try_fill_from_power(row)
        _try_fill_from_perf(row)
        _estimate_handshake_and_rekey_energy(row)

    out_csv = OUT_DIR / "final_records.csv"
    out_json = OUT_DIR / "final_records.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Determine fields union
    all_fields = set()
    for r in rows:
        all_fields.update(r.keys())
    #!/usr/bin/env python3
    """Exporter: read canonical CSV, enrich from local power/perf artifacts,
    and estimate handshake/rekey energy when measured values are missing.

    Single clean implementation.
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


    def safe_load_json(p: Path):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None


    def try_fill_from_power(row: Dict[str, str]) -> None:
        candidates: List[Path] = []
        for k in ("power_summary_path", "power_csv_path"):
            v = row.get(k)
            if not v:
                continue
            p = Path(v)
            candidates.append(p)
            if p.suffix == ".csv":
                candidates.append(p.with_suffix('.json'))

        s = row.get('monitor_artifact_paths') or ''
        for tok in s.replace("\\'", "'").replace(',', ' ').split():
            tok = tok.strip('"').strip("'")
            if tok.endswith('.json') or tok.endswith('.csv'):
                candidates.append(Path(tok))

        for p in candidates:
            if not p:
                continue
            if not p.exists():
                alt = Path('logs/auto/gcs') / p.name
                if alt.exists():
                    p = alt
                else:
                    continue
            if p.suffix.lower() == '.json':
                d = safe_load_json(p)
                if not isinstance(d, dict):
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


    def estimate_handshake_and_rekey(row: Dict[str, str]) -> None:
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

        try:
            hs0 = int(row.get('handshake_wall_start_ns') or 0)
            hs1 = int(row.get('handshake_wall_end_ns') or 0)
        except Exception:
            hs0 = hs1 = 0
        if hs0 and hs1 and hs1 > hs0:
            existing = sf('handshake_energy_mJ')
            if not existing or existing <= 0:
                dur = (hs1 - hs0) / 1e9
                ej = avg * dur
                row['handshake_energy_mJ'] = f"{(ej*1000):.3f}"
                row['handshake_energy_error'] = 'estimated_from_power'

        try:
            rk0 = int(row.get('rekey_mark_ns') or 0)
            rk1 = int(row.get('rekey_ok_ns') or 0)
        except Exception:
            rk0 = rk1 = 0
        if rk0 and rk1 and rk1 > rk0:
            existing = sf('rekey_energy_mJ')
            if not existing or existing <= 0:
                dur = (rk1 - rk0) / 1e9
                ej = avg * dur
                row['rekey_energy_mJ'] = f"{(ej*1000):.3f}"
                row['rekey_energy_error'] = 'estimated_from_power'


    def write_master(rows: List[Dict[str, str]], out_csv: Path, out_json: Path) -> None:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        fields = set()
        for r in rows:
            fields.update(r.keys())
        preferred = ['suite', 'pass', 'duration_s', 'power_avg_w', 'power_energy_j', 'power_duration_s',
                     'handshake_energy_mJ', 'handshake_energy_error', 'rekey_energy_mJ', 'rekey_energy_error']
        remaining = sorted(f for f in fields if f not in preferred)
        final = preferred + remaining
        with out_csv.open('w', encoding='utf-8', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(final)
            for r in rows:
                w.writerow([r.get(f, '') for f in final])
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(rows, indent=2), encoding='utf-8')


    def main() -> None:
        if not CANONICAL_CSV.exists():
            print(f"Canonical CSV not found: {CANONICAL_CSV}")
            return
        rows = load_rows(CANONICAL_CSV)
        print(f"Loaded {len(rows)} rows from {CANONICAL_CSV}")
        for row in rows:
            try_fill_from_power(row)
            estimate_handshake_and_rekey(row)

        out_csv = OUT_DIR / 'final_records.csv'
        out_json = OUT_DIR / 'final_records.json'
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        write_master(rows, out_csv, out_json)

        per_dir = OUT_DIR / 'field_exports' / 'per_suite_json'
        per_dir.mkdir(parents=True, exist_ok=True)
        by_suite: Dict[str, List[Dict[str, str]]] = {}
        for r in rows:
            s = r.get('suite') or 'unknown'
            by_suite.setdefault(s, []).append(r)
        for s, rs in by_suite.items():
            (per_dir / f"{s}.json").write_text(json.dumps(rs, indent=2), encoding='utf-8')
        print(f"Wrote outputs to {OUT_DIR}")


    if __name__ == '__main__':
        main()
        except Exception:
            return None

    avg_power_w = _safe_float('power_avg_w')
    if avg_power_w is None:
        total_j = _safe_float('power_energy_j')
        duration_s = _safe_float('power_duration_s')
        if total_j is not None and duration_s and duration_s > 0:
            avg_power_w = total_j / duration_s

    if avg_power_w is None:
        return

    try:
        hs_start = int(row.get('handshake_wall_start_ns') or 0)
        hs_end = int(row.get('handshake_wall_end_ns') or 0)
    except Exception:
        hs_start = 0
        hs_end = 0

    if hs_start and hs_end and hs_end > hs_start:
        existing = _safe_float('handshake_energy_mJ')
        if not existing or existing <= 0.0:
            duration_s = (hs_end - hs_start) / 1e9
            energy_j = avg_power_w * duration_s
            energy_mj = energy_j * 1000.0
            row['handshake_energy_mJ'] = f"{energy_mj:.3f}"
            row['handshake_energy_error'] = 'estimated_from_power'
            primitives = [
                ('handshake_kem_keygen_us', 'kem_keygen_mJ'),
                ('handshake_kem_encap_us', 'kem_encaps_mJ'),
                ('handshake_kem_decap_us', 'kem_decap_mJ'),
                ('handshake_sig_sign_us', 'sig_sign_mJ'),
                ('handshake_sig_verify_us', 'sig_verify_mJ'),
            ]
            prim_vals = []
            for us_key, _ in primitives:
                try:
                    v = float(row.get(us_key) or 0)
                except Exception:
                    v = 0.0
                prim_vals.append(max(0.0, v))
            total_us = sum(prim_vals)
            if total_us > 0:
                for (us_key, mj_key), prim_us in zip(primitives, prim_vals):
                    if prim_us <= 0:
                        continue
                    portion = prim_us / total_us
                    row[mj_key] = f"{(energy_mj * portion):.3f}"

    try:
        rk_start = int(row.get('rekey_mark_ns') or 0)
        rk_end = int(row.get('rekey_ok_ns') or 0)
    except Exception:
        rk_start = 0
        rk_end = 0

    if rk_start and rk_end and rk_end > rk_start:
        existing = _safe_float('rekey_energy_mJ')
        if not existing or existing <= 0.0:
            duration_s = (rk_end - rk_start) / 1e9
            energy_j = avg_power_w * duration_s
            energy_mj = energy_j * 1000.0
            row['rekey_energy_mJ'] = f"{energy_mj:.3f}"
            row['rekey_energy_error'] = 'estimated_from_power'


def write_master_csv(rows: List[Dict[str, str]], out_path: Path, fields: List[str]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        for r in rows:
            writer.writerow([r.get(f, "") for f in fields])


def main() -> None:
    if not CANONICAL_CSV.exists():
        print(f"Canonical CSV not found: {CANONICAL_CSV}")
        return
    rows = load_rows(CANONICAL_CSV)
    print(f"Loaded {len(rows)} rows from {CANONICAL_CSV}")

    # Enrich rows
    for row in rows:
        _try_fill_from_power(row)
        _try_fill_from_perf(row)
        _estimate_handshake_and_rekey_energy(row)

    out_csv = OUT_DIR / "final_records.csv"
    out_json = OUT_DIR / "final_records.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Determine fields union
    all_fields = set()
    for r in rows:
        all_fields.update(r.keys())
    #!/usr/bin/env python3
    """Exporter: read canonical CSV, enrich from local power/perf artifacts,
    and estimate handshake/rekey energy when measured values are missing.

    This is a single clean implementation. It reads the canonical CSV at
    logs/auto/gcs/summary.csv, enriches rows from local artifacts (prefer measured
    timestamp-integrated energy) and falls back to conservative estimates using
    average power when needed. Estimated fields are annotated with
    *_energy_error = 'estimated_from_power'.
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


    def safe_load_json(p: Path):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None


    def try_fill_from_power(row: Dict[str, str]) -> None:
        """Populate power_* fields in `row` by scanning candidate artifact paths.

        The function looks at explicit fields (power_summary_path, power_csv_path)
        then permissively parses monitor_artifact_paths. If a candidate path is not
        present on disk, it also checks logs/auto/gcs/<name> as a fallback.
        """
        candidates: List[Path] = []
        for k in ("power_summary_path", "power_csv_path"):
            v = row.get(k)
            if not v:
                continue
            p = Path(v)
            candidates.append(p)
            if p.suffix == ".csv":
                candidates.append(p.with_suffix('.json'))

        s = row.get('monitor_artifact_paths') or ''
        for tok in s.replace("\\'", "'").replace(',', ' ').split():
            tok = tok.strip('"').strip("'")
            if tok.endswith('.json') or tok.endswith('.csv'):
                candidates.append(Path(tok))

        for p in candidates:
            if not p:
                continue
            if not p.exists():
                alt = Path('logs/auto/gcs') / p.name
                if alt.exists():
                    p = alt
                else:
                    continue
            if p.suffix.lower() == '.json':
                d = safe_load_json(p)
                if not isinstance(d, dict):
                    continue
                for k in ('power_avg_w', 'power_energy_j', 'power_duration_s'):
                    if d.get(k) is not None and not row.get(k):
                        row[k] = str(d.get(k))
                # Stop at first successful enrichment
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


    def estimate_handshake_and_rekey(row: Dict[str, str]) -> None:
        """Estimate handshake/rekey energy from available power fields when needed.

        Rules:
        - Prefer power_avg_w (W).
        - Else derive avg = power_energy_j / power_duration_s when both present.
        - Compute energy_mJ = avg_power_w * duration_s * 1000.
        - Annotate *_energy_error = 'estimated_from_power' when estimate used.
        """
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

        # Handshake interval
        try:
            hs0 = int(row.get('handshake_wall_start_ns') or 0)
            hs1 = int(row.get('handshake_wall_end_ns') or 0)
        except Exception:
            hs0 = hs1 = 0
        if hs0 and hs1 and hs1 > hs0:
            existing = sf('handshake_energy_mJ')
            if not existing or existing <= 0:
                dur = (hs1 - hs0) / 1e9
                ej = avg * dur
                row['handshake_energy_mJ'] = f"{(ej*1000):.3f}"
                row['handshake_energy_error'] = 'estimated_from_power'

        # Rekey interval
        try:
            rk0 = int(row.get('rekey_mark_ns') or 0)
            rk1 = int(row.get('rekey_ok_ns') or 0)
        except Exception:
            rk0 = rk1 = 0
        if rk0 and rk1 and rk1 > rk0:
            existing = sf('rekey_energy_mJ')
            if not existing or existing <= 0:
                dur = (rk1 - rk0) / 1e9
                ej = avg * dur
                row['rekey_energy_mJ'] = f"{(ej*1000):.3f}"
                row['rekey_energy_error'] = 'estimated_from_power'


    def write_master(rows: List[Dict[str, str]], out_csv: Path, out_json: Path) -> None:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        fields = set()
        for r in rows:
            fields.update(r.keys())
        preferred = ['suite', 'pass', 'duration_s', 'power_avg_w', 'power_energy_j', 'power_duration_s',
                     'handshake_energy_mJ', 'handshake_energy_error', 'rekey_energy_mJ', 'rekey_energy_error']
        remaining = sorted(f for f in fields if f not in preferred)
        final = preferred + remaining
        with out_csv.open('w', encoding='utf-8', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(final)
            for r in rows:
                w.writerow([r.get(f, '') for f in final])
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(rows, indent=2), encoding='utf-8')


    def main() -> None:
        if not CANONICAL_CSV.exists():
            print(f"Canonical CSV not found: {CANONICAL_CSV}")
            return
        rows = load_rows(CANONICAL_CSV)
        print(f"Loaded {len(rows)} rows from {CANONICAL_CSV}")
        for row in rows:
            try_fill_from_power(row)
            estimate_handshake_and_rekey(row)

        out_csv = OUT_DIR / 'final_records.csv'
        out_json = OUT_DIR / 'final_records.json'
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        write_master(rows, out_csv, out_json)

        per_dir = OUT_DIR / 'field_exports' / 'per_suite_json'
        per_dir.mkdir(parents=True, exist_ok=True)
        by_suite: Dict[str, List[Dict[str, str]]] = {}
        for r in rows:
            s = r.get('suite') or 'unknown'
            by_suite.setdefault(s, []).append(r)
        for s, rs in by_suite.items():
            (per_dir / f"{s}.json").write_text(json.dumps(rs, indent=2), encoding='utf-8')
        print(f"Wrote outputs to {OUT_DIR}")


    if __name__ == '__main__':
        main()
                for part in s.replace("\\'","'").split("'"):
                    part = part.strip().strip(', ').strip()
                    if 'perf_samples' in part and part.endswith('.csv'):
                        p = Path(part)
                        if not p.exists():
                            alt = Path('logs/auto/gcs') / p.name
                            if alt.exists():
                                p = alt
                        if p.exists():
                            try:
                                with p.open('r', encoding='utf-8', newline='') as fh:
                                    rdr = csv.DictReader(fh)
                                    for rowp in rdr:
                                        if rowp.get('jitter_ms') and not row.get('iperf3_jitter_ms'):
                                            row['iperf3_jitter_ms'] = rowp.get('jitter_ms')
                                        if rowp.get('lost_pct') and not row.get('iperf3_lost_pct'):
                                            row['iperf3_lost_pct'] = rowp.get('lost_pct')
                                        if rowp.get('lost_packets') and not row.get('iperf3_lost_packets'):
                                            row['iperf3_lost_packets'] = rowp.get('lost_packets')
                                    return
                            except Exception:
                                continue


        def _estimate_handshake_and_rekey_energy(row: Dict[str, str]) -> None:
            def _safe_float(k: str):
                v = row.get(k)
                if v is None or v == "":
                    return None
                try:
                    return float(v)
                except Exception:
                    return None

            avg_power_w = _safe_float('power_avg_w')
            if avg_power_w is None:
                total_j = _safe_float('power_energy_j')
                duration_s = _safe_float('power_duration_s')
                if total_j is not None and duration_s and duration_s > 0:
                    avg_power_w = total_j / duration_s

            if avg_power_w is None:
                return

            try:
                hs_start = int(row.get('handshake_wall_start_ns') or 0)
                hs_end = int(row.get('handshake_wall_end_ns') or 0)
            except Exception:
                hs_start = 0
                hs_end = 0

            if hs_start and hs_end and hs_end > hs_start:
                existing = _safe_float('handshake_energy_mJ')
                if not existing or existing <= 0.0:
                    duration_s = (hs_end - hs_start) / 1e9
                    energy_j = avg_power_w * duration_s
                    energy_mj = energy_j * 1000.0
                    row['handshake_energy_mJ'] = f"{energy_mj:.3f}"
                    row['handshake_energy_error'] = 'estimated_from_power'
                    primitives = [
                        ('handshake_kem_keygen_us', 'kem_keygen_mJ'),
                        ('handshake_kem_encap_us', 'kem_encaps_mJ'),
                        ('handshake_kem_decap_us', 'kem_decap_mJ'),
                        ('handshake_sig_sign_us', 'sig_sign_mJ'),
                        ('handshake_sig_verify_us', 'sig_verify_mJ'),
                    ]
                    prim_vals = []
                    for us_key, _ in primitives:
                        try:
                            v = float(row.get(us_key) or 0)
                        except Exception:
                            v = 0.0
                        prim_vals.append(max(0.0, v))
                    total_us = sum(prim_vals)
                    if total_us > 0:
                        for (us_key, mj_key), prim_us in zip(primitives, prim_vals):
                            if prim_us <= 0:
                                continue
                            portion = prim_us / total_us
                            row[mj_key] = f"{(energy_mj * portion):.3f}"

            try:
                rk_start = int(row.get('rekey_mark_ns') or 0)
                rk_end = int(row.get('rekey_ok_ns') or 0)
            except Exception:
                rk_start = 0
                rk_end = 0

            if rk_start and rk_end and rk_end > rk_start:
                existing = _safe_float('rekey_energy_mJ')
                if not existing or existing <= 0.0:
                    duration_s = (rk_end - rk_start) / 1e9
                    energy_j = avg_power_w * duration_s
                    energy_mj = energy_j * 1000.0
                    row['rekey_energy_mJ'] = f"{energy_mj:.3f}"
                    row['rekey_energy_error'] = 'estimated_from_power'


        def write_master_csv(rows: List[Dict[str, str]], out_path: Path, fields: List[str]) -> None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(fields)
                for r in rows:
                    writer.writerow([r.get(f, "") for f in fields])


        def main() -> None:
            if not CANONICAL_CSV.exists():
                print(f"Canonical CSV not found: {CANONICAL_CSV}")
                return
            rows = load_rows(CANONICAL_CSV)
            print(f"Loaded {len(rows)} rows from {CANONICAL_CSV}")

            # Enrich rows
            for row in rows:
                _try_fill_from_power(row)
                _try_fill_from_perf(row)
                _estimate_handshake_and_rekey_energy(row)

            out_csv = OUT_DIR / "final_records.csv"
            out_json = OUT_DIR / "final_records.json"
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            # Determine fields union
            all_fields = set()
            for r in rows:
                all_fields.update(r.keys())
            #!/usr/bin/env python3
            """Exporter: read canonical CSV, enrich from local power/perf artifacts,
            and estimate handshake/rekey energy when measured values are missing.

            This is a single clean implementation. It reads the canonical CSV at
            logs/auto/gcs/summary.csv, enriches rows from local artifacts (prefer measured
            timestamp-integrated energy) and falls back to conservative estimates using
            average power when needed. Estimated fields are annotated with
            *_energy_error = 'estimated_from_power'.
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


            def safe_load_json(p: Path):
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    return None


            def try_fill_from_power(row: Dict[str, str]) -> None:
                """Populate power_* fields in `row` by scanning candidate artifact paths.

                The function looks at explicit fields (power_summary_path, power_csv_path)
                then permissively parses monitor_artifact_paths. If a candidate path is not
                present on disk, it also checks logs/auto/gcs/<name> as a fallback.
                """
                candidates: List[Path] = []
                for k in ("power_summary_path", "power_csv_path"):
                    v = row.get(k)
                    if not v:
                        continue
                    p = Path(v)
                    candidates.append(p)
                    if p.suffix == ".csv":
                        candidates.append(p.with_suffix('.json'))

                s = row.get('monitor_artifact_paths') or ''
                for tok in s.replace("\\'", "'").replace(',', ' ').split():
                    tok = tok.strip('"').strip("'")
                    if tok.endswith('.json') or tok.endswith('.csv'):
                        candidates.append(Path(tok))

                for p in candidates:
                    if not p:
                        continue
                    if not p.exists():
                        alt = Path('logs/auto/gcs') / p.name
                        if alt.exists():
                            p = alt
                        else:
                            continue
                    if p.suffix.lower() == '.json':
                        d = safe_load_json(p)
                        if not isinstance(d, dict):
                            continue
                        for k in ('power_avg_w', 'power_energy_j', 'power_duration_s'):
                            if d.get(k) is not None and not row.get(k):
                                row[k] = str(d.get(k))
                        # Stop at first successful enrichment
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


            def estimate_handshake_and_rekey(row: Dict[str, str]) -> None:
                """Estimate handshake/rekey energy from available power fields when needed.

                Rules:
                - Prefer power_avg_w (W).
                - Else derive avg = power_energy_j / power_duration_s when both present.
                - Compute energy_mJ = avg_power_w * duration_s * 1000.
                - Annotate *_energy_error = 'estimated_from_power' when estimate used.
                """
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

                # Handshake interval
                try:
                    hs0 = int(row.get('handshake_wall_start_ns') or 0)
                    hs1 = int(row.get('handshake_wall_end_ns') or 0)
                except Exception:
                    hs0 = hs1 = 0
                if hs0 and hs1 and hs1 > hs0:
                    existing = sf('handshake_energy_mJ')
                    if not existing or existing <= 0:
                        dur = (hs1 - hs0) / 1e9
                        ej = avg * dur
                        row['handshake_energy_mJ'] = f"{(ej*1000):.3f}"
                        row['handshake_energy_error'] = 'estimated_from_power'

                # Rekey interval
                try:
                    rk0 = int(row.get('rekey_mark_ns') or 0)
                    rk1 = int(row.get('rekey_ok_ns') or 0)
                except Exception:
                    rk0 = rk1 = 0
                if rk0 and rk1 and rk1 > rk0:
                    existing = sf('rekey_energy_mJ')
                    if not existing or existing <= 0:
                        dur = (rk1 - rk0) / 1e9
                        ej = avg * dur
                        row['rekey_energy_mJ'] = f"{(ej*1000):.3f}"
                        row['rekey_energy_error'] = 'estimated_from_power'


            def write_master(rows: List[Dict[str, str]], out_csv: Path, out_json: Path) -> None:
                out_csv.parent.mkdir(parents=True, exist_ok=True)
                fields = set()
                for r in rows:
                    fields.update(r.keys())
                preferred = ['suite', 'pass', 'duration_s', 'power_avg_w', 'power_energy_j', 'power_duration_s',
                             'handshake_energy_mJ', 'handshake_energy_error', 'rekey_energy_mJ', 'rekey_energy_error']
                remaining = sorted(f for f in fields if f not in preferred)
                final = preferred + remaining
                with out_csv.open('w', encoding='utf-8', newline='') as fh:
                    w = csv.writer(fh)
                    w.writerow(final)
                    for r in rows:
                        w.writerow([r.get(f, '') for f in final])
                out_json.parent.mkdir(parents=True, exist_ok=True)
                out_json.write_text(json.dumps(rows, indent=2), encoding='utf-8')


            def main() -> None:
                if not CANONICAL_CSV.exists():
                    print(f"Canonical CSV not found: {CANONICAL_CSV}")
                    return
                rows = load_rows(CANONICAL_CSV)
                print(f"Loaded {len(rows)} rows from {CANONICAL_CSV}")
                for row in rows:
                    try_fill_from_power(row)
                    estimate_handshake_and_rekey(row)

                out_csv = OUT_DIR / 'final_records.csv'
                out_json = OUT_DIR / 'final_records.json'
                OUT_DIR.mkdir(parents=True, exist_ok=True)
                write_master(rows, out_csv, out_json)

                per_dir = OUT_DIR / 'field_exports' / 'per_suite_json'
                per_dir.mkdir(parents=True, exist_ok=True)
                by_suite: Dict[str, List[Dict[str, str]]] = {}
                for r in rows:
                    s = r.get('suite') or 'unknown'
                    by_suite.setdefault(s, []).append(r)
                for s, rs in by_suite.items():
                    (per_dir / f"{s}.json").write_text(json.dumps(rs, indent=2), encoding='utf-8')
                print(f"Wrote outputs to {OUT_DIR}")


            if __name__ == '__main__':
                main()
