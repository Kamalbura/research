#!/usr/bin/env python3
"""Unit tests for thermal guard component."""

import pytest
import time
from src.scheduler.components.thermal_guard import (
    ThermalGuard, TemperatureSample, ThermalState
)


class TestThermalGuard:
    
    def test_thermal_guard_initialization(self):
        """Test thermal guard initialization with default parameters."""
        guard = ThermalGuard()
        
        assert guard.warning_temp == 70.0
        assert guard.critical_temp == 80.0
        assert guard.emergency_temp == 85.0
        assert guard.current_state == ThermalState.NORMAL
        assert len(guard.temp_history) == 0
    
    def test_normal_temperature_operation(self):
        """Test thermal guard behavior under normal temperatures."""
        guard = ThermalGuard()
        
        sample = TemperatureSample(
            timestamp_ns=time.time_ns(),
            cpu_temp_c=45.0,
            ambient_temp_c=25.0
        )
        
        analysis = guard.update(sample)
        
        assert analysis.state == ThermalState.NORMAL
        assert analysis.current_temp_c == 45.0
        assert analysis.thermal_headroom_c == 35.0  # 80 - 45
        assert analysis.throttling_recommended == False
        assert analysis.emergency_shutdown == False
        assert "NORMAL" in analysis.recommended_action
    
    def test_temperature_trend_calculation(self):
        """Test temperature trend analysis with multiple samples."""
        guard = ThermalGuard(trend_window_s=10.0)
        
        base_time = time.time_ns()
        
        # Add samples with increasing temperature trend
        temperatures = [50.0, 52.0, 54.0, 56.0, 58.0]
        for i, temp in enumerate(temperatures):
            sample = TemperatureSample(
                timestamp_ns=base_time + i * int(2e9),  # 2 second intervals
                cpu_temp_c=temp
            )
            analysis = guard.update(sample)
        
        # Should detect positive temperature trend
        assert analysis.trend_c_per_s > 0
        assert analysis.trend_c_per_s == pytest.approx(1.0, abs=0.5)  # ~1°C/s rise
    
    def test_elevated_temperature_state(self):
        """Test transition to elevated temperature state."""
        guard = ThermalGuard(warning_temp=70.0, critical_temp=80.0)
        
        sample = TemperatureSample(
            timestamp_ns=time.time_ns(),
            cpu_temp_c=75.0  # Between warning and critical
        )
        
        analysis = guard.update(sample)
        
        assert analysis.state == ThermalState.ELEVATED
        assert analysis.thermal_headroom_c == 5.0  # 80 - 75
        assert "ELEVATED" in analysis.recommended_action
    
    def test_critical_temperature_state(self):
        """Test transition to critical temperature state."""
        guard = ThermalGuard(warning_temp=70.0, critical_temp=80.0)
        
        sample = TemperatureSample(
            timestamp_ns=time.time_ns(),
            cpu_temp_c=82.0  # Above critical threshold
        )
        
        analysis = guard.update(sample)
        
        assert analysis.state == ThermalState.CRITICAL
        assert analysis.throttling_recommended == True
        assert "CRITICAL" in analysis.recommended_action
    
    def test_emergency_temperature_state(self):
        """Test emergency temperature conditions."""
        guard = ThermalGuard(emergency_temp=85.0)
        
        # Test emergency due to absolute temperature
        sample_hot = TemperatureSample(
            timestamp_ns=time.time_ns(),
            cpu_temp_c=87.0  # Above emergency threshold
        )
        
        analysis_hot = guard.update(sample_hot)
        
        assert analysis_hot.state == ThermalState.EMERGENCY
        assert analysis_hot.throttling_recommended == True
        assert analysis_hot.emergency_shutdown == True
        assert "EMERGENCY" in analysis_hot.recommended_action
    
    def test_rapid_temperature_rise_emergency(self):
        """Test emergency state due to rapid temperature rise."""
        guard = ThermalGuard(rapid_rise_threshold_c_per_s=2.0)
        
        base_time = time.time_ns()
        
        # Add samples showing rapid temperature rise
        temperatures = [60.0, 65.0, 70.0]  # 5°C rise per sample
        for i, temp in enumerate(temperatures):
            sample = TemperatureSample(
                timestamp_ns=base_time + i * int(1e9),  # 1 second intervals
                cpu_temp_c=temp
            )
            analysis = guard.update(sample)
        
        # Should trigger emergency due to rapid rise (>2°C/s)
        assert analysis.state == ThermalState.EMERGENCY
        assert analysis.trend_c_per_s > 2.0
    
    def test_hysteresis_behavior(self):
        """Test hysteresis to prevent oscillation between states."""
        guard = ThermalGuard(
            warning_temp=70.0, 
            critical_temp=80.0, 
            hysteresis_c=5.0
        )
        
        base_time = time.time_ns()
        
        # Heat up to critical
        sample_critical = TemperatureSample(
            timestamp_ns=base_time,
            cpu_temp_c=82.0
        )
        analysis_critical = guard.update(sample_critical)
        assert analysis_critical.state == ThermalState.CRITICAL
        
        # Cool down slightly but not enough to exit critical (due to hysteresis)
        sample_cool = TemperatureSample(
            timestamp_ns=base_time + int(5e9),
            cpu_temp_c=78.0  # Below critical but within hysteresis band
        )
        analysis_cool = guard.update(sample_cool)
        assert analysis_cool.state == ThermalState.CRITICAL  # Should stay critical
        
        # Cool down enough to exit critical state
        sample_cooler = TemperatureSample(
            timestamp_ns=base_time + int(10e9),
            cpu_temp_c=72.0  # Below critical - hysteresis = 75
        )
        analysis_cooler = guard.update(sample_cooler)
        assert analysis_cooler.state == ThermalState.ELEVATED
    
    def test_time_to_critical_prediction(self):
        """Test prediction of time until critical temperature."""
        guard = ThermalGuard(critical_temp=80.0)
        
        base_time = time.time_ns()
        
        # Create samples with steady temperature rise
        temperatures = [60.0, 62.0, 64.0, 66.0]
        for i, temp in enumerate(temperatures):
            sample = TemperatureSample(
                timestamp_ns=base_time + i * int(5e9),  # 5 second intervals
                cpu_temp_c=temp
            )
            analysis = guard.update(sample)
        
        # Should predict time to reach 80°C based on current trend
        if analysis.time_to_critical_s is not None:
            assert analysis.time_to_critical_s > 0
            assert analysis.time_to_critical_s < 300  # Should be reasonable estimate
    
    def test_thermal_budget_analysis(self):
        """Test thermal budget analysis for additional power loads."""
        guard = ThermalGuard(critical_temp=80.0, warning_temp=70.0)
        
        # Start with moderate temperature
        sample = TemperatureSample(
            timestamp_ns=time.time_ns(),
            cpu_temp_c=65.0
        )
        guard.update(sample)
        
        # Test feasible power increase
        budget_feasible = guard.get_thermal_budget_analysis(target_power_increase_w=2.0)
        assert budget_feasible["feasible"] == True
        assert budget_feasible["projected_temp_c"] < 80.0
        
        # Test excessive power increase
        budget_excessive = guard.get_thermal_budget_analysis(target_power_increase_w=10.0)
        assert budget_excessive["feasible"] == False
        assert budget_excessive["reason"] == "insufficient_headroom"
    
    def test_suite_thermal_mapping(self):
        """Test PQC suite thermal characteristic mapping."""
        guard = ThermalGuard()
        
        mapping = guard.get_suite_thermal_mapping()
        
        # Should have entries for different PQC suites
        assert "cs-mlkem512-aesgcm-mldsa44" in mapping
        assert "cs-mlkem768-aesgcm-mldsa65" in mapping
        assert "cs-mlkem1024-aesgcm-mldsa87" in mapping
        
        # Higher security suites should have higher power/thermal impact
        low_suite = mapping["cs-mlkem512-aesgcm-mldsa44"]
        high_suite = mapping["cs-mlkem1024-aesgcm-mldsa87"]
        
        assert low_suite["typical_power_w"] < high_suite["typical_power_w"]
        assert low_suite["temp_rise_steady_c"] < high_suite["temp_rise_steady_c"]
    
    def test_optimal_suite_recommendation(self):
        """Test optimal PQC suite recommendation based on thermal state."""
        guard = ThermalGuard(critical_temp=80.0)
        
        available_suites = [
            "cs-mlkem512-aesgcm-mldsa44",
            "cs-mlkem768-aesgcm-mldsa65", 
            "cs-mlkem1024-aesgcm-mldsa87"
        ]
        
        # Test recommendation at normal temperature
        recommended_normal = guard.recommend_optimal_suite(
            available_suites, 
            current_temp_c=50.0,
            target_margin_c=15.0
        )
        assert recommended_normal is not None
        
        # Test recommendation at high temperature (should prefer low-power suite)
        recommended_hot = guard.recommend_optimal_suite(
            available_suites,
            current_temp_c=75.0,
            target_margin_c=10.0
        )
        assert recommended_hot == "cs-mlkem512-aesgcm-mldsa44"  # Should pick lowest power
    
    def test_confidence_calculation(self):
        """Test confidence score calculation based on data quality."""
        guard = ThermalGuard()
        
        base_time = time.time_ns()
        
        # Add stable temperature samples
        for i in range(10):
            sample = TemperatureSample(
                timestamp_ns=base_time + i * int(1e9),
                cpu_temp_c=50.0 + 0.1 * i  # Very stable temperatures
            )
            analysis = guard.update(sample)
        
        # Should have high confidence with stable, regular measurements
        assert analysis.confidence_score > 0.7
        
        # Test with noisy data
        guard_noisy = ThermalGuard()
        import random
        
        for i in range(10):
            sample = TemperatureSample(
                timestamp_ns=base_time + i * int(1e9),
                cpu_temp_c=50.0 + random.uniform(-10, 10)  # Very noisy
            )
            analysis_noisy = guard_noisy.update(sample)
        
        # Should have lower confidence with noisy measurements
        assert analysis_noisy.confidence_score < analysis.confidence_score


if __name__ == "__main__":
    pytest.main([__file__, "-v"])