from __future__ import annotations

from typing import Any, Dict, Optional

from .base import Strategy, StrategyContext
from .expert import ExpertStrategy
from .rl import RlStrategy


class HybridStrategy(Strategy):
    def __init__(self) -> None:
        self._expert = ExpertStrategy()
        self._rl = RlStrategy()

    def warmup(self, ctx: StrategyContext) -> None:
        self._expert.warmup(ctx)
        self._rl.warmup(ctx)

    def decide(self, features: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        expert = self._expert.decide(features)
        rl = self._rl.decide(features)
        if rl and expert:
            # Prefer RL if confidence is high, else expert
            rl_conf = float(rl.get("notes", {}).get("confidence", 0.0))
            return rl if rl_conf >= 0.75 else expert
        return rl or expert
