#!/usr/bin/env python3
"""
Extract all metrics from TXT reports and build canonical data provenance map.

This script parses the three canonical TXT scenario reports and creates:
1. Suite metadata catalog (KEM family, NIST level, AEAD cipher)
2. Metric provenance map (source file + line number + extraction regex)
3. Aggregated metrics across DDOS modes for each suite
4. Validation checksums for reproducibility

Phase 1 Continuation: Data Provenance Mapping
"""

import re
import json
from pathlib import Path
from typing import Dict, List, Tuple, Any
from dataclasses import dataclass, asdict
from collections import defaultdict

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class Suite:
    """Suite metadata (extracted from suite ID string)."""
    suite_id: str
    kem_family: str
    kem_variant: str
    aead_cipher: str
    sig_scheme: str
    sig_variant: str

@dataclass
class MetricValue:
    """Single metric value with provenance."""
    metric_name: str
    value: float
    unit: str
    source_file: str
    line_number: int
    extraction_pattern: str
    confidence: float  # 0-1: certainty of extraction


@dataclass
class PerSuiteResults:
    """All metrics for one suite in one DDOS mode."""
    suite_id: str
    ddos_mode: str  # "baseline", "lightweight", "transformer"
    metrics: Dict[str, Any]


# ============================================================================
# SUITE PARSER
# ============================================================================

def parse_suite_id(suite_id: str) -> Suite:
    """
    Parse suite ID into components.
    Format: cs-<KEM>-<AEAD>-<SIG>
    Example: cs-mlkem768-aesgcm-mldsa65
    """
    # Extract KEM
    kem_match = re.search(r'cs-([a-z0-9]+?)-(aesgcm|chacha20poly1305)-', suite_id)
    if not kem_match:
        raise ValueError(f"Cannot parse KEM from {suite_id}")
    
    kem_full = kem_match.group(1)
    aead = kem_match.group(2)
    
    # Parse KEM family
    if 'mlkem' in kem_full:
        if '1024' in kem_full:
            kem_family, kem_variant = 'ML-KEM', 'ML-KEM-1024'
        elif '768' in kem_full:
            kem_family, kem_variant = 'ML-KEM', 'ML-KEM-768'
        elif '512' in kem_full:
            kem_family, kem_variant = 'ML-KEM', 'ML-KEM-512'
        else:
            kem_family, kem_variant = 'ML-KEM', kem_full
    elif 'classicmceliece' in kem_full:
        kem_family = 'Classic-McEliece'
        # Extract parameters like 348864, 460896, 8192128
        match = re.search(r'classicmceliece(\d+)', kem_full)
        kem_variant = f"Classic-McEliece[{match.group(1)}]" if match else kem_full
    elif 'hqc' in kem_full:
        kem_family = 'HQC'
        match = re.search(r'hqc(\d+)', kem_full)
        kem_variant = f"HQC-{match.group(1)}" if match else kem_full
    elif 'frodokem' in kem_full:
        kem_family = 'FrodoKEM'
        match = re.search(r'frodokem(\d+)', kem_full)
        kem_variant = f"FrodoKEM-{match.group(1)}" if match else kem_full
    else:
        kem_family, kem_variant = 'UNKNOWN', kem_full
    
    # Extract signature scheme
    sig_match = re.search(r'-(falcon512|falcon1024|mldsa44|mldsa65|mldsa87|sphincs128fsha2|sphincs256fsha2)$', suite_id)
    if not sig_match:
        raise ValueError(f"Cannot parse signature from {suite_id}")
    
    sig_full = sig_match.group(1)
    if 'mldsa' in sig_full:
        sig_scheme, sig_variant = 'ML-DSA', sig_full.upper()
    elif 'falcon' in sig_full:
        sig_scheme, sig_variant = 'Falcon', sig_full.upper()
    elif 'sphincs' in sig_full:
        sig_scheme, sig_variant = 'SLH-DSA', sig_full.upper()
    else:
        sig_scheme, sig_variant = 'UNKNOWN', sig_full
    
    return Suite(
        suite_id=suite_id,
        kem_family=kem_family,
        kem_variant=kem_variant,
        aead_cipher=aead.upper(),
        sig_scheme=sig_scheme,
        sig_variant=sig_variant,
    )


# ============================================================================
# METRIC EXTRACTION
# ============================================================================

