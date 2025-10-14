"""Utilities for subscribing to telemetry streams emitted by followers."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Iterable, Iterator, Optional

from .state import SuiteTelemetry, TelemetryWindow


@dataclass(slots=True)
class TelemetrySubscriber:
    host: str
    port: int
    session_id: str
    buffer_seconds: float = 15.0
    reconnect_backoff: float = 1.0

    def __post_init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._queue: "Deque[SuiteTelemetry]" = deque(maxlen=int(self.buffer_seconds * 20))
        self._callbacks: list[Callable[[SuiteTelemetry], None]] = []

    def start(self) -> None:
        if self._thread and self._thread.is_alive():  # pragma: no cover - idempotent guard
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="TelemetrySubscriber", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None

    def register_callback(self, func: Callable[[SuiteTelemetry], None]) -> None:
        self._callbacks.append(func)

    def snapshots(self, *, window_seconds: float) -> TelemetryWindow:
        window_ns = max(1, int(window_seconds * 1e9))
        now_ns = time.time_ns()
        window_start = now_ns - window_ns
        selected = [snap for snap in list(self._queue) if snap.timestamp_ns >= window_start]
        return TelemetryWindow(snapshots=selected, window_start_ns=window_start, window_end_ns=now_ns)

    def _run(self) -> None:
        backoff = self.reconnect_backoff
        while not self._stop.is_set():
            try:
                with socket.create_connection((self.host, self.port), timeout=3.0) as conn:
                    writer = conn.makefile("w", encoding="utf-8", buffering=1)
                    hello = {
                        "session_id": self.session_id,
                        "kind": "scheduler_subscribe",
                        "timestamp_ns": time.time_ns(),
                    }
                    writer.write(json.dumps(hello) + "\n")
                    writer.flush()
                    reader = conn.makefile("r", encoding="utf-8")
                    backoff = self.reconnect_backoff
                    for raw in reader:
                        if self._stop.is_set():
                            break
                        snap = self._parse_snapshot(raw)
                        if snap is None:
                            continue
                        self._queue.append(snap)
                        for func in list(self._callbacks):
                            try:
                                func(snap)
                            except Exception:
                                continue
            except Exception:
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 5.0)

    def _parse_snapshot(self, raw: str) -> Optional[SuiteTelemetry]:
        if not raw.strip():
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if payload.get("session_id") != self.session_id:
            return None
        kind = payload.get("kind")
        if kind not in {"telemetry", "proxy_counters", "udp_echo", "power_summary"}:
            return None
        suite_id = payload.get("suite") or payload.get("suite_id") or "unknown"
        timestamp_ns = int(payload.get("timestamp_ns", time.time_ns()))
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        counters = payload.get("counters") if isinstance(payload.get("counters"), dict) else {}

        battery = summary.get("battery_pct") if isinstance(summary, dict) else None
        voltage = summary.get("battery_voltage_v") if isinstance(summary, dict) else None
        current = summary.get("battery_current_a") if isinstance(summary, dict) else None
        cpu_pct = summary.get("cpu_percent") if isinstance(summary, dict) else counters.get("cpu_percent")
        temp_c = summary.get("cpu_temp_c") if isinstance(summary, dict) else counters.get("cpu_temp_c")
        power_w = summary.get("avg_power_w") or counters.get("power_w")
        energy_j = summary.get("energy_j") or counters.get("energy_j")
        throughput = counters.get("throughput_mbps") or summary.get("throughput_mbps")
        goodput = counters.get("goodput_mbps") or summary.get("goodput_mbps")
        loss_pct = counters.get("loss_pct") or counters.get("packet_loss_pct")
        rtt_ms = counters.get("rtt_ms") or counters.get("rtt_avg_ms")
        rekey_ms = counters.get("rekey_ms") or summary.get("rekey_ms")
        ddos_alert = bool(payload.get("ddos_alert")) if "ddos_alert" in payload else None

        return SuiteTelemetry(
            suite_id=suite_id,
            timestamp_ns=timestamp_ns,
            battery_pct=_maybe_float(battery),
            battery_voltage_v=_maybe_float(voltage),
            battery_current_a=_maybe_float(current),
            cpu_percent=_maybe_float(cpu_pct),
            cpu_temp_c=_maybe_float(temp_c),
            power_w=_maybe_float(power_w),
            energy_j=_maybe_float(energy_j),
            throughput_mbps=_maybe_float(throughput),
            goodput_mbps=_maybe_float(goodput),
            packet_loss_pct=_maybe_float(loss_pct),
            rtt_ms=_maybe_float(rtt_ms),
            rekey_ms=_maybe_float(rekey_ms),
            ddos_alert=ddos_alert,
            counters=_flatten_numeric(summary, counters),
        )


def _maybe_float(value: object) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _flatten_numeric(*sources: Dict[str, object]) -> Dict[str, float]:
    merged: Dict[str, float] = {}
    for source in sources:
        for key, value in source.items():
            try:
                merged[key] = float(value)
            except (TypeError, ValueError):
                continue
    return merged


__all__ = ["TelemetrySubscriber"]
