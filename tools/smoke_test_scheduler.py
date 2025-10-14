import sys
import traceback
sys.path.insert(0, '.')

try:
    from src.scheduler.unified_scheduler import UnifiedUAVScheduler, SystemTelemetry
    import time

    s = UnifiedUAVScheduler()
    now = time.time_ns()
    telemetry = SystemTelemetry(
        timestamp_ns=now,
        battery_voltage_v=14.8,
        battery_current_a=1.0,
        battery_power_w=14.8,
        battery_temp_c=30.0,
        cpu_temp_c=55.0,
        gpu_temp_c=None,
        ambient_temp_c=25.0,
        packet_loss_pct=1.0,
        rtt_avg_ms=30.0,
        rtt_p95_ms=60.0,
        throughput_mbps=10.0,
        goodput_mbps=9.5,
        cpu_percent=35.0,
        memory_percent=40.0,
        cpu_freq_mhz=1200.0,
        altitude_m=100.0,
        speed_mps=5.0,
        flight_mode='AUTO'
    )

    s.update_telemetry(telemetry)
    dec = s._make_expert_decision()
    print('Decision:', dec)

except Exception as e:
    print('ERROR during smoke test')
    traceback.print_exc()
    raise
