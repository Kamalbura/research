"""High-frequency power monitoring helpers for drone follower."""

from __future__ import annotations

import csv
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Protocol

try:  # Best-effort hardware import; unavailable on dev hosts.
    import smbus  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised on non-Pi hosts
    try:
        import smbus2 as smbus  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover - exercised on hosts without I2C libs
        smbus = None  # type: ignore[assignment]


_DEFAULT_SAMPLE_HZ = int(os.getenv("INA219_SAMPLE_HZ", "1000"))
_DEFAULT_SHUNT_OHM = float(os.getenv("INA219_SHUNT_OHM", "0.1"))
_DEFAULT_I2C_BUS = int(os.getenv("INA219_I2C_BUS", "1"))
_DEFAULT_ADDR = int(os.getenv("INA219_ADDR", "0x40"), 16)
_DEFAULT_SIGN_MODE = os.getenv("INA219_SIGN_MODE", "auto").lower()

_RPI5_HWMON_PATH_ENV = "RPI5_HWMON_PATH"
_RPI5_HWMON_NAME_ENV = "RPI5_HWMON_NAME"
_RPI5_VOLTAGE_FILE_ENV = "RPI5_VOLTAGE_FILE"
_RPI5_CURRENT_FILE_ENV = "RPI5_CURRENT_FILE"
_RPI5_POWER_FILE_ENV = "RPI5_POWER_FILE"
_RPI5_VOLTAGE_SCALE_ENV = "RPI5_VOLTAGE_SCALE"
_RPI5_CURRENT_SCALE_ENV = "RPI5_CURRENT_SCALE"
_RPI5_POWER_SCALE_ENV = "RPI5_POWER_SCALE"

_RPI5_VOLTAGE_CANDIDATES = (
    "in0_input",
    "in1_input",
    "voltage0_input",
    "voltage1_input",
    "voltage_input",
    "vbus_input",
)

_RPI5_CURRENT_CANDIDATES = (
    "curr0_input",
    "curr1_input",
    "current0_input",
    "current1_input",
    "current_input",
    "ibus_input",
)

_RPI5_POWER_CANDIDATES = (
    "power0_input",
    "power1_input",
    "power_input",
)


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


@dataclass
class PowerSample:
    """Single instantaneous power sample."""

    timestamp_ns: int
    current_a: float
    voltage_v: float
    power_w: float


class PowerMonitorUnavailable(RuntimeError):
    """Raised when a power monitor backend cannot be initialised."""


class PowerMonitor(Protocol):
    sample_hz: int

    @property
    def sign_factor(self) -> int:  # pragma: no cover - protocol definition only
        ...

    def capture(
        self,
        *,
        label: str,
        duration_s: float,
        start_ns: Optional[int] = None,
    ) -> PowerSummary:  # pragma: no cover - protocol definition only
        ...

    def iter_samples(self, duration_s: Optional[float] = None) -> Iterator[PowerSample]:  # pragma: no cover - protocol definition only
        ...


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

    @property
    def sign_factor(self) -> int:
        return self._sign_factor

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

    def iter_samples(self, duration_s: Optional[float] = None) -> Iterator[PowerSample]:
        if self._bus is None:
            raise PowerMonitorUnavailable("power monitor not initialised")
        limit = None if duration_s is None or duration_s <= 0 else duration_s
        dt = 1.0 / float(self.sample_hz)
        next_tick = time.perf_counter()
        start_perf = time.perf_counter()
        while True:
            if limit is not None and (time.perf_counter() - start_perf) >= limit:
                break
            timestamp_ns = time.time_ns()
            current_a, voltage_v = self._read_current_voltage()
            power_w = current_a * voltage_v
            yield PowerSample(
                timestamp_ns=timestamp_ns,
                current_a=current_a,
                voltage_v=voltage_v,
                power_w=power_w,
            )
            next_tick += dt
            sleep_for = next_tick - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)

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


