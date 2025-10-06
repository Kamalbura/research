"""Real-time TST-only DDoS detector for MAVLink-over-UDP."""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Deque, Dict, List

import joblib
import numpy as np
import torch

from config import (
    BUFFER_SIZE,
    IFACE,
    PORT,
    SCALER_FILE,
    TORCH_NUM_THREADS,
    TST_ATTACK_THRESHOLD,
    TST_CLEAR_THRESHOLD,
    TST_CONFIRM_POSITIVES,
    TST_MODEL_FILE,
    TST_QUEUE_MAX,
    TST_SEQ_LENGTH,
    TST_TORCHSCRIPT_FILE,
    WINDOW_SIZE,
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
    start_ts: float
    end_ts: float
    count: int
    total_length: int


class RateLimiter:
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


def _safe_torch_load(path):
    try:
        return torch.load(str(path), map_location="cpu", weights_only=False)
    except TypeError:
        return torch.load(str(path), map_location="cpu")


def load_tst_model():
    ensure_file(SCALER_FILE, "StandardScaler pickle")
    scaler = joblib.load(SCALER_FILE)

    if TST_TORCHSCRIPT_FILE.exists():
        model = torch.jit.load(str(TST_TORCHSCRIPT_FILE), map_location="cpu")
        scripted = True
        LOGGER.info("Loaded TorchScript TST model from %s", TST_TORCHSCRIPT_FILE)
    else:
        ensure_file(TST_MODEL_FILE, "PyTorch TST model")
        LOGGER.warning(
            "TorchScript model not found; falling back to .pth (requires tstplus module)."
        )
        try:
            from tstplus import (
                TSTPlus,
                _TSTBackbone,
                _TSTEncoder,
                _TSTEncoderLayer,
            )  # noqa: F401  (register classes for torch.load)
            globals().setdefault("TSTPlus", TSTPlus)
            globals().setdefault("_TSTBackbone", _TSTBackbone)
            globals().setdefault("_TSTEncoder", _TSTEncoder)
            globals().setdefault("_TSTEncoderLayer", _TSTEncoderLayer)
        except Exception as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "TorchScript model missing and fallback import of tstplus.TSTPlus failed. "
                "Install the 'tsai' dependency and ensure tstplus.py is accessible."
            ) from exc
        model = _safe_torch_load(TST_MODEL_FILE)
        scripted = False

    model.eval()
    torch.set_num_threads(TORCH_NUM_THREADS)

    # Validate that scaler and model agree with the configured sequence length and
    # produce a 2-class output tensor. Failing fast here avoids obscure runtime errors
    # deep inside the detector thread.
    try:
        zero_counts = np.zeros((TST_SEQ_LENGTH, 1), dtype=np.float32)
        scaled = scaler.transform(zero_counts).astype(np.float32)
    except Exception as exc:
        raise ValueError(
            "Scaler failed to transform a zero vector; ensure scaler.pkl matches training pipeline"
        ) from exc

    tensor = torch.from_numpy(scaled.reshape(1, 1, -1))
    with torch.no_grad():
        try:
            logits = model(tensor)
        except Exception as exc:
            raise ValueError(
                f"TST model rejected input shaped (1, 1, {TST_SEQ_LENGTH}); check seq length and architecture"
            ) from exc

    _ = _logits_to_probs(logits)

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
            if payload and payload[0] in (0xFD, 0xFE):
                length = len(payload)
                with counter_lock:
                    counter["count"] += 1
                    counter["bytes"] += length
    bpf = get_udp_bpf()
    try:
        sniffer = scapy.AsyncSniffer(
            iface=IFACE,
            store=False,
            prn=packet_callback,
            filter=bpf,
        )
        sniffer.start()
    except Exception:
        LOGGER.exception("Failed to start sniffer with payload filter; falling back to port-only")
        sniffer = scapy.AsyncSniffer(
            iface=IFACE,
            store=False,
            prn=packet_callback,
            filter=f"udp and port {PORT}",
        )
        sniffer.start()

    try:
        while not stop_event.wait(0.5):
            pass
    finally:
        try:
            sniffer.stop()
        except Exception:
            LOGGER.exception("Error stopping sniffer")


def window_thread(
    stop_event: threading.Event,
    counter: Dict[str, int],
    counter_lock: threading.Lock,
    buffer: Deque[WindowSample],
    buffer_lock: threading.Lock,
    detect_queue: Queue,
) -> None:
    LOGGER.info("Window aggregator started (window=%.2fs seq=%d)", WINDOW_SIZE, TST_SEQ_LENGTH)
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

        sequence: List[WindowSample] = []
        with buffer_lock:
            buffer.append(sample)
            if len(buffer) >= TST_SEQ_LENGTH:
                sequence = list(buffer)[-TST_SEQ_LENGTH:]

        LOGGER.info(
            "window_end=%.3f count=%d bytes=%d buffered=%d",
            sample.end_ts,
            sample.count,
            sample.total_length,
            len(buffer),
        )

        if sequence:
            try:
                detect_queue.put_nowait(sequence)
            except Full:
                if drop_limiter.should_log():
                    LOGGER.warning("Detection queue full; dropping TST sequence")

        window_start = deadline

    LOGGER.info("Window aggregator exiting")


