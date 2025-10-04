"""Hybrid two-stage DDoS detector for MAVLink-over-UDP."""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Deque, Dict, List, Optional

import joblib
import numpy as np
import torch
import xgboost as xgb

from config import (
    BUFFER_SIZE,
    IFACE,
    PORT,
    SCALER_FILE,
    TST_ATTACK_THRESHOLD,
    TST_COOLDOWN_WINDOWS,
    TST_MODEL_FILE,
    TST_QUEUE_MAX,
    TST_SEQ_LENGTH,
    TST_TORCHSCRIPT_FILE,
    WINDOW_SIZE,
    XGB_CONSECUTIVE_POSITIVES,
    XGB_MODEL_FILE,
    XGB_QUEUE_MAX,
    XGB_SEQ_LENGTH,
    configure_logging,
    ensure_file,
    get_udp_bpf,
)

try:
    import scapy.all as scapy
except ImportError as exc:  # pragma: no cover - runtime guard
    raise SystemExit(
        "Scapy is required for packet capture. Install via `pip install scapy`."
    ) from exc


LOGGER = logging.getLogger(__name__)


@dataclass
class WindowSample:
    """Aggregated statistics for a single window."""

    start_ts: float
    end_ts: float
    count: int
    total_length: int


class RateLimiter:
    """Allow logging a message at most once per interval."""

    def __init__(self, interval_sec: float) -> None:
        self.interval = interval_sec
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def should_log(self) -> bool:
        now = time.time()
        with self._lock:
            if now >= self._next_allowed:
                self._next_allowed = now + self.interval
                return True
        return False


def load_xgb_model() -> xgb.XGBClassifier:
    ensure_file(XGB_MODEL_FILE, "XGBoost model")
    model = xgb.XGBClassifier()
    model.load_model(str(XGB_MODEL_FILE))
    if getattr(model, "n_features_in_", None) not in (None, XGB_SEQ_LENGTH):
        raise ValueError(
            f"XGBoost model expects {model.n_features_in_} features, "
            f"but config specifies {XGB_SEQ_LENGTH}"
        )
    LOGGER.info("Loaded XGBoost model from %s", XGB_MODEL_FILE)
    return model


def load_tst_model():
    ensure_file(SCALER_FILE, "StandardScaler pickle")
    scaler = joblib.load(SCALER_FILE)
    model: Optional[torch.nn.Module]
    scripted = False

    if TST_TORCHSCRIPT_FILE.exists():
        model = torch.jit.load(str(TST_TORCHSCRIPT_FILE), map_location="cpu")
        scripted = True
        LOGGER.info("Loaded TorchScript TST model from %s", TST_TORCHSCRIPT_FILE)
    else:
        ensure_file(TST_MODEL_FILE, "PyTorch TST model")
        LOGGER.warning(
            "TorchScript model not found; falling back to .pth (requires tstplus module)."
        )
        model = torch.load(str(TST_MODEL_FILE), map_location="cpu", weights_only=False)

    model.eval()
    torch.set_num_threads(1)

    # Verify that the scaler + model pair accepts the configured sequence length and
    # produces a 2-class output. This catches mismatched artifacts early instead of
    # failing inside the inference threads.
    try:
        zero_counts = np.zeros((TST_SEQ_LENGTH, 1), dtype=np.float32)
        scaled = scaler.transform(zero_counts).astype(np.float32)
    except Exception as exc:
        raise ValueError(
            "Scaler failed to transform a zero vector; verify scaler.pkl matches training pipeline"
        ) from exc

    tensor = torch.from_numpy(scaled.reshape(1, 1, -1))
    with torch.no_grad():
        try:
            logits = model(tensor)
        except Exception as exc:
            raise ValueError(
                f"TST model rejected input shaped (1, 1, {TST_SEQ_LENGTH}); check seq length and architecture"
            ) from exc

    if logits.ndim != 2 or logits.shape[1] < 2:
        raise ValueError(
            "TST model must return a 2D tensor with >=2 classes; got shape "
            f"{tuple(logits.shape)}"
        )

    LOGGER.info("Validated TST model output shape=%s", tuple(logits.shape))
    return scaler, model, scripted