class Rpi5PowerMonitor:
    """Power monitor backend using Raspberry Pi 5 onboard telemetry via hwmon."""

    def __init__(
        self,
        output_dir: Path,
        *,
        sample_hz: int = _DEFAULT_SAMPLE_HZ,
        sign_mode: str = _DEFAULT_SIGN_MODE,
        hwmon_path: Optional[str] = None,
        hwmon_name_hint: Optional[str] = None,
        voltage_file: Optional[str] = None,
        current_file: Optional[str] = None,
        power_file: Optional[str] = None,
        voltage_scale: Optional[float] = None,
        current_scale: Optional[float] = None,
        power_scale: Optional[float] = None,
    ) -> None:
        del sign_mode  # Pi 5 telemetry reports already-correct sign
        if sample_hz <= 0:
            raise PowerMonitorUnavailable("sample_hz must be > 0")

        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.sample_hz = sample_hz
        self._sign_factor = 1
        self._hwmon_dir = self._find_hwmon_dir(hwmon_path, hwmon_name_hint, strict=True)
        self._voltage_path, self._current_path, self._power_path = self._resolve_channels(
            voltage_file,
            current_file,
            power_file,
        )
        self._voltage_scale = self._resolve_scale(voltage_scale, _RPI5_VOLTAGE_SCALE_ENV, 1e-6)
        self._current_scale = self._resolve_scale(current_scale, _RPI5_CURRENT_SCALE_ENV, 1e-6)
        self._power_scale = self._resolve_scale(power_scale, _RPI5_POWER_SCALE_ENV, 1e-6)

    @property
    def sign_factor(self) -> int:
        return self._sign_factor

    def capture(
        self,
        *,
        label: str,
        duration_s: float,
        start_ns: Optional[int] = None,
    ) -> PowerSummary:
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")

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
                current_a, voltage_v, power_w = self._read_measurements()
                writer.writerow([
                    time.time_ns(),
                    f"{current_a:.6f}",
                    f"{voltage_v:.6f}",
                    f"{power_w:.6f}",
                    self._sign_factor,
                ])
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

    def iter_samples(self, duration_s: Optional[float] = None) -> Iterator[PowerSample]:
        limit = None if duration_s is None or duration_s <= 0 else duration_s
        dt = 1.0 / float(self.sample_hz)
        next_tick = time.perf_counter()
        start_perf = time.perf_counter()
        while True:
            if limit is not None and (time.perf_counter() - start_perf) >= limit:
                break
            timestamp_ns = time.time_ns()
            current_a, voltage_v, power_w = self._read_measurements()
            yield PowerSample(
                timestamp_ns=timestamp_ns,
                current_a=current_a,
                voltage_v=voltage_v,
                power_w=power_w,
            )
            next_tick += dt
            sleep_for = next_tick - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)

    @staticmethod
    def is_supported(
        hwmon_path: Optional[str] = None,
        hwmon_name_hint: Optional[str] = None,
    ) -> bool:
        try:
            return Rpi5PowerMonitor._find_hwmon_dir(hwmon_path, hwmon_name_hint, strict=False) is not None
        except PowerMonitorUnavailable:
            return False

    @staticmethod
    def _find_hwmon_dir(
        hwmon_path: Optional[str],
        hwmon_name_hint: Optional[str],
        *,
        strict: bool,
    ) -> Optional[Path]:
        candidates = []
        if hwmon_path:
            candidates.append(hwmon_path)
        env_path = os.getenv(_RPI5_HWMON_PATH_ENV)
        if env_path:
            candidates.append(env_path)

        for candidate in candidates:
            path = Path(candidate).expanduser()
            if path.is_dir():
                return path
            if strict:
                raise PowerMonitorUnavailable(f"hwmon path not found: {path}")

        hwmon_root = Path("/sys/class/hwmon")
        if not hwmon_root.exists():
            if strict:
                raise PowerMonitorUnavailable("/sys/class/hwmon not present on host")
            return None

        hint_source = hwmon_name_hint or os.getenv(_RPI5_HWMON_NAME_ENV) or ""
        hints = [part.strip().lower() for part in hint_source.split(",") if part.strip()]

        for entry in sorted(hwmon_root.iterdir()):
            name_file = entry / "name"
            try:
                name_value = name_file.read_text().strip().lower()
            except Exception:
                continue
            if not name_value:
                continue
            if hints:
                if any(hint in name_value for hint in hints):
                    return entry
            else:
                if "rpi" in name_value and (
                    "power" in name_value
                    or "pmic" in name_value
                    or "monitor" in name_value
                    or "volt" in name_value
                ):
                    return entry

        if strict:
            raise PowerMonitorUnavailable("unable to locate Raspberry Pi power hwmon device")
        return None

    def _resolve_channels(
        self,
        voltage_file: Optional[str],
        current_file: Optional[str],
        power_file: Optional[str],
    ) -> tuple[Path, Path, Optional[Path]]:
        search_dirs = [self._hwmon_dir]
        device_dir = self._hwmon_dir / "device"
        if device_dir.is_dir():
            search_dirs.append(device_dir)

        def pick(
            defaults: tuple[str, ...],
            override: Optional[str],
            env_var: str,
            *,
            required: bool,
        ) -> Optional[Path]:
            # Prefer explicit override paths first.
            if override:
                override_path = Path(override)
                if override_path.is_absolute() or override_path.exists():
                    if override_path.exists():
                        return override_path
                    if required:
                        raise PowerMonitorUnavailable(f"override channel path not found: {override_path}")
                else:
                    for base in search_dirs:
                        candidate = base / override
                        if candidate.exists():
                            return candidate
                    if required:
                        raise PowerMonitorUnavailable(f"override channel name not found: {override}")

            env_override = os.getenv(env_var)
            if env_override:
                for token in env_override.split(","):
                    name = token.strip()
                    if not name:
                        continue
                    env_path = Path(name)
                    if env_path.is_absolute() or env_path.exists():
                        if env_path.exists():
                            return env_path
                        continue
                    for base in search_dirs:
                        candidate = base / name
                        if candidate.exists():
                            return candidate

            for name in defaults:
                for base in search_dirs:
                    candidate = base / name
                    if candidate.exists():
                        return candidate

            if required:
                raise PowerMonitorUnavailable(f"missing required hwmon channel {defaults[0] if defaults else 'unknown'}")
            return None

        voltage_path = pick(_RPI5_VOLTAGE_CANDIDATES, voltage_file, _RPI5_VOLTAGE_FILE_ENV, required=True)
        current_path = pick(_RPI5_CURRENT_CANDIDATES, current_file, _RPI5_CURRENT_FILE_ENV, required=True)
        power_path = pick(_RPI5_POWER_CANDIDATES, power_file, _RPI5_POWER_FILE_ENV, required=False)
        if voltage_path is None or current_path is None:
            raise PowerMonitorUnavailable("incomplete hwmon channel mapping")
        return voltage_path, current_path, power_path

    def _read_measurements(self) -> tuple[float, float, float]:
        voltage_v = self._read_channel(self._voltage_path, self._voltage_scale)
        current_a = self._read_channel(self._current_path, self._current_scale)
        if self._power_path is not None:
            power_w = self._read_channel(self._power_path, self._power_scale)
        else:
            power_w = voltage_v * current_a
        return current_a, voltage_v, power_w

    def _read_channel(self, path: Path, scale: float) -> float:
        try:
            raw = path.read_text().strip()
        except FileNotFoundError as exc:
            raise PowerMonitorUnavailable(f"hwmon channel missing: {path}") from exc
        except PermissionError as exc:  # pragma: no cover - depends on host permissions
            raise PowerMonitorUnavailable(f"insufficient permissions for {path}") from exc
        if not raw:
            raise PowerMonitorUnavailable(f"empty hwmon reading from {path}")
        try:
            value = float(raw)
        except ValueError as exc:
            raise PowerMonitorUnavailable(f"invalid hwmon reading from {path}: {raw!r}") from exc
        return value * scale

    def _resolve_scale(self, explicit: Optional[float], env_name: str, default: float) -> float:
        if explicit is not None:
            return explicit
        raw = os.getenv(env_name)
        if raw is None or raw == "":
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise PowerMonitorUnavailable(f"invalid {env_name} value: {raw!r}") from exc