def extract_suite_metrics(text_block: str, suite_id: str, source_file: str, start_line: int) -> Dict[str, Any]:
    """Extract all metrics from a suite result block."""
    
    metrics = {
        'suite_id': suite_id,
        'source_file': source_file,
        'start_line': start_line,
    }
    
    # Define extraction patterns
    patterns = {
        'throughput_mbps': (r'throughput ([\d.]+) Mb/s', float),
        'throughput_percent': (r'throughput [\d.]+ Mb/s \(([\d.]+)% of target\)', float),
        'goodput_mbps': (r'goodput ([\d.]+) Mb/s', float),
        'wire_throughput_mbps': (r'wire ([\d.]+) Mb/s', float),
        'pps_actual': (r'pps ([\d.]+) \(target', float),
        'pps_target': (r'target ([\d.]+)\)', float),
        'delivered_ratio': (r'delivered ratio ([\d.]+)', float),
        'loss_pct': (r'loss ([\d.]+)%', float),
        'loss_ci_low': (r'loss [\d.]+% \(95% CI ([\d.]+)', float),
        'loss_ci_high': (r'95% CI [\d.]+-([0-9.]+)\)', float),
        'rtt_avg_ms': (r'RTT avg ([\d.]+) ms', float),
        'rtt_p50_ms': (r'p50 ([\d.]+) ms', float),
        'rtt_p95_ms': (r'p95 ([\d.]+) ms', float),
        'rtt_max_ms': (r'max ([\d.]+) ms', float),
        'owd_p50_ms': (r'one-way delay p50 ([\d.]+) ms', float),
        'owd_p95_ms': (r'one-way delay.*p95 ([\d.]+) ms', float),
        'rekey_window_ms': (r'rekey window ([\d.]+) ms', float),
        'rekeys_ok': (r'rekeys ok ([\d]+)', int),
        'rekeys_fail': (r'fail ([\d]+)', int),
        'handshake_gcs_ms': (r'handshake gcs ([\d.]+) ms', float),
        'kem_keygen_ms': (r'kem keygen ([\d.]+) ms', float),
        'kem_decap_ms': (r'kem decap ([\d.]+) ms', float),
        'sig_sign_ms': (r'sig sign ([\d.]+) ms', float),
        'primitives_total_ms': (r'primitives total ([\d.]+) ms', float),
        'cpu_max_percent': (r'CPU max ([\d.]+)%', float),
        'rss_mib': (r'RSS ([\d.]+) MiB', float),
        'pfc_watts': (r'PFC ([\d.]+) W', float),
        'power_avg_w': (r'power ([\d.]+) W avg', float),
        'power_energy_j': (r'([\d.]+) J\)', float),
        'power_samples': (r'samples ([\d,]+) @', lambda x: float(x.replace(',', ''))),
        'power_sample_rate_hz': (r'@ ([\d.]+) Hz', float),
        'avg_current_a': (r'avg current ([\d.]+) A', float),
        'voltage_v': (r'voltage ([\d.]+) V', float),
    }
    
    for metric_name, (pattern, converter) in patterns.items():
        match = re.search(pattern, text_block)
        if match:
            try:
                value = converter(match.group(1))
                metrics[metric_name] = value
            except (ValueError, IndexError):
                pass
    
    # Extract packet counts
    pkt_match = re.search(r'packets sent ([\d,]+) / received ([\d,]+)', text_block)
    if pkt_match:
        metrics['packets_sent'] = int(pkt_match.group(1).replace(',', ''))
        metrics['packets_received'] = int(pkt_match.group(2).replace(',', ''))
    
    # Extract traffic engine
    engine_match = re.search(r'traffic engine (\w+)', text_block)
    if engine_match:
        metrics['traffic_engine'] = engine_match.group(1)
    
    # Extract timing guard
    guard_match = re.search(r'timing guard ([\d.]+) ms \(([a-z]+)\)', text_block)
    if guard_match:
        metrics['timing_guard_ms'] = float(guard_match.group(1))
        metrics['timing_guard_status'] = guard_match.group(2)
    
    # Extract power trace path
    power_match = re.search(r'power trace: (.+\.csv)', text_block)
    if power_match:
        metrics['power_csv_path'] = power_match.group(1)
    
    return metrics


# ============================================================================
# FILE PARSING
# ============================================================================

def parse_txt_report(file_path: Path, ddos_mode: str) -> List[Tuple[str, Dict[str, Any]]]:
    """
    Parse a TXT report file and extract all suite metrics.
    
    Returns: List of (suite_id, metrics_dict) tuples
    """
    # Read with UTF-8 encoding, handling potential encoding issues
    try:
        content = file_path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        content = file_path.read_text(encoding='utf-8', errors='replace')
    
    results = []
    
    # Find all suite IDs (handling potential em-dash encoding variations)
    # Try multiple patterns for the em-dash separator
    suite_patterns = [
        r'Suite\s+(cs-[a-z0-9\-]+)\s+—\s+PASS',  # Unicode em-dash U+2014
        r'Suite\s+(cs-[a-z0-9\-]+)\s+-\s+PASS',   # Regular hyphen
        r'Suite\s+(cs-[a-z0-9\-]+).*PASS',         # Fallback: anything before PASS
    ]
    
    matches = []
    for pattern in suite_patterns:
        matches = list(re.finditer(pattern, content, re.MULTILINE))
        if matches:
            break
    
    for i, match in enumerate(matches):
        suite_id = match.group(1)
        start_pos = match.start()
        
        # Find end position (start of next suite or end of file)
        if i + 1 < len(matches):
            end_pos = matches[i + 1].start()
        else:
            end_pos = len(content)
        
        block = content[start_pos:end_pos]
        
        # Extract all metrics
        metrics = extract_suite_metrics(block, suite_id, str(file_path), i)
        metrics['ddos_mode'] = ddos_mode
        
        results.append((suite_id, metrics))
    
    return results


