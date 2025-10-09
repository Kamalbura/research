#!/usr/bin/env python3
"""Compare CPU/latency of XGBoost vs TST and a matrix multiply baseline."""
from __future__ import annotations

import argparse
import math
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

xgb = None
try:
    import xgboost as xgb  # type: ignore[assignment]
except Exception:  # pragma: no cover - optional dependency guard
    xgb = None

torch = None
try:
    import torch  # type: ignore[assignment]
except Exception:  # pragma: no cover - optional dependency guard
    torch = None

from config import (
    TORCH_NUM_THREADS,
    TST_MODEL_FILE,
    TST_SEQ_LENGTH,
    TST_TORCHSCRIPT_FILE,
    XGB_MODEL_FILE,
    XGB_SEQ_LENGTH,
)
try:
    from run_tst import load_model as load_tst_model
except Exception as exc:  # pragma: no cover - optional dependency guard
    load_tst_model = None  # type: ignore[assignment]
    _LOAD_TST_ERROR = exc
else:
    _LOAD_TST_ERROR = None


def calculate_predicted_flight_constraint(
    v_h: float,
    v_v: float,
    weight_n: float,
    *,
    air_density: float = 1.225,
    rotor_radius_m: float = 0.16,
    rotor_count: int = 4,
    profile_coefficient: float = 0.012,
    drag_area_m2: float = 0.12,
    drag_coefficient: float = 1.05,
) -> float:
    """Compute the predicted flight constraint (W) using a multirotor power model.

    The implementation follows the standard decomposition of rotorcraft
    power (Equation 19 from the simplified multirotor operating envelope
    derivation):

    ``P_total = P_induced + P_profile + P_parasitic + P_climb``

    Parameters mirror the physical quantities of the vehicle, defaulting to a
    mid-sized quadrotor (16 cm radius rotors, four count). The caller provides
    horizontal and vertical airspeed components in metres per second and the
    vehicle weight in Newtons.

    Returns
    -------
    float
        Estimated mechanical power demand in Watts. The result is always
        non-negative.
    """

    try:
        horiz = float(v_h)
        vert = float(v_v)
        weight = max(0.0, float(weight_n))
        density = float(air_density)
        rotor_r = max(1e-6, float(rotor_radius_m))
        rotor_n = max(1, int(rotor_count))
        profile_coeff = max(0.0, float(profile_coefficient))
        drag_area = max(0.0, float(drag_area_m2))
        drag_coeff = max(0.0, float(drag_coefficient))
    except (TypeError, ValueError):
        raise ValueError("velocity components, weight, and model parameters must be numeric") from None

    if weight == 0.0:
        return 0.0

    disk_area = rotor_n * math.pi * rotor_r ** 2
    if disk_area <= 0.0:
        return 0.0

    total_speed = math.hypot(horiz, vert)

    hover_induced_velocity = math.sqrt(max(weight, 0.0) / (2.0 * density * disk_area))
    induced_term = math.sqrt(max(0.0, hover_induced_velocity ** 2 + (vert * 0.5) ** 2))
    induced_velocity = max(0.0, induced_term - 0.5 * vert)
    induced_power = weight * induced_velocity

    profile_power = profile_coeff * weight ** 1.5 / math.sqrt(max(1e-9, 2.0 * density * disk_area))

    parasitic_power = 0.5 * density * drag_coeff * drag_area * total_speed ** 3

    climb_power = weight * max(0.0, vert)

    total_power = induced_power + profile_power + parasitic_power + climb_power
    return max(0.0, total_power)


def load_xgb_from_config() -> xgb.XGBClassifier:
    if xgb is None:  # pragma: no cover - runtime dependency check
        raise RuntimeError("xgboost is required for load_xgb_from_config(); install xgboost")
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
    if torch is None:  # pragma: no cover - runtime dependency check
        raise RuntimeError("torch is required for bench_matmul(); install torch")
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

    if torch is None:  # pragma: no cover - runtime dependency check
        raise RuntimeError("torch is required for benchmarking; install torch")

    torch.set_num_threads(max(1, args.torch_threads))

    xgb_model = load_xgb_from_config()
    xgb_feat = np.random.randint(low=0, high=50, size=(1, XGB_SEQ_LENGTH)).astype(np.float32)

    def xgb_infer() -> None:
        _ = xgb_model.predict(xgb_feat)
        _ = xgb_model.predict_proba(xgb_feat)

    tst_model = None
    if load_tst_model is not None:
        try:
            scaler, tst_model, scripted = load_tst_model()
        except Exception as exc:
            print(
                "❌ Unable to load TST model. If you don't have TorchScript, install 'tsai' for tstplus.py."
            )
            print(f"   Details: {exc}")
    elif _LOAD_TST_ERROR is not None:
        print(
            "[WARN] TST model loader unavailable (missing dependency). Install torch/joblib to enable TST benchmarking."
        )
        print(f"   Import error: {_LOAD_TST_ERROR}")

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