def create_power_monitor(
    output_dir: Path,
    *,
    backend: str = "auto",
    sample_hz: Optional[int] = None,
    sign_mode: Optional[str] = None,
    shunt_ohm: Optional[float] = None,
    i2c_bus: Optional[int] = None,
    address: Optional[int] = None,
    hwmon_path: Optional[str] = None,
    hwmon_name_hint: Optional[str] = None,
    voltage_file: Optional[str] = None,
    current_file: Optional[str] = None,
    power_file: Optional[str] = None,
    voltage_scale: Optional[float] = None,
    current_scale: Optional[float] = None,
    power_scale: Optional[float] = None,
) -> PowerMonitor:
    resolved_backend = (backend or "auto").lower()
    env_backend = os.getenv("POWER_MONITOR_BACKEND")
    if resolved_backend == "auto" and env_backend:
        resolved_backend = env_backend.lower()

    resolved_sample_hz = int(sample_hz if sample_hz is not None else _DEFAULT_SAMPLE_HZ)
    resolved_sign_mode = (sign_mode or _DEFAULT_SIGN_MODE).lower()
    resolved_shunt = float(shunt_ohm if shunt_ohm is not None else _DEFAULT_SHUNT_OHM)
    resolved_i2c_bus = int(i2c_bus if i2c_bus is not None else _DEFAULT_I2C_BUS)
    resolved_address = address if address is not None else _DEFAULT_ADDR
    if isinstance(resolved_address, str):
        resolved_address = int(resolved_address, 0)

    ina_kwargs = {
        "i2c_bus": resolved_i2c_bus,
        "address": resolved_address,
        "shunt_ohm": resolved_shunt,
        "sample_hz": resolved_sample_hz,
        "sign_mode": resolved_sign_mode,
    }
    rpi_kwargs = {
        "sample_hz": resolved_sample_hz,
        "sign_mode": resolved_sign_mode,
        "hwmon_path": hwmon_path,
        "hwmon_name_hint": hwmon_name_hint,
        "voltage_file": voltage_file,
        "current_file": current_file,
        "power_file": power_file,
        "voltage_scale": voltage_scale,
        "current_scale": current_scale,
        "power_scale": power_scale,
    }

    if resolved_backend == "ina219":
        return Ina219PowerMonitor(output_dir, **ina_kwargs)
    if resolved_backend == "rpi5":
        return Rpi5PowerMonitor(output_dir, **rpi_kwargs)
    if resolved_backend != "auto":
        raise ValueError(f"unknown power monitor backend: {backend}")

    rpi_error: Optional[PowerMonitorUnavailable] = None
    if Rpi5PowerMonitor.is_supported(hwmon_path=hwmon_path, hwmon_name_hint=hwmon_name_hint):
        try:
            return Rpi5PowerMonitor(output_dir, **rpi_kwargs)
        except PowerMonitorUnavailable as exc:
            rpi_error = exc

    try:
        return Ina219PowerMonitor(output_dir, **ina_kwargs)
    except PowerMonitorUnavailable as exc:
        if rpi_error is not None:
            raise rpi_error
        raise


__all__ = [
    "Ina219PowerMonitor",
    "Rpi5PowerMonitor",
    "PowerMonitor",
    "PowerSummary",
    "PowerSample",
    "PowerMonitorUnavailable",
    "create_power_monitor",
]
