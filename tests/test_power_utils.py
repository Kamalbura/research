"""Tests for tools.power_utils helper functions."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from tools.power_utils import (
    PowerSample,
    align_gcs_to_drone,
    integrate_energy_mj,
    load_power_trace,
)


def _write_csv(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_load_power_trace_preserves_order(tmp_path: Path) -> None:
    csv_path = tmp_path / "trace.csv"
    _write_csv(
        csv_path,
        [
            "timestamp_ns,power_w,sign",
            "0,1.5,1",
            "1000000000,2.5,1",
            "2000000000,3.5,-1",
        ],
    )

    samples = load_power_trace(csv_path)
    assert [sample.ts_ns for sample in samples] == [0, 1_000_000_000, 2_000_000_000]
    assert math.isclose(samples[0].power_w, 1.5)
    assert math.isclose(samples[1].power_w, 2.5)
    assert math.isclose(samples[2].power_w, -3.5)


def test_load_power_trace_derives_power_from_voltage_current(tmp_path: Path) -> None:
    csv_path = tmp_path / "trace_voltage.csv"
    _write_csv(
        csv_path,
        [
            "timestamp_ns,current_a,voltage_v",
            "0,0.5,12.0",
            "500000000,0.75,12.0",
        ],
    )

    samples = load_power_trace(csv_path)
    assert len(samples) == 2
    assert math.isclose(samples[0].power_w, 6.0)
    assert math.isclose(samples[1].power_w, 9.0)


def test_integrate_energy_mj_trapezoid(tmp_path: Path) -> None:
    csv_path = tmp_path / "trace_energy.csv"
    _write_csv(
        csv_path,
        [
            "timestamp_ns,power_w",
            "0,2.0",
            "1000000000,6.0",
        ],
    )

    samples = load_power_trace(csv_path)
    energy_mj, segments = integrate_energy_mj(samples, 0, 1_000_000_000)
    assert segments == 1
    assert math.isclose(energy_mj, 4_000.0, rel_tol=1e-6)

    half_energy, _ = integrate_energy_mj(samples, 500_000_000, 1_000_000_000)
    assert math.isclose(half_energy, 2_500.0, rel_tol=1e-6)


def test_align_gcs_to_drone() -> None:
    assert align_gcs_to_drone(100, -50) == 50
    assert align_gcs_to_drone(1_000_000_000, 250) == 1_000_000_250


@pytest.mark.parametrize(
    "start_ns, end_ns",
    [
        (0, 0),
        (100, 50),
    ],
)
def test_integrate_energy_mj_empty_window(start_ns: int, end_ns: int) -> None:
    samples: list[PowerSample] = [PowerSample(ts_ns=0, power_w=1.0)]
    energy_mj, segments = integrate_energy_mj(samples, start_ns, end_ns)
    assert energy_mj == 0.0
    assert segments == 0
