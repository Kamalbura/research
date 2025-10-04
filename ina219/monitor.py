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
SAMPLE_HZ = int(os.getenv("SAMPLE_HZ", "10"))
PHASE_SEC = float(os.getenv("PHASE_SEC", "30"))
SIGN_MODE = os.getenv("FORCE_SIGN", "auto").lower()  # 'auto' | 'positive' | 'negative'
SIGN_PROBE_SEC = float(os.getenv("SIGN_PROBE_SEC", "3"))  # how long to sniff orientation at start (auto mode)

CSV_OUT = f"ina219_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

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
    t0 = time.time()
    neg_seen = False
    while True:
        now = time.time()
        if now - t0 >= seconds:
            break
        amps, vsh, amps_raw = read_current_A(sign_factor)
        vbus = read_bus_voltage_V()
        if vsh < 0:
            neg_seen = True
        writer.writerow([
            f"{time.time():.3f}",
            label,
            f"{amps:.6f}",
            f"{vbus:.3f}",
            f"{vsh:.6e}",
            f"{amps_raw:.6f}",
            f"{sign_factor:+d}",
        ])
        time.sleep(max(0, dt - (time.time() - now)))
    return neg_seen

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
    print(f"INA219 @ {hex(INA_ADDR)}, SHUNT={SHUNT_OHM} ohm, sample={SAMPLE_HZ} Hz, each phase={PHASE_SEC}s")
    sign_factor, sign_mode = resolve_sign()
    print(f"Sign handling  : {sign_mode} (factor {sign_factor:+d})")

    with open(CSV_OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "phase", "amps_A", "vbus_V", "vshunt_V", "amps_raw_A", "sign_factor"])

        print("Phase A: idle (30s)...")
        negA = sample_phase("idle1", PHASE_SEC, w, sign_factor)

        print("Phase B: CPU load (30s)...")
        p = mp.Process(target=cpu_stress, args=(PHASE_SEC,))
        p.start()
        negB = sample_phase("load", PHASE_SEC, w, sign_factor)
        p.join()

        print("Phase C: idle (30s)...")
        negC = sample_phase("idle2", PHASE_SEC, w, sign_factor)

    res = summarize(CSV_OUT)
    print("\n--- Summary (corrected current in A) ---")
    for k in ["idle1", "load", "idle2"]:
        r = res[k]
        print(f"{k:>6s}: mean={r['mean']:.3f}  stdev={r['stdev']:.3f}  n={r['n']}")

    print(f"\nCSV saved -> {CSV_OUT}")

    if (negA or negB or negC) and sign_factor == +1:
        print(
            "WARNING: Negative shunt voltage was seen while sign factor is +1. "
            "If your wiring intentionally measures reverse current, ignore. "
            "Otherwise set FORCE_SIGN=negative or swap VIN+/VIN-."
        )

if __name__ == "__main__":
    main()
