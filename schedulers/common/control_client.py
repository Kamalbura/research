"""Thin wrapper around follower control servers."""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(slots=True)
class ControlClient:
    host: str
    port: int
    timeout: float = 2.0

    def _rpc(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") + b"\n"
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as conn:
            conn.sendall(data)
            buffer = conn.makefile("r", encoding="utf-8")
            raw = buffer.readline()
            if not raw:
                raise RuntimeError("control server closed connection")
            try:
                response = json.loads(raw)
            except json.JSONDecodeError as exc:  # pragma: no cover - defensive
                raise RuntimeError(f"invalid control response: {raw!r}") from exc
            return response

    def ping(self) -> bool:
        resp = self._rpc({"cmd": "ping"})
        return bool(resp.get("ok"))

    def timesync(self) -> Dict[str, int]:
        t1 = time.time_ns()
        resp = self._rpc({"cmd": "timesync", "t1_ns": t1})
        if not resp.get("ok"):
            raise RuntimeError(f"timesync failed: {resp}")
        return {
            "t1_ns": int(resp.get("t1_ns", t1)),
            "t2_ns": int(resp.get("t2_ns", t1)),
            "t3_ns": int(resp.get("t3_ns", t1)),
            "t4_ns": time.time_ns(),
        }

    def status(self) -> Dict[str, Any]:
        return self._rpc({"cmd": "status"})

    def schedule_suite(
        self,
        *,
        suite_id: str,
        duration_s: float,
        pre_gap_s: float,
        algorithm: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "cmd": "switch_suite",
            "suite": suite_id,
            "duration_s": float(duration_s),
            "pre_gap_s": float(pre_gap_s),
        }
        if algorithm:
            payload["algorithm"] = algorithm
        return self._rpc(payload)

    def schedule_mark(self, suite_id: str, *, start_ns: int) -> Dict[str, Any]:
        return self._rpc({"cmd": "schedule_mark", "suite": suite_id, "t0_ns": int(start_ns)})

    def request_power_capture(self, suite_id: str, *, duration_s: float) -> Dict[str, Any]:
        payload = {
            "cmd": "power_capture",
            "suite": suite_id,
            "duration_s": float(duration_s),
        }
        return self._rpc(payload)

    def stop(self) -> Dict[str, Any]:
        return self._rpc({"cmd": "stop"})


__all__ = ["ControlClient"]
