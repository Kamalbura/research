"""Battery- and thermal-aware expert scheduler foundation."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from ..common.state import (
    DdosMode,
    SchedulerContext,
    SchedulerDecision,
    SuiteTelemetry,
    TelemetryWindow,
)
from ..common.strategy import SchedulerStrategy


@dataclass(slots=True)
class DecisionBand:
    """Tuple describing the resource envelope for a suite."""

    suite_id: str
    battery_min_pct: Optional[float] = None
    battery_max_pct: Optional[float] = None
    power_max_w: Optional[float] = None
    cpu_percent_max: Optional[float] = None
    cpu_temp_max_c: Optional[float] = None
    throughput_min_mbps: Optional[float] = None
    loss_max_pct: Optional[float] = None

    def matches(self, metrics: Dict[str, float]) -> bool:
        battery = metrics.get("battery_pct")
        if self.battery_min_pct is not None and battery is not None and battery < self.battery_min_pct:
            return False
        if self.battery_max_pct is not None and battery is not None and battery > self.battery_max_pct:
            return False
        if self.power_max_w is not None and metrics.get("power_w", 0.0) > self.power_max_w:
            return False
        if self.cpu_percent_max is not None and metrics.get("cpu_percent", 0.0) > self.cpu_percent_max:
            return False
        if self.cpu_temp_max_c is not None and metrics.get("cpu_temp_c", 0.0) > self.cpu_temp_max_c:
            return False
        if self.throughput_min_mbps is not None:
            throughput = metrics.get("throughput_mbps")
            if throughput is None or throughput < self.throughput_min_mbps:
                return False
        if self.loss_max_pct is not None and metrics.get("loss_pct", 0.0) > self.loss_max_pct:
            return False
        return True


@dataclass(slots=True)
class NextGenExpertConfig:
    """Configuration knobs for the next-generation expert heuristic."""

    bands: List[DecisionBand] = field(default_factory=list)
    fallback_suite: str = "cs-mlkem768-aesgcm-mldsa65"
    ddos_light_loss_pct: float = 1.5
    ddos_heavy_loss_pct: float = 3.0
    ddos_alert_hold_s: float = 60.0


class NextGenExpertStrategy(SchedulerStrategy):
    name = "nextgen_expert"

    def __init__(
        self,
        *,
        config: Optional[NextGenExpertConfig] = None,
        lookback_windows: int = 3,
    ) -> None:
        super().__init__(lookback_windows=lookback_windows)
        self.config = config or default_expert_config()
        self._last_alert_ns: Optional[int] = None

    def warmup(self, context: SchedulerContext) -> None:
        suites = {band.suite_id for band in self.config.bands}
        if context.initial_suite not in suites:
            self.config.bands.insert(0, DecisionBand(suite_id=context.initial_suite))

    def decide(
        self,
        *,
        context: SchedulerContext,
        telemetry: TelemetryWindow,
    ) -> Optional[SchedulerDecision]:
        snapshots = list(telemetry.snapshots)
        if not snapshots:
            return None

        metrics = aggregate_metrics(snapshots)
        target_suite = self._select_suite(context, metrics)
        ddos_mode = self._select_ddos_mode(metrics, snapshots)

        decision = SchedulerDecision(target_suite=target_suite, ddos_mode=ddos_mode, notes=format_metrics(metrics))

        if context.last_decision and _decisions_equal(context.last_decision, decision):
            return None

        context.last_decision = decision
        context.last_snapshot = snapshots[-1]
        return decision

    def teardown(self, context: SchedulerContext) -> None:
        context.last_decision = None
        context.last_snapshot = None

    def _select_suite(self, context: SchedulerContext, metrics: Dict[str, float]) -> str:
        for band in self.config.bands:
            if band.matches(metrics):
                return band.suite_id
        return context.initial_suite or self.config.fallback_suite

    def _select_ddos_mode(
        self,
        metrics: Dict[str, float],
        snapshots: Sequence[SuiteTelemetry],
    ) -> DdosMode:
        loss = metrics.get("loss_pct", 0.0)
        latest = snapshots[-1]
        alert = bool(latest.ddos_alert)

        if alert or loss >= self.config.ddos_heavy_loss_pct:
            self._last_alert_ns = latest.timestamp_ns
            return DdosMode.HEAVYWEIGHT

        if loss >= self.config.ddos_light_loss_pct:
            self._last_alert_ns = latest.timestamp_ns
            return DdosMode.LIGHTWEIGHT

        if self._last_alert_ns is None:
            return DdosMode.LIGHTWEIGHT

        elapsed = (latest.timestamp_ns - self._last_alert_ns) / 1e9
        if elapsed >= self.config.ddos_alert_hold_s:
            return DdosMode.DISABLED
        return DdosMode.LIGHTWEIGHT


def aggregate_metrics(snapshots: Iterable[SuiteTelemetry]) -> Dict[str, float]:
    buckets: Dict[str, List[float]] = {
        "battery_pct": [],
        "battery_voltage_v": [],
        "battery_current_a": [],
        "cpu_percent": [],
        "cpu_temp_c": [],
        "power_w": [],
        "throughput_mbps": [],
        "loss_pct": [],
        "rtt_ms": [],
    }

    first = None
    last = None
    for snap in snapshots:
        if first is None:
            first = snap
        last = snap
        if snap.battery_pct is not None:
            buckets["battery_pct"].append(snap.battery_pct)
        if snap.battery_voltage_v is not None:
            buckets["battery_voltage_v"].append(snap.battery_voltage_v)
        if snap.battery_current_a is not None:
            buckets["battery_current_a"].append(snap.battery_current_a)
        if snap.cpu_percent is not None:
            buckets["cpu_percent"].append(snap.cpu_percent)
        if snap.cpu_temp_c is not None:
            buckets["cpu_temp_c"].append(snap.cpu_temp_c)
        if snap.power_w is not None:
            buckets["power_w"].append(snap.power_w)
        if snap.throughput_mbps is not None:
            buckets["throughput_mbps"].append(snap.throughput_mbps)
        if snap.packet_loss_pct is not None:
            buckets["loss_pct"].append(snap.packet_loss_pct)
        if snap.rtt_ms is not None:
            buckets["rtt_ms"].append(snap.rtt_ms)

    metrics: Dict[str, float] = {}
    for key, values in buckets.items():
        if not values:
            continue
        if key == "cpu_temp_c":
            metrics[key] = max(values)
        elif key == "loss_pct":
            metrics[key] = max(values)
        else:
            metrics[key] = statistics.fmean(values)

    if first and last and last.timestamp_ns > first.timestamp_ns:
        dt = (last.timestamp_ns - first.timestamp_ns) / 1e9
        if dt > 0:
            if first.battery_pct is not None and last.battery_pct is not None:
                metrics["battery_pct_slope"] = (last.battery_pct - first.battery_pct) / dt
            if first.cpu_temp_c is not None and last.cpu_temp_c is not None:
                metrics["cpu_temp_c_slope"] = (last.cpu_temp_c - first.cpu_temp_c) / dt
            if first.power_w is not None and last.power_w is not None:
                metrics["power_w_slope"] = (last.power_w - first.power_w) / dt

    return metrics


def format_metrics(metrics: Dict[str, float]) -> Dict[str, str]:
    view = {}
    for key, value in metrics.items():
        view[key] = f"{value:.3f}"
    return view


def _decisions_equal(a: SchedulerDecision, b: SchedulerDecision) -> bool:
    return (
        a.target_suite == b.target_suite
        and a.ddos_mode == b.ddos_mode
        and (a.traffic_rate_mbps or 0.0) == (b.traffic_rate_mbps or 0.0)
    )


def default_expert_config() -> NextGenExpertConfig:
    return NextGenExpertConfig(
        bands=[
            DecisionBand(
                suite_id="cs-mlkem512-aesgcm-mldsa44",
                battery_min_pct=30.0,
                power_max_w=4.6,
                cpu_percent_max=65.0,
                cpu_temp_max_c=60.0,
                throughput_min_mbps=6.0,
                loss_max_pct=2.5,
            ),
            DecisionBand(
                suite_id="cs-mlkem768-aesgcm-mldsa65",
                battery_min_pct=20.0,
                power_max_w=5.2,
                cpu_percent_max=72.0,
                cpu_temp_max_c=68.0,
                throughput_min_mbps=7.0,
                loss_max_pct=3.5,
            ),
            DecisionBand(
                suite_id="cs-mlkem1024-aesgcm-mldsa87",
                battery_min_pct=12.0,
                power_max_w=5.8,
                cpu_percent_max=82.0,
                cpu_temp_max_c=74.0,
                throughput_min_mbps=5.5,
                loss_max_pct=5.0,
            ),
        ],
        fallback_suite="cs-mlkem768-aesgcm-mldsa65",
        ddos_light_loss_pct=1.8,
        ddos_heavy_loss_pct=4.0,
        ddos_alert_hold_s=120.0,
    )


__all__ = [
    "DecisionBand",
    "NextGenExpertConfig",
    "NextGenExpertStrategy",
    "aggregate_metrics",
]
