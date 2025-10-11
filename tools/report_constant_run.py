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
    pps: float
    target_pps: float
    goodput_mbps: float
    wire_throughput_mbps: float
    rtt_avg_ms: float
    rtt_p50_ms: float
    rtt_p95_ms: float
    rtt_max_ms: float
    owd_p50_ms: Optional[float]
    owd_p95_ms: Optional[float]
    rekey_ms: Optional[float]
    enc_out: int
    enc_in: int
    drops: int
    rekeys_ok: int
    rekeys_fail: int
    power_ok: bool
    power_request_ok: bool
    power_avg_w: Optional[float]
    power_energy_j: Optional[float]
    power_samples: Optional[int]
    power_sample_rate: Optional[float]
    power_duration_s: Optional[float]
    power_avg_current_a: Optional[float]
    power_avg_voltage_v: Optional[float]
    power_csv_path: Optional[str]
    power_note: Optional[str]
    power_error: Optional[str]
    power_fetch_status: Optional[str]
    monitor_fetch_status: Optional[str]
    cpu_max_percent: Optional[float]
    max_rss_bytes: Optional[int]
    pfc_watts: Optional[float]
    kinematics_vh: Optional[float]
    kinematics_vv: Optional[float]
    rekey_energy_mj: Optional[float]
    rekey_energy_error: Optional[str]
    handshake_role: Optional[str]
    handshake_total_ms: Optional[float]
    handshake_energy_mj: Optional[float]
    handshake_energy_error: Optional[str]
    kem_keygen_ms: Optional[float]
    kem_encaps_ms: Optional[float]
    kem_decap_ms: Optional[float]
    sig_sign_ms: Optional[float]
    sig_verify_ms: Optional[float]
    primitive_total_ms: Optional[float]
    timing_guard_ms: Optional[float]
    timing_guard_violation: bool
    clock_offset_ns: Optional[float]
    blackout_ms: Optional[float]
    gap_p99_ms: Optional[float]
    gap_max_ms: Optional[float]
    steady_gap_ms: Optional[float]
    traffic_engine: Optional[str]
    iperf3_jitter_ms: Optional[float]
    iperf3_lost_pct: Optional[float]
    iperf3_lost_packets: Optional[int]
    iperf3_report_path: Optional[str]

    @property
    def throughput_pct(self) -> Optional[float]:
        if self.target_mbps <= 0:
            return None
        return (self.throughput_mbps / self.target_mbps) * 100.0

    @property
    def max_rss_mib(self) -> Optional[float]:
        if self.max_rss_bytes is None or self.max_rss_bytes <= 0:
            return None
        return self.max_rss_bytes / (1024 * 1024)


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
        pps=_float(row.get("pps"), 0.0) or 0.0,
        target_pps=_float(row.get("target_rate_pps"), 0.0) or 0.0,
        goodput_mbps=_float(row.get("goodput_mbps"), 0.0) or 0.0,
        wire_throughput_mbps=_float(row.get("wire_throughput_mbps_est"), 0.0) or 0.0,
        rtt_avg_ms=_float(row.get("rtt_avg_ms"), 0.0) or 0.0,
        rtt_p50_ms=_float(row.get("rtt_p50_ms"), 0.0) or 0.0,
        rtt_p95_ms=_float(row.get("rtt_p95_ms"), 0.0) or 0.0,
        rtt_max_ms=_float(row.get("rtt_max_ms"), 0.0) or 0.0,
        owd_p50_ms=_float(row.get("owd_p50_ms")),
        owd_p95_ms=_float(row.get("owd_p95_ms")),
        rekey_ms=_float(row.get("rekey_ms")),
        enc_out=_int(row.get("enc_out"), 0) or 0,
        enc_in=_int(row.get("enc_in"), 0) or 0,
        drops=_int(row.get("drops"), 0) or 0,
        rekeys_ok=_int(row.get("rekeys_ok"), 0) or 0,
        rekeys_fail=_int(row.get("rekeys_fail"), 0) or 0,
        power_ok=_bool_from_field(row.get("power_capture_ok", "false")),
        power_request_ok=_bool_from_field(row.get("power_request_ok", "false")),
        power_avg_w=_float(row.get("power_avg_w")),
        power_energy_j=_float(row.get("power_energy_j")),
        power_samples=_int(row.get("power_samples")),
        power_sample_rate=_float(row.get("power_sample_rate_hz")),
        power_duration_s=_float(row.get("power_duration_s")),
        power_avg_current_a=_float(row.get("power_avg_current_a")),
        power_avg_voltage_v=_float(row.get("power_avg_voltage_v")),
        power_csv_path=row.get("power_csv_path"),
        power_note=(row.get("power_note") or None),
        power_error=(row.get("power_error") or None),
        power_fetch_status=(row.get("power_fetch_status") or None),
        monitor_fetch_status=(row.get("monitor_fetch_status") or None),
        cpu_max_percent=_float(row.get("cpu_max_percent")),
        max_rss_bytes=_int(row.get("max_rss_bytes")),
        pfc_watts=_float(row.get("pfc_watts")),
        kinematics_vh=_float(row.get("kinematics_vh")),
        kinematics_vv=_float(row.get("kinematics_vv")),
        rekey_energy_mj=_float(row.get("rekey_energy_mJ")),
        rekey_energy_error=(row.get("rekey_energy_error") or None),
        handshake_role=(row.get("handshake_role") or None),
        handshake_total_ms=_float(row.get("handshake_total_ms")),
        handshake_energy_mj=_float(row.get("handshake_energy_mJ")),
        handshake_energy_error=(row.get("handshake_energy_error") or None),
        kem_keygen_ms=_float(row.get("kem_keygen_ms")),
        kem_encaps_ms=_float(row.get("kem_encaps_ms")),
        kem_decap_ms=_float(row.get("kem_decap_ms")),
        sig_sign_ms=_float(row.get("sig_sign_ms")),
        sig_verify_ms=_float(row.get("sig_verify_ms")),
        primitive_total_ms=_float(row.get("primitive_total_ms")),
        timing_guard_ms=_float(row.get("timing_guard_ms")),
        timing_guard_violation=_bool_from_field(row.get("timing_guard_violation", "false")),
        clock_offset_ns=_float(row.get("clock_offset_ns")),
        blackout_ms=_float(row.get("blackout_ms")),
        gap_p99_ms=_float(row.get("gap_p99_ms")),
        gap_max_ms=_float(row.get("gap_max_ms")),
        steady_gap_ms=_float(row.get("steady_gap_ms")),
    traffic_engine=((row.get("traffic_engine") or "").strip() or None),
    iperf3_jitter_ms=_float(row.get("iperf3_jitter_ms")),
    iperf3_lost_pct=_float(row.get("iperf3_lost_pct")),
    iperf3_lost_packets=_int(row.get("iperf3_lost_packets")),
    iperf3_report_path=((row.get("iperf3_report_path") or "").strip() or None),
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
    goodput_parts: List[str] = [f"throughput {record.throughput_mbps:.3f} Mb/s ({pct_text})"]
    if record.goodput_mbps > 0:
        goodput_parts.append(f"goodput {record.goodput_mbps:.3f} Mb/s")
    if record.wire_throughput_mbps > 0:
        goodput_parts.append(f"wire {record.wire_throughput_mbps:.3f} Mb/s")
    rate_line = ", ".join(goodput_parts)

    pps_line = None
    if record.pps > 0 or record.target_pps > 0:
        pps_line = f"pps {record.pps:.1f}"
        if record.target_pps > 0:
            pps_line += f" (target {record.target_pps:.1f})"

    owd_parts: List[str] = []
    if record.owd_p50_ms is not None:
        owd_parts.append(f"p50 {record.owd_p50_ms:.3f} ms")
    if record.owd_p95_ms is not None:
        owd_parts.append(f"p95 {record.owd_p95_ms:.3f} ms")
    owd_text = "one-way delay " + ", ".join(owd_parts) if owd_parts else "one-way delay not captured"

    rekey_text = (
        f"rekey window {record.rekey_ms:.2f} ms"
        if record.rekey_ms is not None
        else "rekey window not reported"
    )
    if record.rekey_energy_error:
        rekey_text += f" (energy error: {record.rekey_energy_error})"
    elif record.rekey_energy_mj is not None and record.rekey_energy_mj > 0:
        rekey_text += f", energy {record.rekey_energy_mj:.3f} mJ"

    handshake_line: Optional[str] = None
    handshake_role = (record.handshake_role or "").strip()
    if record.handshake_total_ms is not None and record.handshake_total_ms > 0:
        role_prefix = f"{handshake_role} " if handshake_role else ""
        handshake_line = f"handshake {role_prefix}{record.handshake_total_ms:.3f} ms"
        if record.handshake_energy_error:
            handshake_line += f" (energy error: {record.handshake_energy_error})"
        elif record.handshake_energy_mj is not None and record.handshake_energy_mj > 0:
            handshake_line += f", energy {record.handshake_energy_mj:.3f} mJ"
    elif handshake_role:
        handshake_line = f"handshake role {handshake_role}"
    elif record.handshake_energy_error:
        handshake_line = f"handshake energy error: {record.handshake_energy_error}"

    resource_parts: List[str] = []
    if record.cpu_max_percent is not None and record.cpu_max_percent > 0:
        resource_parts.append(f"CPU max {record.cpu_max_percent:.1f}%")
    rss_mib = record.max_rss_mib
    if rss_mib is not None:
        resource_parts.append(f"RSS {rss_mib:.1f} MiB")
    if record.pfc_watts is not None and record.pfc_watts > 0:
        resource_parts.append(f"PFC {record.pfc_watts:.3f} W")
    kinematics_parts: List[str] = []
    if record.kinematics_vh is not None and record.kinematics_vh != 0:
        kinematics_parts.append(f"vh {record.kinematics_vh:.3f}")
    if record.kinematics_vv is not None and record.kinematics_vv != 0:
        kinematics_parts.append(f"vv {record.kinematics_vv:.3f}")
    if kinematics_parts:
        resource_parts.append("kinematics " + ", ".join(kinematics_parts))

    power_lines: List[str] = []

    def _append_power(text: Optional[str]) -> None:
        if text and text not in power_lines:
            power_lines.append(text)

    note_value = (record.power_note or "").strip()
    if record.power_ok and record.power_avg_w is not None and record.power_energy_j is not None:
        rate = record.power_sample_rate or 0.0
        samples = record.power_samples or 0
        duration = record.power_duration_s or 0.0
        _append_power(
            f"power {record.power_avg_w:.3f} W avg over {duration:.1f} s ({record.power_energy_j:.3f} J)"
        )
        _append_power(f"samples {samples:,} @ {rate:.1f} Hz")
        if record.power_avg_current_a is not None and record.power_avg_voltage_v is not None:
            _append_power(
                f"avg current {record.power_avg_current_a:.6f} A, voltage {record.power_avg_voltage_v:.6f} V"
            )
    elif not record.power_ok:
        reason_parts: List[str] = []
        if not record.power_request_ok:
            reason_parts.append("request failed")
        if record.power_error:
            reason_parts.append(record.power_error)
        if note_value and note_value.lower() != "ok":
            reason_parts.append(record.power_note)
        reason = f" ({'; '.join(reason_parts)})" if reason_parts else ""
        _append_power(f"power capture unavailable{reason}")
    else:
        _append_power("power metrics missing")

    if note_value and note_value.lower() != "ok":
        _append_power(f"note {record.power_note}")
    if record.power_error:
        _append_power(f"error {record.power_error}")
    if record.power_fetch_status and record.power_fetch_status.lower() != "ok":
        _append_power(f"fetch status {record.power_fetch_status}")

    lines = [
        f"Suite {record.suite} — {record.status}",
        f"  • {rate_line}",
    ]
    if pps_line:
        lines.append(f"  • {pps_line}")
    lines.extend([
        f"  • delivered ratio {record.delivered_ratio:.3f}, loss {record.loss_pct:.3f}% (95% CI {record.loss_low_pct:.3f}-{record.loss_high_pct:.3f})",
        f"  • RTT avg {record.rtt_avg_ms:.3f} ms (p50 {record.rtt_p50_ms:.3f} ms, p95 {record.rtt_p95_ms:.3f} ms, max {record.rtt_max_ms:.3f} ms)",
        f"  • {owd_text}",
        f"  • {rekey_text}",
        f"  • packets sent {record.sent:,} / received {record.received:,}; encoded {record.enc_out:,} / {record.enc_in:,}; drops {record.drops:,}",
        f"  • rekeys ok {record.rekeys_ok:,} / fail {record.rekeys_fail:,}",
    ])
    if handshake_line:
        lines.append(f"  • {handshake_line}")
    breakdown_parts: List[str] = []
    if record.kem_keygen_ms is not None:
        breakdown_parts.append(f"kem keygen {record.kem_keygen_ms:.3f} ms")
    if record.kem_encaps_ms is not None and record.kem_encaps_ms > 0:
        breakdown_parts.append(f"kem encaps {record.kem_encaps_ms:.3f} ms")
    if record.kem_decap_ms is not None and record.kem_decap_ms > 0:
        breakdown_parts.append(f"kem decap {record.kem_decap_ms:.3f} ms")
    if record.sig_sign_ms is not None and record.sig_sign_ms > 0:
        breakdown_parts.append(f"sig sign {record.sig_sign_ms:.3f} ms")
    if record.sig_verify_ms is not None and record.sig_verify_ms > 0:
        breakdown_parts.append(f"sig verify {record.sig_verify_ms:.3f} ms")
    if record.primitive_total_ms is not None and record.primitive_total_ms > 0:
        breakdown_parts.append(f"primitives total {record.primitive_total_ms:.3f} ms")
    if breakdown_parts:
        lines.append(f"  • crypto breakdown {', '.join(breakdown_parts)}")
    if resource_parts:
        lines.append(f"  • resources {', '.join(resource_parts)}")
    engine_raw = (record.traffic_engine or "").strip()
    if engine_raw:
        engine = engine_raw.lower()
        detail_parts: List[str] = []
        if record.iperf3_jitter_ms is not None and record.iperf3_jitter_ms > 0:
            detail_parts.append(f"jitter {record.iperf3_jitter_ms:.3f} ms")
        if record.iperf3_lost_pct is not None and record.iperf3_lost_pct >= 0:
            lost_text = f"loss {record.iperf3_lost_pct:.3f}%"
            if record.iperf3_lost_packets is not None and record.iperf3_lost_packets >= 0:
                lost_text += f" ({record.iperf3_lost_packets:,} packets)"
            detail_parts.append(lost_text)
        if record.iperf3_report_path:
            detail_parts.append("report captured")
        detail = ", ".join(detail_parts)
        if detail:
            lines.append(f"  • traffic engine {engine}{' — ' + detail if detail else ''}")
        else:
            lines.append(f"  • traffic engine {engine}")
    if record.timing_guard_ms is not None and record.timing_guard_ms > 0:
        guard_status = "violation" if record.timing_guard_violation else "clear"
        lines.append(f"  • timing guard {record.timing_guard_ms:.1f} ms ({guard_status})")
    elif record.timing_guard_violation:
        lines.append("  • timing guard violation detected")
    if record.blackout_ms is not None or record.gap_max_ms is not None:
        gap_bits: List[str] = []
        if record.blackout_ms is not None:
            gap_bits.append(f"blackout {record.blackout_ms:.3f} ms")
        if record.gap_max_ms is not None:
            gap_bits.append(f"gap max {record.gap_max_ms:.3f} ms")
        if record.gap_p99_ms is not None:
            gap_bits.append(f"gap p99 {record.gap_p99_ms:.3f} ms")
        if record.steady_gap_ms is not None:
            gap_bits.append(f"steady {record.steady_gap_ms:.3f} ms")
        if gap_bits:
            lines.append(f"  • framing {', '.join(gap_bits)}")
    lines.extend(f"  • {entry}" for entry in power_lines)
    if record.power_csv_path:
        lines.append(f"  • power trace: {record.power_csv_path}")
    if record.monitor_fetch_status:
        lines.append(f"  • monitor fetch: {record.monitor_fetch_status}")
    if record.clock_offset_ns is not None:
        lines.append(f"  • clock offset {record.clock_offset_ns / 1_000_000:.3f} ms")
    return "\n".join(lines)


