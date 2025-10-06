#!/usr/bin/env python3
"""Compare CPU/latency of XGBoost vs TST and a matrix multiply baseline."""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

import numpy as np

try:
    import psutil
except Exception:  # pragma: no cover - import guard
    print("❌ psutil is required. Install: pip install psutil")
    raise

import sys

ROOT = Path(__file__).resolve().parent
DDOS_DIR = ROOT / "ddos"
if str(DDOS_DIR) not in sys.path:
    sys.path.insert(0, str(DDOS_DIR))

try:
    import xgboost as xgb
except Exception:  # pragma: no cover - import guard
    print("❌ xgboost is required. Install: pip install xgboost")
    raise

try:
    import torch
except Exception:  # pragma: no cover - import guard
    print("❌ torch is required. Install CPU build of PyTorch.")
    raise

from config import (
    TORCH_NUM_THREADS,
    TST_MODEL_FILE,
    TST_SEQ_LENGTH,
    TST_TORCHSCRIPT_FILE,
    XGB_MODEL_FILE,
    XGB_SEQ_LENGTH,
)
from run_tst import load_model as load_tst_model


def load_xgb_from_config() -> xgb.XGBClassifier:
    if not Path(XGB_MODEL_FILE).exists():
        raise FileNotFoundError(f"Missing XGBoost model: {XGB_MODEL_FILE}")
    model = xgb.XGBClassifier()
    model.load_model(str(XGB_MODEL_FILE))
    feats = getattr(model, "n_features_in_", None)
    if feats not in (None, XGB_SEQ_LENGTH):
        raise ValueError(
            f"XGBoost model expects {feats} features but XGB_SEQ_LENGTH={XGB_SEQ_LENGTH}"
        )
    return model


class CPUSampler:
    """Background sampler of process CPU% and RSS."""

    def __init__(self, interval: float = 0.1) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._samples: list[float] = []
        self._rss: list[int] = []
        self._proc = psutil.Process()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        self._proc.cpu_percent(None)
        while not self._stop.is_set():
            self._samples.append(self._proc.cpu_percent(interval=self.interval))
            try:
                self._rss.append(self._proc.memory_info().rss)
            except Exception:
                pass

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    @property
    def mean_cpu(self) -> float:
        return (sum(self._samples) / len(self._samples)) if self._samples else 0.0

    @property
    def max_cpu(self) -> float:
        return max(self._samples) if self._samples else 0.0

    @property
    def max_rss_mb(self) -> float:
        return (max(self._rss) / (1024 * 1024)) if self._rss else 0.0


def time_loop(
    fn: callable,
    iters: int,
    warmup: int,
    pace_ms: float | None,
    sample_interval: float,
) -> tuple[float, float, float, float, float]:
    for _ in range(warmup):
        fn()

    proc = psutil.Process()
    sampler = CPUSampler(interval=sample_interval)
    cpu0 = proc.cpu_times()
    t0 = time.perf_counter()
    sampler.start()
    try:
        for _ in range(iters):
            fn()
            if pace_ms:
                time.sleep(pace_ms / 1000.0)
    finally:
        sampler.stop()
    t1 = time.perf_counter()
    cpu1 = proc.cpu_times()

    wall_ms = (t1 - t0) * 1000.0 / iters
    cpu_ms = ((cpu1.user - cpu0.user) + (cpu1.system - cpu0.system)) * 1000.0 / iters
    return wall_ms, cpu_ms, sampler.mean_cpu, sampler.max_cpu, sampler.max_rss_mb


