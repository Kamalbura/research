"""High-frequency INA219 power monitoring helpers for drone follower."""

from __future__ import annotations

import csv
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:  # Best-effort hardware import; unavailable on dev hosts.
    import smbus  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised on non-Pi hosts
    smbus = None  # type: ignore[assignment]


_DEFAULT_SAMPLE_HZ = int(os.getenv("INA219_SAMPLE_HZ", "1000"))
_DEFAULT_SHUNT_OHM = float(os.getenv("INA219_SHUNT_OHM", "0.1"))
_DEFAULT_I2C_BUS = int(os.getenv("INA219_I2C_BUS", "1"))
_DEFAULT_ADDR = int(os.getenv("INA219_ADDR", "0x40"), 16)
_DEFAULT_SIGN_MODE = os.getenv("INA219_SIGN_MODE", "auto").lower()


# Registers and config masks from INA219 datasheet.
_CFG_BUS_RANGE_32V = 0x2000
_CFG_GAIN_8_320MV = 0x1800
_CFG_MODE_SANDBUS_CONT = 0x0007

_ADC_PROFILES = {
    "highspeed": {"badc": 0x0080, "sadc": 0x0000, "settle": 0.0004, "hz": 1100},
    "balanced": {"badc": 0x0400, "sadc": 0x0018, "settle": 0.0010, "hz": 900},
    "precision": {"badc": 0x0400, "sadc": 0x0048, "settle": 0.0020, "hz": 450},
}


@dataclass
class PowerSummary:
    """Aggregate statistics for a capture window."""

    label: str
    duration_s: float
    samples: int
    avg_current_a: float
    avg_voltage_v: float
    avg_power_w: float
    energy_j: float
    sample_rate_hz: float
    csv_path: str
    start_ns: int
    end_ns: int


class PowerMonitorUnavailable(RuntimeError):
    """Raised when INA219 sampling cannot be initialised."""


def _pick_profile(sample_hz: float) -> tuple[str, dict]:
    profile_key = os.getenv("INA219_ADC_PROFILE", "auto").lower()
    if profile_key == "auto":
        if sample_hz >= 900:
            profile_key = "highspeed"
        elif sample_hz >= 500:
            profile_key = "balanced"
        else:
            profile_key = "precision"
    return profile_key if profile_key in _ADC_PROFILES else "balanced", _ADC_PROFILES.get(profile_key, _ADC_PROFILES["balanced"])


def _sanitize_label(label: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in label)[:64] or "capture"


