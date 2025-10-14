"""Reinforcement-learning inference strategy implementation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..common.state import DdosMode, SchedulerContext, SchedulerDecision, TelemetryWindow
from ..common.strategy import SchedulerStrategy
from ..common.state import SuiteTelemetry
from ..expert_policy.policy import aggregate_metrics
from .model import LinearPolicy, load_policy


class RlStrategy(SchedulerStrategy):
    name = "rl_linear_policy"

    def __init__(
        self,
        *,
        policy_path: Optional[Path] = None,
        confidence_threshold: float = 0.55,
        ddos_threshold: float = 0.6,
        lookback_windows: int = 3,
    ) -> None:
        super().__init__(lookback_windows=lookback_windows)
        self.policy_path = policy_path or default_policy_path()
        self.confidence_threshold = float(confidence_threshold)
        self.ddos_threshold = float(ddos_threshold)
        self._policy: Optional[LinearPolicy] = None

    def warmup(self, context: SchedulerContext) -> None:
        self._policy = load_policy(self.policy_path)
        logging.info(
            "Loaded RL policy suites=%s from %s",
            self._policy.suites,
            self.policy_path,
        )
        if context.initial_suite not in self._policy.suites:
            logging.warning("Initial suite %s not present in RL policy; using first entry", context.initial_suite)

    def decide(
        self,
        *,
        context: SchedulerContext,
        telemetry: TelemetryWindow,
    ) -> Optional[SchedulerDecision]:
        if self._policy is None:
            raise RuntimeError("RL policy not loaded")
        snapshots = list(telemetry.snapshots)
        if not snapshots:
            return None
        metrics = aggregate_metrics(snapshots)
        inference = self._policy.predict(metrics)
        confidence = inference.get("confidence", 0.0)
        suite_id = inference.get("suite_id", context.initial_suite)
        rate = inference.get("traffic_rate", 0.0)

        if confidence < self.confidence_threshold and context.last_decision:
            logging.debug("Confidence %.3f below threshold %.3f; keeping prior decision", confidence, self.confidence_threshold)
            return None

        ddos_score = inference.get("ddos_score", 0.0)
        ddos_mode = DdosMode.HEAVYWEIGHT if ddos_score >= self.ddos_threshold else DdosMode.LIGHTWEIGHT
        if snapshots[-1].ddos_alert:
            ddos_mode = DdosMode.HEAVYWEIGHT

        decision = SchedulerDecision(
            target_suite=suite_id,
            ddos_mode=ddos_mode,
            traffic_rate_mbps=rate,
            notes={
                "confidence": f"{confidence:.3f}",
                "ddos_score": f"{ddos_score:.3f}",
            },
        )

        if context.last_decision and decision.target_suite == context.last_decision.target_suite and decision.ddos_mode == context.last_decision.ddos_mode:
            return None

        context.last_decision = decision
        context.last_snapshot = snapshots[-1]
        return decision

    def teardown(self, context: SchedulerContext) -> None:
        context.last_decision = None


def default_policy_path() -> Path:
    default_dir = Path("models")
    default_dir.mkdir(exist_ok=True)
    default_path = default_dir / "rl_linear_policy.json"
    if not default_path.exists():
        default_path.write_text(
            """
{
  "suites": [
    "cs-mlkem512-aesgcm-mldsa44",
    "cs-mlkem768-aesgcm-mldsa65",
    "cs-mlkem1024-aesgcm-mldsa87"
  ],
  "weights": [
    [ -0.4, -0.08, -0.05, 0.02, 0.18, -0.01 ],
    [ 0.15, 0.05, 0.02, -0.01, 0.08, -0.005 ],
    [ 0.32, 0.11, 0.09, -0.02, -0.09, 0.01 ]
  ],
  "bias": [ 0.5, 0.25, 0.1 ],
  "ddos_weights": [ 0.02, 0.01, 0.015, 0.4, -0.05, 0.08 ],
  "ddos_bias": -1.2,
  "rate_table": [ 6.0, 8.0, 10.0 ]
}
""".strip(),
            encoding="utf-8",
        )
    return default_path


__all__ = ["RlStrategy", "default_policy_path"]
