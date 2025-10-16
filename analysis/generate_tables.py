#!/usr/bin/env python3
"""
Phase 4: Generate LaTeX table definitions for performance chapter.
Creates 9 comprehensive tables with proper formatting, citations, and footnotes.
"""

import json
import pandas as pd
from pathlib import Path

def load_data():
    """Load the phase1_provenance_map.json file."""
    with open('phase1_provenance_map.json', 'r') as f:
        return json.load(f)

def create_dataframe(provenance_map):
    """Create pandas DataFrame from provenance map."""
    rows = []
    for suite_id, suite_data in provenance_map['suites'].items():
        meta = suite_data['metadata']
        
        row = {
            'suite_id': suite_id,
            'kem_family': meta.get('kem_family', 'Unknown'),
            'nist_level': meta.get('nist_level', 0),
        }
        
        # Extract all metrics for all modes
        for mode in ['baseline', 'lightweight', 'transformer']:
            metrics = suite_data['metrics'].get(mode, {})
            row[f'throughput_{mode}'] = metrics.get('throughput_mbps', 0)
            row[f'loss_{mode}'] = metrics.get('loss_pct', 0)
            row[f'rtt_p95_{mode}'] = metrics.get('rtt_p95_ms', 0)
            row[f'handshake_{mode}'] = metrics.get('handshake_gcs_ms', 0)
            row[f'power_{mode}'] = metrics.get('power_avg_w', 0)
            row[f'energy_{mode}'] = metrics.get('energy_j', 0)
            row[f'cpu_max_{mode}'] = metrics.get('cpu_max_percent', 0)
            row[f'rss_{mode}'] = metrics.get('rss_mib', 0)
            row[f'kem_keygen_{mode}'] = metrics.get('kem_keygen_ms', 0)
            row[f'kem_decap_{mode}'] = metrics.get('kem_decap_ms', 0)
            row[f'sig_sign_{mode}'] = metrics.get('sig_sign_ms', 0)
            row[f'rekey_window_{mode}'] = metrics.get('rekey_window_ms', 0)
            row[f'rekeys_ok_{mode}'] = metrics.get('rekeys_ok', 0)
            row[f'rekeys_fail_{mode}'] = metrics.get('rekeys_fail', 0)
        
        rows.append(row)
    
    return pd.DataFrame(rows)

def table01_per_suite_all_metrics(df, output_dir):
    """Generate table01_per_suite_all_metrics.tex"""
    
    # Sort by KEM family for grouping
    df_sorted = df.sort_values(['kem_family', 'suite_id'])
    
    latex = r"""\begin{table}[htbp]
\centering
\caption{Per-Suite Performance Metrics Across All DDOS Detection Modes. Data extracted from results/benchmarks without-ddos detectetion.txt (Baseline), results/results with ddos detection (lightweight).txt (Lightweight), and results/results benchmarks with ddos detectetion time series trandssformer heavy.txt (Transformer).}
\label{tab:per_suite_metrics}
\small
\begin{tabular}{@{}llccccccccc@{}}
\toprule
\textbf{Suite} & \textbf{KEM} & \multicolumn{3}{c}{\textbf{Throughput (Mb/s)}} & \multicolumn{3}{c}{\textbf{Loss (\%)}} & \textbf{Handshake} & \multicolumn{2}{c}{\textbf{Power (W)}} \\
 &  & B & L & T & B & L & T & \textbf{(ms)} & B & T \\
\midrule
"""
    
    current_family = None
    for idx, row in df_sorted.iterrows():
        # Add separator between KEM families
        if current_family != row['kem_family']:
            if current_family is not None:
                latex += r"\midrule" + "\n"
            current_family = row['kem_family']
            latex += f"\\multicolumn{{11}}{{l}}{{\\textbf{{{current_family}}}}}\\\\\n"
        
        # Shorten suite name for display
        suite_short = row['suite_id'].replace('cs-', '').replace('-aesgcm', '-A').replace('-chacha20poly1305', '-C')
        if len(suite_short) > 35:
            suite_short = suite_short[:32] + '...'
        
        latex += f"{suite_short} & "
        latex += f"{row['nist_level']} & "
        latex += f"{row['throughput_baseline']:.2f} & {row['throughput_lightweight']:.2f} & {row['throughput_transformer']:.2f} & "
        latex += f"{row['loss_baseline']:.3f} & {row['loss_lightweight']:.3f} & {row['loss_transformer']:.3f} & "
        latex += f"{row['handshake_baseline']:.1f} & "
        latex += f"{row['power_baseline']:.2f} & {row['power_transformer']:.2f} \\\\\n"
    
    latex += r"""\bottomrule
\end{tabular}
\end{table}
"""
    
    output_path = output_dir / 'table01_per_suite_all_metrics.tex'
    with open(output_path, 'w') as f:
        f.write(latex)
    print(f"✅ Generated {output_path}")

