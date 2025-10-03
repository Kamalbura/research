"""Diagnostic helper for the XGBoost screener model."""

from __future__ import annotations

import sys

import numpy as np
import xgboost as xgb

from config import XGB_MODEL_FILE, XGB_SEQ_LENGTH, configure_logging, ensure_file


def main() -> int:
    configure_logging("run-xgboost")

    try:
        ensure_file(XGB_MODEL_FILE, "XGBoost model")
    except FileNotFoundError as exc:
        print(f"❌ {exc}")
        return 1

    model = xgb.XGBClassifier()
    model.load_model(str(XGB_MODEL_FILE))

    expected = XGB_SEQ_LENGTH
    features_in = getattr(model, "n_features_in_", None)
    if features_in not in (None, expected):
        print(
            f"❌ Model expects {features_in} features but config specifies {expected}. "
            "Adjust XGB_SEQ_LENGTH or retrain the model."
        )
        return 1

    print("--- run_xgboost diagnostics ---")
    print(f"Model path        : {XGB_MODEL_FILE}")
    print(f"Expected features : {expected}")

    samples = {
        "NORMAL": np.array([10, 15, 12, 18, 14], dtype=np.float32),
        "ATTACK": np.array([150, 200, 180, 220, 190], dtype=np.float32),
    }

    for label, arr in samples.items():
        if arr.size != expected:
            print(
                f"Skipping sample '{label}' because it has {arr.size} entries but expected {expected}."
            )
            continue

        sample = arr.reshape(1, -1)
        try:
            pred = int(model.predict(sample)[0])
            probs = model.predict_proba(sample)[0]
            confidence = probs[pred]
            verdict = "ATTACK" if pred == 1 else "NORMAL"
            print(
                f"\nSample '{label}' -> predicted={verdict} (confidence={confidence:.3f})"
            )
            print(f"Probabilities (normal, attack): {probs}")
        except Exception as exc:  # pragma: no cover - defensive
            print(f"Error evaluating sample '{label}': {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
