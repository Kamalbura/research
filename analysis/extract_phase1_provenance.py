#!/usr/bin/env python3
"""
Phase 1: Extract metrics from TXT benchmark reports and create provenance map.
Parses the 3 canonical TXT files (30 suites each) and extracts all performance metrics.
"""

import re
import json
from pathlib import Path
from typing import Dict, List, Any

def parse_suite_block(lines: List[str], start_idx: int) -> Dict[str, Any]:
    """Parse a single suite block starting from 'Suite ...' line."""
    suite_data = {}
    
    # Parse suite name from first line
    suite_line = lines[start_idx]
    match = re.match(r'Suite (.+?) — (\w+)', suite_line)
    if match:
        suite_data['suite_id'] = match.group(1)
        suite_data['status'] = match.group(2)
    
    # Parse all metrics from subsequent lines
    idx = start_idx + 1
    while idx < len(lines) and not lines[idx].startswith('Suite '):
        line = lines[idx].strip()
        
        if not line or line.startswith('•'):
            # Remove bullet point if present
            line = line.lstrip('• ')
            
            # Throughput
            if m := re.search(r'throughput ([\d.]+) Mb/s', line):
                suite_data['throughput_mbps'] = float(m.group(1))
            if m := re.search(r'goodput ([\d.]+) Mb/s', line):
                suite_data['goodput_mbps'] = float(m.group(1))
            
            # Loss
            if m := re.search(r'loss ([\d.]+)%', line):
                suite_data['loss_pct'] = float(m.group(1))
            
            # RTT
            if m := re.search(r'RTT avg ([\d.]+) ms', line):
                suite_data['rtt_avg_ms'] = float(m.group(1))
            if m := re.search(r'p50 ([\d.]+) ms', line):
                suite_data['rtt_p50_ms'] = float(m.group(1))
            if m := re.search(r'p95 ([\d.]+) ms', line):
                suite_data['rtt_p95_ms'] = float(m.group(1))
            if m := re.search(r'max ([\d.]+) ms', line):
                suite_data['rtt_max_ms'] = float(m.group(1))
            
            # Handshake
            if m := re.search(r'handshake gcs ([\d.]+) ms', line):
                suite_data['handshake_gcs_ms'] = float(m.group(1))
            
            # Crypto breakdown
            if m := re.search(r'kem keygen ([\d.]+) ms', line):
                suite_data['kem_keygen_ms'] = float(m.group(1))
            if m := re.search(r'kem decap ([\d.]+) ms', line):
                suite_data['kem_decap_ms'] = float(m.group(1))
            if m := re.search(r'sig sign ([\d.]+) ms', line):
                suite_data['sig_sign_ms'] = float(m.group(1))
            if m := re.search(r'primitives total ([\d.]+) ms', line):
                suite_data['primitives_total_ms'] = float(m.group(1))
            
            # Resources
            if m := re.search(r'CPU max ([\d.]+)%', line):
                suite_data['cpu_max_percent'] = float(m.group(1))
            if m := re.search(r'RSS ([\d.]+) MiB', line):
                suite_data['rss_mib'] = float(m.group(1))
            
            # Power
            if m := re.search(r'power ([\d.]+) W avg over', line):
                suite_data['power_avg_w'] = float(m.group(1))
            if m := re.search(r'avg over [\d.]+ s \(([\d.]+) J\)', line):
                suite_data['energy_j'] = float(m.group(1))
            
            # Rekey
            if m := re.search(r'rekey window ([\d.]+) ms', line):
                suite_data['rekey_window_ms'] = float(m.group(1))
            if m := re.search(r'rekeys ok (\d+) / fail (\d+)', line):
                suite_data['rekeys_ok'] = int(m.group(1))
                suite_data['rekeys_fail'] = int(m.group(2))
            
            # Packets
            if m := re.search(r'packets sent ([\d,]+) / received ([\d,]+)', line):
                suite_data['packets_sent'] = int(m.group(1).replace(',', ''))
                suite_data['packets_received'] = int(m.group(2).replace(',', ''))
        
        idx += 1
    
    return suite_data, idx

def parse_txt_file(filepath: str) -> Dict[str, Dict[str, Any]]:
    """Parse an entire TXT benchmark file and extract all suites."""
    with open(filepath, 'r') as f:
        lines = f.readlines()
    
    suites = {}
    idx = 0
    
    while idx < len(lines):
        line = lines[idx].strip()
        if line.startswith('Suite '):
            suite_data, next_idx = parse_suite_block(lines, idx)
            if 'suite_id' in suite_data:
                suites[suite_data['suite_id']] = suite_data
            idx = next_idx
        else:
            idx += 1
    
    return suites

