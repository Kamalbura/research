"""Rule-based expert policy for suite selection and DDOS posture."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from core.config import CONFIG

from ..common.state import (
    DdosMode,
    SchedulerContext,
    SchedulerDecision,
    SuiteTelemetry,
    TelemetryWindow,
)
from ..common.strategy import SchedulerStrategy


@dataclass(slots=True)
class PolicyBand:
    """Single decision band linking resource envelopes to a suite."""

    name: str
    suite_id: str
    max_power_w: Optional[float] = None
    max_cpu_percent: Optional[float] = None
    max_temp_c: Optional[float] = None
    max_loss_pct: Optional[float] = None
    min_throughput_mbps: Optional[float] = None

    def matches(self, metrics: Dict[str, float]) -> bool:
        if self.max_power_w is not None and metrics.get("power_w", 0.0) > self.max_power_w:
            return False
        if self.max_cpu_percent is not None and metrics.get("cpu_percent", 0.0) > self.max_cpu_percent:
            return False
        if self.max_temp_c is not None and metrics.get("cpu_temp_c", 0.0) > self.max_temp_c:
            return False
        if self.max_loss_pct is not None and metrics.get("loss_pct", 0.0) > self.max_loss_pct:
            return False
        if self.min_throughput_mbps is not None:
            throughput = metrics.get("throughput_mbps", 0.0)
            if throughput < self.min_throughput_mbps:
                return False
        return True


@dataclass(slots=True)
class ExpertPolicyConfig:
    """Configuration for expert policy heuristics."""

    policy_bands: List[PolicyBand] = field(default_factory=list)
    loss_alert_pct: float = 2.5
    ddos_escalate_loss_pct: float = 4.0
    ddos_cooldown_seconds: float = 90.0
    battery_bins: Dict[str, float] = field(default_factory=lambda: {
        "critical": 10.0,
        "low": 25.0,
        "medium": 40.0,
    })
    ddos_heavy_suites: List[str] = field(default_factory=lambda: [
        "cs-mlkem1024-aesgcm-mldsa87",
        "cs-hqc256-aesgcm-mldsa87",
    ])


class ExpertPolicyStrategy(SchedulerStrategy):
    name = "expert_policy"

    def __init__(
        self,
        *,
        config: Optional[ExpertPolicyConfig] = None,
        lookback_windows: int = 3,
    ) -> None:
        super().__init__(lookback_windows=lookback_windows)
        self.config = config or default_expert_config()
        self._last_ddos_change_ns: Optional[int] = None

    def warmup(self, context: SchedulerContext) -> None:  # pragma: no cover - mostly IO
        # Ensure at least one policy band referencing the initial suite
        suites = {band.suite_id for band in self.config.policy_bands}
        if context.initial_suite not in suites:
            self.config.policy_bands.append(
                PolicyBand(name="initial", suite_id=context.initial_suite, max_power_w=None)
            )

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
        next_suite = self._select_suite(context, metrics)
        ddos_mode = self._select_ddos_mode(context, snapshots, metrics)

        decision = SchedulerDecision(
            target_suite=next_suite,
            ddos_mode=ddos_mode,
            notes={
                "avg_power_w": f"{metrics.get('power_w', 0.0):.3f}",
                "avg_cpu_percent": f"{metrics.get('cpu_percent', 0.0):.2f}",
                "max_temp_c": f"{metrics.get('cpu_temp_c', 0.0):.2f}",
                "loss_pct": f"{metrics.get('loss_pct', 0.0):.3f}",
            },
        )

        if context.last_decision and decisions_equivalent(context.last_decision, decision):
            return None

        context.last_decision = decision
        context.last_snapshot = snapshots[-1]
        return decision

    def teardown(self, context: SchedulerContext) -> None:  # pragma: no cover - clean up only
        context.last_decision = None

    def _select_suite(self, context: SchedulerContext, metrics: Dict[str, float]) -> str:
        for band in self.config.policy_bands:
            if band.matches(metrics):
                return band.suite_id
        return context.initial_suite

    def _select_ddos_mode(
        self,
        context: SchedulerContext,
        snapshots: List[SuiteTelemetry],
        metrics: Dict[str, float],
    ) -> DdosMode:
        latest = snapshots[-1]
        heavy_suite = latest.suite_id in self.config.ddos_heavy_suites
        loss = metrics.get("loss_pct", 0.0)
        alert = bool(latest.ddos_alert)

        if alert or loss >= self.config.ddos_escalate_loss_pct or heavy_suite:
            self._last_ddos_change_ns = latest.timestamp_ns
            return DdosMode.HEAVYWEIGHT

        if loss >= self.config.loss_alert_pct:
            self._last_ddos_change_ns = latest.timestamp_ns
            return DdosMode.LIGHTWEIGHT

        if self._last_ddos_change_ns is None:
            return DdosMode.LIGHTWEIGHT

        elapsed_s = (latest.timestamp_ns - self._last_ddos_change_ns) / 1e9
        if elapsed_s >= self.config.ddos_cooldown_seconds:
            return DdosMode.DISABLED
        return DdosMode.LIGHTWEIGHT


def aggregate_metrics(snapshots: Iterable[SuiteTelemetry]) -> Dict[str, float]:
    values: Dict[str, List[float]] = {
        "power_w": [],
        "cpu_percent": [],
        "cpu_temp_c": [],
        "loss_pct": [],
        "throughput_mbps": [],
        "rtt_ms": [],
    }
    for snap in snapshots:
        if snap.power_w is not None:
            values["power_w"].append(snap.power_w)
        if snap.cpu_percent is not None:
            values["cpu_percent"].append(snap.cpu_percent)
        if snap.cpu_temp_c is not None:
            values["cpu_temp_c"].append(snap.cpu_temp_c)
        if snap.packet_loss_pct is not None:
            values["loss_pct"].append(snap.packet_loss_pct)
        if snap.throughput_mbps is not None:
            values["throughput_mbps"].append(snap.throughput_mbps)
        if snap.rtt_ms is not None:
            values["rtt_ms"].append(snap.rtt_ms)
    metrics = {key: statistics.fmean(vals) for key, vals in values.items() if vals}
    if "cpu_temp_c" in values and values["cpu_temp_c"]:
        metrics["cpu_temp_c"] = max(values["cpu_temp_c"])
    if "loss_pct" in values and values["loss_pct"]:
        metrics["loss_pct"] = max(values["loss_pct"])
    if "rtt_ms" in values and values["rtt_ms"]:
        metrics["rtt_ms"] = statistics.fmean(values["rtt_ms"])
    metrics.setdefault("power_w", 0.0)
    metrics.setdefault("cpu_percent", 0.0)
    metrics.setdefault("cpu_temp_c", 0.0)
    metrics.setdefault("loss_pct", 0.0)
    metrics.setdefault("throughput_mbps", 0.0)
    return metrics


def decisions_equivalent(a: SchedulerDecision, b: SchedulerDecision) -> bool:
    return (
        a.target_suite == b.target_suite
        and a.ddos_mode == b.ddos_mode
        and (a.traffic_rate_mbps or 0.0) == (b.traffic_rate_mbps or 0.0)
    )


def default_expert_config() -> ExpertPolicyConfig:
    policy_bands = [
        PolicyBand(
            name="eco",
            suite_id="cs-mlkem512-aesgcm-mldsa44",
            max_power_w=4.5,
            max_cpu_percent=62.0,
            max_temp_c=58.0,
            max_loss_pct=2.0,
            min_throughput_mbps=6.0,
        ),
        PolicyBand(
            name="balanced",
            suite_id="cs-mlkem768-aesgcm-mldsa65",
            max_power_w=5.0,
            max_cpu_percent=70.0,
            max_temp_c=65.0,
            max_loss_pct=3.5,
            min_throughput_mbps=7.0,
        ),
        PolicyBand(
            name="resilient",
            suite_id="cs-mlkem1024-aesgcm-mldsa87",
            max_power_w=5.6,
            max_cpu_percent=78.0,
            max_temp_c=72.0,
            max_loss_pct=5.0,
            min_throughput_mbps=5.5,
        ),
    ]
    return ExpertPolicyConfig(policy_bands=policy_bands)


__all__ = [
    "ExpertPolicyStrategy",
    "ExpertPolicyConfig",
    "PolicyBand",
]