class Ina219PowerMonitor:
    """Wraps basic INA219 sampling with CSV logging and summary stats."""

    def __init__(
        self,
        output_dir: Path,
        *,
        i2c_bus: int = _DEFAULT_I2C_BUS,
        address: int = _DEFAULT_ADDR,
        shunt_ohm: float = _DEFAULT_SHUNT_OHM,
        sample_hz: int = _DEFAULT_SAMPLE_HZ,
        sign_mode: str = _DEFAULT_SIGN_MODE,
    ) -> None:
        if smbus is None:
            raise PowerMonitorUnavailable("smbus module not available on host")
        if sample_hz <= 0:
            raise PowerMonitorUnavailable("sample_hz must be > 0")

        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.address = address
        self.shunt_ohm = shunt_ohm
        self.sample_hz = sample_hz
        self._bus = None
        self._bus_lock = threading.Lock()
        self._sign_factor = 1
        self._sign_mode = sign_mode

        try:
            self._bus = smbus.SMBus(i2c_bus)
        except Exception as exc:  # pragma: no cover - requires hardware
            raise PowerMonitorUnavailable(f"failed to open I2C bus {i2c_bus}: {exc}") from exc

        try:
            self._configure(sample_hz)
            self._sign_factor = self._resolve_sign()
        except Exception as exc:  # pragma: no cover - requires hardware
            raise PowerMonitorUnavailable(f"INA219 init failed: {exc}") from exc

    def capture(
        self,
        *,
        label: str,
        duration_s: float,
        start_ns: Optional[int] = None,
    ) -> PowerSummary:
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if self._bus is None:
            raise PowerMonitorUnavailable("power monitor not initialised")

        if start_ns is not None:
            delay_ns = start_ns - time.time_ns()
            if delay_ns > 0:
                time.sleep(delay_ns / 1_000_000_000)

        safe_label = _sanitize_label(label)
        ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        csv_path = self.output_dir / f"power_{safe_label}_{ts}.csv"

        dt = 1.0 / float(self.sample_hz)
        next_tick = time.perf_counter()
        start_wall_ns = time.time_ns()
        start_perf = time.perf_counter()

        sum_current = 0.0
        sum_voltage = 0.0
        sum_power = 0.0
        samples = 0

        with open(csv_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp_ns", "current_a", "voltage_v", "power_w", "sign_factor"])

            while True:
                elapsed = time.perf_counter() - start_perf
                if elapsed >= duration_s:
                    break
                try:
                    current_a, voltage_v = self._read_current_voltage()
                except Exception as exc:  # pragma: no cover - hardware failure path
                    raise PowerMonitorUnavailable(f"INA219 read failed: {exc}") from exc

                power_w = current_a * voltage_v
                writer.writerow([time.time_ns(), f"{current_a:.6f}", f"{voltage_v:.6f}", f"{power_w:.6f}", self._sign_factor])
                if samples % 250 == 0:
                    handle.flush()

                sum_current += current_a
                sum_voltage += voltage_v
                sum_power += power_w
                samples += 1

                next_tick += dt
                sleep_for = next_tick - time.perf_counter()
                if sleep_for > 0:
                    time.sleep(sleep_for)

        end_perf = time.perf_counter()
        end_wall_ns = time.time_ns()
        elapsed_s = max(end_perf - start_perf, 1e-9)
        avg_current = sum_current / samples if samples else 0.0
        avg_voltage = sum_voltage / samples if samples else 0.0
        avg_power = sum_power / samples if samples else 0.0
        energy_j = avg_power * elapsed_s
        sample_rate = samples / elapsed_s if elapsed_s > 0 else 0.0

        return PowerSummary(
            label=safe_label,
            duration_s=elapsed_s,
            samples=samples,
            avg_current_a=avg_current,
            avg_voltage_v=avg_voltage,
            avg_power_w=avg_power,
            energy_j=energy_j,
            sample_rate_hz=sample_rate,
            csv_path=str(csv_path.resolve()),
            start_ns=start_wall_ns,
            end_ns=end_wall_ns,
        )

    def _configure(self, sample_hz: float) -> None:
        profile_key, profile = _pick_profile(sample_hz)
        cfg = (
            _CFG_BUS_RANGE_32V
            | _CFG_GAIN_8_320MV
            | profile["badc"]
            | profile["sadc"]
            | _CFG_MODE_SANDBUS_CONT
        )
        payload = [(cfg >> 8) & 0xFF, cfg & 0xFF]
        with self._bus_lock:
            self._bus.write_i2c_block_data(self.address, 0x00, payload)  # type: ignore[union-attr]
        time.sleep(profile["settle"])

    def _resolve_sign(self) -> int:
        mode = self._sign_mode
        if mode.startswith("pos"):
            return 1
        if mode.startswith("neg"):
            return -1
        probe_deadline = time.time() + float(os.getenv("INA219_SIGN_PROBE_SEC", "2"))
        readings = []
        while time.time() < probe_deadline:
            vsh = self._read_shunt_voltage()
            readings.append(vsh)
            time.sleep(0.005)
        if not readings:
            return 1
        readings.sort()
        median = readings[len(readings) // 2]
        return -1 if median < -20e-6 else 1

    def _read_current_voltage(self) -> tuple[float, float]:
        vsh = self._read_shunt_voltage()
        current = (vsh / self.shunt_ohm) * self._sign_factor
        voltage = self._read_bus_voltage()
        return current, voltage

    def _read_shunt_voltage(self) -> float:
        raw = self._read_s16(0x01)
        return raw * 10e-6

    def _read_bus_voltage(self) -> float:
        raw = self._read_u16(0x02)
        return ((raw >> 3) & 0x1FFF) * 0.004

    def _read_u16(self, register: int) -> int:
        with self._bus_lock:
            hi, lo = self._bus.read_i2c_block_data(self.address, register, 2)  # type: ignore[union-attr]
        return (hi << 8) | lo

    def _read_s16(self, register: int) -> int:
        val = self._read_u16(register)
        if val & 0x8000:
            val -= 1 << 16
        return val


__all__ = [
    "Ina219PowerMonitor",
    "PowerSummary",
    "PowerMonitorUnavailable",
]
