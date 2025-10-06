"""Offline diagnostic script for the Time Series Transformer model."""

from __future__ import annotations

import sys
from pathlib import Path
from statistics import multimode

import joblib
import numpy as np
import pandas as pd
import torch


def _logits_to_probs(logits: torch.Tensor) -> torch.Tensor:
    if logits.ndim == 1:
        logits = logits.unsqueeze(0)
    if logits.ndim != 2:
        raise ValueError(f"TST model must return rank-2 logits; got shape {tuple(logits.shape)}")
    if logits.shape[1] == 1:
        attack = torch.sigmoid(logits)
        probs = torch.cat([1 - attack, attack], dim=1)
    elif logits.shape[1] >= 2:
        probs = torch.softmax(logits, dim=1)
    else:
        raise ValueError(f"TST model produced invalid class dimension: {tuple(logits.shape)}")
    return probs


def _safe_torch_load(path: Path):
    try:
        return torch.load(str(path), map_location="cpu", weights_only=False)
    except TypeError:
        return torch.load(str(path), map_location="cpu")

from config import (
    SCALER_FILE,
    TORCH_NUM_THREADS,
    TST_ATTACK_THRESHOLD,
    TST_MODEL_FILE,
    TST_SEQ_LENGTH,
    TST_TORCHSCRIPT_FILE,
    configure_logging,
    ensure_file,
)

TEST_DATA_FILE = Path("tcp_test_ddos_data_0.1.csv")


def load_model():
    ensure_file(SCALER_FILE, "StandardScaler pickle")
    scaler = joblib.load(SCALER_FILE)

    if TST_TORCHSCRIPT_FILE.exists():
        model = torch.jit.load(str(TST_TORCHSCRIPT_FILE), map_location="cpu")
        scripted = True
    else:
        ensure_file(TST_MODEL_FILE, "PyTorch TST model")
        try:
            from tstplus import (  # type: ignore
                TSTPlus,
                _TSTBackbone,
                _TSTEncoder,
                _TSTEncoderLayer,
            )

            for name, obj in (
                ("TSTPlus", TSTPlus),
                ("_TSTBackbone", _TSTBackbone),
                ("_TSTEncoder", _TSTEncoder),
                ("_TSTEncoderLayer", _TSTEncoderLayer),
            ):
                globals().setdefault(name, obj)

            main_mod = sys.modules.get("__main__")
            if main_mod is not None:
                for name, obj in (
                    ("TSTPlus", TSTPlus),
                    ("_TSTBackbone", _TSTBackbone),
                    ("_TSTEncoder", _TSTEncoder),
                    ("_TSTEncoderLayer", _TSTEncoderLayer),
                ):
                    setattr(main_mod, name, obj)
        except Exception as exc:
            print(
                "❌ TorchScript model missing and unable to import tstplus module for .pth loading."
            )
            print("   Install the 'tsai' extra or ensure tstplus.py is available.")
            raise
        model = _safe_torch_load(TST_MODEL_FILE)
        scripted = False

    model.eval()
    torch.set_num_threads(TORCH_NUM_THREADS)
    return scaler, model, scripted


def main() -> int:
    configure_logging("run-tst")

    try:
        ensure_file(TEST_DATA_FILE, "test dataset")
        scaler, model, scripted = load_model()
    except FileNotFoundError as exc:
        print(f"❌ {exc}")
        return 1

    print("--- run_tst diagnostics ---")
    print(f"Model source : {'TorchScript' if scripted else 'PyTorch state_dict'}")
    print(f"Scaler file  : {SCALER_FILE}")
    print(f"Test dataset : {TEST_DATA_FILE}")
    print(f"Seq length   : {TST_SEQ_LENGTH}")

    df = pd.read_csv(TEST_DATA_FILE)
    for col in ("Mavlink_Count", "Status"):
        if col not in df.columns:
            print(f"❌ Column '{col}' not found. Available: {list(df.columns)}")
            return 1
    if len(df) < TST_SEQ_LENGTH:
        print(
            f"❌ Test data has only {len(df)} rows; need at least {TST_SEQ_LENGTH} to form a sequence."
        )
        return 1

    counts = df["Mavlink_Count"].iloc[:TST_SEQ_LENGTH].to_numpy(dtype=np.float32)
    labels = df["Status"].iloc[:TST_SEQ_LENGTH].to_numpy()
    true_label = multimode(labels)[0]

    scaled = scaler.transform(counts.reshape(-1, 1)).astype(np.float32)
    tensor = torch.from_numpy(scaled.reshape(1, 1, -1))

    with torch.no_grad():
        logits = model(tensor)
        probs = _logits_to_probs(logits)
        predicted_idx = int(torch.argmax(probs, dim=1))
        attack_prob = float(probs[0, 1])

    prediction = "ATTACK" if predicted_idx == 1 else "NORMAL"
    confidence = attack_prob if predicted_idx == 1 else float(probs[0, 0])
    threshold_hit = attack_prob >= TST_ATTACK_THRESHOLD

    print("\n--- Results ---")
    print(f"Probabilities (normal, attack): {probs.numpy().flatten()}")
    print(f"Predicted class            : {prediction}")
    print(f"Attack probability         : {attack_prob:.3f}")
    print(f"Threshold (config)         : {TST_ATTACK_THRESHOLD:.3f} -> {'trigger' if threshold_hit else 'no trigger'}")
    print(f"True label (mode)          : {'ATTACK' if true_label == 1 else 'NORMAL'}")
    print(f"Confidence                 : {confidence:.3f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
