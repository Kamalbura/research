"""Synthetic traffic simulator for the hybrid DDoS detector.

This script lets you exercise the XGBoost gating and TST cooldown logic without
needing live packet capture. It generates synthetic packet-count windows,
feeds them through the screener, and optionally runs the TST confirmer.
"""
from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from typing import Iterable, List

import joblib
import numpy as np
import torch
import xgboost as xgb

from config import (
    SCALER_FILE,
    TST_ATTACK_THRESHOLD,
    TST_MODEL_FILE,
    TST_SEQ_LENGTH,
    TST_TORCHSCRIPT_FILE,
    XGB_CONSECUTIVE_POSITIVES,
    XGB_MODEL_FILE,
    XGB_SEQ_LENGTH,
    TST_COOLDOWN_WINDOWS,
)


def load_xgb_model() -> xgb.XGBClassifier:
    model = xgb.XGBClassifier()
    model.load_model(str(XGB_MODEL_FILE))
    expected = XGB_SEQ_LENGTH
    features_in = getattr(model, "n_features_in_", None)
    if features_in not in (None, expected):
        raise ValueError(
            f"XGBoost model expects {features_in} features; config specifies {expected}."
        )
    return model


def load_tst_model():
    scaler = joblib.load(SCALER_FILE)
    if TST_TORCHSCRIPT_FILE.exists():
        model = torch.jit.load(str(TST_TORCHSCRIPT_FILE), map_location="cpu")
        scripted = True
    else:
        model = torch.load(str(TST_MODEL_FILE), map_location="cpu")
        scripted = False
    model.eval()
    torch.set_num_threads(1)
    return scaler, model, scripted


@dataclass
class Scenario:
    name: str
    total_windows: int
    base_rate: float
    spike_rate: float
    spike_start: int
    spike_duration: int
    decay_windows: int = 0

    def generate(self, seed: int) -> Iterable[int]:
        rng = random.Random(seed)
        for step in range(self.total_windows):
            if self.spike_start <= step < self.spike_start + self.spike_duration:
                lam = self.spike_rate
            elif self.decay_windows and step < self.spike_start + self.spike_duration + self.decay_windows:
                # Exponential decay back to baseline.
                offset = step - (self.spike_start + self.spike_duration) + 1
                lam = self.base_rate + (self.spike_rate - self.base_rate) * (0.5 ** offset)
            else:
                lam = self.base_rate
            yield max(0, int(rng.gauss(lam, lam * 0.2)))


def run_simulation(args: argparse.Namespace) -> None:
    xgb_model = load_xgb_model()
    scaler = model = None
    if args.run_tst:
        scaler, model, scripted = load_tst_model()
        print(f"Loaded TST model ({'TorchScript' if scripted else 'PyTorch'})")

    buffer: List[int] = []
    consecutive = 0
    cooldown = 0

    print(
        f"Running scenario '{args.scenario.name}' for {args.scenario.total_windows} windows"
        f" (base={args.scenario.base_rate}, spike={args.scenario.spike_rate})"
    )
    print(
        f"XGB gate requires {XGB_CONSECUTIVE_POSITIVES} positives; TST cooldown={TST_COOLDOWN_WINDOWS} windows"
    )

    for idx, count in enumerate(args.scenario.generate(args.seed)):
        buffer.append(count)
        if len(buffer) > max(TST_SEQ_LENGTH, XGB_SEQ_LENGTH):
            buffer.pop(0)

        pred = None
        proba = None
        if len(buffer) >= XGB_SEQ_LENGTH:
            features = np.array(buffer[-XGB_SEQ_LENGTH:], dtype=np.float32).reshape(1, -1)
            pred = int(xgb_model.predict(features)[0])
            proba = float(xgb_model.predict_proba(features)[0][1])

            if pred == 1:
                consecutive += 1
            else:
                consecutive = 0
        else:
            consecutive = 0

        if cooldown > 0:
            cooldown -= 1

        print(
            f"win={idx:03d} count={count:4d} xgb_pred={pred if pred is not None else '-'}"
            f" proba={proba:.3f}" if proba is not None else "",
            end="",
        )

        triggered = (
            pred == 1
            and consecutive >= XGB_CONSECUTIVE_POSITIVES
            and cooldown == 0
            and len(buffer) >= TST_SEQ_LENGTH
        )

        if triggered:
            cooldown = TST_COOLDOWN_WINDOWS
            consecutive = 0
            print(" -> TST trigger", end="")
            if args.run_tst and scaler is not None and model is not None:
                counts = np.array(buffer[-TST_SEQ_LENGTH:], dtype=np.float32)
                scaled = scaler.transform(counts.reshape(-1, 1)).astype(np.float32)
                tensor = torch.from_numpy(scaled.reshape(1, 1, -1))
                with torch.no_grad():
                    logits = model(tensor)
                    probs = torch.softmax(logits, dim=1)
                    attack_prob = float(probs[0, 1])
                    verdict = (
                        "CONFIRMED ATTACK" if attack_prob >= TST_ATTACK_THRESHOLD else "NORMAL"
                    )
                print(f" (TST verdict={verdict} prob={attack_prob:.3f})", end="")
        print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=["benign", "pulse", "flood"], help="Traffic scenario to simulate")
    parser.add_argument("--seed", type=int, default=2025, help="Random seed for reproducibility")
    parser.add_argument(
        "--run-tst",
        action="store_true",
        help="Run the TST confirmer when the screener triggers",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    scenarios = {
        "benign": Scenario("benign", total_windows=120, base_rate=30, spike_rate=45, spike_start=999, spike_duration=0),
        "pulse": Scenario("pulse", total_windows=180, base_rate=25, spike_rate=120, spike_start=60, spike_duration=10, decay_windows=10),
        "flood": Scenario("flood", total_windows=180, base_rate=20, spike_rate=200, spike_start=40, spike_duration=80),
    }

    args.scenario = scenarios[args.scenario]
    run_simulation(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
