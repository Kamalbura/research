from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Dict, List, Optional


def _read_marks(path: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    if not path.exists():
        return rows
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            for row in reader:
                if not row or row[0] in {"kind", ""}:
                    continue
                kind = row[0].strip().lower()
                try:
                    ts_val = int(row[1])
                except (IndexError, ValueError):
                    continue
                entry: Dict[str, object] = {"kind": kind, "ts": ts_val, "raw": row}
                rows.append(entry)
    except Exception:
        return []
    return rows


def _read_packets(path: Path) -> List[Dict[str, int]]:
    packets: List[Dict[str, int]] = []
    if not path.exists():
        return packets
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            recv_idx = 0
            proc_idx = 2
            if header:
                try:
                    recv_idx = header.index("recv_timestamp_ns")
                except ValueError:
                    recv_idx = 0
                try:
                    proc_idx = header.index("processing_ns")
                except ValueError:
                    proc_idx = 2
            for row in reader:
                try:
                    recv_ns = int(row[recv_idx])
                except (IndexError, ValueError):
                    continue
                proc_ns = 0
                try:
                    proc_ns = int(row[proc_idx])
                except (IndexError, ValueError):
                    proc_ns = 0
                packets.append({"recv_ns": recv_ns, "proc_ns": proc_ns})
    except Exception:
        return []
    packets.sort(key=lambda item: item["recv_ns"])
    return packets


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = pct * (len(ordered) - 1)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _find_mark_pair(
    marks: List[Dict[str, object]],
    window_start: int,
    window_end: int,
) -> Optional[Dict[str, int]]:
    current_start: Optional[Dict[str, object]] = None
    pairs: List[Dict[str, int]] = []
    for entry in marks:
        kind = entry.get("kind")
        if kind == "start":
            current_start = entry
        elif kind == "end" and current_start:
            start_ts = int(current_start.get("ts", 0))
            end_ts = int(entry.get("ts", 0))
            pairs.append({"start": start_ts, "end": end_ts})
            current_start = None
    candidate = None
    for pair in pairs:
        if pair["start"] >= window_start and pair["end"] <= window_end:
            if candidate is None or pair["start"] > candidate["start"]:
                candidate = pair
    if candidate:
        return candidate
    if pairs:
        return pairs[-1]
    return None


def _rate_kpps(packets: List[Dict[str, int]]) -> Optional[float]:
    if len(packets) < 2:
        return None
    duration_ns = packets[-1]["recv_ns"] - packets[0]["recv_ns"]
    if duration_ns <= 0:
        return None
    rate_pps = len(packets) / (duration_ns / 1_000_000_000)
    return rate_pps / 1000.0


def compute_blackout(
    session_dir: Path,
    t_mark_ns: int,
    t_ok_ns: int,
) -> Dict[str, Optional[float]]:
    packets = _read_packets(session_dir / "packet_timing.csv")
    mark_candidates = sorted(session_dir.glob("rekey_marks_*.csv"))
    marks_path = mark_candidates[-1] if mark_candidates else session_dir / "rekey_marks.csv"
    marks = _read_marks(marks_path)
    if not packets:
        return {"blackout_ms": None, "gap_max_ms": None}
    window_start = t_mark_ns - 2_000_000_000
    window_end = t_ok_ns + 2_000_000_000
    window_packets = [pkt for pkt in packets if window_start <= pkt["recv_ns"] <= window_end]
    if len(window_packets) < 3:
        return {"blackout_ms": None, "gap_max_ms": None}
    gaps = [
        (window_packets[i]["recv_ns"] - window_packets[i - 1]["recv_ns"]) / 1_000_000
        for i in range(1, len(window_packets))
    ]
    gap_max = max(gaps)
    gap_p99 = _percentile(gaps, 0.99)
    pre_start = t_mark_ns - 3_000_000_000
    pre_end = t_mark_ns - 500_000_000
    pre_packets = [pkt for pkt in packets if pre_start <= pkt["recv_ns"] < pre_end]
    pre_gaps = [
        (pre_packets[i]["recv_ns"] - pre_packets[i - 1]["recv_ns"]) / 1_000_000
        for i in range(1, len(pre_packets))
    ]
    steady_gap = _percentile(pre_gaps, 0.95) or 0.0
    blackout = max(0.0, gap_max - steady_gap)
    post_end = t_ok_ns + 3_000_000_000
    post_packets = [pkt for pkt in packets if t_ok_ns <= pkt["recv_ns"] <= post_end]
    recv_rate_before = _rate_kpps(pre_packets)
    recv_rate_after = _rate_kpps(post_packets)
    proc_values = [pkt["proc_ns"] for pkt in window_packets if pkt["proc_ns"] > 0]
    proc_p95 = _percentile([val for val in proc_values], 0.95)
    pair = _find_mark_pair(marks, window_start, window_end)
    result: Dict[str, Optional[float]] = {
        "blackout_ms": round(blackout, 3),
        "gap_max_ms": round(gap_max, 3),
        "steady_gap_ms": round(steady_gap, 3) if steady_gap is not None else None,
        "gap_p99_ms": round(gap_p99, 3) if gap_p99 is not None else None,
        "recv_rate_kpps_before": round(recv_rate_before, 3) if recv_rate_before is not None else None,
        "recv_rate_kpps_after": round(recv_rate_after, 3) if recv_rate_after is not None else None,
        "proc_ns_p95": round(proc_p95, 3) if proc_p95 is not None else None,
    }
    if pair:
        result["pair_start_ns"] = pair.get("start")
        result["pair_end_ns"] = pair.get("end")
    return result
