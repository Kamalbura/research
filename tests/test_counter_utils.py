from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.counter_utils import (
    ProxyCounters,
    TrafficSummary,
    load_proxy_counters,
    load_traffic_summary,
)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_proxy_counters_success(tmp_path: Path) -> None:
    payload = {
        "role": "gcs",
        "suite": "cs-kyber768-aesgcm-dilithium3",
        "counters": {
            "rekeys_ok": 2,
            "rekeys_fail": 0,
            "last_rekey_suite": "cs-kyber1024-aesgcm-dilithium5",
        },
        "ts_stop_ns": 42,
    }
    file_path = tmp_path / "proxy.json"
    _write_json(file_path, payload)

    result = load_proxy_counters(file_path)

    assert isinstance(result, ProxyCounters)
    assert result.role == "gcs"
    assert result.suite == "cs-kyber768-aesgcm-dilithium3"
    assert result.rekeys_ok == 2
    assert result.rekeys_fail == 0
    assert result.last_rekey_suite == "cs-kyber1024-aesgcm-dilithium5"
    assert result.ts_stop_ns == 42
    assert result.path == file_path

    # Should not raise when suite matches
    result.ensure_rekey("cs-kyber1024-aesgcm-dilithium5")
    with pytest.raises(ValueError):
        result.ensure_rekey("cs-kyber512-aesgcm-dilithium2")


def test_ensure_rekey_failure(tmp_path: Path) -> None:
    payload = {
        "role": "drone",
        "suite": "cs-kyber768-aesgcm-dilithium3",
        "counters": {"rekeys_ok": 0, "last_rekey_suite": ""},
    }
    file_path = tmp_path / "proxy_fail.json"
    _write_json(file_path, payload)

    result = load_proxy_counters(file_path)
    with pytest.raises(ValueError):
        result.ensure_rekey("cs-kyber1024-aesgcm-dilithium5")


def test_load_traffic_summary(tmp_path: Path) -> None:
    payload = {
        "role": "gcs",
        "peer_role": "drone",
        "sent_total": 200,
        "recv_total": 198,
        "tx_bytes_total": 4096,
        "rx_bytes_total": 4000,
        "first_send_ts": "2025-09-26T06:37:00Z",
        "last_send_ts": "2025-09-26T06:38:10Z",
        "first_recv_ts": "2025-09-26T06:37:01Z",
        "last_recv_ts": "2025-09-26T06:38:12Z",
        "out_of_order": 0,
        "unique_senders": 1,
    }
    file_path = tmp_path / "traffic.json"
    _write_json(file_path, payload)

    summary = load_traffic_summary(file_path)
    assert isinstance(summary, TrafficSummary)
    assert summary.role == "gcs"
    assert summary.peer_role == "drone"
    assert summary.sent_total == 200
    assert summary.recv_total == 198
    assert summary.tx_bytes_total == 4096
    assert summary.rx_bytes_total == 4000
    assert summary.out_of_order == 0
    assert summary.unique_senders == 1
    assert summary.first_send_ts == "2025-09-26T06:37:00Z"
    assert summary.path == file_path


def test_missing_proxy_file_raises(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.json"
    with pytest.raises(FileNotFoundError):
        load_proxy_counters(missing_path)
