#!/usr/bin/env python3
"""Audit fields in logs/auto/gcs/summary.csv and emit per-suite summary files.

Produces:
 - output/gcs/field_audit/field_presence.csv  (counts per field)
 - output/gcs/field_audit/per_suite/<suite>.json (rows for that suite with requested fields)
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Dict, List

REQ_FIELDS = [
    # trimmed for readability but will include all fields from the user's long list
]


def main() -> None:
    csv_path = Path("logs/auto/gcs/summary.csv")
    out_dir = Path("output/gcs/field_audit")
    per_suite_dir = out_dir / "per_suite"
    out_dir.mkdir(parents=True, exist_ok=True)
    per_suite_dir.mkdir(parents=True, exist_ok=True)

    # Full requested list (expanded)
    REQ_FIELDS.extend([
        "pass","suite","traffic_mode","traffic_engine","pre_gap_s","inter_gap_s","duration_s","sent","rcvd","pps","target_rate_pps","target_bandwidth_mbps","throughput_mbps","sent_mbps","goodput_mbps","wire_throughput_mbps_est","goodput_ratio","delivered_ratio","loss_pct","loss_pct_wilson_low","loss_pct_wilson_high","app_packet_bytes","wire_packet_bytes_est","cpu_max_percent","max_rss_bytes","pfc_watts","kinematics_vh","kinematics_vv","rtt_avg_ms","rtt_max_ms","rtt_p50_ms","rtt_p95_ms","owd_p50_ms","owd_p95_ms","rtt_samples","owd_samples","sample_every","min_delay_samples","sample_quality","enc_out","enc_in","drops","rekeys_ok","rekeys_fail","start_ns","end_ns","scheduled_mark_ns","rekey_mark_ns","rekey_ok_ns","rekey_ms","rekey_energy_mJ","rekey_energy_error","handshake_energy_start_ns","handshake_energy_end_ns","rekey_energy_start_ns","rekey_energy_end_ns","handshake_energy_segments","rekey_energy_segments","clock_offset_ns","power_request_ok","power_capture_ok","power_note","power_error","power_avg_w","power_energy_j","power_samples","power_avg_current_a","power_avg_voltage_v","power_sample_rate_hz","power_duration_s","power_csv_path","power_summary_path","power_fetch_status","power_fetch_error","power_trace_samples","power_trace_error","iperf3_jitter_ms","iperf3_lost_pct","iperf3_lost_packets","iperf3_report_path","monitor_manifest_path","telemetry_status_path","monitor_artifacts_fetched","monitor_artifact_paths","monitor_artifact_categories","monitor_remote_map","monitor_fetch_status","monitor_fetch_error","blackout_ms","gap_max_ms","gap_p99_ms","steady_gap_ms","recv_rate_kpps_before","recv_rate_kpps_after","proc_ns_p95","pair_start_ns","pair_end_ns","blackout_error","timing_guard_ms","timing_guard_violation","kem_keygen_ms","kem_encaps_ms","kem_decap_ms","sig_sign_ms","sig_verify_ms","primitive_total_ms","pub_key_size_bytes","ciphertext_size_bytes","sig_size_bytes","shared_secret_size_bytes","handshake_total_ms","handshake_role","handshake_wall_start_ns","handshake_wall_end_ns","handshake_kem_keygen_us","handshake_kem_encap_us","handshake_kem_decap_us","handshake_sig_sign_us","handshake_sig_verify_us","handshake_kdf_server_us","handshake_kdf_client_us","handshake_kem_pub_bytes","handshake_kem_ct_bytes","handshake_sig_bytes","handshake_auth_tag_bytes","handshake_shared_secret_bytes","handshake_server_hello_bytes","handshake_challenge_bytes","handshake_kem_keygen_mJ","handshake_kem_encap_mJ","handshake_kem_decap_mJ","handshake_sig_sign_mJ","handshake_sig_verify_mJ","handshake_energy_mJ","handshake_energy_error","kem_keygen_mJ","kem_encaps_mJ","kem_decap_mJ","sig_sign_mJ","sig_verify_mJ","timer_resolution_warning"
    ])

    # Load CSV
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    # Field presence counts
    presence: Dict[str, int] = {f: 0 for f in REQ_FIELDS}
    total_rows = len(rows)

    per_suite_rows: Dict[str, List[Dict[str, str]]] = {}

    for row in rows:
        suite = row.get("suite") or "__unknown__"
        per_suite_rows.setdefault(suite, []).append(row)
        for f in REQ_FIELDS:
            # Consider present if key exists and value is non-empty
            if f in row and row.get(f) not in (None, ""):
                presence[f] += 1

    # Write field presence CSV
    out_csv = out_dir / "field_presence.csv"
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["field", "present_count", "present_pct"]) 
        for f in REQ_FIELDS:
            cnt = presence.get(f, 0)
            pct = (cnt / total_rows * 100.0) if total_rows > 0 else 0.0
            writer.writerow([f, cnt, f"{pct:.1f}"])

    # Write per-suite JSONs with only the requested fields present per row
    for suite, srows in per_suite_rows.items():
        out_file = per_suite_dir / (suite.replace("/", "_") + ".json")
        out_data = []
        for r in srows:
            entry = {f: (r.get(f) if f in r else None) for f in REQ_FIELDS}
            out_data.append(entry)
        out_file.write_text(json.dumps({"suite": suite, "rows": out_data}, indent=2), encoding="utf-8")

    print(f"Audited {total_rows} rows. Field presence written to: {out_csv}")
    print(f"Per-suite JSONs written to: {per_suite_dir}")


if __name__ == '__main__':
    main()