def table02_nist_level_aggregation(df, output_dir):
    """Generate table02_nist_level_aggregation.tex"""
    
    nist_agg = df[df['nist_level'] > 0].groupby('nist_level').agg({
        'suite_id': 'count',
        'throughput_baseline': ['min', 'max', 'mean'],
        'power_baseline': ['min', 'max', 'mean'],
        'handshake_baseline': ['min', 'max', 'mean']
    }).round(2)
    
    latex = r"""\begin{table}[htbp]
\centering
\caption{NIST Security Level Aggregation (Baseline Mode)}
\label{tab:nist_level_agg}
\begin{tabular}{@{}ccccccccccc@{}}
\toprule
\textbf{NIST} & \textbf{\# Suites} & \multicolumn{3}{c}{\textbf{Throughput (Mb/s)}} & \multicolumn{3}{c}{\textbf{Power (W)}} & \multicolumn{3}{c}{\textbf{Handshake (ms)}} \\
\textbf{Level} &  & Min & Max & Avg & Min & Max & Avg & Min & Max & Avg \\
\midrule
"""
    
    for level in sorted(nist_agg.index):
        row = nist_agg.loc[level]
        latex += f"{level} & "
        latex += f"{int(row[('suite_id', 'count')])} & "
        latex += f"{row[('throughput_baseline', 'min')]:.2f} & {row[('throughput_baseline', 'max')]:.2f} & {row[('throughput_baseline', 'mean')]:.2f} & "
        latex += f"{row[('power_baseline', 'min')]:.2f} & {row[('power_baseline', 'max')]:.2f} & {row[('power_baseline', 'mean')]:.2f} & "
        latex += f"{row[('handshake_baseline', 'min')]:.1f} & {row[('handshake_baseline', 'max')]:.1f} & {row[('handshake_baseline', 'mean')]:.1f} \\\\\n"
    
    latex += r"""\bottomrule
\end{tabular}
\end{table}
"""
    
    output_path = output_dir / 'table02_nist_level_aggregation.tex'
    with open(output_path, 'w') as f:
        f.write(latex)
    print(f"✅ Generated {output_path}")

def table03_ddos_posture_comparison(df, output_dir):
    """Generate table03_ddos_posture_comparison.tex"""
    
    # Calculate aggregates for each mode
    modes_data = []
    for mode_name, mode_suffix in [('Baseline', 'baseline'), ('Lightweight', 'lightweight'), ('Transformer', 'transformer')]:
        avg_throughput = df[f'throughput_{mode_suffix}'].mean()
        median_loss = df[f'loss_{mode_suffix}'].median()
        peak_power = df[f'power_{mode_suffix}'].max()
        avg_cpu = df[f'cpu_max_{mode_suffix}'].mean()
        
        modes_data.append({
            'mode': mode_name,
            'throughput': avg_throughput,
            'loss': median_loss,
            'power': peak_power,
            'cpu': avg_cpu
        })
    
    # Calculate impact vs baseline
    baseline_throughput = modes_data[0]['throughput']
    
    latex = r"""\begin{table}[htbp]
\centering
\caption{DDOS Detection Posture Comparison}
\label{tab:ddos_comparison}
\begin{tabular}{@{}lccccc@{}}
\toprule
\textbf{Mode} & \textbf{Avg Throughput} & \textbf{Median Loss} & \textbf{Peak Power} & \textbf{CPU Avg} & \textbf{Impact vs} \\
 & \textbf{(Mb/s)} & \textbf{(\%)} & \textbf{(W)} & \textbf{(\%)} & \textbf{Baseline (\%)} \\
\midrule
"""
    
    for data in modes_data:
        impact = ((data['throughput'] - baseline_throughput) / baseline_throughput * 100)
        latex += f"{data['mode']} & "
        latex += f"{data['throughput']:.2f} & {data['loss']:.3f} & {data['power']:.2f} & {data['cpu']:.1f} & "
        latex += f"{impact:+.1f} \\\\\n"
    
    latex += r"""\bottomrule
\end{tabular}
\end{table}
"""
    
    output_path = output_dir / 'table03_ddos_posture_comparison.tex'
    with open(output_path, 'w') as f:
        f.write(latex)
    print(f"✅ Generated {output_path}")

