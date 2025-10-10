"""Utility script to exercise manual rekey across AEAD variants.

Starts local GCS and drone proxies on random loopback ports, triggers a manual
rekey to a target suite via the console automation, and prints condensed
counters for verification.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from contextlib import closing
from typing import Dict, Optional, Tuple

from pathlib import Path
from unittest.mock import patch

from oqs.oqs import Signature

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.async_proxy import run_proxy
from core.config import CONFIG
from core.suites import get_suite


def _alloc_port(sock_type: int) -> int:
    family = socket.AF_INET
    with closing(socket.socket(family, sock_type)) as sock:
        bind_host = "127.0.0.1"
        if sock_type == socket.SOCK_STREAM:
            sock.bind((bind_host, 0))
            sock.listen(1)
        else:
            sock.bind((bind_host, 0))
        return sock.getsockname()[1]


def _make_config() -> Dict[str, int | str]:
    cfg = dict(CONFIG)
    cfg.update(
        {
            "TCP_HANDSHAKE_PORT": _alloc_port(socket.SOCK_STREAM),
            "UDP_GCS_RX": _alloc_port(socket.SOCK_DGRAM),
            "UDP_DRONE_RX": _alloc_port(socket.SOCK_DGRAM),
            "GCS_PLAINTEXT_TX": _alloc_port(socket.SOCK_DGRAM),
            "GCS_PLAINTEXT_RX": _alloc_port(socket.SOCK_DGRAM),
            "DRONE_PLAINTEXT_TX": _alloc_port(socket.SOCK_DGRAM),
            "DRONE_PLAINTEXT_RX": _alloc_port(socket.SOCK_DGRAM),
            "DRONE_HOST": "127.0.0.1",
            "GCS_HOST": "127.0.0.1",
            "DRONE_PLAINTEXT_HOST": "127.0.0.1",
            "GCS_PLAINTEXT_HOST": "127.0.0.1",
        }
    )
    return cfg


def _scripted_input(commands: list[Tuple[float, str]]):
    pending = commands.copy()

    def _inner(prompt: str = "") -> str:  # noqa: D401 - matches builtins.input signature
        delay: float
        value: str
        if pending:
            delay, value = pending.pop(0)
            if delay > 0:
                time.sleep(delay)
            print(f"[manual] {value}")
            return value
        time.sleep(0.5)
        return "quit"

    return _inner


def run_case(initial_suite_id: str, target_suite_id: str, dwell_s: float = 12.0) -> Dict[str, Dict[str, object]]:
    initial_suite = get_suite(initial_suite_id)
    target_suite = get_suite(target_suite_id)

    signature = Signature(initial_suite["sig_name"])
    gcs_public = signature.generate_keypair()

    cfg = _make_config()
    cfg["SUITE_AEAD_TOKEN"] = initial_suite["aead_token"]

    commands = [
        (2.0, target_suite["suite_id"]),
        (4.0, "quit"),
    ]

    gcs_ready = threading.Event()
    results: Dict[str, Dict[str, object]] = {}
    errors: Dict[str, Exception] = {}

    def gcs_worker() -> None:
        try:
            with patch("builtins.input", _scripted_input(commands)):
                counters = run_proxy(
                    role="gcs",
                    suite=initial_suite,
                    cfg=cfg,
                    gcs_sig_secret=signature,
                    gcs_sig_public=None,
                    stop_after_seconds=dwell_s,
                    manual_control=True,
                    quiet=True,
                    ready_event=gcs_ready,
                    load_gcs_secret=lambda suite_info: signature,
                )
            results["gcs"] = counters
        except Exception as exc:  # pragma: no cover - diagnostic script
            errors["gcs"] = exc

    def drone_worker() -> None:
        try:
            counters = run_proxy(
                role="drone",
                suite=initial_suite,
                cfg=cfg,
                gcs_sig_secret=None,
                gcs_sig_public=gcs_public,
                stop_after_seconds=dwell_s,
                manual_control=False,
                quiet=True,
                load_gcs_public=lambda suite_info: gcs_public,
            )
            results["drone"] = counters
        except Exception as exc:  # pragma: no cover - diagnostic script
            errors["drone"] = exc

    gcs_thread = threading.Thread(target=gcs_worker, name="gcs-runner", daemon=True)
    drone_thread = threading.Thread(target=drone_worker, name="drone-runner", daemon=True)

    gcs_thread.start()
    if not gcs_ready.wait(timeout=5.0):
        raise RuntimeError("GCS proxy failed to bind handshake socket in time")
    drone_thread.start()

    gcs_thread.join(timeout=dwell_s + 5.0)
    drone_thread.join(timeout=dwell_s + 5.0)

    if gcs_thread.is_alive() or drone_thread.is_alive():
        raise RuntimeError("Proxies did not terminate as expected")

    if errors:
        raise RuntimeError(f"Proxy errors: {errors}")

    return results


def _summarise(label: str, counters: Optional[Dict[str, object]]) -> str:
    if not counters:
        return f"{label}: no counters"
    fields = {
        "suite": counters.get("suite"),
        "last_rekey_suite": counters.get("last_rekey_suite"),
        "rekeys_ok": counters.get("rekeys_ok"),
        "rekeys_fail": counters.get("rekeys_fail"),
    }
    return f"{label}: " + ", ".join(f"{key}={value}" for key, value in fields.items())


def main() -> None:
    scenarios = [
        ("cs-mlkem768-aesgcm-mldsa65", "cs-mlkem768-chacha20poly1305-mldsa65"),
        ("cs-mlkem768-aesgcm-mldsa65", "cs-mlkem768-ascon128-mldsa65"),
    ]

    for initial, target in scenarios:
        print(f"=== {initial} -> {target} ===")
        counters = run_case(initial, target)
        gcs_summary = _summarise("gcs", counters.get("gcs"))
        drone_summary = _summarise("drone", counters.get("drone"))
        print(gcs_summary)
        print(drone_summary)
        print()


if __name__ == "__main__":
    main()
