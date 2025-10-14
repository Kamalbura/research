#!/usr/bin/env python3
"""Fuse GCS summary.csv, telemetry samples, and power artifacts into a master dataset.

- Reads logs/auto/gcs/summary.csv
- For each row, resolves telemetry_status_path and power_summary_path/csv_path
- Loads power CSV if present to derive peak/gradients; aligns by timestamp_ns
- Emits a flattened Parquet and CSV under output/datasets/<run_id>/

Usage:
  python -m tools.dataset.build_master_dataset --summary logs/auto/gcs/summary.csv \
      --out output/datasets/run_YYYYmmdd_HHMMSS
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


try:
    import pandas as pd  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    pd = None  # type: ignore


@dataclass
class Inputs:
    summary_csv: Path
    output_dir: Path


def _read_summary_rows(path: Path) -> List[dict]:
    rows: List[dict] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def _safe_float(v: object) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _load_power_trace(csv_path: Optional[str]) -> Optional["pd.DataFrame"]:
    if not pd or not csv_path:
        return None
    p = Path(csv_path)
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p)
        # Ensure expected columns
        for col in ("timestamp_ns", "current_a", "voltage_v", "power_w"):
            if col not in df.columns:
                return None
        return df
    except Exception:
        return None


def _load_telemetry_status(path: Optional[str]) -> Dict[str, object]:
    if not path:
        return {}
    p = Path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _derive_power_features(df: "pd.DataFrame") -> Dict[str, float]:
    if df is None or df.empty:
        return {}
    out: Dict[str, float] = {}
    out["power_w_mean"] = float(df["power_w"].mean())
    out["power_w_p95"] = float(df["power_w"].quantile(0.95))
    out["power_w_max"] = float(df["power_w"].max())
    # Gradient: linear fit over time (approximate dP/dt)
    try:
        t = (df["timestamp_ns"] - df["timestamp_ns"].iloc[0]) / 1e9
        p = df["power_w"]
        coeffs = pd.Series(t).cov(p) / pd.Series(t).var() if pd.Series(t).var() != 0 else 0.0
        out["power_w_gradient"] = float(coeffs)
    except Exception:
        pass
    return out


def build(inputs: Inputs) -> Tuple[Path, Optional[Path]]:
    rows = _read_summary_rows(inputs.summary_csv)
    inputs.output_dir.mkdir(parents=True, exist_ok=True)
    flat_records: List[Dict[str, object]] = []

    for r in rows:
        rec: Dict[str, object] = dict(r)
        # Coerce common numeric fields
        for k in (
            "throughput_mbps",
            "goodput_mbps",
            "delivered_ratio",
            "loss_pct",
            "blackout_ms",
            "rekey_ms",
            "power_avg_w",
            "power_energy_j",
            "power_duration_s",
        ):
            if k in rec:
                rec[k] = _safe_float(rec[k])

        # Telemetry heartbeat context
        telem_status = _load_telemetry_status(r.get("telemetry_status_path"))
        if telem_status:
            rec["telemetry_connected"] = bool(telem_status.get("connected_once"))
            rec["telemetry_active_clients"] = int(telem_status.get("active_clients", 0))

        # Load power JSON summary if present to fill gaps
        power_json = r.get("power_summary_path")
        if power_json and Path(power_json).exists():
            try:
                pj = json.loads(Path(power_json).read_text(encoding="utf-8"))
            except Exception:
                pj = {}
            for k in (
                "avg_power_w",
                "energy_j",
                "duration_s",
                "samples",
                "avg_current_a",
                "avg_voltage_v",
                "sample_rate_hz",
            ):
                if k in pj and rec.get(f"power_{k}") in (None, "", 0, 0.0):
                    rec[f"power_{k}"] = pj.get(k)

        # Load power CSV trace to derive peaks and gradients
        power_csv = r.get("power_csv_path")
        df = _load_power_trace(power_csv)
        if df is not None and pd is not None:
            feats = _derive_power_features(df)
            rec.update(feats)

        # Optional battery binning if percentage present (from MAVLink ingestion)
        pct = _safe_float(r.get("battery_pct") or rec.get("battery_pct"))
        if pct is not None:
            # 100-90, 90-80, ..., 20-10, <10 => "<10"
            b = int(pct // 10) * 10
            if b < 10:
                rec["battery_bin"] = "<10"
            elif b >= 90:
                rec["battery_bin"] = "90-100"
            else:
                rec["battery_bin"] = f"{b}-{b+10}"

        flat_records.append(rec)

    # Write CSV always
    csv_out = inputs.output_dir / "master_dataset.csv"
    if flat_records:
        # Determine stable field order
        all_keys: List[str] = []
        for rec in flat_records:
            for k in rec.keys():
                if k not in all_keys:
                    all_keys.append(k)
        with csv_out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=all_keys)
            writer.writeheader()
            writer.writerows(flat_records)

    parquet_out: Optional[Path] = None
    if pd is not None and flat_records:
        try:
            df = pd.DataFrame(flat_records)
            parquet_out = inputs.output_dir / "master_dataset.parquet"
            df.to_parquet(parquet_out, index=False)
        except Exception:
            parquet_out = None

    return csv_out, parquet_out


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Build master dataset from GCS run outputs")
    p.add_argument("--summary", type=Path, default=Path("logs/auto/gcs/summary.csv"))
    p.add_argument("--out", type=Path, default=Path("output/datasets/latest"))
    args = p.parse_args(argv)
    csv_out, pq_out = build(Inputs(summary_csv=args.summary, output_dir=args.out))
    print(f"wrote {csv_out}")
    if pq_out:
        print(f"wrote {pq_out}")
    else:
        print("parquet not written (pandas/pyarrow missing?)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