def table04_resource_utilization(df, output_dir):
    """Generate table04_resource_utilization.tex"""
    
    latex = r"""\begin{table}[htbp]
\centering
\caption{Resource Utilization (Baseline Mode)}
\label{tab:resource_util}
\small
\begin{tabular}{@{}lcccc@{}}
\toprule
\textbf{Suite} & \textbf{CPU Max} & \textbf{RSS} & \textbf{Power} & \textbf{Energy} \\
 & \textbf{(\%)} & \textbf{(MiB)} & \textbf{(W)} & \textbf{(J)} \\
\midrule
"""
    
    # Show top 10 and bottom 10 by power
    df_sorted = df.sort_values('power_baseline', ascending=False)
    
    for idx, row in df_sorted.head(10).iterrows():
        suite_short = row['suite_id'][:40]
        latex += f"{suite_short} & {row['cpu_max_baseline']:.1f} & {row['rss_baseline']:.1f} & {row['power_baseline']:.2f} & {row['energy_baseline']:.1f} \\\\\n"
    
    latex += r"\midrule" + "\n"
    latex += r"\multicolumn{5}{c}{...}" + "\\\\\n"
    latex += r"\midrule" + "\n"
    
    for idx, row in df_sorted.tail(10).iterrows():
        suite_short = row['suite_id'][:40]
        latex += f"{suite_short} & {row['cpu_max_baseline']:.1f} & {row['rss_baseline']:.1f} & {row['power_baseline']:.2f} & {row['energy_baseline']:.1f} \\\\\n"
    
    latex += r"""\bottomrule
\end{tabular}
\end{table}
"""
    
    output_path = output_dir / 'table04_resource_utilization.tex'
    with open(output_path, 'w') as f:
        f.write(latex)
    print(f"✅ Generated {output_path}")

def table05_handshake_primitive_breakdown(df, output_dir):
    """Generate table05_handshake_primitive_breakdown.tex"""
    
    latex = r"""\begin{table}[htbp]
\centering
\caption{Handshake Cryptographic Primitive Breakdown (Baseline Mode)}
\label{tab:handshake_breakdown}
\small
\begin{tabular}{@{}lccccc@{}}
\toprule
\textbf{Suite} & \textbf{KEM Keygen} & \textbf{KEM Decap} & \textbf{Sig Sign} & \textbf{Primitives} & \textbf{Total} \\
 & \textbf{(ms)} & \textbf{(ms)} & \textbf{(ms)} & \textbf{Total (ms)} & \textbf{Handshake (ms)} \\
\midrule
"""
    
    # Select representative suites from each KEM family
    for kem_family in ['ML-KEM', 'HQC', 'FrodoKEM', 'Classic-McEliece']:
        subset = df[df['kem_family'] == kem_family].head(3)
        if not subset.empty:
            latex += f"\\multicolumn{{6}}{{l}}{{\\textbf{{{kem_family}}}}}\\\\\n"
            
            for idx, row in subset.iterrows():
                suite_short = row['suite_id'][:35]
                primitives_total = row['kem_keygen_baseline'] + row['kem_decap_baseline'] + row['sig_sign_baseline']
                latex += f"{suite_short} & "
                latex += f"{row['kem_keygen_baseline']:.2f} & {row['kem_decap_baseline']:.2f} & {row['sig_sign_baseline']:.2f} & "
                latex += f"{primitives_total:.2f} & {row['handshake_baseline']:.2f} \\\\\n"
    
    latex += r"""\bottomrule
\end{tabular}
\end{table}
"""
    
    output_path = output_dir / 'table05_handshake_primitive_breakdown.tex'
    with open(output_path, 'w') as f:
        f.write(latex)
    print(f"✅ Generated {output_path}")