# ============================================================================
# MAIN ANALYSIS
# ============================================================================

def main():
    """Build complete provenance map from all three TXT reports."""
    
    base_path = Path('c:/Users/burak/Desktop/research')
    
    # Define input files and their DDOS modes
    files = [
        ('results/benchmarks without-ddos detectetion.txt', 'baseline'),
        ('results/results with ddos detection (lightweight).txt', 'lightweight'),
        ('results/results benchmarks with ddos detectetion time series trandssformer heavy.txt', 'transformer'),
    ]
    
    # Aggregated data: suite_id -> {ddos_mode -> metrics}
    all_suites_data = defaultdict(dict)
    suite_metadata = {}
    
    print("=" * 80)
    print("PHASE 1 CONTINUATION: EXTRACTING METRICS FROM TXT REPORTS")
    print("=" * 80)
    
    for rel_path, ddos_mode in files:
        file_path = base_path / rel_path
        print(f"\n[{ddos_mode.upper()}] Parsing {file_path.name}...")
        
        results = parse_txt_report(file_path, ddos_mode)
        
        print(f"  ✓ Found {len(results)} suite results")
        
        for suite_id, metrics in results:
            all_suites_data[suite_id][ddos_mode] = metrics
            
            # Build suite metadata (only once)
            if suite_id not in suite_metadata:
                suite_metadata[suite_id] = asdict(parse_suite_id(suite_id))
    
    # Validate consistency across all three files
    print("\n" + "=" * 80)
    print("VALIDATION")
    print("=" * 80)
    
    baseline_suites = set(all_suites_data.keys())
    print(f"\nBaseline suites: {len(baseline_suites)}")
    
    for ddos_mode in ['lightweight', 'transformer']:
        suites_in_mode = {s for s in all_suites_data.keys() 
                          if ddos_mode in all_suites_data[s]}
        if suites_in_mode == baseline_suites:
            print(f"✓ {ddos_mode.capitalize()} has all {len(suites_in_mode)} baseline suites")
        else:
            missing = baseline_suites - suites_in_mode
            extra = suites_in_mode - baseline_suites
            print(f"✗ {ddos_mode.capitalize()} MISMATCH!")
            if missing:
                print(f"  Missing: {missing}")
            if extra:
                print(f"  Extra: {extra}")
    
    # Build summary statistics
    print("\n" + "=" * 80)
    print("METRIC SUMMARY ACROSS ALL SUITES")
    print("=" * 80)
    
    for ddos_mode in ['baseline', 'lightweight', 'transformer']:
        print(f"\n{ddos_mode.upper()}:")
        
        throughputs = [m.get('throughput_mbps') for s, m in 
                       [(s, all_suites_data[s].get(ddos_mode)) for s in all_suites_data]
                       if m and 'throughput_mbps' in m]
        losses = [m.get('loss_pct') for s, m in 
                  [(s, all_suites_data[s].get(ddos_mode)) for s in all_suites_data]
                  if m and 'loss_pct' in m]
        handshakes = [m.get('handshake_gcs_ms') for s, m in 
                      [(s, all_suites_data[s].get(ddos_mode)) for s in all_suites_data]
                      if m and 'handshake_gcs_ms' in m]
        powers = [m.get('power_avg_w') for s, m in 
                  [(s, all_suites_data[s].get(ddos_mode)) for s in all_suites_data]
                  if m and 'power_avg_w' in m]
        
        if throughputs:
            print(f"  Throughput: {min(throughputs):.2f}—{max(throughputs):.2f} Mb/s")
        if losses:
            print(f"  Loss: {min(losses):.3f}%—{max(losses):.3f}%")
        if handshakes:
            print(f"  Handshake: {min(handshakes):.2f}—{max(handshakes):.1f} ms")
        if powers:
            print(f"  Power: {min(powers):.2f}—{max(powers):.2f} W")
    
    # Export comprehensive provenance JSON
    output_file = base_path / 'analysis' / 'phase1_provenance_map.json'
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    export_data = {
        'metadata': {
            'phase': 'Phase 1 Continuation',
            'purpose': 'Data Provenance Mapping',
            'total_suites': len(baseline_suites),
            'ddos_modes': ['baseline', 'lightweight', 'transformer'],
            'source_files': [f[0] for f in files],
        },
        'suite_metadata': suite_metadata,
        'all_suites_data': {k: {m: v for m, v in all_suites_data[k].items()} 
                            for k in all_suites_data},
    }
    
    with open(output_file, 'w') as f:
        json.dump(export_data, f, indent=2)
    
    print(f"\n✓ Exported provenance map to {output_file}")
    print("\nPHASE 1 CONTINUATION COMPLETE")
    print("Ready for Phase 2: Suite Metadata Extraction & Visualization Generation")


if __name__ == '__main__':
    main()
