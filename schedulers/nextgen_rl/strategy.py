"""Rule-backed RL scheduler scaffold with feature extraction."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from ..common.state import (
    DdosMode,
    SchedulerContext,
    SchedulerDecision,
    SuiteTelemetry,
    TelemetryWindow,
)
from ..common.strategy import SchedulerStrategy
from ..nextgen_expert.strategy import aggregate_metrics as expert_aggregate


@dataclass(slots=True)
class RlRule:
    """Single tabular rule exported by the offline RL pipeline."""

    suite_id: str
    confidence: float
    min_battery_pct: Optional[float] = None
    max_battery_pct: Optional[float] = None
    max_temp_c: Optional[float] = None
    max_power_w: Optional[float] = None
    ddos_mode: Optional[str] = None

    def matches(self, metrics: Dict[str, float]) -> bool:
        battery = metrics.get("battery_pct")
        if self.min_battery_pct is not None and battery is not None and battery < self.min_battery_pct:
            return False
        if self.max_battery_pct is not None and battery is not None and battery > self.max_battery_pct:
            return False
        if self.max_temp_c is not None and metrics.get("cpu_temp_c", 0.0) > self.max_temp_c:
            return False
        if self.max_power_w is not None and metrics.get("power_w", 0.0) > self.max_power_w:
            return False
        return True

    def as_ddos_mode(self, fallback: DdosMode) -> DdosMode:
        if self.ddos_mode is None:
            return fallback
        try:
            return DdosMode(self.ddos_mode)
        except ValueError:
            return fallback


@dataclass(slots=True)
class NextGenRlConfig:
    """Runtime configuration for the RL-driven scheduler."""

    policy_path: Optional[Path] = None
    confidence_threshold: float = 0.6
    default_suite: str = "cs-mlkem768-aesgcm-mldsa65"
    ddos_light_loss_pct: float = 1.5
    ddos_heavy_loss_pct: float = 3.5


class NextGenRlStrategy(SchedulerStrategy):
    name = "nextgen_rl"

    def __init__(
        self,
        *,
        config: Optional[NextGenRlConfig] = None,
        lookback_windows: int = 3,
    ) -> None:
        super().__init__(lookback_windows=lookback_windows)
        self.config = config or NextGenRlConfig()
        self.rules: List[RlRule] = []
        if self.config.policy_path:
            self.rules = load_rules(self.config.policy_path)

    def warmup(self, context: SchedulerContext) -> None:
        context.last_decision = None
        context.last_snapshot = None

    def decide(
        self,
        *,
        context: SchedulerContext,
        telemetry: TelemetryWindow,
    ) -> Optional[SchedulerDecision]:
        snapshots = list(telemetry.snapshots)
        if not snapshots:
            return None

        metrics = expert_aggregate(snapshots)
        decision = self._evaluate_rules(metrics, snapshots)
        if decision is None:
            return None

        if context.last_decision and _decisions_equal(context.last_decision, decision):
            return None

        context.last_decision = decision
        context.last_snapshot = snapshots[-1]
        return decision

    def teardown(self, context: SchedulerContext) -> None:
        context.last_decision = None
        context.last_snapshot = None

    def _evaluate_rules(
        self,
        metrics: Dict[str, float],
        snapshots: Sequence[SuiteTelemetry],
    ) -> Optional[SchedulerDecision]:
        rule = select_rule(self.rules, metrics, self.config.confidence_threshold)
        if rule is None:
            return None

        ddos_mode = rule.as_ddos_mode(self._fallback_ddos(metrics, snapshots))
        notes = {
            **{key: f"{value:.3f}" for key, value in metrics.items()},
            "rule_suite": rule.suite_id,
            "rule_confidence": f"{rule.confidence:.3f}",
        }
        return SchedulerDecision(target_suite=rule.suite_id, ddos_mode=ddos_mode, notes=notes)

    def _fallback_ddos(
        self,
        metrics: Dict[str, float],
        snapshots: Sequence[SuiteTelemetry],
    ) -> DdosMode:
        loss = metrics.get("loss_pct", 0.0)
        latest = snapshots[-1]
        if loss >= self.config.ddos_heavy_loss_pct:
            return DdosMode.HEAVYWEIGHT
        if loss >= self.config.ddos_light_loss_pct:
            return DdosMode.LIGHTWEIGHT
        if latest.ddos_alert:
            return DdosMode.LIGHTWEIGHT
        return DdosMode.DISABLED


def load_rules(path: Path) -> List[RlRule]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []

    rules_data = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules_data, list):
        return []

    rules: List[RlRule] = []
    for entry in rules_data:
        if not isinstance(entry, dict):
            continue
        try:
            suite_id = str(entry["suite_id"])
            confidence = float(entry.get("confidence", 0.0))
        except (KeyError, ValueError, TypeError):
            continue
        rule = RlRule(
            suite_id=suite_id,
            confidence=confidence,
            min_battery_pct=_try_float(entry.get("min_battery_pct")),
            max_battery_pct=_try_float(entry.get("max_battery_pct")),
            max_temp_c=_try_float(entry.get("max_temp_c")),
            max_power_w=_try_float(entry.get("max_power_w")),
            ddos_mode=entry.get("ddos_mode"),
        )
        rules.append(rule)
    rules.sort(key=lambda item: item.confidence, reverse=True)
    return rules


def select_rule(
    rules: Sequence[RlRule],
    metrics: Dict[str, float],
    threshold: float,
) -> Optional[RlRule]:
    for rule in rules:
        if rule.confidence < threshold:
            continue
        if rule.matches(metrics):
            return rule
    return None


def _decisions_equal(a: SchedulerDecision, b: SchedulerDecision) -> bool:
    return (
        a.target_suite == b.target_suite
        and a.ddos_mode == b.ddos_mode
        and (a.traffic_rate_mbps or 0.0) == (b.traffic_rate_mbps or 0.0)
    )


def _try_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "NextGenRlStrategy",
    "NextGenRlConfig",
    "RlRule",
    "load_rules",
    "select_rule",
]