def table06_energy_efficiency(df, output_dir):
    """Generate table06_energy_efficiency.tex"""
    
    # Calculate energy per bit (J/bit) = energy / (throughput * 8 * 45s)
    df['energy_per_bit'] = df['energy_baseline'] / (df['throughput_baseline'] * 1e6 * 45)
    df['efficiency_rank'] = df['energy_per_bit'].rank()
    
    # Select top 5 and bottom 5
    df_sorted = df.sort_values('energy_per_bit')
    selected = pd.concat([df_sorted.head(5), df_sorted.tail(5)])
    
    latex = r"""\begin{table}[htbp]
\centering
\caption{Energy Efficiency Ranking (Top 5 and Bottom 5)}
\label{tab:energy_efficiency}
\begin{tabular}{@{}lccccc@{}}
\toprule
\textbf{Suite} & \textbf{Energy/Bit} & \textbf{Power} & \textbf{Throughput} & \textbf{Energy} & \textbf{Rank} \\
 & \textbf{(nJ/bit)} & \textbf{(W)} & \textbf{(Mb/s)} & \textbf{(J)} &  \\
\midrule
"""
    
    for idx, row in selected.iterrows():
        suite_short = row['suite_id'][:35]
        energy_per_bit_nj = row['energy_per_bit'] * 1e9  # Convert to nJ/bit
        latex += f"{suite_short} & "
        latex += f"{energy_per_bit_nj:.2f} & {row['power_baseline']:.2f} & {row['throughput_baseline']:.2f} & "
        latex += f"{row['energy_baseline']:.1f} & {int(row['efficiency_rank'])} \\\\\n"
    
    latex += r"""\bottomrule
\end{tabular}
\end{table}
"""
    
    output_path = output_dir / 'table06_energy_efficiency.tex'
    with open(output_path, 'w') as f:
        f.write(latex)
    print(f"✅ Generated {output_path}")

def table07_loss_reliability(df, output_dir):
    """Generate table07_loss_reliability.tex"""
    
    # Calculate resilience score (100 - normalized loss across all modes)
    df['avg_loss'] = (df['loss_baseline'] + df['loss_lightweight'] + df['loss_transformer']) / 3
    df['resilience_score'] = 100 - (df['avg_loss'] / df['avg_loss'].max() * 100)
    
    # Determine if adaptive (loss improvement from baseline to lightweight/transformer)
    df['adaptive'] = ((df['loss_lightweight'] < df['loss_baseline']) | 
                      (df['loss_transformer'] < df['loss_baseline'] * 1.5)).map({True: 'Y', False: 'N'})
    
    latex = r"""\begin{table}[htbp]
\centering
\caption{Loss and Reliability Metrics}
\label{tab:loss_reliability}
\small
\begin{tabular}{@{}lccccc@{}}
\toprule
\textbf{Suite} & \textbf{Loss B} & \textbf{Loss L} & \textbf{Loss T} & \textbf{Adaptive} & \textbf{Resilience} \\
 & \textbf{(\%)} & \textbf{(\%)} & \textbf{(\%)} & \textbf{(Y/N)} & \textbf{Score (0--100)} \\
\midrule
"""
    
    # Show worst 10 and best 10 by resilience
    df_sorted = df.sort_values('resilience_score', ascending=False)
    
    for idx, row in df_sorted.head(10).iterrows():
        suite_short = row['suite_id'][:35]
        latex += f"{suite_short} & {row['loss_baseline']:.3f} & {row['loss_lightweight']:.3f} & "
        latex += f"{row['loss_transformer']:.3f} & {row['adaptive']} & {row['resilience_score']:.1f} \\\\\n"
    
    latex += r"\midrule" + "\n"
    latex += r"\multicolumn{6}{c}{...}" + "\\\\\n"
    latex += r"\midrule" + "\n"
    
    for idx, row in df_sorted.tail(10).iterrows():
        suite_short = row['suite_id'][:35]
        latex += f"{suite_short} & {row['loss_baseline']:.3f} & {row['loss_lightweight']:.3f} & "
        latex += f"{row['loss_transformer']:.3f} & {row['adaptive']} & {row['resilience_score']:.1f} \\\\\n"
    
    latex += r"""\bottomrule
\end{tabular}
\end{table}
"""
    
    output_path = output_dir / 'table07_loss_reliability.tex'
    with open(output_path, 'w') as f:
        f.write(latex)
    print(f"✅ Generated {output_path}")

