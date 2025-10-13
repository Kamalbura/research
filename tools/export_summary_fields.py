#!/usr/bin/env python3
"""Export summary fields from the canonical scheduler CSV, enrich from local
artifacts (power/perf) and produce master CSV/JSON and per-suite JSON files.

This script is conservative: when handshake/rekey energy (mJ) is missing it will
attempt to estimate it using average power (W) × duration (s) and mark the
field with an error flag 'estimated_from_power'.

Usage: python -m tools.export_summary_fields
"""

import csv
import json
from pathlib import Path
from typing import List, Dict


CANONICAL_CSV = Path("logs/auto/gcs/summary.csv")
OUT_DIR = Path("output/gcs")


def load_rows(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader)


def _safe_load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _try_fill_from_power(row: Dict[str, str]) -> None:
    candidates: List[Path] = []
    if row.get("power_summary_path"):
        candidates.append(Path(row["power_summary_path"]))
    if row.get("power_csv_path"):
        p = Path(row["power_csv_path"])
        candidates.append(p.with_suffix('.json'))
        candidates.append(p)
    if row.get("monitor_artifact_paths"):
        s = row["monitor_artifact_paths"]
        for part in s.replace("\\'", "'").split("'"):
            part = part.strip().strip(', ').strip()
            if part.endswith('.json') or part.endswith('.csv'):
                candidates.append(Path(part))

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
            data = _safe_load_json(p)
            if not isinstance(data, dict):
                continue
            for k in ('power_avg_w','power_energy_j','power_samples','power_sample_rate_hz','power_duration_s'):
                if data.get(k) is not None and not row.get(k):
                    row[k] = str(data.get(k))
            for key in ['handshake_kem_keygen_mJ','handshake_kem_encap_mJ','handshake_kem_decap_mJ','handshake_sig_sign_mJ','handshake_sig_verify_mJ']:
                if data.get(key) is not None and not row.get(key):
                    row[key] = str(data.get(key))
            return
        elif p.suffix.lower() == '.csv':
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


def _try_fill_from_perf(row: Dict[str, str]) -> None:
    if row.get('iperf3_report_path'):
        p = Path(row['iperf3_report_path'])
        if not p.exists():
            alt = Path('logs/auto/gcs') / p.name
            if alt.exists():
                p = alt
        if p.exists() and p.suffix.lower() == '.json':
            data = _safe_load_json(p)
            if isinstance(data, dict):
                end = data.get('end', {})
                if end:
                    if not row.get('iperf3_lost_pct'):
                        lost = end.get('sum', {}).get('lost_percent')
                        if lost is not None:
                            row['iperf3_lost_pct'] = str(lost)
                    if not row.get('iperf3_lost_packets'):
                        lost_pkts = end.get('sum', {}).get('lost')
                        if lost_pkts is not None:
                            row['iperf3_lost_packets'] = str(lost_pkts)
                    if not row.get('iperf3_jitter_ms'):
                        jitter = end.get('sum', {}).get('jitter_ms') or end.get('sum', {}).get('jitter')
                        if jitter is not None:
                            row['iperf3_jitter_ms'] = str(jitter)
                return

    if row.get('monitor_artifact_paths'):
        s = row['monitor_artifact_paths']
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
    # Preserve a stable ordering: known fields first, then the rest sorted
    preferred = [
        'suite','pass','duration_s','power_avg_w','power_energy_j','power_duration_s',
        'handshake_energy_mJ','handshake_energy_error','rekey_energy_mJ','rekey_energy_error',
    ]
    remaining = sorted(f for f in all_fields if f not in preferred)
    fields = preferred + remaining

    write_master_csv(rows, out_csv, fields)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(rows, indent=2), encoding='utf-8')

    # per-suite JSONs
    per_suite_dir = OUT_DIR / 'field_exports' / 'per_suite_json'
    per_suite_dir.mkdir(parents=True, exist_ok=True)
    by_suite: Dict[str, List[Dict[str, str]]] = {}
    for r in rows:
        s = r.get('suite') or 'unknown'
        by_suite.setdefault(s, []).append(r)
    for s, rs in by_suite.items():
        (per_suite_dir / f"{s}.json").write_text(json.dumps(rs, indent=2), encoding='utf-8')

    print(f"Wrote master CSV -> {out_csv}")
    print(f"Wrote master JSON -> {out_json}")
    print(f"Wrote per-suite JSONs -> {per_suite_dir}")


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
from __future__ import annotations

"""Export summary fields from the canonical scheduler CSV, enrich from local
artifacts (power/perf) and produce master CSV/JSON and per-suite JSON files.

This script is conservative: when handshake/rekey energy (mJ) is missing it will
attempt to estimate it using average power (W) × duration (s) and mark the
field with an error flag 'estimated_from_power'.

Usage: python -m tools.export_summary_fields
"""

