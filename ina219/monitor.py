#!/usr/bin/env python3
import os
import time
import csv
import math
from datetime import datetime
import smbus
import multiprocessing as mp

# ----------------- Config (overridable by env) -----------------
I2C_BUS = 1
INA_ADDR = int(os.getenv("INA_ADDR", "0x40"), 16)
SHUNT_OHM = float(os.getenv("SHUNT_OHMS", "0.1"))  # R100=0.10 ohm, R050=0.05 ohm
SAMPLE_HZ = int(os.getenv("SAMPLE_HZ", "1000"))
PHASE_SEC = float(os.getenv("PHASE_SEC", "10"))
SIGN_MODE = os.getenv("FORCE_SIGN", "auto").lower()  # 'auto' | 'positive' | 'negative'
SIGN_PROBE_SEC = float(os.getenv("SIGN_PROBE_SEC", "3"))  # how long to sniff orientation at start (auto mode)

CSV_OUT = f"ina219_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

# Default register masks (INA219 datasheet)
_CFG_BUS_RANGE_32V = 0x2000
_CFG_GAIN_8_320MV = 0x1800
_CFG_MODE_SANDBUS_CONT = 0x0007
_CFG_RESET = 0x8000

_ADC_PROFILES = {
    "highspeed": {
        "badc": 0x0080,   # 9-bit 84us
        "sadc": 0x0000,   # 9-bit 84us
        "label": "9-bit (84us conversions)",
        "max_hz": 1100,
        "settle": 0.0004,
    },
    "balanced": {
        "badc": 0x0400,   # 12-bit 532us
        "sadc": 0x0018,   # 12-bit 532us
        "label": "12-bit (532us conversions)",
        "max_hz": 900,
        "settle": 0.001,
    },
    "precision": {
        "badc": 0x0400,
        "sadc": 0x0048,   # 12-bit w/2x averaging (~1.06ms)
        "label": "12-bit w/2x averaging (~1.06ms)",
        "max_hz": 450,
        "settle": 0.002,
    },
}

# ----------------- I2C helpers -----------------
bus = smbus.SMBus(I2C_BUS)

def read_u16(addr, reg):
    hi, lo = bus.read_i2c_block_data(addr, reg, 2)
    return (hi << 8) | lo

def read_s16(addr, reg):
    val = read_u16(addr, reg)
    if val & 0x8000:
        val -= 1 << 16
    return val

def read_shunt_voltage_V():
    # 0x01: shunt voltage, 10 microvolt LSB, signed
    raw = read_s16(INA_ADDR, 0x01)
    return raw * 10e-6

def read_bus_voltage_V():
    # 0x02: bus voltage, bits 15..3 value, LSB = 4 mV
    raw = read_u16(INA_ADDR, 0x02)
    return ((raw >> 3) & 0x1FFF) * 0.004

