"""Merge external power meter CSV output with benchmark manifests.

For each manifest.json produced by the benchmark runner, slice the power-meter
CSV to the START/END timestamps and compute aggregate energy statistics.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def parse_args() -> argparse.Namespace:
+    parser = argparse.ArgumentParser(description="Merge benchmark manifests with external power meter CSV data")
+    parser.add_argument("--manifest-dir", required=True, help="Directory containing manifest.json files")
+    parser.add_argument("--meter-csv", required=True, help="Power meter CSV file containing timestamped power samples")
+    parser.add_argument("--time-col", default="timestamp_ns", help="Column name for sample timestamps (nanoseconds)")
+    parser.add_argument("--power-col", default="power_w", help="Column name for power samples (watts)")
+    parser.add_argument("--out", default="benchmarks/out/merged.csv", help="Output CSV path with merged statistics")
+    return parser.parse_args()
+
+
+def load_meter_samples(csv_path: Path, time_col: str, power_col: str) -> List[Dict[str, float]]:
+    rows: List[Dict[str, float]] = []
+    with csv_path.open(newline="", encoding="utf-8") as handle:
+        reader = csv.DictReader(handle)
+        if time_col not in reader.fieldnames or power_col not in reader.fieldnames:
+            raise SystemExit(f"Required columns '{time_col}' and/or '{power_col}' missing from meter CSV")
+        for row in reader:
+            try:
+                t_ns = int(row[time_col])
+                p_w = float(row[power_col])
+            except (TypeError, ValueError) as exc:
+                raise SystemExit(f"Invalid meter row: {row}") from exc
+            rows.append({"t_ns": t_ns, "p_w": p_w})
+    if not rows:
+        print("Warning: meter CSV contained no samples")
+    return rows
+
+
+def slice_samples(samples: Iterable[Dict[str, float]], start_ns: int, end_ns: int) -> List[float]:
+    return [sample["p_w"] for sample in samples if start_ns <= sample["t_ns"] < end_ns]
+
+
+def compute_stats(samples: List[float], start_ns: int, end_ns: int) -> Dict[str, Optional[float]]:
+    duration_s = (end_ns - start_ns) / 1e9
+    if not samples:
+        return {
+            "samples": 0,
+            "avg_w": None,
+            "p95_w": None,
+            "max_w": None,
+            "joules": None,
+            "dur_s": duration_s,
+        }
+
+    sorted_samples = sorted(samples)
+    avg = sum(sorted_samples) / len(sorted_samples)
+    max_val = sorted_samples[-1]
+    p95_index = max(0, min(len(sorted_samples) - 1, math.floor(0.95 * (len(sorted_samples) - 1))))
+    p95_val = sorted_samples[p95_index]
+    joules = avg * duration_s
+    return {
+        "samples": len(sorted_samples),
+        "avg_w": avg,
+        "p95_w": p95_val,
+        "max_w": max_val,
+        "joules": joules,
+        "dur_s": duration_s,
+    }
+
+
+def collect_manifests(manifest_dir: Path) -> List[Dict[str, object]]:
+    manifests = []
+    for manifest_path in manifest_dir.rglob("manifest.json"):
+        data = json.loads(manifest_path.read_text(encoding="utf-8"))
+        data["_manifest_path"] = manifest_path
+        manifests.append(data)
+    if not manifests:
+        raise SystemExit(f"No manifest.json files found under {manifest_dir}")
+    manifests.sort(key=lambda entry: (entry.get("start_wall_ns", 0), entry.get("run_id", "")))
+    return manifests
+
+
+def merge(args: argparse.Namespace) -> None:
+    meter_samples = load_meter_samples(Path(args.meter_csv), args.time_col, args.power_col)
+    manifests = collect_manifests(Path(args.manifest_dir))
+
+    output_rows: List[Dict[str, object]] = []
+    for manifest in manifests:
+        start_ns = int(manifest["start_wall_ns"])
+        end_ns = int(manifest["end_wall_ns"])
+        sliced = slice_samples(meter_samples, start_ns, end_ns)
+        stats = compute_stats(sliced, start_ns, end_ns)
+        row: Dict[str, object] = {
+            "run_id": manifest.get("run_id"),
+            "suite": manifest.get("suite"),
+            "kem": manifest.get("kem"),
+            "sig": manifest.get("sig"),
+            "aead": manifest.get("aead"),
+            "repeat_idx": manifest.get("repeat_idx"),
+            "duration_s": manifest.get("duration_s"),
+            "start_wall_ns": start_ns,
+            "end_wall_ns": end_ns,
+            "manifest_path": str(manifest.get("_manifest_path")),
+            **stats,
+        }
+        output_rows.append(row)
+
+    out_path = Path(args.out)
+    out_path.parent.mkdir(parents=True, exist_ok=True)
+    fieldnames = [
+        "run_id",
+        "suite",
+        "kem",
+        "sig",
+        "aead",
+        "repeat_idx",
+        "duration_s",
+        "start_wall_ns",
+        "end_wall_ns",
+        "samples",
+        "avg_w",
+        "p95_w",
+        "max_w",
+        "joules",
+        "dur_s",
+        "manifest_path",
+    ]
+
+    with out_path.open("w", newline="", encoding="utf-8") as handle:
+        writer = csv.DictWriter(handle, fieldnames=fieldnames)
+        writer.writeheader()
+        for row in output_rows:
+            writer.writerow(row)
+    print(f"Merged {len(output_rows)} manifest entries into {out_path}")
+
+
+def main() -> None:
+    args = parse_args()
+    merge(args)
+
+
+if __name__ == "__main__":
+    main()