def bench_matmul(n: int, iters: int) -> tuple[float, float]:
    torch.set_num_threads(max(1, torch.get_num_threads()))
    a = torch.randn((n, n), dtype=torch.float32)
    b = torch.randn((n, n), dtype=torch.float32)
    with torch.no_grad():
        for _ in range(10):
            _ = a @ b
    proc = psutil.Process()
    cpu0 = proc.cpu_times()
    t0 = time.perf_counter()
    with torch.no_grad():
        for _ in range(iters):
            _ = a @ b
    t1 = time.perf_counter()
    cpu1 = proc.cpu_times()
    wall_ms = (t1 - t0) * 1000.0 / iters
    cpu_ms = ((cpu1.user - cpu0.user) + (cpu1.system - cpu0.system)) * 1000.0 / iters
    return wall_ms, cpu_ms


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iters", type=int, default=500)
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument(
        "--torch-threads",
        type=int,
        default=TORCH_NUM_THREADS,
        help="Override DDOS_TORCH_THREADS",
    )
    parser.add_argument("--mode", choices=["burst", "paced"], default="burst")
    parser.add_argument(
        "--pace-ms",
        type=float,
        default=600.0,
        help="Sleep per inference in paced mode (ms)",
    )
    parser.add_argument(
        "--sample-interval",
        type=float,
        default=0.1,
        help="CPU sampler interval (s)",
    )
    parser.add_argument("--mm-n", type=int, default=300)
    parser.add_argument("--mm-iters", type=int, default=200)
    args = parser.parse_args()

    torch.set_num_threads(max(1, args.torch_threads))

    xgb_model = load_xgb_from_config()
    xgb_feat = np.random.randint(low=0, high=50, size=(1, XGB_SEQ_LENGTH)).astype(np.float32)

    def xgb_infer() -> None:
        _ = xgb_model.predict(xgb_feat)
        _ = xgb_model.predict_proba(xgb_feat)

    tst_model = None
    try:
        scaler, tst_model, scripted = load_tst_model()
    except Exception as exc:
        print(
            "❌ Unable to load TST model. If you don't have TorchScript, install 'tsai' for tstplus.py."
        )
        print(f"   Details: {exc}")

    if tst_model is not None:
        counts = np.random.randint(low=0, high=50, size=(TST_SEQ_LENGTH, 1)).astype(np.float32)
        scaled = scaler.transform(counts).astype(np.float32)
        tst_tensor = torch.from_numpy(scaled.reshape(1, 1, -1))
        tst_model.eval()

        @torch.no_grad()
        def tst_infer() -> None:
            _ = tst_model(tst_tensor)

    print("\n=== Settings ===")
    print(f"Torch threads      : {torch.get_num_threads()}")
    print(f"Mode               : {args.mode}")
    if args.mode == "paced":
        print(f"Pace per inference : {args.pace_ms:.1f} ms")

    pace = args.pace_ms if args.mode == "paced" else None

    print("\n=== XGBoost ===")
    x_wall, x_cpu, x_avg, x_max, x_rss = time_loop(
        xgb_infer, args.iters, args.warmup, pace, args.sample_interval
    )
    print(f"Wall per inf (ms)  : {x_wall:.3f}")
    print(f"CPU  per inf (ms)  : {x_cpu:.3f}")
    print(f"Process CPU% avg   : {x_avg:.1f}%  (max {x_max:.1f}%)")
    print(f"Max RSS (MB)       : {x_rss:.1f}")

    if tst_model is not None:
        print("\n=== TST ===")
        t_wall, t_cpu, t_avg, t_max, t_rss = time_loop(
            tst_infer, args.iters, args.warmup, pace, args.sample_interval
        )
        print(f"Wall per inf (ms)  : {t_wall:.3f}")
        print(f"CPU  per inf (ms)  : {t_cpu:.3f}")
        print(f"Process CPU% avg   : {t_avg:.1f}%  (max {t_max:.1f}%)")
        print(f"Max RSS (MB)       : {t_rss:.1f}")

        ratio_wall = (t_wall / x_wall) if x_wall > 0 else float("inf")
        ratio_cpu = (t_cpu / x_cpu) if x_cpu > 0 else float("inf")
        print("\n=== Heaviness Ratios (TST / XGB) ===")
        print(f"Wall time ratio    : {ratio_wall:.1f}×")
        print(f"CPU time ratio     : {ratio_cpu:.1f}×")
    else:
        print("\n(TST section skipped due to load error.)")

    print(f"\n=== {args.mm_n}×{args.mm_n} matmul (torch, CPU) ===")
    mm_wall, mm_cpu = bench_matmul(args.mm_n, args.mm_iters)
    print(f"Wall per mm (ms)   : {mm_wall:.3f}")
    print(f"CPU  per mm (ms)   : {mm_cpu:.3f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