# ----------------- Current calc w/ sign handling -----------------
def detect_sign_auto(seconds=SIGN_PROBE_SEC):
    """Sniff shunt polarity for a short window. If median shunt V < -20 microvolt, assume reversed."""
    if seconds <= 0:
        return +1
    samples = []
    t0 = time.time()
    dt = 1.0 / max(5, SAMPLE_HZ)  # at least 5 Hz during probe
    while time.time() - t0 < seconds:
        samples.append(read_shunt_voltage_V())
        time.sleep(dt)
    if not samples:
        return +1
    med = sorted(samples)[len(samples) // 2]
    # Threshold avoids flipping due to noise around 0
    return -1 if med < -20e-6 else +1

def resolve_sign():
    if SIGN_MODE.startswith("pos"):
        return +1, "forced-positive"
    if SIGN_MODE.startswith("neg"):
        return -1, "forced-negative"
    s = detect_sign_auto()
    return s, "auto-inverted" if s == -1 else "auto-normal"

def read_current_A(sign_factor):
    vsh = read_shunt_voltage_V()  # raw (can be negative)
    amps_raw = vsh / SHUNT_OHM
    amps = amps_raw * sign_factor  # corrected to positive for your wiring
    return amps, vsh, amps_raw

# ----------------- Device setup -----------------
def _pick_profile(sample_hz: float) -> tuple[str, dict]:
    profile_key = os.getenv("INA219_ADC_PROFILE", "auto").lower()
    if profile_key == "auto":
        if sample_hz >= 900:
            profile_key = "highspeed"
        elif sample_hz >= 500:
            profile_key = "balanced"
        else:
            profile_key = "precision"
    if profile_key not in _ADC_PROFILES:
        profile_key = "balanced"
    return profile_key, _ADC_PROFILES[profile_key]

def configure_ina219(sample_hz: float) -> tuple[str, float]:
    profile_key, profile = _pick_profile(sample_hz)
    cfg = (
        _CFG_BUS_RANGE_32V
        | _CFG_GAIN_8_320MV
        | profile["badc"]
        | profile["sadc"]
        | _CFG_MODE_SANDBUS_CONT
    )
    bus.write_i2c_block_data(INA_ADDR, 0x00, [(cfg >> 8) & 0xFF, cfg & 0xFF])
    time.sleep(profile["settle"])
    return profile["label"], profile["max_hz"]

# ----------------- Load generator (for the 'load' phase) -----------------
def _burn(stop_ts):
    x = 0.0
    while time.time() < stop_ts:
        x = math.sin(x) * math.cos(x) + 1.234567

def cpu_stress(seconds, procs=None):
    if procs is None:
        procs = max(1, mp.cpu_count() - 1)
    stop_ts = time.time() + seconds
    ps = [mp.Process(target=_burn, args=(stop_ts,)) for _ in range(procs)]
    for p in ps:
        p.start()
    for p in ps:
        p.join()

# ----------------- Phases & summary -----------------
def sample_phase(label, seconds, writer, sign_factor):
    dt = 1.0 / SAMPLE_HZ
    t0 = time.perf_counter()
    neg_seen = False
    sample_count = 0
    target = t0
    read_time = time.time
    sleep_fn = time.sleep
    writerow = writer.writerow
    while True:
        now = time.perf_counter()
        if now - t0 >= seconds:
            break
        amps, vsh, amps_raw = read_current_A(sign_factor)
        vbus = read_bus_voltage_V()
        if vsh < 0:
            neg_seen = True
        writerow([
            f"{read_time():.3f}",
            label,
            f"{amps:.6f}",
            f"{vbus:.3f}",
            f"{vsh:.6e}",
            f"{amps_raw:.6f}",
            f"{sign_factor:+d}",
        ])
        sample_count += 1
        target += dt
        sleep_duration = target - time.perf_counter()
        if sleep_duration > 0:
            sleep_fn(sleep_duration)
    elapsed = time.perf_counter() - t0
    return neg_seen, sample_count, elapsed

def summarize(csv_path):
    phases = {"idle1": [], "load": [], "idle2": []}
    with open(csv_path, newline="") as f:
        r = csv.reader(f)
        next(r)
        for ts, phase, amps, vbus, vsh, amps_raw, signf in r:
            if phase in phases:
                phases[phase].append(float(amps))
    results = {}
    for k, arr in phases.items():
        if arr:
            mean = sum(arr) / len(arr)
            var = sum((x - mean) ** 2 for x in arr) / len(arr)
            results[k] = dict(mean=mean, stdev=var ** 0.5, n=len(arr))
        else:
            results[k] = dict(mean=0.0, stdev=0.0, n=0)
    return results

def main():
    profile_label, profile_ceiling = configure_ina219(SAMPLE_HZ)
    print(f"INA219 @ {hex(INA_ADDR)}, SHUNT={SHUNT_OHM} ohm, sample={SAMPLE_HZ} Hz, each phase={PHASE_SEC}s")
    print(f"ADC profile     : {profile_label} (recommended <= {profile_ceiling} Hz)")
    sign_factor, sign_mode = resolve_sign()
    print(f"Sign handling  : {sign_mode} (factor {sign_factor:+d})")

    with open(CSV_OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "phase", "amps_A", "vbus_V", "vshunt_V", "amps_raw_A", "sign_factor"])

        print(f"Phase A: idle ({PHASE_SEC:.1f}s)...")
        negA, countA, elapsedA = sample_phase("idle1", PHASE_SEC, w, sign_factor)
        print(f"  Captured {countA} samples in {elapsedA:.2f} s")

        print(f"Phase B: CPU load ({PHASE_SEC:.1f}s)...")
        p = mp.Process(target=cpu_stress, args=(PHASE_SEC,))
        p.start()
        negB, countB, elapsedB = sample_phase("load", PHASE_SEC, w, sign_factor)
        p.join()
        print(f"  Captured {countB} samples in {elapsedB:.2f} s")

        print(f"Phase C: idle ({PHASE_SEC:.1f}s)...")
        negC, countC, elapsedC = sample_phase("idle2", PHASE_SEC, w, sign_factor)
        print(f"  Captured {countC} samples in {elapsedC:.2f} s")

    res = summarize(CSV_OUT)
    print("\n--- Summary (corrected current in A) ---")
    for k in ["idle1", "load", "idle2"]:
        r = res[k]
        print(f"{k:>6s}: mean={r['mean']:.3f}  stdev={r['stdev']:.3f}  n={r['n']}")

    total_samples = countA + countB + countC
    total_time = elapsedA + elapsedB + elapsedC
    print(f"\nTotal samples captured: {total_samples} across {total_time:.2f} s")
    if total_time > 0:
        print(f"Effective average sample rate: {total_samples / total_time:.1f} Hz")

    print(f"\nCSV saved -> {CSV_OUT}")

    if (negA or negB or negC) and sign_factor == +1:
        print(
            "WARNING: Negative shunt voltage was seen while sign factor is +1. "
            "If your wiring intentionally measures reverse current, ignore. "
            "Otherwise set FORCE_SIGN=negative or swap VIN+/VIN-."
        )

if __name__ == "__main__":
    main()
