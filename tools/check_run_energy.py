#!/usr/bin/env python3
import json
from pathlib import Path

fn = Path('output/gcs/final_records.json')
if not fn.exists():
    print(f'Missing {fn}'); raise SystemExit(1)

rows = json.loads(fn.read_text(encoding='utf-8'))
run_id = 'run_1760308685'

matches = []
for r in rows:
    joined = ' '.join([str(r.get('monitor_remote_map','')), str(r.get('monitor_artifact_paths','')), str(r.get('power_csv_path','') or ''), str(r.get('power_summary_path','') or ''), str(r.get('monitor_manifest_path','') or '')])
    if run_id in joined:
        matches.append(r)

if not matches:
    print('No rows matched', run_id); raise SystemExit(0)

# Print header
print('suite,handshake_mJ,handshake_error,rekey_mJ,rekey_error,power_avg_w,power_energy_j,power_fetch_status,monitor_fetch_status')
for r in matches:
    suite = r.get('suite')
    he = r.get('handshake_energy_mJ') or ''
    he_err = r.get('handshake_energy_error') or ''
    re = r.get('rekey_energy_mJ') or ''
    re_err = r.get('rekey_energy_error') or ''
    pavg = r.get('power_avg_w') or ''
    penj = r.get('power_energy_j') or ''
    pfetch = r.get('power_fetch_status') or ''
    mfetch = r.get('monitor_fetch_status') or ''
    print(f'{suite},{he},{he_err},{re},{re_err},{pavg},{penj},{pfetch},{mfetch}')

print('\nTotal matched rows:', len(matches))
