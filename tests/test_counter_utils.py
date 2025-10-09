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
            "primitive_metrics": {
                "aead_encrypt": {
                    "count": 4,
                    "total_ns": 2_000,
                    "min_ns": 300,
                    "max_ns": 900,
                    "total_in_bytes": 2400,
                    "total_out_bytes": 3200,
                },
                "aead_decrypt_ok": {
                    "count": 3,
                    "total_ns": 1_500,
                    "min_ns": 400,
                    "max_ns": 700,
                    "total_in_bytes": 3300,
                    "total_out_bytes": 2100,
                },
            },
            "part_b_metrics": {
                "kem_keygen_max_ms": 1.25,
                "kem_keygen_avg_ms": 1.25,
                "kem_keygen_ms": 1.25,
                "kem_encaps_max_ms": 2.5,
                "kem_encaps_avg_ms": 2.5,
                "kem_encaps_ms": 2.5,
                "kem_decaps_max_ms": 3.75,
                "kem_decaps_avg_ms": 3.75,
                "kem_decap_ms": 3.75,
                "sig_sign_max_ms": 4.0,
                "sig_sign_avg_ms": 4.0,
                "sig_sign_ms": 4.0,
                "sig_verify_max_ms": 5.5,
                "sig_verify_avg_ms": 5.5,
                "sig_verify_ms": 5.5,
                "aead_encrypt_avg_ms": 0.42,
                "aead_decrypt_avg_ms": 0.55,
                "aead_encrypt_ms": 0.42,
                "aead_decrypt_ms": 0.55,
                "pub_key_size_bytes": 1184,
                "ciphertext_size_bytes": 1088,
                "sig_size_bytes": 3293,
                "shared_secret_size_bytes": 32,
                "primitive_total_ms": 17.0,
                "rekey_ms": 18.5,
                "kem_keygen_mJ": 0.8,
                "kem_encaps_mJ": 1.6,
                "kem_decap_mJ": 2.4,
                "sig_sign_mJ": 3.2,
                "sig_verify_mJ": 4.0,
            },
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
    assert result.handshake_metrics == {}
    assert "aead_encrypt" in result.primitive_metrics
    encrypt_stats = result.primitive_metrics["aead_encrypt"]
    assert encrypt_stats["count"] == 4
    assert encrypt_stats["min_ns"] == 300
    assert encrypt_stats["total_out_bytes"] == 3200
    assert result.primitive_average_ns("aead_encrypt") == 500
    assert result.primitive_average_ns("aead_decrypt_ok") == 500
    assert result.primitive_average_ns("missing") is None

    part_b = result.part_b_metrics
    assert part_b["kem_decaps_max_ms"] == pytest.approx(3.75)
    assert part_b["kem_decap_ms"] == pytest.approx(3.75)
    assert part_b["pub_key_size_bytes"] == 1184
    assert part_b["rekey_ms"] == pytest.approx(18.5)
    assert part_b["aead_encrypt_avg_ms"] == pytest.approx(0.42)
    assert part_b["aead_encrypt_ms"] == pytest.approx(0.42)
    assert result.get_part_b_metric("sig_sign_ms") == pytest.approx(4.0)
    assert result.get_part_b_metric("missing", default=-1.0) == -1.0
    assert result.get_part_b_metric("sig_verify_mJ") == pytest.approx(4.0)

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