def collector_thread(
    stop_event: threading.Event,
    counter: Dict[str, int],
    counter_lock: threading.Lock,
) -> None:
    LOGGER.info("Collector running on iface=%s port=%s", IFACE, PORT)

    def packet_callback(packet) -> None:
        if stop_event.is_set():
            return
        if scapy.UDP in packet and scapy.Raw in packet:
            payload = packet[scapy.Raw].load
            if payload and payload[0] == 0xFD:
                length = len(payload)
                with counter_lock:
                    counter["count"] += 1
                    counter["bytes"] += length

    try:
        scapy.sniff(
            iface=IFACE,
            store=False,
            prn=packet_callback,
            filter=get_udp_bpf(),
            stop_filter=lambda _: stop_event.is_set(),
        )
    except Exception:  # pragma: no cover - hardware interaction
        LOGGER.exception("Collector thread encountered an error")
        stop_event.set()


def window_aggregator_thread(
    stop_event: threading.Event,
    counter: Dict[str, int],
    counter_lock: threading.Lock,
    buffer: Deque[WindowSample],
    buffer_lock: threading.Lock,
    xgb_queue: Queue,
) -> None:
    LOGGER.info("Window aggregator started (window=%.2fs)", WINDOW_SIZE)
    drop_limiter = RateLimiter(30.0)
    window_start = time.time()

    while not stop_event.is_set():
        deadline = window_start + WINDOW_SIZE
        remaining = deadline - time.time()
        if remaining > 0:
            stop_event.wait(remaining)
            if stop_event.is_set():
                break

        with counter_lock:
            count = counter["count"]
            total_len = counter["bytes"]
            counter["count"] = 0
            counter["bytes"] = 0

        sample = WindowSample(window_start, deadline, count, total_len)

        with buffer_lock:
            buffer.append(sample)
            if len(buffer) >= XGB_SEQ_LENGTH:
                xgb_input = [s.count for s in list(buffer)[-XGB_SEQ_LENGTH:]]
                payload = (xgb_input, sample)
            else:
                payload = None

        if payload:
            try:
                xgb_queue.put_nowait(payload)
            except Full:
                if drop_limiter.should_log():
                    LOGGER.warning("XGBoost queue full; dropping window sample")

        window_start = deadline

    LOGGER.info("Window aggregator exiting")


def xgboost_screener_thread(
    stop_event: threading.Event,
    model: xgb.XGBClassifier,
    buffer: Deque[WindowSample],
    buffer_lock: threading.Lock,
    xgb_queue: Queue,
    tst_queue: Queue,
) -> None:
    LOGGER.info(
        "XGBoost screener running (seq=%d, threshold=%d)",
        XGB_SEQ_LENGTH,
        XGB_CONSECUTIVE_POSITIVES,
    )
    consecutive = 0
    cooldown = 0
    drop_limiter = RateLimiter(30.0)

    while not stop_event.is_set():
        try:
            xgb_input, sample = xgb_queue.get(timeout=0.5)
        except Empty:
            continue

        if stop_event.is_set():
            break

        if cooldown > 0:
            cooldown -= 1

        features = np.array(xgb_input, dtype=np.float32).reshape(1, -1)
        pred = int(model.predict(features)[0])
        proba = float(model.predict_proba(features)[0][1])

        if pred == 1:
            consecutive += 1
        else:
            consecutive = 0

        LOGGER.info(
            "window_end=%.3f count=%d bytes=%d xgb_pred=%d proba=%.3f streak=%d cooldown=%d",
            sample.end_ts,
            sample.count,
            sample.total_length,
            pred,
            proba,
            consecutive,
            cooldown,
        )

        if (
            pred == 1
            and consecutive >= XGB_CONSECUTIVE_POSITIVES
            and cooldown == 0
        ):
            with buffer_lock:
                if len(buffer) >= TST_SEQ_LENGTH:
                    sequence = list(buffer)[-TST_SEQ_LENGTH:]
                else:
                    sequence = []

            if not sequence:
                LOGGER.warning(
                    "TST trigger skipped: only %d/%d windows available",
                    len(buffer),
                    TST_SEQ_LENGTH,
                )
                continue

            if tst_queue.full():
                if drop_limiter.should_log():
                    LOGGER.warning("TST queue full; dropping trigger")
                continue

            tst_queue.put(sequence)
            LOGGER.warning(
                "XGBoost trigger: queued TST confirmation after %d consecutive positives",
                consecutive,
            )
            consecutive = 0
            cooldown = TST_COOLDOWN_WINDOWS

    LOGGER.info("XGBoost screener exiting")


