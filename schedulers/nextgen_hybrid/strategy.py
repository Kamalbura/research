"""Hybrid fusion of next-generation expert and RL schedulers."""

from __future__ import annotations

from typing import Optional

from ..common.state import SchedulerContext, SchedulerDecision, TelemetryWindow
from ..common.strategy import SchedulerStrategy
from ..nextgen_expert.strategy import NextGenExpertConfig, NextGenExpertStrategy
from ..nextgen_rl.strategy import NextGenRlConfig, NextGenRlStrategy


class NextGenHybridStrategy(SchedulerStrategy):
    name = "nextgen_hybrid"

    def __init__(
        self,
        *,
        expert_config: Optional[NextGenExpertConfig] = None,
        rl_config: Optional[NextGenRlConfig] = None,
        handoff_confidence: float = 0.7,
        lookback_windows: int = 3,
    ) -> None:
        super().__init__(lookback_windows=lookback_windows)
        self.expert = NextGenExpertStrategy(config=expert_config)
        self.rl = NextGenRlStrategy(config=rl_config)
        self.handoff_confidence = float(handoff_confidence)

    def warmup(self, context: SchedulerContext) -> None:
        self.expert.warmup(context)
        self.rl.warmup(context)

    def decide(
        self,
        *,
        context: SchedulerContext,
        telemetry: TelemetryWindow,
    ) -> Optional[SchedulerDecision]:
        snapshots = list(telemetry.snapshots)
        if not snapshots:
            return None

        rl_decision = self.rl.decide(context=context, telemetry=telemetry)
        if rl_decision is not None:
            confidence = _confidence_from_notes(rl_decision)
            if confidence >= self.handoff_confidence:
                context.last_decision = rl_decision
                context.last_snapshot = snapshots[-1]
                return rl_decision

        expert_decision = self.expert.decide(context=context, telemetry=telemetry)
        if expert_decision is not None:
            context.last_decision = expert_decision
            context.last_snapshot = snapshots[-1]
            return expert_decision

        return None

    def teardown(self, context: SchedulerContext) -> None:
        self.expert.teardown(context)
        self.rl.teardown(context)


def _confidence_from_notes(decision: SchedulerDecision) -> float:
    if not decision.notes:
        return 0.0
    value = decision.notes.get("rule_confidence") or decision.notes.get("confidence")
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = ["NextGenHybridStrategy"]
