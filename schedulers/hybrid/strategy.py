"""Hybrid scheduler blending expert heuristics with RL inference."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..common.state import SchedulerContext, SchedulerDecision, TelemetryWindow
from ..common.strategy import SchedulerStrategy
from ..expert_policy.policy import ExpertPolicyStrategy, ExpertPolicyConfig
from ..rl.strategy import RlStrategy


class HybridStrategy(SchedulerStrategy):
    name = "hybrid_expert_rl"

    def __init__(
        self,
        *,
        expert_config: Optional[ExpertPolicyConfig] = None,
        rl_policy_path: Optional[Path] = None,
        handoff_confidence: float = 0.7,
        lookback_windows: int = 3,
    ) -> None:
        super().__init__(lookback_windows=lookback_windows)
        self.expert = ExpertPolicyStrategy(config=expert_config)
        self.rl = RlStrategy(policy_path=rl_policy_path)
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
            confidence = float(rl_decision.notes.get("confidence", 0.0)) if rl_decision.notes else 0.0
            if confidence >= self.handoff_confidence:
                logging.debug("Hybrid adopting RL decision confidence=%.3f >= %.3f", confidence, self.handoff_confidence)
                context.last_decision = rl_decision
                context.last_snapshot = snapshots[-1]
                return rl_decision
            logging.debug("Hybrid RL confidence %.3f below %.3f -> deferring to expert", confidence, self.handoff_confidence)

        expert_decision = self.expert.decide(context=context, telemetry=telemetry)
        if expert_decision is not None:
            context.last_decision = expert_decision
            context.last_snapshot = snapshots[-1]
            return expert_decision

        return None

    def teardown(self, context: SchedulerContext) -> None:
        self.expert.teardown(context)
        self.rl.teardown(context)


__all__ = ["HybridStrategy"]
