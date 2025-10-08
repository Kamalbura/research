from __future__ import annotations

from typing import Any, Dict


def extract_power_fields(status: Dict[str, Any]) -> Dict[str, Any]:
    summary = status.get("last_summary") or {}
    return {
        "energy_j": summary.get("energy_j"),
        "avg_power_w": summary.get("avg_power_w"),
        "duration_s": summary.get("duration_s"),
        "summary_json_path": summary.get("summary_json_path") or summary.get("csv_path"),
    }
