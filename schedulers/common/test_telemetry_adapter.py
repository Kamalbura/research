import time
from schedulers.common.telemetry_adapter import normalize_message


def test_system_sample_normalization():
    msg = {
        "kind": "system_sample",
        "timestamp_ns": time.time_ns(),
        "suite": "cs-mlkem768-aesgcm-mldsa65",
        "cpu_percent": 12.5,
        "cpu_freq_mhz": 1500.0,
        "cpu_temp_c": 45.0,
        "mem_used_mb": 128.0,
        "mem_percent": 10.0,
    }
    out = normalize_message(msg)
    assert out["suite"] == msg["suite"]
    assert out["cpu_percent"] == 12.5
    assert out["cpu_temp_c"] == 45.0


def test_power_summary_normalization():
    msg = {"kind": "power_summary", "timestamp_ns": time.time_ns(), "suite": "s", "avg_power_w": 5.25, "energy_j": 10.0}
    out = normalize_message(msg)
    assert out["power_avg_w"] == 5.25
    assert out["power_energy_j"] == 10.0


def test_udp_echo_sample_normalization():
    msg = {"kind": "udp_echo_sample", "timestamp_ns": time.time_ns(), "sequence": 123, "processing_ns": 2000, "suite": "s"}
    out = normalize_message(msg)
    assert out["udp_sequence"] == 123
    assert out["udp_processing_ns"] == 2000
