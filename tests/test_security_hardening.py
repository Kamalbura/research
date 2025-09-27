import logging
import socket
import threading
import time

import pytest

from core.async_proxy import _perform_handshake, run_proxy
from core.config import CONFIG
from core.handshake import HandshakeVerifyError
from core.logging_utils import get_logger
from core.suites import get_suite

try:
    from oqs.oqs import Signature
except ModuleNotFoundError:  # pragma: no cover - tests require oqs in CI
    Signature = None  # type: ignore


pytestmark = pytest.mark.skipif(Signature is None, reason="oqs-python is required for security hardening tests")


def _free_port(sock_type: int) -> int:
    if sock_type == socket.SOCK_STREAM:
        family = socket.AF_INET
    else:
        family = socket.AF_INET
    with socket.socket(family, sock_type) as s:
        if sock_type == socket.SOCK_DGRAM:
            s.bind(("127.0.0.1", 0))
        else:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
        return s.getsockname()[1]


def _make_test_config() -> dict:
    cfg = dict(CONFIG)
    cfg.update(
        {
            "TCP_HANDSHAKE_PORT": _free_port(socket.SOCK_STREAM),
            "UDP_GCS_RX": _free_port(socket.SOCK_DGRAM),
            "UDP_DRONE_RX": _free_port(socket.SOCK_DGRAM),
            "GCS_PLAINTEXT_TX": _free_port(socket.SOCK_DGRAM),
            "GCS_PLAINTEXT_RX": _free_port(socket.SOCK_DGRAM),
            "DRONE_PLAINTEXT_TX": _free_port(socket.SOCK_DGRAM),
            "DRONE_PLAINTEXT_RX": _free_port(socket.SOCK_DGRAM),
            "GCS_PLAINTEXT_HOST": "127.0.0.1",
            "DRONE_PLAINTEXT_HOST": "127.0.0.1",
            "GCS_HOST": "127.0.0.1",
            "DRONE_HOST": "127.0.0.1",
        }
    )
    return cfg


def test_gcs_handshake_rejects_unauthorized_ip():
    suite = get_suite("cs-mlkem768-aesgcm-mldsa65")
    cfg = _make_test_config()
    cfg["DRONE_HOST"] = "127.0.0.2"

    sig = Signature(suite["sig_name"])
    sig.generate_keypair()

    ready = threading.Event()

    logger = get_logger("pqc")
    captured_messages: list[str] = []

    class _ProbeHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()

        def emit(self, record):  # type: ignore[override]
            captured_messages.append(record.getMessage())

    probe = _ProbeHandler()
    logger.addHandler(probe)
    try:
        def run_server():
            with pytest.raises(NotImplementedError):
                _perform_handshake("gcs", suite, sig, None, cfg, stop_after_seconds=0.5, ready_event=ready)

        thread = threading.Thread(target=run_server)
        thread.start()
        assert ready.wait(timeout=1.0)

        with socket.create_connection(("127.0.0.1", cfg["TCP_HANDSHAKE_PORT"])):
            pass

        thread.join(timeout=2.0)
        assert not thread.is_alive()
    finally:
        logger.removeHandler(probe)

    assert any("Rejected handshake from unauthorized IP" in msg for msg in captured_messages)


def test_drone_rejects_mismatched_suite():
    suite_gcs = get_suite("cs-mlkem768-aesgcm-mldsa65")
    suite_drone = get_suite("cs-mlkem512-aesgcm-mldsa44")
    cfg = _make_test_config()

    sig = Signature(suite_gcs["sig_name"])
    gcs_public = sig.generate_keypair()

    ready = threading.Event()

    def run_server():
        with pytest.raises((ConnectionError, NotImplementedError)):
            _perform_handshake("gcs", suite_gcs, sig, None, cfg, stop_after_seconds=2.0, ready_event=ready)

    thread = threading.Thread(target=run_server)
    thread.start()
    assert ready.wait(timeout=1.0)

    with pytest.raises(HandshakeVerifyError):
        _perform_handshake("drone", suite_drone, None, gcs_public, cfg, stop_after_seconds=2.0)

    thread.join(timeout=3.0)
    assert not thread.is_alive()


def test_proxy_drops_spoofed_udp_source():
    suite = get_suite("cs-mlkem768-aesgcm-mldsa65")
    cfg = _make_test_config()

    sig = Signature(suite["sig_name"])
    gcs_public = sig.generate_keypair()

    ready = threading.Event()
    counters_holder = {}

    def run_gcs():
        counters_holder["result"] = run_proxy(
            role="gcs",
            suite=suite,
            cfg=cfg,
            gcs_sig_secret=sig,
            gcs_sig_public=None,
            stop_after_seconds=1.5,
            manual_control=False,
            quiet=True,
            ready_event=ready,
        )

    thread = threading.Thread(target=run_gcs)
    thread.start()
    assert ready.wait(timeout=1.0)

    _perform_handshake("drone", suite, None, gcs_public, cfg, stop_after_seconds=1.0)

    time.sleep(0.2)

    spoof_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        spoof_sock.bind(("127.0.0.2", 0))
        spoof_sock.sendto(b"spoof", (cfg["GCS_HOST"], cfg["UDP_GCS_RX"]))
    finally:
        spoof_sock.close()

    thread.join(timeout=5.0)
    assert not thread.is_alive()

    counters = counters_holder["result"]
    assert counters["drops"] >= 1
    assert (counters.get("drop_src_addr", 0) >= 1) or (counters.get("drop_other", 0) >= 1)
    assert counters["enc_in"] == 0