def detector_thread(
    stop_event: threading.Event,
    scaler,
    model,
    scripted: bool,
    detect_queue: Queue,
) -> None:
    LOGGER.info(
        "Detector running (seq=%d threshold=%.2f scripted=%s)",
        TST_SEQ_LENGTH,
        TST_ATTACK_THRESHOLD,
        scripted,
    )

    current_alert = False
    confirm_streak = 0
    rate_limiter = RateLimiter(15.0)

    while not stop_event.is_set():
        try:
            sequence: List[WindowSample] = detect_queue.get(timeout=0.5)
        except Empty:
            continue

        counts = np.array([s.count for s in sequence], dtype=np.float32)
        scaled = scaler.transform(counts.reshape(-1, 1)).astype(np.float32)
        tensor = torch.from_numpy(scaled.reshape(1, 1, -1))

        start = time.time()
        with torch.no_grad():
            logits = model(tensor)
            probs = _logits_to_probs(logits)
            attack_prob = float(probs[0, 1])
            predicted_idx = int(torch.argmax(probs, dim=1))
        duration_ms = (time.time() - start) * 1000.0

        LOGGER.debug(
            "TST inference attack_prob=%.3f predicted=%d duration_ms=%.1f window_end=%.3f",
            attack_prob,
            predicted_idx,
            duration_ms,
            sequence[-1].end_ts,
        )

        if not current_alert:
            if attack_prob >= TST_ATTACK_THRESHOLD:
                confirm_streak += 1
                if confirm_streak >= TST_CONFIRM_POSITIVES:
                    current_alert = True
                    confirm_streak = 0
                    LOGGER.warning(
                        "TST CONFIRMED ATTACK (consecutive=%d, prob=%.3f, window_end=%.3f)",
                        TST_CONFIRM_POSITIVES,
                        attack_prob,
                        sequence[-1].end_ts,
                    )
                elif rate_limiter.should_log():
                    LOGGER.info(
                        "TST pending confirmation %d/%d prob=%.3f window_end=%.3f",
                        confirm_streak,
                        TST_CONFIRM_POSITIVES,
                        attack_prob,
                        sequence[-1].end_ts,
                    )
            else:
                if confirm_streak and rate_limiter.should_log():
                    LOGGER.info(
                        "TST reset confirmation streak prob=%.3f window_end=%.3f",
                        attack_prob,
                        sequence[-1].end_ts,
                    )
                confirm_streak = 0
        else:
            if attack_prob <= TST_CLEAR_THRESHOLD:
                current_alert = False
                LOGGER.warning(
                    "TST back to NORMAL (prob=%.3f <= clear=%.2f, window_end=%.3f)",
                    attack_prob,
                    TST_CLEAR_THRESHOLD,
                    sequence[-1].end_ts,
                )
            elif rate_limiter.should_log():
                LOGGER.info(
                    "TST sustained attack prob=%.3f window_end=%.3f",
                    attack_prob,
                    sequence[-1].end_ts,
                )

    LOGGER.info("Detector exiting")


def install_signal_handlers(stop_event: threading.Event) -> None:
    def _handle_signal(signum, _frame):
        LOGGER.info("Received signal %s; shutting down", signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)


def main() -> int:
    configure_logging("tst-realtime")
    LOGGER.info("Starting realtime TST detector")

    try:
        scaler, model, scripted = load_tst_model()
    except FileNotFoundError as exc:
        LOGGER.error(str(exc))
        return 1
    except Exception:
        LOGGER.exception("Failed to initialize model or scaler")
        return 1

    stop_event = threading.Event()
    install_signal_handlers(stop_event)

    counter = {"count": 0, "bytes": 0}
    counter_lock = threading.Lock()
    buffer: Deque[WindowSample] = deque(maxlen=BUFFER_SIZE)
    buffer_lock = threading.Lock()
    detect_queue: Queue = Queue(maxsize=TST_QUEUE_MAX)

    threads = [
        threading.Thread(
            target=collector_thread,
            name="collector",
            args=(stop_event, counter, counter_lock),
            daemon=True,
        ),
        threading.Thread(
            target=window_thread,
            name="window",
            args=(stop_event, counter, counter_lock, buffer, buffer_lock, detect_queue),
            daemon=True,
        ),
        threading.Thread(
            target=detector_thread,
            name="detector",
            args=(stop_event, scaler, model, scripted, detect_queue),
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
        LOGGER.info("Realtime TST detector stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