import csv
import json
from pathlib import Path
from typing import List, Dict


CANONICAL_CSV = Path("logs/auto/gcs/summary.csv")
OUT_DIR = Path("output/gcs")


def load_rows(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader)


def _safe_load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _try_fill_from_power(row: Dict[str, str]) -> None:
    candidates: List[Path] = []
    if row.get("power_summary_path"):
        candidates.append(Path(row["power_summary_path"]))
    if row.get("power_csv_path"):
        p = Path(row["power_csv_path"])
        candidates.append(p.with_suffix('.json'))
        candidates.append(p)
    if row.get("monitor_artifact_paths"):
        s = row["monitor_artifact_paths"]
        for part in s.replace("\\'", "'").split("'"):
            part = part.strip().strip(', ').strip()
            if part.endswith('.json') or part.endswith('.csv'):
                candidates.append(Path(part))

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
            data = _safe_load_json(p)
            if not isinstance(data, dict):
                continue
            for k in ('power_avg_w','power_energy_j','power_samples','power_sample_rate_hz','power_duration_s'):
                if data.get(k) is not None and not row.get(k):
                    row[k] = str(data.get(k))
            for key in ['handshake_kem_keygen_mJ','handshake_kem_encap_mJ','handshake_kem_decap_mJ','handshake_sig_sign_mJ','handshake_sig_verify_mJ']:
                if data.get(key) is not None and not row.get(key):
                    row[key] = str(data.get(key))
            return
        elif p.suffix.lower() == '.csv':
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


