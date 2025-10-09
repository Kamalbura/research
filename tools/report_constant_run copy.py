#!/usr/bin/env python3
"""Generate per-suite summaries and aggregate tables for constant-rate runs."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


@dataclass
class SuiteRecord:
    suite: str
    status: str
    duration_s: float
    sent: int
    received: int
    throughput_mbps: float
    target_mbps: float
    delivered_ratio: float
    loss_pct: float
    loss_low_pct: float
    loss_high_pct: float
    rtt_avg_ms: float
    rtt_p95_ms: float
    rtt_max_ms: float
    owd_p95_ms: Optional[float]
    rekey_ms: Optional[float]
    enc_out: int
    enc_in: int
    power_ok: bool
    power_avg_w: Optional[float]
    power_energy_j: Optional[float]
    power_samples: Optional[int]
    power_sample_rate: Optional[float]
    power_duration_s: Optional[float]
    power_csv_path: Optional[str]

    @property
    def throughput_pct(self) -> Optional[float]:
        if self.target_mbps <= 0:
            return None
        return (self.throughput_mbps / self.target_mbps) * 100.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarise constant-rate run artifacts")
    parser.add_argument(
        "--summary-csv",
        type=Path,
        default=Path("logs/auto/gcs/summary.csv"),
        help="Path to gcs summary CSV produced by the scheduler",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Optional run identifier (e.g. run_1759849642) to filter rows",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory to write summaries (defaults to output/gcs/<run-id>)",
    )
    parser.add_argument(
        "--table-name",
        type=str,
        default="run_summary_table.md",
        help="Filename for the Markdown summary table",
    )
    parser.add_argument(
        "--text-name",
        type=str,
        default="run_suite_summaries.txt",
        help="Filename for the per-suite narrative summary",
    )
    return parser.parse_args()


def _read_summary_rows(summary_csv: Path) -> List[dict]:
    with summary_csv.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader)


def _detect_run_id(rows: Iterable[dict]) -> Optional[str]:
    for row in rows:
        path = row.get("power_csv_path") or ""
        for part in Path(path).parts:
            if part.startswith("run_"):
                return part
    for row in rows:
        start_ns = row.get("start_ns")
        if start_ns:
            return f"run_{start_ns}"
    return None


def _bool_from_field(value: str) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _float(value: str, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _int(value: str, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def _status_from_flag(flag: str) -> str:
    try:
        value = int(flag)
    except (TypeError, ValueError):
        value = 1
    return "PASS" if value == 0 else "FAIL"


def _row_to_record(row: dict) -> SuiteRecord:
    return SuiteRecord(
        suite=row.get("suite", "unknown"),
        status=_status_from_flag(row.get("pass")),
        duration_s=_float(row.get("duration_s"), 0.0) or 0.0,
        sent=_int(row.get("sent"), 0) or 0,
        received=_int(row.get("rcvd"), 0) or 0,
        throughput_mbps=_float(row.get("throughput_mbps"), 0.0) or 0.0,
        target_mbps=_float(row.get("target_bandwidth_mbps"), 0.0) or 0.0,
        delivered_ratio=_float(row.get("delivered_ratio"), 0.0) or 0.0,
        loss_pct=_float(row.get("loss_pct"), 0.0) or 0.0,
        loss_low_pct=_float(row.get("loss_pct_wilson_low"), 0.0) or 0.0,
        loss_high_pct=_float(row.get("loss_pct_wilson_high"), 0.0) or 0.0,
        rtt_avg_ms=_float(row.get("rtt_avg_ms"), 0.0) or 0.0,
        rtt_p95_ms=_float(row.get("rtt_p95_ms"), 0.0) or 0.0,
        rtt_max_ms=_float(row.get("rtt_max_ms"), 0.0) or 0.0,
        owd_p95_ms=_float(row.get("owd_p95_ms")),
        rekey_ms=_float(row.get("rekey_ms")),
        enc_out=_int(row.get("enc_out"), 0) or 0,
        enc_in=_int(row.get("enc_in"), 0) or 0,
        power_ok=_bool_from_field(row.get("power_capture_ok", "false")),
        power_avg_w=_float(row.get("power_avg_w")),
        power_energy_j=_float(row.get("power_energy_j")),
        power_samples=_int(row.get("power_samples")),
        power_sample_rate=_float(row.get("power_sample_rate_hz")),
        power_duration_s=_float(row.get("power_duration_s")),
        power_csv_path=row.get("power_csv_path"),
    )


def _filter_by_run(rows: List[dict], run_id: Optional[str]) -> List[dict]:
    if not run_id:
        return rows
    filtered: List[dict] = []
    for row in rows:
        path = row.get("power_csv_path", "")
        if run_id and run_id in path:
            filtered.append(row)
    return filtered


def _format_summary(record: SuiteRecord) -> str:
    pct = record.throughput_pct
    pct_text = f"{pct:.1f}% of target" if pct is not None else "target unknown"
    owd_text = (
        f"one-way delay p95 {record.owd_p95_ms:.3f} ms"
        if record.owd_p95_ms is not None
        else "one-way delay not captured"
    )
    rekey_text = (
        f"rekey window {record.rekey_ms:.2f} ms"
        if record.rekey_ms is not None
        else "rekey window not reported"
    )
    power_lines: List[str] = []
    if record.power_ok and record.power_avg_w is not None and record.power_energy_j is not None:
        rate = record.power_sample_rate or 0.0
        samples = record.power_samples or 0
        duration = record.power_duration_s or 0.0
        power_lines.append(
            f"power {record.power_avg_w:.3f} W avg over {duration:.1f} s ({record.power_energy_j:.3f} J)"
        )
        power_lines.append(
            f"samples {samples:,} @ {rate:.1f} Hz"
        )
    elif not record.power_ok:
        power_lines.append("power capture unavailable")
    else:
        power_lines.append("power metrics missing")

    lines = [
        f"Suite {record.suite} — {record.status}",
        f"  • throughput {record.throughput_mbps:.3f} Mb/s ({pct_text})",
        f"  • delivered ratio {record.delivered_ratio:.3f}, loss {record.loss_pct:.3f}% (95% CI {record.loss_low_pct:.3f}-{record.loss_high_pct:.3f})",
        f"  • RTT avg {record.rtt_avg_ms:.3f} ms (p95 {record.rtt_p95_ms:.3f} ms, max {record.rtt_max_ms:.3f} ms)",
        f"  • {owd_text}",
        f"  • {rekey_text}",
        f"  • encoded packets {record.enc_out:,} sent / {record.enc_in:,} received",
    ]
    lines.extend(f"  • {entry}" for entry in power_lines)
    if record.power_csv_path:
        lines.append(f"  • power trace: {record.power_csv_path}")
    return "\n".join(lines)


def _write_text_summary(records: List[SuiteRecord], path: Path) -> None:
    content = "\n\n".join(_format_summary(record) for record in records)
    path.write_text(content + "\n", encoding="utf-8")


def _write_markdown_table(records: List[SuiteRecord], path: Path) -> None:
    headers = [
        "Suite",
        "Status",
        "Throughput (Mb/s)",
        "Target (Mb/s)",
        "Target %",
        "Loss %",
        "RTT avg (ms)",
        "RTT max (ms)",
        "Power (W)",
        "Energy (J)",
        "Samples",
        "Rekey (ms)",
    ]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for record in records:
        pct = record.throughput_pct
        pct_str = f"{pct:.1f}%" if pct is not None else "-"
        power_w = f"{record.power_avg_w:.3f}" if record.power_avg_w is not None else "-"
        power_j = f"{record.power_energy_j:.3f}" if record.power_energy_j is not None else "-"
        samples = f"{record.power_samples:,}" if record.power_samples is not None else "-"
        rekey = f"{record.rekey_ms:.1f}" if record.rekey_ms is not None else "-"
        row = [
            record.suite,
            record.status,
            f"{record.throughput_mbps:.3f}",
            f"{record.target_mbps:.3f}",
            pct_str,
            f"{record.loss_pct:.3f}",
            f"{record.rtt_avg_ms:.3f}",
            f"{record.rtt_max_ms:.3f}",
            power_w,
            power_j,
            samples,
            rekey,
        ]
        lines.append("| " + " | ".join(row) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    rows = _read_summary_rows(args.summary_csv)
    if not rows:
        raise SystemExit(f"No rows found in {args.summary_csv}")

    run_id = args.run_id or _detect_run_id(rows)
    filtered_rows = _filter_by_run(rows, run_id)
    if not filtered_rows:
        raise SystemExit("No rows matched the requested run")

    records = sorted((_row_to_record(row) for row in filtered_rows), key=lambda item: item.suite)

    if args.output_dir is not None:
        output_dir = args.output_dir
    elif run_id is not None:
        output_dir = Path("output/gcs") / run_id
    else:
        output_dir = Path("output/gcs/latest")
    output_dir.mkdir(parents=True, exist_ok=True)

    text_path = output_dir / args.text_name
    table_path = output_dir / args.table_name

    _write_text_summary(records, text_path)
    _write_markdown_table(records, table_path)

    print(f"Wrote narrative summary -> {text_path}")
    print(f"Wrote Markdown table -> {table_path}")
    if run_id:
        print(f"Run ID: {run_id}")


if __name__ == "__main__":
    main()