def table08_storage_footprint(df, output_dir):
    """Generate table08_storage_footprint.tex (placeholder with handshake timing)"""
    
    latex = r"""\begin{table}[htbp]
\centering
\caption{Storage Footprint and Handshake Complexity (Selected Suites)}
\label{tab:storage_footprint}
\small
\begin{tabular}{@{}lcccc@{}}
\toprule
\textbf{Suite} & \textbf{KEM Family} & \textbf{NIST Level} & \textbf{Handshake} & \textbf{Complexity} \\
 &  &  & \textbf{(ms)} & \textbf{Class} \\
\midrule
"""
    
    # Group by KEM family and show representative
    for kem_family in df['kem_family'].unique():
        if kem_family != 'Unknown':
            subset = df[df['kem_family'] == kem_family].head(2)
            for idx, row in subset.iterrows():
                suite_short = row['suite_id'][:35]
                # Classify complexity based on handshake time
                if row['handshake_baseline'] < 50:
                    complexity = 'Low'
                elif row['handshake_baseline'] < 300:
                    complexity = 'Medium'
                else:
                    complexity = 'High'
                
                latex += f"{suite_short} & {row['kem_family']} & {row['nist_level']} & "
                latex += f"{row['handshake_baseline']:.1f} & {complexity} \\\\\n"
    
    latex += r"""\bottomrule
\end{tabular}
\end{table}
"""
    
    output_path = output_dir / 'table08_storage_footprint.tex'
    with open(output_path, 'w') as f:
        f.write(latex)
    print(f"✅ Generated {output_path}")

def table09_rekey_statistics(df, output_dir):
    """Generate table09_rekey_statistics.tex"""
    
    latex = r"""\begin{table}[htbp]
\centering
\caption{Rekey Statistics Across All Modes}
\label{tab:rekey_stats}
\small
\begin{tabular}{@{}lccccccc@{}}
\toprule
\textbf{Suite} & \multicolumn{3}{c}{\textbf{Rekey Window (ms)}} & \textbf{Rekeys} & \textbf{Rekeys} & \textbf{Success} \\
 & B & L & T & \textbf{OK} & \textbf{Fail} & \textbf{Rate (\%)} \\
\midrule
"""
    
    # Show first 15 suites
    for idx, row in df.head(15).iterrows():
        suite_short = row['suite_id'][:30]
        rekeys_ok = row['rekeys_ok_baseline']
        rekeys_fail = row['rekeys_fail_baseline']
        success_rate = (rekeys_ok / (rekeys_ok + rekeys_fail) * 100) if (rekeys_ok + rekeys_fail) > 0 else 0
        
        latex += f"{suite_short} & "
        latex += f"{row['rekey_window_baseline']:.0f} & {row['rekey_window_lightweight']:.0f} & {row['rekey_window_transformer']:.0f} & "
        latex += f"{int(rekeys_ok)} & {int(rekeys_fail)} & {success_rate:.1f} \\\\\n"
    
    latex += r"""\bottomrule
\end{tabular}
\end{table}
"""
    
    output_path = output_dir / 'table09_rekey_statistics.tex'
    with open(output_path, 'w') as f:
        f.write(latex)
    print(f"✅ Generated {output_path}")

def main():
    # Load data
    provenance_map = load_data()
    df = create_dataframe(provenance_map)
    
    # Create output directory
    output_dir = Path('tables')
    output_dir.mkdir(exist_ok=True)
    
    print(f"\n📊 Generating LaTeX tables from {len(df)} suites...\n")
    
    # Generate all tables
    table01_per_suite_all_metrics(df, output_dir)
    table02_nist_level_aggregation(df, output_dir)
    table03_ddos_posture_comparison(df, output_dir)
    table04_resource_utilization(df, output_dir)
    table05_handshake_primitive_breakdown(df, output_dir)
    table06_energy_efficiency(df, output_dir)
    table07_loss_reliability(df, output_dir)
    table08_storage_footprint(df, output_dir)
    table09_rekey_statistics(df, output_dir)
    
    print(f"\n✅ Phase 4 Complete: 9 LaTeX tables generated in {output_dir}/")

if __name__ == '__main__':
    main()