def extract_suite_metadata(suite_id: str) -> Dict[str, Any]:
    """Extract KEM family, NIST level, AEAD, signature from suite ID."""
    metadata = {}
    
    # Parse suite ID format: [cs-]<kem>-<aead>-<sig>
    parts = suite_id.split('-')
    
    # Check for cs- prefix (Classic Suite)
    if parts[0] == 'cs':
        metadata['classic_suite'] = True
        parts = parts[1:]
    else:
        metadata['classic_suite'] = False
    
    # Extract KEM (first part)
    kem = parts[0]
    metadata['kem_full'] = kem
    
    # Determine KEM family
    if 'mlkem' in kem.lower() or 'kyber' in kem.lower():
        metadata['kem_family'] = 'ML-KEM'
    elif 'hqc' in kem.lower():
        metadata['kem_family'] = 'HQC'
    elif 'mceliece' in kem.lower():
        metadata['kem_family'] = 'Classic-McEliece'
    elif 'frodo' in kem.lower():
        metadata['kem_family'] = 'FrodoKEM'
    else:
        metadata['kem_family'] = 'Unknown'
    
    # Extract NIST level from KEM variant
    if '512' in kem or '128' in kem:
        metadata['nist_level'] = 1
    elif '768' in kem or '192' in kem or '256' in kem:
        metadata['nist_level'] = 3
    elif '1024' in kem or '348864' in kem or '460896' in kem:
        metadata['nist_level'] = 5
    elif '6688128' in kem or '6960119' in kem or '8192128' in kem:
        metadata['nist_level'] = 5
    elif '976' in kem or '1344' in kem:
        metadata['nist_level'] = 5
    else:
        metadata['nist_level'] = None
    
    # Extract AEAD (usually second part)
    if len(parts) > 1:
        aead = parts[1]
        if 'aesgcm' in aead.lower():
            metadata['aead_cipher'] = 'AES-GCM'
        elif 'chacha' in aead.lower():
            metadata['aead_cipher'] = 'ChaCha20-Poly1305'
        else:
            metadata['aead_cipher'] = aead
    
    # Extract signature (remaining parts)
    if len(parts) > 2:
        sig = '-'.join(parts[2:])
        metadata['sig_scheme'] = sig
        
        if 'mldsa' in sig.lower() or 'dilithium' in sig.lower():
            metadata['sig_family'] = 'ML-DSA'
        elif 'falcon' in sig.lower():
            metadata['sig_family'] = 'Falcon'
        elif 'sphincs' in sig.lower():
            metadata['sig_family'] = 'SPHINCS+'
        else:
            metadata['sig_family'] = 'Unknown'
    
    return metadata

def main():
    # File paths
    base_path = Path('/home/runner/work/research/research/results')
    
    files = {
        'baseline': base_path / 'benchmarks without-ddos detectetion.txt',
        'lightweight': base_path / 'results with ddos detection (lightweight).txt',
        'transformer': base_path / 'results benchmarks with ddos detectetion time series trandssformer heavy.txt'
    }
    
    # Parse all files
    all_data = {}
    for mode, filepath in files.items():
        print(f"Parsing {mode}: {filepath}")
        suites = parse_txt_file(str(filepath))
        print(f"  Found {len(suites)} suites")
        all_data[mode] = suites
    
    # Build provenance map
    provenance_map = {
        'metadata': {
            'description': 'Phase 1 provenance map: extracted metrics from 3 TXT benchmark reports',
            'sources': {
                'baseline': str(files['baseline']),
                'lightweight': str(files['lightweight']),
                'transformer': str(files['transformer'])
            },
            'total_suites': len(all_data.get('baseline', {})),
            'total_combinations': sum(len(suites) for suites in all_data.values())
        },
        'suites': {}
    }
    
    # Merge data by suite ID
    all_suite_ids = set()
    for mode_data in all_data.values():
        all_suite_ids.update(mode_data.keys())
    
    for suite_id in sorted(all_suite_ids):
        suite_metadata = extract_suite_metadata(suite_id)
        
        suite_entry = {
            'suite_id': suite_id,
            'metadata': suite_metadata,
            'metrics': {
                'baseline': all_data['baseline'].get(suite_id, {}),
                'lightweight': all_data['lightweight'].get(suite_id, {}),
                'transformer': all_data['transformer'].get(suite_id, {})
            }
        }
        
        provenance_map['suites'][suite_id] = suite_entry
    
    # Write to JSON
    output_path = Path('/home/runner/work/research/research/analysis/phase1_provenance_map.json')
    with open(output_path, 'w') as f:
        json.dump(provenance_map, f, indent=2)
    
    print(f"\n✅ Phase 1 provenance map created: {output_path}")
    print(f"   Total suites: {provenance_map['metadata']['total_suites']}")
    print(f"   Total combinations: {provenance_map['metadata']['total_combinations']}")
    
    # Print summary statistics
    print("\n📊 Summary Statistics:")
    for mode in ['baseline', 'lightweight', 'transformer']:
        throughputs = [s['metrics'][mode].get('throughput_mbps', 0) 
                      for s in provenance_map['suites'].values() 
                      if mode in s['metrics'] and s['metrics'][mode]]
        losses = [s['metrics'][mode].get('loss_pct', 0) 
                 for s in provenance_map['suites'].values() 
                 if mode in s['metrics'] and s['metrics'][mode]]
        handshakes = [s['metrics'][mode].get('handshake_gcs_ms', 0) 
                     for s in provenance_map['suites'].values() 
                     if mode in s['metrics'] and s['metrics'][mode]]
        powers = [s['metrics'][mode].get('power_avg_w', 0) 
                 for s in provenance_map['suites'].values() 
                 if mode in s['metrics'] and s['metrics'][mode]]
        
        if throughputs:
            print(f"\n  {mode.upper()}:")
            print(f"    Throughput: {min(throughputs):.2f}-{max(throughputs):.2f} Mb/s")
            print(f"    Loss: {min(losses):.3f}-{max(losses):.3f}%")
            print(f"    Handshake: {min(handshakes):.2f}-{max(handshakes):.1f} ms")
            print(f"    Power: {min(powers):.2f}-{max(powers):.2f} W")

if __name__ == '__main__':
    main()