def _try_fill_from_perf(row: Dict[str, str]) -> None:
    if row.get('iperf3_report_path'):
        p = Path(row['iperf3_report_path'])
        if not p.exists():
            alt = Path('logs/auto/gcs') / p.name
            if alt.exists():
                p = alt
        if p.exists() and p.suffix.lower() == '.json':
            data = _safe_load_json(p)
            if isinstance(data, dict):
                end = data.get('end', {})
                if end:
                    if not row.get('iperf3_lost_pct'):
                        lost = end.get('sum', {}).get('lost_percent')
                        if lost is not None:
                            row['iperf3_lost_pct'] = str(lost)
                    if not row.get('iperf3_lost_packets'):
                        lost_pkts = end.get('sum', {}).get('lost')
                        if lost_pkts is not None:
                            row['iperf3_lost_packets'] = str(lost_pkts)
                    if not row.get('iperf3_jitter_ms'):
                        jitter = end.get('sum', {}).get('jitter_ms') or end.get('sum', {}).get('jitter')
                        if jitter is not None:
                            row['iperf3_jitter_ms'] = str(jitter)
                return

    if row.get('monitor_artifact_paths'):
        s = row['monitor_artifact_paths']
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
    # Preserve a stable ordering: known fields first, then the rest sorted
    preferred = [
        'suite','pass','duration_s','power_avg_w','power_energy_j','power_duration_s',
        'handshake_energy_mJ','handshake_energy_error','rekey_energy_mJ','rekey_energy_error',
    ]
    remaining = sorted(f for f in all_fields if f not in preferred)
    fields = preferred + remaining

    write_master_csv(rows, out_csv, fields)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(rows, indent=2), encoding='utf-8')

    # per-suite JSONs
    per_suite_dir = OUT_DIR / 'field_exports' / 'per_suite_json'
    per_suite_dir.mkdir(parents=True, exist_ok=True)
    by_suite: Dict[str, List[Dict[str, str]]] = {}
    for r in rows:
        s = r.get('suite') or 'unknown'
        by_suite.setdefault(s, []).append(r)
    for s, rs in by_suite.items():
        (per_suite_dir / f"{s}.json").write_text(json.dumps(rs, indent=2), encoding='utf-8')

    print(f"Wrote master CSV -> {out_csv}")
    print(f"Wrote master JSON -> {out_json}")
    print(f"Wrote per-suite JSONs -> {per_suite_dir}")


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
"""Export summary fields from the canonical scheduler CSV, enrich from local
artifacts (power/perf) and produce master CSV/JSON and per-suite JSON files.

This script is conservative: when handshake/rekey energy (mJ) is missing it will
attempt to estimate it using average power (W) × duration (s) and mark the
field with an error flag 'estimated_from_power'.

Usage: python -m tools.export_summary_fields
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import List, Dict


CANONICAL_CSV = Path("logs/auto/gcs/summary.csv")
OUT_DIR = Path("output/gcs")


def load_rows(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader)


def _safe_load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        #!/usr/bin/env python3
        from __future__ import annotations

        """Export summary fields from the canonical scheduler CSV, enrich from local
        artifacts (power/perf) and produce master CSV/JSON and per-suite JSON files.

        This script is conservative: when handshake/rekey energy (mJ) is missing it will
        attempt to estimate it using average power (W) × duration (s) and mark the
        field with an error flag 'estimated_from_power'.

        Usage: python -m tools.export_summary_fields
        """

        import csv
        import json
        from pathlib import Path
        from typing import List, Dict


        CANONICAL_CSV = Path("logs/auto/gcs/summary.csv")
        OUT_DIR = Path("output/gcs")


        def load_rows(csv_path: Path) -> List[Dict[str, str]]:
            with csv_path.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                return list(reader)


        def _safe_load_json(path: Path):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None


        def _try_fill_from_power(row: Dict[str, str]) -> None:
            candidates: List[Path] = []
            if row.get("power_summary_path"):
                candidates.append(Path(row["power_summary_path"]))
            if row.get("power_csv_path"):
                p = Path(row["power_csv_path"])
                candidates.append(p.with_suffix('.json'))
                candidates.append(p)
            if row.get("monitor_artifact_paths"):
                s = row["monitor_artifact_paths"]
                for part in s.replace("\\'", "'").split("'"):
                    part = part.strip().strip(', ').strip()
                    if part.endswith('.json') or part.endswith('.csv'):
                        candidates.append(Path(part))

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
                    data = _safe_load_json(p)
                    if not isinstance(data, dict):
                        continue
                    for k in ('power_avg_w','power_energy_j','power_samples','power_sample_rate_hz','power_duration_s'):
                        if data.get(k) is not None and not row.get(k):
                            row[k] = str(data.get(k))
                    for key in ['handshake_kem_keygen_mJ','handshake_kem_encap_mJ','handshake_kem_decap_mJ','handshake_sig_sign_mJ','handshake_sig_verify_mJ']:
                        if data.get(key) is not None and not row.get(key):
                            row[key] = str(data.get(key))
                    return
                elif p.suffix.lower() == '.csv':
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


        def _try_fill_from_perf(row: Dict[str, str]) -> None:
            if row.get('iperf3_report_path'):
                p = Path(row['iperf3_report_path'])
                if not p.exists():
                    alt = Path('logs/auto/gcs') / p.name
                    if alt.exists():
                        p = alt
                if p.exists() and p.suffix.lower() == '.json':
                    data = _safe_load_json(p)
                    if isinstance(data, dict):
                        end = data.get('end', {})
                        if end:
                            if not row.get('iperf3_lost_pct'):
                                lost = end.get('sum', {}).get('lost_percent')
                                if lost is not None:
                                    row['iperf3_lost_pct'] = str(lost)
                            if not row.get('iperf3_lost_packets'):
                                lost_pkts = end.get('sum', {}).get('lost')
                                if lost_pkts is not None:
                                    row['iperf3_lost_packets'] = str(lost_pkts)
                            if not row.get('iperf3_jitter_ms'):
                                jitter = end.get('sum', {}).get('jitter_ms') or end.get('sum', {}).get('jitter')
                                if jitter is not None:
                                    row['iperf3_jitter_ms'] = str(jitter)
                        return

            if row.get('monitor_artifact_paths'):
                s = row['monitor_artifact_paths']
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
            # Preserve a stable ordering: known fields first, then the rest sorted
            preferred = [
                'suite','pass','duration_s','power_avg_w','power_energy_j','power_duration_s',
                'handshake_energy_mJ','handshake_energy_error','rekey_energy_mJ','rekey_energy_error',
            ]
            remaining = sorted(f for f in all_fields if f not in preferred)
            fields = preferred + remaining

            write_master_csv(rows, out_csv, fields)
            out_json.parent.mkdir(parents=True, exist_ok=True)
            out_json.write_text(json.dumps(rows, indent=2), encoding='utf-8')

            # per-suite JSONs
            per_suite_dir = OUT_DIR / 'field_exports' / 'per_suite_json'
            per_suite_dir.mkdir(parents=True, exist_ok=True)
            by_suite: Dict[str, List[Dict[str, str]]] = {}
            for r in rows:
                s = r.get('suite') or 'unknown'
                by_suite.setdefault(s, []).append(r)
            for s, rs in by_suite.items():
                (per_suite_dir / f"{s}.json").write_text(json.dumps(rs, indent=2), encoding='utf-8')

            print(f"Wrote master CSV -> {out_csv}")
            print(f"Wrote master JSON -> {out_json}")
            print(f"Wrote per-suite JSONs -> {per_suite_dir}")


        if __name__ == '__main__':
            main()
