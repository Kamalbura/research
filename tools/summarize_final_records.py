"""Summarize final_records.json and report missing/zero metrics per field.

Outputs:
 - prints summary to stdout
 - writes tools/final_records_field_report.csv with counts
"""
import json
import csv
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(r"c:/Users/burak/Desktop/research")
IN_PATH = REPO_ROOT / "output" / "gcs" / "final_records.json"
OUT_CSV = REPO_ROOT / "tools" / "final_records_field_report.csv"

KEYS_OF_INTEREST = [
    'suite', 'traffic_engine', 'throughput_mbps', 'rtt_avg_ms', 'rtt_p95_ms', 'owd_p50_ms',
    'loss_pct', 'power_avg_w', 'power_energy_j', 'handshake_total_ms',
    'handshake_kem_keygen_us','handshake_kem_encap_us','handshake_kem_decap_us',
    'handshake_sig_sign_us','handshake_sig_verify_us','blackout_ms', 'rekey_ms'
]

def load():
    text = IN_PATH.read_text(encoding='utf-8')
    data = json.loads(text)
    return data


def analyze(records):
    total = len(records)
    missing_counts = Counter()
    zero_counts = Counter()
    value_samples = defaultdict(list)

    for r in records:
        for k in KEYS_OF_INTEREST:
            v = r.get(k, None)
            if v is None or v == "":
                missing_counts[k] += 1
            else:
                # treat numeric zero-ish strings as zero
                s = str(v).strip()
                if s in {"0", "0.0", "0.00"}:
                    zero_counts[k] += 1
                else:
                    value_samples[k].append(s)

    return total, missing_counts, zero_counts, value_samples


def write_csv(total, missing, zero, samples):
    with OUT_CSV.open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(["field","total_records","missing_count","zero_count","sample_example"])
        for k in KEYS_OF_INTEREST:
            example = samples[k][0] if samples[k] else ""
            w.writerow([k, total, missing.get(k,0), zero.get(k,0), example])


def main():
    if not IN_PATH.exists():
        print(f"Could not find {IN_PATH}")
        return
    records = load()
    total, missing, zero, samples = analyze(records)
    print(f"Total suite records: {total}\n")
    print("Field, missing_count, zero_count, sample_example")
    for k in KEYS_OF_INTEREST:
        print(f"{k}: {missing.get(k,0)}, {zero.get(k,0)}, example={samples[k][0] if samples[k] else ''}")
    write_csv(total, missing, zero, samples)
    print(f"Wrote report to {OUT_CSV}")

if __name__ == '__main__':
    main()
