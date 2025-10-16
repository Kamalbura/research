# Performance Chapter Documentation

This directory contains the complete performance analysis for the PQC Drone↔GCS research paper.

## Files

### Main Document
- **`performance.tex`** - Complete LaTeX source for the performance chapter (22 pages)
- **`performance.pdf`** - Compiled PDF output (2.7 MB)

### Supporting Materials
All supporting analysis materials are in the `../analysis/` directory:
- `phase1_provenance_map.json` - Extracted metrics from 3 TXT benchmark reports (90 suite-mode combinations)
- `generate_visualizations_and_metadata.ipynb` - Jupyter notebook generating 15 figures
- `generate_tables.py` - Python script generating 9 LaTeX tables
- `narrative_sections.md` - 7 analytical narrative sections (~1500 words)
- `mermaid_diagrams.md` - 5 flowchart definitions
- `reproducibility_appendix.md` - Complete reproducibility documentation

### Generated Artifacts
- **Figures:** `../figures/figure01-15.png` (15 publication-quality PNG files, 300 DPI)
- **Tables:** `../analysis/tables/table01-09.tex` (9 LaTeX table definitions)

## Compilation

### Prerequisites
```bash
# LaTeX distribution (Ubuntu/Debian)
sudo apt-get install texlive-latex-base texlive-latex-extra

# Python environment for regenerating figures/tables
pip3 install matplotlib pandas seaborn numpy jupyter
```

### Compile PDF
```bash
cd docs
pdflatex performance.tex
pdflatex performance.tex  # Second pass for cross-references
```

Output: `performance.pdf` (22 pages, ~2.7 MB)

## Regenerating from Source Data

To fully reproduce all analysis artifacts from the raw benchmark TXT files:

### Step 1: Extract Provenance Map
```bash
cd /path/to/research
python3 analysis/extract_phase1_provenance.py
```
**Output:** `analysis/phase1_provenance_map.json` (30 suites × 3 modes = 90 combinations)

### Step 2: Generate Figures
```bash
jupyter nbconvert --execute --to notebook \
  analysis/generate_visualizations_and_metadata.ipynb \
  --output generate_visualizations_and_metadata_executed.ipynb
```
**Output:** 15 PNG files in `figures/` directory (300 DPI, 2.5 MB total)

### Step 3: Generate Tables
```bash
cd analysis
python3 generate_tables.py
```
**Output:** 9 `.tex` files in `analysis/tables/` directory

### Step 4: Compile Final Document
```bash
cd docs
pdflatex performance.tex
pdflatex performance.tex  # Second pass
```
**Output:** `docs/performance.pdf`

## Document Structure

### Sections
1. **Introduction** - Motivation, scope, contributions
2. **Experimental Setup** - Hardware, workload, metrics collection
3. **Baseline Performance** - Throughput, loss, reliability (no DDOS detection)
4. **Lightweight DDOS Detection** - XGBoost-based anomaly detection
5. **Heavyweight DDOS Detection** - Time Series Transformer
6. **KEM Family Comparison** - ML-KEM, FrodoKEM, HQC, Classic-McEliece
7. **Cryptographic Cost Analysis** - Handshake latency breakdown, primitive costs
8. **Power & Resource Utilization** - CPU, memory, energy consumption
9. **Loss Resilience & Adaptation** - Adaptive scheduler effectiveness
10. **Suite Recommendations** - Decision matrix for operational deployment
11. **Conclusions** - Key findings and recommended deployment strategy

### Appendices
- **A. Reproducibility** - Data sources, reconstruction procedure, checksums, known limitations

### Figures (15 total)
- `figure01-03`: Throughput per mode (baseline, lightweight, transformer)
- `figure04`: Grouped throughput comparison (all modes)
- `figure05`: Loss distribution violin plot
- `figure06`: RTT CDF
- `figure07`: Handshake latency scatter by KEM family
- `figure08-09`: Power consumption comparisons
- `figure10`: Energy heatmap (cryptographic operations)
- `figure11`: CPU utilization heatmap
- `figure12`: RSS memory heatmap
- `figure13`: Goodput ratio overlay
- `figure14`: NIST level aggregation boxplots
- `figure15`: KEM family comparison bars

