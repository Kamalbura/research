#!/usr/bin/env python3
"""Summarise saturation run artifacts for each suite.

This script inspects the JSON saturation summary emitted by the scheduler
along with the combined workbook to build a per-suite report. It can emit a
human-readable text summary or JSON suitable for further processing.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from openpyxl import load_workbook
except ImportError as exc:  # pragma: no cover
    raise SystemExit("openpyxl is required to parse the combined workbook") from exc


Numeric = Optional[float]


@dataclass
class RateSample:
    rate_mbps: float
    throughput_mbps: float
    loss_pct: float
    avg_rtt_ms: float
    min_rtt_ms: float
    max_rtt_ms: float


@dataclass
class SuiteReport:
    suite: str
    baseline_rtt_ms: Numeric = None
    saturation_point_mbps: Numeric = None
    rekey_ms: Numeric = None
    excel_path: Optional[str] = None
    rates: List[RateSample] = field(default_factory=list)
    telemetry: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_text(self) -> str:
        lines = [f"Suite: {self.suite}"]
        lines.append(f"  Baseline RTT (ms): {self._fmt_numeric(self.baseline_rtt_ms)}")
        lines.append(f"  Saturation point (Mbps): {self._fmt_numeric(self.saturation_point_mbps)}")
        lines.append(f"  Rekey duration (ms): {self._fmt_numeric(self.rekey_ms)}")
        if self.excel_path:
            lines.append(f"  Per-suite workbook: {self.excel_path}")
        if self.rates:
            lines.append("  Rates exercised:")
            for sample in sorted(self.rates, key=lambda s: s.rate_mbps):
                lines.append(
                    "    - "
                    f"{sample.rate_mbps:.1f} Mbps | thr={sample.throughput_mbps:.3f} Mbps | "
                    f"loss={sample.loss_pct:.3f}% | avg_rtt={sample.avg_rtt_ms:.3f} ms "
                    f"(min={sample.min_rtt_ms:.3f}, max={sample.max_rtt_ms:.3f})"
                )
        if self.telemetry:
            lines.append("  Telemetry summary:")
            for kind, stats in sorted(self.telemetry.items()):
                count = stats.get("count", 0)
                lines.append(f"    - {kind}: {count} samples")
                metrics = stats.get("metrics", {})
                for metric, values in sorted(metrics.items()):
                    lines.append(
                        "      "
                        f"{metric}: avg={self._fmt_numeric(values.get('avg'))} | "
                        f"min={self._fmt_numeric(values.get('min'))} | "
                        f"max={self._fmt_numeric(values.get('max'))}"
                    )
        return "\n".join(lines)

    @staticmethod
    def _fmt_numeric(value: Numeric) -> str:
        if value is None:
            return "n/a"
        return f"{value:.3f}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suite": self.suite,
            "baseline_rtt_ms": self.baseline_rtt_ms,
            "saturation_point_mbps": self.saturation_point_mbps,
            "rekey_ms": self.rekey_ms,
            "excel_path": self.excel_path,
            "rates": [
                {
                    "rate_mbps": sample.rate_mbps,
                    "throughput_mbps": sample.throughput_mbps,
                    "loss_pct": sample.loss_pct,
                    "avg_rtt_ms": sample.avg_rtt_ms,
                    "min_rtt_ms": sample.min_rtt_ms,
                    "max_rtt_ms": sample.max_rtt_ms,
                }
                for sample in self.rates
            ],
            "telemetry": self.telemetry,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract saturation run details from artifacts")
    parser.add_argument(
        "--summary-json",
        type=Path,
        default=Path("logs/auto/gcs/saturation_summary_satrun_20250930.json"),
        help="Path to saturation summary JSON file",
    )
    parser.add_argument(
        "--combined-xlsx",
        type=Path,
        default=Path("output/gcs/satrun_20250930_combined.xlsx"),
        help="Path to combined workbook",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format",
    )
    return parser.parse_args()


def load_json_summary(path: Path) -> Dict[str, Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    records: Dict[str, Dict[str, Any]] = {}
    for entry in data:
        suite = entry.get("suite")
        if not suite:
            continue
        records[suite] = {
            "baseline_rtt_ms": _coerce_float(entry.get("baseline_rtt_ms")),
            "saturation_point_mbps": _coerce_float(entry.get("saturation_point_mbps")),
            "rekey_ms": _coerce_float(entry.get("rekey_ms")),
            "excel_path": entry.get("excel_path"),
        }
    return records


def load_workbook_sheets(path: Path) -> Tuple[Dict[str, Any], Dict[str, List[dict]]]:
    workbook = load_workbook(path, data_only=True, read_only=True)
    run_info = _load_run_info(workbook)
    sheets: Dict[str, List[dict]] = {}
    for name in ("gcs_summary", "saturation_overview", "saturation_samples", "telemetry_samples"):
        if name in workbook.sheetnames:
            sheets[name] = _sheet_as_dicts(workbook[name])
        else:
            sheets[name] = []
    workbook.close()
    return run_info, sheets


def _load_run_info(workbook) -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    if "run_info" not in workbook.sheetnames:
        return info
    ws = workbook["run_info"]
    for row in ws.iter_rows(min_row=1, values_only=True):
        if not row:
            continue
        key = row[0]
        if key is None:
            continue
        value = row[1] if len(row) > 1 else None
        info[str(key)] = value
    return info


def _sheet_as_dicts(ws) -> List[dict]:
    rows: List[dict] = []
    header: List[str] = []
    for idx, row in enumerate(ws.iter_rows(values_only=True)):
        if idx == 0:
            header = [str(col).strip() if col is not None else "" for col in row]
            continue
        if not header:
            continue
        record: Dict[str, Any] = {}
        for key, value in zip(header, row):
            if key:
                record[key] = value
        if record:
            rows.append(record)
    return rows


def build_suite_reports(
    json_records: Dict[str, Dict[str, Any]],
    sheets: Dict[str, List[dict]],
) -> Dict[str, SuiteReport]:
    reports: Dict[str, SuiteReport] = {}
    for suite, payload in json_records.items():
        reports[suite] = SuiteReport(
            suite=suite,
            baseline_rtt_ms=payload.get("baseline_rtt_ms"),
            saturation_point_mbps=payload.get("saturation_point_mbps"),
            rekey_ms=payload.get("rekey_ms"),
            excel_path=payload.get("excel_path"),
        )

    samples_by_suite: Dict[str, List[dict]] = defaultdict(list)
    for sample in sheets.get("saturation_samples", []):
        suite = sample.get("suite")
        if not suite:
            continue
        samples_by_suite[suite].append(sample)

    for suite, samples in samples_by_suite.items():
        report = reports.setdefault(suite, SuiteReport(suite=suite))
        for sample in samples:
            rate = _coerce_float(sample.get("rate_mbps"))
            thr = _coerce_float(sample.get("throughput_mbps"))
            loss = _coerce_float(sample.get("loss_pct"))
            avg_rtt = _coerce_float(sample.get("avg_rtt_ms"))
            min_rtt = _coerce_float(sample.get("min_rtt_ms"))
            max_rtt = _coerce_float(sample.get("max_rtt_ms"))
            if None in (rate, thr, loss, avg_rtt, min_rtt, max_rtt):
                continue
            report.rates.append(
                RateSample(
                    rate_mbps=rate,
                    throughput_mbps=thr,
                    loss_pct=loss,
                    avg_rtt_ms=avg_rtt,
                    min_rtt_ms=min_rtt,
                    max_rtt_ms=max_rtt,
                )
            )

    telemetry_samples = sheets.get("telemetry_samples", [])
    telemetry_stats = _summarise_telemetry(telemetry_samples)
    for suite, payload in telemetry_stats.items():
        report = reports.setdefault(suite, SuiteReport(suite=suite))
        report.telemetry = payload

    overview_by_suite = {row.get("suite"): row for row in sheets.get("saturation_overview", []) if row.get("suite")}
    for suite, row in overview_by_suite.items():
        report = reports.setdefault(suite, SuiteReport(suite=suite))
        if report.baseline_rtt_ms is None:
            report.baseline_rtt_ms = _coerce_float(row.get("baseline_rtt_ms"))
        if report.saturation_point_mbps is None:
            report.saturation_point_mbps = _coerce_float(row.get("saturation_point_mbps"))
        if report.rekey_ms is None:
            report.rekey_ms = _coerce_float(row.get("rekey_ms"))
        if not report.excel_path and row.get("excel_path"):
            report.excel_path = str(row.get("excel_path"))

    return reports


def _summarise_telemetry(samples: Iterable[dict]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    summary: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(lambda: defaultdict(lambda: {"count": 0, "metrics": {}}))
    for sample in samples:
        kind = sample.get("kind") or "unknown"
        suite = (
            sample.get("suite")
            or sample.get("new_suite")
            or sample.get("old_suite")
            or sample.get("current_suite")
            or "unknown"
        )
        bucket = summary[suite][kind]
        bucket["count"] = bucket.get("count", 0) + 1
        for key, value in sample.items():
            if key in {"kind", "session_id", "peer", "source"}:
                continue
            numeric = _coerce_float(value)
            if numeric is None:
                continue
            metrics = bucket.setdefault("metrics", {})
            entry = metrics.setdefault(key, {"sum": 0.0, "count": 0, "min": numeric, "max": numeric})
            entry["sum"] += numeric
            entry["count"] += 1
            entry["min"] = min(entry["min"], numeric)
            entry["max"] = max(entry["max"], numeric)
    for suite, kinds in summary.items():
        for kind, stats in kinds.items():
            metrics = stats.get("metrics", {})
            for key, values in metrics.items():
                count = values.get("count", 0)
                avg = None
                if count:
                    avg = values["sum"] / count
                values["avg"] = avg
                del values["sum"]
                del values["count"]
    return summary


def _coerce_float(value: Any) -> Numeric:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def emit_text(run_info: Dict[str, Any], reports: Dict[str, SuiteReport]) -> str:
    session_id = run_info.get("session_id", "unknown")
    generated = run_info.get("generated_utc", "unknown")
    lines = [
        f"Session: {session_id}",
        f"Generated (UTC): {generated}",
        f"Suites discovered: {len(reports)}",
        "",
    ]
    for suite in sorted(reports):
        report = reports[suite]
        lines.append(report.to_text())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def emit_json(run_info: Dict[str, Any], reports: Dict[str, SuiteReport]) -> str:
    payload = {
        "session_id": run_info.get("session_id"),
        "generated_utc": run_info.get("generated_utc"),
        "suites": [reports[name].to_dict() for name in sorted(reports)],
    }
    return json.dumps(payload, indent=2) + "\n"


def main() -> None:
    args = parse_args()
    json_records = load_json_summary(args.summary_json)
    run_info, sheets = load_workbook_sheets(args.combined_xlsx)
    reports = build_suite_reports(json_records, sheets)
    if args.format == "json":
        output = emit_json(run_info, reports)
    else:
        output = emit_text(run_info, reports)

    results_dir = Path("results")
    results_dir.mkdir(parents=True, exist_ok=True)
    report_path = results_dir / "report.txt"
    report_path.write_text(output, encoding="utf-8")
    print(output, end="")
    print(f"[info] wrote {report_path}")


if __name__ == "__main__":
    main()
