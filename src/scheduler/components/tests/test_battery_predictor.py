#!/usr/bin/env python3
"""Unit tests for battery predictor component."""

import pytest
import time
from src.scheduler.components.battery_predictor import (
    BatteryPredictor, BatteryState, BatterySpec, create_default_lipo_spec
)


class TestBatteryPredictor:
    
    def test_create_default_lipo_spec(self):
        """Test creation of default Li-Po battery specification."""
        spec = create_default_lipo_spec(5.0, 4)
        
        assert spec.nominal_capacity_ah == 5.0
        assert spec.series_cells == 4
        assert spec.nominal_voltage_v == 3.7
        assert spec.total_nominal_voltage_v == 14.8  # 3.7V * 4 cells
        assert spec.cutoff_voltage_total_v == 13.2   # 3.3V * 4 cells
        assert spec.peukert_exponent > 1.0
    
    def test_battery_predictor_initialization(self):
        """Test battery predictor initialization."""
        spec = create_default_lipo_spec(5.0, 4)
        predictor = BatteryPredictor(spec)
        
        assert predictor.spec == spec
        assert len(predictor.state_history) == 0
        assert predictor.cumulative_ah_consumed == 0.0
        assert predictor.last_update_ns is None
    
    def test_single_battery_update(self):
        """Test single battery state update."""
        spec = create_default_lipo_spec(5.0, 4)
        predictor = BatteryPredictor(spec)
        
        # Simulate fully charged battery at rest
        state = BatteryState(
            timestamp_ns=time.time_ns(),
            voltage_v=16.8,  # 4.2V per cell (fully charged)
            current_a=0.0,   # At rest
            temperature_c=25.0
        )
        
        prediction = predictor.update(state)
        
        assert prediction.soc_percent > 95.0  # Should be nearly full
        assert prediction.remaining_capacity_ah > 4.5
        assert prediction.critical_warning == False
        assert prediction.temperature_derating_factor == pytest.approx(1.0, abs=0.1)
    
    def test_discharge_behavior(self):
        """Test battery discharge behavior and coulomb counting."""
        spec = create_default_lipo_spec(5.0, 4)
        predictor = BatteryPredictor(spec)
        
        base_time = time.time_ns()
        
        # Start with full battery
        state1 = BatteryState(
            timestamp_ns=base_time,
            voltage_v=16.8,
            current_a=0.0,
            temperature_c=25.0
        )
        prediction1 = predictor.update(state1)
        initial_soc = prediction1.soc_percent
        
        # Simulate 1A discharge for 1 hour (should consume 1Ah from 5Ah capacity)
        state2 = BatteryState(
            timestamp_ns=base_time + int(3600 * 1e9),  # 1 hour later
            voltage_v=15.6,  # Lower voltage under load
            current_a=1.0,   # 1A discharge
            temperature_c=25.0
        )
        prediction2 = predictor.update(state2)
        
        # Should have consumed approximately 1Ah (20% of 5Ah capacity)
        expected_soc = initial_soc - 20.0
        assert prediction2.soc_percent < initial_soc
        assert abs(prediction2.soc_percent - expected_soc) < 10.0  # Allow some tolerance
        assert prediction2.discharge_rate_c == pytest.approx(0.2, abs=0.05)  # 1A/5Ah = 0.2C
    
    def test_temperature_compensation(self):
        """Test temperature effects on battery capacity."""
        spec = create_default_lipo_spec(5.0, 4)
        predictor = BatteryPredictor(spec)
        
        base_time = time.time_ns()
        
        # Test at reference temperature (25°C)
        state_normal = BatteryState(
            timestamp_ns=base_time,
            voltage_v=15.6,
            current_a=0.5,
            temperature_c=25.0
        )
        prediction_normal = predictor.update(state_normal)
        
        # Reset predictor for cold temperature test
        predictor_cold = BatteryPredictor(spec)
        
        # Test at cold temperature (0°C)
        state_cold = BatteryState(
            timestamp_ns=base_time,
            voltage_v=15.6,
            current_a=0.5,
            temperature_c=0.0
        )
        prediction_cold = predictor_cold.update(state_cold)
        
        # Cold temperature should reduce effective capacity
        assert prediction_cold.temperature_derating_factor < prediction_normal.temperature_derating_factor
        assert prediction_cold.effective_capacity_ah < prediction_normal.effective_capacity_ah
    
    def test_peukert_effect(self):
        """Test Peukert's equation for high discharge rates."""
        spec = create_default_lipo_spec(5.0, 4)
        predictor = BatteryPredictor(spec)
        
        # Test at low discharge rate (0.5C)
        low_rate_capacity = predictor._apply_peukert_equation(5.0, 0.5)
        
        # Test at high discharge rate (2C)
        high_rate_capacity = predictor._apply_peukert_equation(5.0, 2.0)
        
        # High discharge rate should reduce effective capacity due to Peukert effect
        assert high_rate_capacity < low_rate_capacity
        assert high_rate_capacity < 5.0  # Should be less than nominal
    
    def test_critical_warning_conditions(self):
        """Test critical battery warning conditions."""
        spec = create_default_lipo_spec(5.0, 4)
        predictor = BatteryPredictor(spec, critical_soc_threshold=20.0)
        
        # Test low voltage warning
        state_low_voltage = BatteryState(
            timestamp_ns=time.time_ns(),
            voltage_v=13.0,  # Below safe cutoff
            current_a=1.0,
            temperature_c=25.0
        )
        prediction_low_voltage = predictor.update(state_low_voltage)
        assert prediction_low_voltage.critical_warning == True
        
        # Reset for low SOC test
        predictor_soc = BatteryPredictor(spec, critical_soc_threshold=20.0)
        
        # Simulate very low SOC by high cumulative consumption
        predictor_soc.cumulative_ah_consumed = 4.2  # Consumed 4.2Ah from 5Ah
        
        state_low_soc = BatteryState(
            timestamp_ns=time.time_ns(),
            voltage_v=14.4,  # Voltage still OK
            current_a=0.5,
            temperature_c=25.0
        )
        prediction_low_soc = predictor_soc.update(state_low_soc)
        assert prediction_low_soc.critical_warning == True
    
    def test_power_trend_analysis(self):
        """Test power consumption trend analysis."""
        spec = create_default_lipo_spec(5.0, 4)
        predictor = BatteryPredictor(spec)
        
        base_time = time.time_ns()
        
        # Add several power samples with increasing trend
        powers = [3.0, 3.5, 4.0, 4.5, 5.0]  # Watts
        for i, power in enumerate(powers):
            state = BatteryState(
                timestamp_ns=base_time + i * int(10 * 1e9),  # 10 second intervals
                voltage_v=15.0,
                current_a=power / 15.0,  # I = P/V
                power_w=power,
                temperature_c=25.0
            )
            predictor.update(state)
        
        trend = predictor.get_power_trend_analysis(window_s=60.0)
        
        assert trend["trend_w_per_s"] > 0  # Should show increasing trend
        assert trend["avg_power_w"] == pytest.approx(4.0, abs=0.5)
        assert trend["peak_power_w"] == 5.0
    
    def test_mission_viability_prediction(self):
        """Test mission viability prediction."""
        spec = create_default_lipo_spec(5.0, 4)
        predictor = BatteryPredictor(spec)
        
        # Start with partially charged battery
        state = BatteryState(
            timestamp_ns=time.time_ns(),
            voltage_v=15.6,  # ~70% charge
            current_a=0.0,
            temperature_c=25.0
        )
        predictor.update(state)
        
        # Test viable mission (low power, short duration)
        viable_mission = predictor.predict_mission_viability(
            target_duration_s=1800,  # 30 minutes
            expected_avg_power_w=3.0  # 3 watts
        )
        assert viable_mission["viable"] == True
        assert viable_mission["margin_s"] > 0
        
        # Test non-viable mission (high power, long duration)
        non_viable_mission = predictor.predict_mission_viability(
            target_duration_s=7200,  # 2 hours
            expected_avg_power_w=8.0  # 8 watts
        )
        assert non_viable_mission["viable"] == False
        assert non_viable_mission["reason"] == "insufficient_capacity"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])