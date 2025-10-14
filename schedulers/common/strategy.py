"""Strategy interfaces for drone/GCS scheduling."""

from __future__ import annotations

import abc
from typing import Optional

from .state import SchedulerContext, SchedulerDecision, TelemetryWindow


class SchedulerStrategy(abc.ABC):
    """Abstract base class for expert, RL, or hybrid schedulers."""

    name: str = "base"

    def __init__(self, *, lookback_windows: int = 1) -> None:
        self.lookback_windows = max(1, int(lookback_windows))

    @abc.abstractmethod
    def warmup(self, context: SchedulerContext) -> None:
        """Perform any model loading or calibration before the decision loop."""

    @abc.abstractmethod
    def decide(
        self,
        *,
        context: SchedulerContext,
        telemetry: TelemetryWindow,
    ) -> Optional[SchedulerDecision]:
        """Return the next decision for the scheduler loop.

        Returning ``None`` keeps the current cryptographic suite and settings.
        """

    def teardown(self, context: SchedulerContext) -> None:  # pragma: no cover - optional override
        """Optional cleanup when shutting down the scheduler loop."""


__all__ = ["SchedulerStrategy"]
