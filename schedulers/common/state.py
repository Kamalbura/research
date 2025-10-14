"""Shared dataclasses and helpers for scheduler implementations.

These structures describe telemetry snapshots, decisions, and runtime context.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional


class DdosMode(enum.Enum):
    """Enumeration of DDOS detector tiers used by schedulers."""

    DISABLED = "disabled"
    LIGHTWEIGHT = "lightweight"
    HEAVYWEIGHT = "heavyweight"


@dataclass(slots=True)
class SuiteTelemetry:
    """Normalized telemetry extracted from drone + GCS streams."""

    suite_id: str
    timestamp_ns: int
    battery_pct: Optional[float] = None
    battery_voltage_v: Optional[float] = None
    battery_current_a: Optional[float] = None
    cpu_percent: Optional[float] = None
    cpu_temp_c: Optional[float] = None
    power_w: Optional[float] = None
    energy_j: Optional[float] = None
    throughput_mbps: Optional[float] = None
    goodput_mbps: Optional[float] = None
    packet_loss_pct: Optional[float] = None
    rtt_ms: Optional[float] = None
    rekey_ms: Optional[float] = None
    ddos_alert: Optional[bool] = None
    counters: Dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class SchedulerDecision:
    """Decision issued by a scheduler for the next control window."""

    target_suite: str
    ddos_mode: DdosMode = DdosMode.LIGHTWEIGHT
    traffic_rate_mbps: Optional[float] = None
    notes: Dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SchedulerContext:
    """Mutable runtime context shared across decision iterations."""

    session_id: str
    role: str  # "drone" or "gcs"
    initial_suite: str
    last_decision: Optional[SchedulerDecision] = None
    last_snapshot: Optional[SuiteTelemetry] = None
    start_time_ns: int = field(default_factory=time.time_ns)

    def elapsed_seconds(self) -> float:
        return max(0.0, (time.time_ns() - self.start_time_ns) / 1e9)


@dataclass(slots=True)
class TelemetryWindow:
    """Collection of snapshots aggregated over a decision horizon."""

    snapshots: Iterable[SuiteTelemetry]
    window_start_ns: int
    window_end_ns: int


__all__ = [
    "DdosMode",
    "SuiteTelemetry",
    "SchedulerDecision",
    "SchedulerContext",
    "TelemetryWindow",
]
