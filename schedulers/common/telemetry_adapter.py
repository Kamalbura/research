"""Normalize raw telemetry messages from drone_follower into SuiteTelemetry snapshots.

This module provides a small, robust mapping from the various telemetry 'kinds'
published by `tools/auto/drone_follower.py` into a canonical dict shape used by
scheduler strategies. The mapping is intentionally permissive: missing fields are
coerced to sensible defaults and types are normalized.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


# Canonical keys produced by normalize_message
CANONICAL_KEYS = (
    "timestamp_ns",
    "suite",
    "cpu_percent",
    "cpu_freq_mhz",
    "cpu_temp_c",
    "mem_used_mb",
    "mem_percent",
    "power_avg_w",
    "power_energy_j",
    "pfc_last_w",
    "pfc_peak_w",
    "udp_processing_ns",
    "udp_sequence",
    "kinematics_speed_mps",
    "kinematics_altitude_m",
    "ddos_alert",
    # Heartbeat summary fields (added to support scheduler heartbeat-aware decisions)
    "heartbeat_ok",
    "heartbeat_missed_count",
    "heartbeat_last_ok_step",
)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def normalize_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw telemetry message into the canonical SuiteTelemetry dict.

    Supported input kinds (message["kind"]) are those emitted by the drone
    follower: 'system_sample', 'psutil_sample', 'power_summary', 'kinematics',
    'udp_echo_sample', 'perf_sample', 'thermal_sample', 'rekey_transition_*',
    and 'hardware_context'.

    The returned dict always contains a timestamp_ns and suite key plus any
    canonical keys found; missing keys are set to 0/None as appropriate.
    """
    kind = message.get("kind")
    payload = {"timestamp_ns": int(message.get("timestamp_ns") or 0), "suite": message.get("suite") or message.get("session_id") or "unknown"}

    if kind == "system_sample":
        payload.update(
            {
                "cpu_percent": _coerce_float(message.get("cpu_percent")),
                "cpu_freq_mhz": _coerce_float(message.get("cpu_freq_mhz")),
                "cpu_temp_c": _coerce_float(message.get("cpu_temp_c")),
                "mem_used_mb": _coerce_float(message.get("mem_used_mb")),
                "mem_percent": _coerce_float(message.get("mem_percent")),
            }
        )
    elif kind == "psutil_sample":
        payload.update(
            {
                "cpu_percent": _coerce_float(message.get("cpu_percent")),
                "mem_percent": _coerce_float(message.get("mem_percent"), 0.0),
                "mem_used_mb": _coerce_float(message.get("rss_bytes") or message.get("rss_mb"), 0.0) / (1024 * 1024) if message.get("rss_bytes") else 0.0,
            }
        )
    elif kind == "power_summary":
        payload.update(
            {
                "power_avg_w": _coerce_float(message.get("avg_power_w")),
                "power_energy_j": _coerce_float(message.get("energy_j")),
            }
        )
    elif kind == "kinematics":
        payload.update(
            {
                "kinematics_speed_mps": _coerce_float(message.get("speed_mps")),
                "kinematics_altitude_m": _coerce_float(message.get("altitude_m")),
                "pfc_last_w": _coerce_float(message.get("predicted_flight_constraint_w")),
            }
        )
    elif kind == "udp_echo_sample":
        payload.update(
            {
                "udp_processing_ns": _coerce_int(message.get("processing_ns")),
                "udp_sequence": _coerce_int(message.get("sequence")),
            }
        )
    elif kind == "rekey_transition_end":
        payload.update(
            {
                "rekey_success": bool(message.get("success")),
                "rekey_duration_ms": _coerce_float(message.get("duration_ms")),
            }
        )
    elif kind == "perf_sample":
        # keep a compact representation: instructions/cycles/cache-misses etc are
        # left to specialized readers; here we support a minimal performance
        # signal by exposing 'task-clock' if present.
        tc = message.get("task-clock") or message.get("task_clock") or message.get("task_clock_ms")
        if tc is not None:
            payload["task_clock"] = _coerce_float(tc)
    elif kind == "thermal_sample":
        payload.update({"cpu_temp_c": _coerce_float(message.get("temp_c"))})
    elif kind == "hardware_context":
        # Not used directly in scheduling decisions but keep as audit record
        payload.update({"hardware_context": message})
    elif kind == "heartbeat_summary":
        # Map heartbeat summary into convenient telemetry fields. The summary
        # payload is expected to be a dict: {"heartbeats": {source_id: {...}}}
        # We try to attribute an entry for this suite/session (using the same
        # suite/session_id keys used above). If none found, set conservative
        # defaults (heartbeat_ok=False).
        hb_map = message.get("heartbeats") or {}
        # Determine key for this telemetry record
        key = message.get("suite") or message.get("session_id") or payload.get("suite")
        hb_entry = None
        if isinstance(hb_map, dict) and key in hb_map:
            hb_entry = hb_map.get(key)

        if hb_entry is None:
            # Try best-effort: if only one entry exists, use that
            if isinstance(hb_map, dict) and len(hb_map) == 1:
                hb_entry = next(iter(hb_map.values()))

        if hb_entry:
            missed = _coerce_int(hb_entry.get("missed"), 0)
            last_ok = _coerce_int(hb_entry.get("last_ok_step"), 0)
            ok = missed == 0 and last_ok > 0
            payload.update({
                "heartbeat_ok": bool(ok),
                "heartbeat_missed_count": int(missed),
                "heartbeat_last_ok_step": int(last_ok),
            })
        else:
            payload.update({
                "heartbeat_ok": False,
                "heartbeat_missed_count": 0,
                "heartbeat_last_ok_step": 0,
            })
    else:
        # Unknown kinds: carry the raw payload under 'raw'
        payload["raw"] = message

    # Provide some convenience copies for PFC fields
    if payload.get("pfc_last_w") is None:
        pfc = message.get("predicted_flight_constraint_w") or message.get("pfc") or None
        if pfc is not None:
            payload["pfc_last_w"] = _coerce_float(pfc)

    return payload
