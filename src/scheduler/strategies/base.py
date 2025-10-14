from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class StrategyContext:
    session_id: str
    role: str
    initial_suite: str


class Strategy:
    def warmup(self, ctx: StrategyContext) -> None:  # pragma: no cover - thin adapter
        pass

    def decide(self, features: Dict[str, Any]) -> Optional[Dict[str, Any]]:  # pragma: no cover
        return None
