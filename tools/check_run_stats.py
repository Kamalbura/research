#!/usr/bin/env python3
"""Check handshake/rekey energy measured vs estimated for a given run id.
Usage: python tools/check_run_stats.py <run_id>
"""
import json
import sys
from pathlib import Path

if len(sys.argv) < 2:
    print("Usage: python tools/check_run_stats.py <run_id>")
    sys.exit(2)

run = sys.argv[1]
rows = json.loads(Path('output/gcs/final_records.json').read_text(encoding='utf-8'))

# match if run id appears in any of several artifact/path fields
candidates = []
for r in rows:
    hay = ' '.join([str(r.get('monitor_remote_map','') or ''),
                    str(r.get('monitor_artifact_paths','') or ''),
                    str(r.get('power_csv_path','') or ''),
                    str(r.get('power_summary_path','') or ''),
                    str(r.get('monitor_manifest_path','') or '')])
    if run in hay:
        candidates.append(r)

matches = candidates
print(f'matched {len(matches)} rows for {run}')

hs_est=hs_meas=re_est=re_meas=have_power=0
hs_total=re_total=0
for r in matches:
    he = r.get('handshake_energy_mJ')
    he_err = r.get('handshake_energy_error')
    re = r.get('rekey_energy_mJ')
    re_err = r.get('rekey_energy_error')
    pavg = r.get('power_avg_w')
    penj = r.get('power_energy_j')
    if pavg or penj:
        have_power += 1
    if he and str(he) not in ('', '0', '0.0'):
        hs_total += 1
        if he_err == 'estimated_from_power':
            hs_est += 1
        else:
            hs_meas += 1
    if re and str(re) not in ('', '0', '0.0'):
        re_total += 1
        if re_err == 'estimated_from_power':
            re_est += 1
        else:
            re_meas += 1

print('have_power', have_power)
print('handshake: total', hs_total, 'estimated', hs_est, 'measured', hs_meas)
print('rekey: total', re_total, 'estimated', re_est, 'measured', re_meas)

print('\nExamples:')
for r in matches[:6]:
    print(r.get('suite'),
          'handshake=', r.get('handshake_energy_mJ'), r.get('handshake_energy_error'),
          'rekey=', r.get('rekey_energy_mJ'), r.get('rekey_energy_error'),
          'power_avg_w=', r.get('power_avg_w'))

# print a short CSV summary of counts to stdout
print('\nSummary CSV:')
print('run,suite,handshake_mJ,handshake_error,rekey_mJ,rekey_error,power_avg_w')
for r in matches:
    print(f"{run},{r.get('suite')},{r.get('handshake_energy_mJ')},{r.get('handshake_energy_error')},{r.get('rekey_energy_mJ')},{r.get('rekey_energy_error')},{r.get('power_avg_w')}")
