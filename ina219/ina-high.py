import time

try:
    import board
    import busio
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing CircuitPython dependencies; install 'adafruit-blinka' before running this script."
    ) from exc

from adafruit_ina219 import INA219, ADCResolution, BusVoltageRange

# --- CONFIGURATION ---
SHUNT_OHMS = 0.1
# Number of samples to take before calculating the frequency
SAMPLES_TO_TAKE = 2000 

def main():
    """
    This script is optimized for the HIGHEST POSSIBLE sampling rate.
    It takes a batch of readings in a tight loop and then reports the
    actual frequency achieved.
    """
    try:
    i2c_bus = busio.I2C(board.SCL, board.SDA)
    ina219 = INA219(i2c_bus, shunt_resistance=SHUNT_OHMS)

        print("INA219 High-Frequency Benchmark")
        
        # 1. CONFIGURE FOR MAXIMUM SPEED
        # This is the most critical step. We use the lowest resolution (9-bit)
        # which has the fastest conversion time (~84µs per measurement).
        ina219.bus_adc_resolution = ADCResolution.ADCRES_9BIT_1
        ina219.shunt_adc_resolution = ADCResolution.ADCRES_9BIT_1
        ina219.bus_voltage_range = BusVoltageRange.RANGE_16V
        
        print(f"Configuration: {ina219.bus_adc_resolution=}, {ina219.shunt_adc_resolution=}")
        print(f"Taking {SAMPLES_TO_TAKE} samples as fast as possible...")
        print("-" * 40)
        
        # Allow a moment for the first conversion to complete
        time.sleep(0.01)

        # 2. THE "HOT LOOP"
        # This loop is intentionally minimal. No printing, no complex math,
        # just raw data acquisition to reduce Python overhead.
        
        # Pre-allocate a list to store results for speed
        readings = [0] * SAMPLES_TO_TAKE
        
        start_time = time.monotonic()

        for i in range(SAMPLES_TO_TAKE):
            # We only read the shunt voltage here as it's the most
            # rapidly changing value for power measurement. Reading both
            # bus and shunt voltage would nearly double the I2C traffic.
            readings[i] = ina219.shunt_voltage 

        end_time = time.monotonic()

        # 3. CALCULATE AND REPORT RESULTS
        total_time = end_time - start_time
        # Frequency is the number of samples divided by the total time
        frequency = SAMPLES_TO_TAKE / total_time

        print("Benchmark Complete!")
        print(f"  - Total time taken: {total_time:.4f} seconds")
        print(f"  - Samples captured: {SAMPLES_TO_TAKE}")
        print(f"  - Achieved Sample Rate: {frequency:.2f} Hz")
        print("-" * 40)

        if frequency < 1000:
            print("💡 Note: Reaching a perfect 1 kHz is tough due to Python/OS overhead.")
            print("   This result is likely the practical maximum for this setup.")
        else:
            print("✅ Success! Achieved a sample rate at or above 1 kHz.")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    main()
