from __future__ import annotations

from typing import Any, Dict, Optional
from pathlib import Path
import sys

from .base import Strategy, StrategyContext


class RlStrategy(Strategy):
    def __init__(self) -> None:
        root = Path(__file__).resolve().parents[3]
        root_str = str(root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        try:
            from schedulers.nextgen_rl.strategy import NextGenRlStrategy  # type: ignore
        except Exception as exc:  # pragma: no cover - optional
            self._impl = None
            self._import_error = exc
        else:
            self._impl = NextGenRlStrategy()
            self._import_error = None

    def warmup(self, ctx: StrategyContext) -> None:
        if self._impl is not None:
            from schedulers.common.state import SchedulerContext  # type: ignore
            self._impl.warmup(SchedulerContext(ctx.session_id, ctx.role, ctx.initial_suite))

    def decide(self, features: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if self._impl is None:
            return None
        try:
            decision = self._impl.decide(features)  # type: ignore[attr-defined]
        except Exception:
            return None
        if decision is None:
            return None
        return {
            "target_suite": getattr(decision, "target_suite", None),
            "ddos_mode": getattr(getattr(decision, "ddos_mode", None), "value", None),
            "notes": getattr(decision, "notes", {}) or {},
        }