def tst_confirmer_thread(
    stop_event: threading.Event,
    scaler,
    model,
    scripted: bool,
    tst_queue: Queue,
) -> None:
    LOGGER.info(
        "TST confirmer running (seq=%d, threshold=%.2f, scripted=%s)",
        TST_SEQ_LENGTH,
        TST_ATTACK_THRESHOLD,
        scripted,
    )

    while not stop_event.is_set():
        try:
            samples: List[WindowSample] = tst_queue.get(timeout=0.5)
        except Empty:
            continue

        counts = np.array([s.count for s in samples], dtype=np.float32)
        scaled = scaler.transform(counts.reshape(-1, 1)).astype(np.float32)
        tensor = torch.from_numpy(scaled.reshape(1, 1, -1))

        with torch.no_grad():
            logits = model(tensor)
            probs = torch.softmax(logits, dim=1)
            attack_prob = float(probs[0, 1])
            predicted_idx = int(torch.argmax(probs, dim=1))

        status = "CONFIRMED ATTACK" if attack_prob >= TST_ATTACK_THRESHOLD else "NORMAL"
        LOGGER.warning(
            "TST result status=%s attack_prob=%.3f predicted=%d window_end=%.3f",
            status,
            attack_prob,
            predicted_idx,
            samples[-1].end_ts,
        )

    LOGGER.info("TST confirmer exiting")


def install_signal_handlers(stop_event: threading.Event) -> None:
    def _handle_signal(signum, _frame):
        LOGGER.info("Received signal %s; shutting down", signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)


def main() -> int:
    configure_logging("hybrid-detector")
    LOGGER.info("Starting hybrid detector")

    try:
        xgb_model = load_xgb_model()
        scaler, tst_model, scripted = load_tst_model()
    except FileNotFoundError as exc:
        LOGGER.error(str(exc))
        return 1
    except Exception:
        LOGGER.exception("Failed to initialize models")
        return 1

    stop_event = threading.Event()
    install_signal_handlers(stop_event)

    counter = {"count": 0, "bytes": 0}
    counter_lock = threading.Lock()
    buffer: Deque[WindowSample] = deque(maxlen=BUFFER_SIZE)
    buffer_lock = threading.Lock()

    xgb_queue: Queue = Queue(maxsize=XGB_QUEUE_MAX)
    tst_queue: Queue = Queue(maxsize=TST_QUEUE_MAX)

    threads = [
        threading.Thread(
            target=collector_thread,
            name="collector",
            args=(stop_event, counter, counter_lock),
            daemon=True,
        ),
        threading.Thread(
            target=window_aggregator_thread,
            name="window",
            args=(stop_event, counter, counter_lock, buffer, buffer_lock, xgb_queue),
            daemon=True,
        ),
        threading.Thread(
            target=xgboost_screener_thread,
            name="xgb",
            args=(stop_event, xgb_model, buffer, buffer_lock, xgb_queue, tst_queue),
            daemon=True,
        ),
        threading.Thread(
            target=tst_confirmer_thread,
            name="tst",
            args=(stop_event, scaler, tst_model, scripted, tst_queue),
            daemon=True,
        ),
    ]

    for thread in threads:
        thread.start()

    try:
        while not stop_event.is_set():
            time.sleep(1.0)
    except KeyboardInterrupt:
        LOGGER.info("Keyboard interrupt received; stopping")
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=2.0)
        LOGGER.info("Hybrid detector stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