### Tables (9 total)
- `table01`: Per-suite all metrics (30 rows)
- `table02`: NIST level aggregation
- `table03`: DDOS posture comparison
- `table04`: Resource utilization (top 10 + bottom 10)
- `table05`: Handshake primitive breakdown
- `table06`: Energy efficiency ranking (top 5 + bottom 5)
- `table07`: Loss reliability (top 10 + bottom 10)
- `table08`: Storage footprint
- `table09`: Rekey statistics

## Data Sources

### Primary Benchmark Reports
All performance metrics extracted from three canonical TXT files in `../results/`:
1. **`benchmarks without-ddos detectetion.txt`** - Baseline (30 suites, 629 lines)
2. **`results with ddos detection (lightweight).txt`** - XGBoost (30 suites, 629 lines)
3. **`results benchmarks with ddos detectetion time series trandssformer heavy.txt`** - Transformer (30 suites, 629 lines)

Each report contains 30 suites × 21 metrics per suite.

### Metrics Captured
Per suite: throughput (Mb/s), loss (%), RTT (avg/p50/p95/max), handshake latency (ms), crypto breakdown (KEM keygen/decap, sig sign), CPU max (%), RSS (MiB), power (W), energy (J), rekey window (ms), rekeys ok/fail, packets sent/received.

## Key Findings

### Performance Summary
- **Best Throughput:** ML-KEM768-aesgcm-mldsa65 (7.83-7.94 Mb/s, 98-99% efficiency)
- **Fastest Handshake:** ML-KEM1024-chacha20-falcon1024 (4.22 ms under transformer mode)
- **Lowest Loss:** ML-KEM suites (<0.2% under all DDOS modes)
- **Highest Loss:** CS-classicmceliece348864-aesgcm (6.447% under transformer - critical failure)

### Recommended Suites
1. **Real-time control (<20 ms):** ML-KEM768-aesgcm-mldsa65
2. **NIST Level 5 mandatory:** ML-KEM1024-chacha20-mldsa87
3. **Conservative/high assurance:** FrodoKEM976-aesgcm-mldsa65
4. **AVOID:** Classic-McEliece (1.6+ s handshake), HQC (3.1-3.3% loss)

### DDOS Detection Trade-offs
- **Lightweight (XGBoost):** +0.02-0.16 W (+0.5-4%), modest throughput gains
- **Heavyweight (Transformer):** +0.35-0.46 W (+10-11%), 15× loss reduction for ML-KEM

## Validation

### Checksums (SHA256)
```
667b97ab26682e7a2314e7c6bec3c77cffe3d8586a0e3605b002825a1c979ef1  phase1_provenance_map.json
2af4910365670a876cabe5db8184f8e3cc29e802caa32e426387876035210fc9  generate_visualizations_and_metadata.ipynb
7415d4c0b0c964779d87e02ef435123b540e1e06259c59cb62f5110b6c7e33e2  generate_tables.py
```

### Known Limitations
1. **RTT Loopback:** Measurements from loopback interface; does not represent wireless propagation delay
2. **Handshake Timing:** GCS-side only; drone-side primitive costs estimated at 10-15% of total
3. **Power Traces:** Per-operation energy estimated from average power × duration
4. **Baseline Blackouts:** Blackout metrics for run_1760308685 unavailable

## Citation

If using this performance analysis in academic work, please cite:

```bibtex
@article{pqc_drone_gcs_performance,
  title={PQC Drone--GCS Secure Proxy: Performance \& Reliability Analysis},
  year={2025},
  note={Post-Quantum Cryptography Performance Evaluation for UAV-GCS Communication}
}
```

## License

See repository root LICENSE file for terms.

## Contact

For questions about the performance analysis methodology or reproducibility issues, please open an issue in the repository.