def _write_text_summary(records: List[SuiteRecord], path: Path) -> None:
    content = "\n\n".join(_format_summary(record) for record in records)
    path.write_text(content + "\n", encoding="utf-8")


def _write_markdown_table(records: List[SuiteRecord], path: Path) -> None:
    headers = [
        "Suite",
        "Status",
        "Throughput (Mb/s)",
        "Goodput (Mb/s)",
        "Wire (Mb/s)",
        "Target (Mb/s)",
        "Target %",
        "PPS",
        "Target PPS",
        "Packets Sent",
        "Packets Rcvd",
        "Delivered",
        "Loss %",
        "Loss 95% Low",
        "Loss 95% High",
        "RTT avg (ms)",
        "RTT p50 (ms)",
        "RTT p95 (ms)",
        "RTT max (ms)",
        "OWD p50 (ms)",
        "OWD p95 (ms)",
        "Rekey (ms)",
        "Rekey Energy (mJ)",
        "Rekeys OK",
        "Rekeys Fail",
        "Drops",
        "Handshake (ms)",
        "Handshake Energy (mJ)",
        "KEM keygen (ms)",
        "KEM decap (ms)",
        "Sig sign (ms)",
        "Primitive total (ms)",
        "CPU max (%)",
        "RSS (MiB)",
        "Power (W)",
        "Energy (J)",
        "Samples",
        "Power rate (Hz)",
        "Power duration (s)",
        "Power current (A)",
        "Power voltage (V)",
        "Power fetch",
        "Monitor fetch",
        "Timing guard (ms)",
        "Timing violation",
        "Clock offset (ms)",
        "Blackout (ms)",
        "Gap p99 (ms)",
        "Gap max (ms)",
        "Steady gap (ms)",
    ]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for record in records:
        pct = record.throughput_pct
        pct_str = f"{pct:.1f}%" if pct is not None else "-"
        power_w = f"{record.power_avg_w:.3f}" if record.power_avg_w is not None else "-"
        power_j = f"{record.power_energy_j:.3f}" if record.power_energy_j is not None else "-"
        samples = f"{record.power_samples:,}" if record.power_samples is not None else "-"
        rekey = f"{record.rekey_ms:.2f}" if record.rekey_ms is not None else "-"
        rekey_energy = (
            "ERR"
            if record.rekey_energy_error
            else f"{record.rekey_energy_mj:.3f}" if record.rekey_energy_mj is not None and record.rekey_energy_mj > 0 else "-"
        )
        handshake_total = (
            f"{record.handshake_total_ms:.3f}"
            if record.handshake_total_ms is not None and record.handshake_total_ms > 0
            else "-"
        )
        handshake_energy = (
            "ERR"
            if record.handshake_energy_error
            else f"{record.handshake_energy_mj:.3f}" if record.handshake_energy_mj is not None and record.handshake_energy_mj > 0 else "-"
        )
        cpu_max = f"{record.cpu_max_percent:.1f}" if record.cpu_max_percent is not None else "-"
        rss_mib = record.max_rss_mib
        rss = f"{rss_mib:.1f}" if rss_mib is not None else "-"
        owd_p50 = f"{record.owd_p50_ms:.3f}" if record.owd_p50_ms is not None else "-"
        owd_p95 = f"{record.owd_p95_ms:.3f}" if record.owd_p95_ms is not None else "-"
        kem_keygen = f"{record.kem_keygen_ms:.3f}" if record.kem_keygen_ms is not None else "-"
        kem_decap = f"{record.kem_decap_ms:.3f}" if record.kem_decap_ms is not None and record.kem_decap_ms > 0 else "-"
        sig_sign = f"{record.sig_sign_ms:.3f}" if record.sig_sign_ms is not None and record.sig_sign_ms > 0 else "-"
        primitive_total = (
            f"{record.primitive_total_ms:.3f}" if record.primitive_total_ms is not None and record.primitive_total_ms > 0 else "-"
        )
        power_rate = f"{record.power_sample_rate:.1f}" if record.power_sample_rate is not None else "-"
        power_duration = f"{record.power_duration_s:.1f}" if record.power_duration_s is not None else "-"
        power_current = (
            f"{record.power_avg_current_a:.6f}" if record.power_avg_current_a is not None else "-"
        )
        power_voltage = (
            f"{record.power_avg_voltage_v:.6f}" if record.power_avg_voltage_v is not None else "-"
        )
        timing_guard = (
            f"{record.timing_guard_ms:.1f}" if record.timing_guard_ms is not None else "-"
        )
        timing_violation = "YES" if record.timing_guard_violation else "NO"
        clock_offset_ms = (
            f"{record.clock_offset_ns / 1_000_000:.3f}"
            if record.clock_offset_ns is not None
            else "-"
        )
        blackout = f"{record.blackout_ms:.3f}" if record.blackout_ms is not None else "-"
        gap_p99 = f"{record.gap_p99_ms:.3f}" if record.gap_p99_ms is not None else "-"
        gap_max = f"{record.gap_max_ms:.3f}" if record.gap_max_ms is not None else "-"
        steady_gap = f"{record.steady_gap_ms:.3f}" if record.steady_gap_ms is not None else "-"
        row = [
            record.suite,
            record.status,
            f"{record.throughput_mbps:.3f}",
            f"{record.goodput_mbps:.3f}" if record.goodput_mbps > 0 else "-",
            f"{record.wire_throughput_mbps:.3f}" if record.wire_throughput_mbps > 0 else "-",
            f"{record.target_mbps:.3f}",
            pct_str,
            f"{record.pps:.1f}" if record.pps > 0 else "-",
            f"{record.target_pps:.1f}" if record.target_pps > 0 else "-",
            f"{record.sent:,}",
            f"{record.received:,}",
            f"{record.delivered_ratio:.3f}",
            f"{record.loss_pct:.3f}",
            f"{record.loss_low_pct:.3f}",
            f"{record.loss_high_pct:.3f}",
            f"{record.rtt_avg_ms:.3f}",
            f"{record.rtt_p50_ms:.3f}",
            f"{record.rtt_p95_ms:.3f}",
            f"{record.rtt_max_ms:.3f}",
            owd_p50,
            owd_p95,
            rekey,
            rekey_energy,
            f"{record.rekeys_ok:,}",
            f"{record.rekeys_fail:,}",
            f"{record.drops:,}",
            handshake_total,
            handshake_energy,
            kem_keygen,
            kem_decap,
            sig_sign,
            primitive_total,
            cpu_max,
            rss,
            power_w,
            power_j,
            samples,
            power_rate,
            power_duration,
            power_current,
            power_voltage,
            record.power_fetch_status or "-",
            record.monitor_fetch_status or "-",
            timing_guard,
            timing_violation,
            clock_offset_ms,
            blackout,
            gap_p99,
            gap_max,
            steady_gap,
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
