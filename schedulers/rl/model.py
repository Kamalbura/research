"""Lightweight linear policy used for RL inference deployment."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List

import math

FeatureVector = List[float]


def build_feature_vector(metrics: Dict[str, float]) -> FeatureVector:
    keys = [
        "power_w",
        "cpu_percent",
        "cpu_temp_c",
        "loss_pct",
        "throughput_mbps",
        "rtt_ms",
    ]
    return [float(metrics.get(key, 0.0)) for key in keys]


@dataclass(slots=True)
class LinearPolicy:
    suites: List[str]
    weights: List[List[float]]
    bias: List[float]
    ddos_weights: List[float]
    ddos_bias: float
    rate_table: List[float]

    def predict(self, metrics: Dict[str, float]) -> Dict[str, float]:
        features = build_feature_vector(metrics)
        logits = [
            sum(w * f for w, f in zip(weight_row, features)) + bias
            for weight_row, bias in zip(self.weights, self.bias)
        ]
        idx = max(range(len(logits)), key=lambda i: logits[i])
        confidence = softmax(logits)[idx]

        ddos_score = sum(w * f for w, f in zip(self.ddos_weights, features)) + self.ddos_bias
        ddos_score = 1.0 / (1.0 + math.exp(-ddos_score))

        rate = self.rate_table[idx] if idx < len(self.rate_table) else 0.0

        return {
            "suite_id": self.suites[idx],
            "confidence": confidence,
            "ddos_score": ddos_score,
            "traffic_rate": rate,
        }


def load_policy(path: Path) -> LinearPolicy:
    data = json.loads(path.read_text(encoding="utf-8"))
    suites = list(data["suites"]) if "suites" in data else []
    weights = [list(row) for row in data.get("weights", [])]
    bias = list(data.get("bias", [0.0] * len(weights)))
    ddos_weights = list(data.get("ddos_weights", [0.1] * len(build_feature_vector({}))))
    ddos_bias = float(data.get("ddos_bias", 0.0))
    rate_table = list(data.get("rate_table", [8.0 for _ in suites]))

    if not suites or not weights:
        raise ValueError("policy file missing suites/weights")

    if len(weights) != len(suites):
        raise ValueError("weights shape mismatch vs suites")

    return LinearPolicy(
        suites=suites,
        weights=weights,
        bias=bias,
        ddos_weights=ddos_weights,
        ddos_bias=ddos_bias,
        rate_table=rate_table,
    )


def softmax(logits: Iterable[float]) -> List[float]:
    exp_vals = [math.exp(x) for x in logits]
    total = sum(exp_vals) or 1.0
    return [val / total for val in exp_vals]


__all__ = ["LinearPolicy", "load_policy", "build_feature_vector"]
