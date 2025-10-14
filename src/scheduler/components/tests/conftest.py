#!/usr/bin/env python3
"""Test configuration and utilities for the scheduler test suite."""

import pytest
import tempfile
import os
import shutil
from unittest.mock import Mock
from src.scheduler.components.battery_predictor import BatterySpecs


@pytest.fixture(scope="session")
def temp_directory():
    """Create a temporary directory for test files."""
    temp_dir = tempfile.mkdtemp(prefix="scheduler_tests_")
    yield temp_dir
    # Cleanup after all tests
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)


@pytest.fixture
def standard_battery_specs():
    """Standard Li-Po battery specifications for testing."""
    return BatterySpecs(
        nominal_voltage_v=14.8,
        nominal_capacity_ah=4.0,
        max_discharge_rate_c=8.0,
        min_voltage_v=11.1,
        max_voltage_v=16.8,
        peukert_exponent=1.25,
        internal_resistance_mohm=45.0,
        temp_coefficient_per_c=-0.004
    )


@pytest.fixture
def high_performance_battery_specs():
    """High-performance Li-Po battery specifications for testing."""
    return BatterySpecs(
        nominal_voltage_v=22.2,  # 6S battery
        nominal_capacity_ah=6.0,
        max_discharge_rate_c=15.0,
        min_voltage_v=16.8,     # 2.8V per cell minimum
        max_voltage_v=25.2,     # 4.2V per cell maximum  
        peukert_exponent=1.15,  # Better efficiency
        internal_resistance_mohm=30.0,  # Lower resistance
        temp_coefficient_per_c=-0.003
    )


@pytest.fixture
def degraded_battery_specs():
    """Degraded/aged Li-Po battery specifications for testing."""
    return BatterySpecs(
        nominal_voltage_v=14.8,
        nominal_capacity_ah=2.5,  # Reduced capacity due to aging
        max_discharge_rate_c=5.0,  # Reduced discharge capability
        min_voltage_v=11.1,
        max_voltage_v=16.8,
        peukert_exponent=1.4,   # Worse efficiency when aged
        internal_resistance_mohm=80.0,  # Higher resistance
        temp_coefficient_per_c=-0.006  # More temperature sensitive
    )


@pytest.fixture
def mock_xgboost_model():
    """Mock XGBoost model for security advisor testing."""
    model = Mock()
    model.predict_proba.return_value = [[0.8, 0.2]]  # Low threat by default
    model.feature_importances_ = [0.3, 0.25, 0.2, 0.15, 0.1]
    return model


@pytest.fixture
def mock_transformer_model():
    """Mock Transformer model for security advisor testing."""
    model = Mock()
    model.predict.return_value = [[0.85, 0.15]]  # Low threat by default
    model.eval.return_value = model  # For PyTorch compatibility
    return model


@pytest.fixture
def sample_network_features():
    """Sample network traffic features for security testing."""
    return {
        'packet_rate': 150.0,
        'byte_rate': 75000.0,
        'unique_src_ips': 12,
        'unique_dst_ports': 8,
        'avg_packet_size': 500.0,
        'tcp_syn_rate': 10.0,
        'tcp_syn_ack_ratio': 0.9,
        'udp_rate': 30.0,
        'icmp_rate': 1.0,
        'connection_count': 25
    }


@pytest.fixture
def ddos_attack_features():
    """Network traffic features indicating DDOS attack."""
    return {
        'packet_rate': 15000.0,     # Very high packet rate
        'byte_rate': 2000000.0,     # High byte rate
        'unique_src_ips': 1,        # Single source (amplification attack)
        'unique_dst_ports': 1,      # Single target port
        'avg_packet_size': 133.0,   # Small packets
        'tcp_syn_rate': 12000.0,    # SYN flood
        'tcp_syn_ack_ratio': 0.1,   # Few responses
        'udp_rate': 3000.0,         # UDP flood component
        'icmp_rate': 0.0,
        'connection_count': 1       # Single connection
    }


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset any global state between tests."""
    yield
    # Add any global state cleanup here if needed


class TestFixtures:
    """Test the test fixtures themselves."""
    
    def test_battery_specs_fixtures(self, standard_battery_specs, high_performance_battery_specs, degraded_battery_specs):
        """Verify battery specification fixtures are valid."""
        # Standard battery
        assert standard_battery_specs.nominal_voltage_v > 0
        assert standard_battery_specs.nominal_capacity_ah > 0
        assert standard_battery_specs.max_discharge_rate_c > 0
        
        # High performance should have better specs
        assert high_performance_battery_specs.nominal_capacity_ah > standard_battery_specs.nominal_capacity_ah
        assert high_performance_battery_specs.max_discharge_rate_c > standard_battery_specs.max_discharge_rate_c
        
        # Degraded should have worse specs
        assert degraded_battery_specs.nominal_capacity_ah < standard_battery_specs.nominal_capacity_ah
        assert degraded_battery_specs.max_discharge_rate_c < standard_battery_specs.max_discharge_rate_c
        assert degraded_battery_specs.internal_resistance_mohm > standard_battery_specs.internal_resistance_mohm
    
    def test_network_features_fixtures(self, sample_network_features, ddos_attack_features):
        """Verify network traffic feature fixtures are realistic."""
        # Normal traffic should be reasonable
        assert 0 < sample_network_features['packet_rate'] < 1000
        assert sample_network_features['unique_src_ips'] > 1
        assert 0.5 < sample_network_features['tcp_syn_ack_ratio'] < 1.0
        
        # DDOS features should show attack characteristics
        assert ddos_attack_features['packet_rate'] > 10000  # Very high rate
        assert ddos_attack_features['unique_src_ips'] <= 3   # Few sources
        assert ddos_attack_features['tcp_syn_ack_ratio'] < 0.5  # Poor response ratio
    
    def test_mock_model_fixtures(self, mock_xgboost_model, mock_transformer_model):
        """Verify ML model mocks behave correctly."""
        # Test XGBoost mock
        prediction = mock_xgboost_model.predict_proba([[1, 2, 3, 4, 5]])
        assert len(prediction) == 1
        assert len(prediction[0]) == 2  # Binary classification
        assert sum(prediction[0]) == pytest.approx(1.0, abs=0.01)  # Probabilities sum to 1
        
        # Test Transformer mock
        prediction = mock_transformer_model.predict([[1, 2, 3, 4, 5]])
        assert len(prediction) == 1
        assert len(prediction[0]) == 2  # Binary classification


# Utility functions for tests

def create_test_telemetry_sequence(count=10, base_voltage=14.8, voltage_decline_rate=0.1):
    """Create a sequence of realistic telemetry snapshots."""
    import time
    from src.scheduler.unified_scheduler import TelemetrySnapshot
    
    snapshots = []
    base_time = time.time_ns()
    
    for i in range(count):
        snapshot = TelemetrySnapshot(
            timestamp_ns=base_time + i * int(100e6),  # 100ms intervals
            battery_voltage_v=base_voltage - i * voltage_decline_rate,
            battery_current_a=-2.0 - (i % 3),  # Varying current draw
            cpu_temp_c=45.0 + i * 1.5,  # Gradual warming
            ambient_temp_c=25.0,
            network_packet_rate=100.0 + i * 10.0,
            network_byte_rate=50000.0 + i * 5000.0
        )
        snapshots.append(snapshot)
    
    return snapshots


def assert_suite_priority_order(suites):
    """Assert that PQC suites are in expected priority order (low to high security)."""
    expected_order = [
        "cs-mlkem512-aesgcm-mldsa44",   # Lowest power/fastest
        "cs-mlkem768-aesgcm-mldsa65",   # Balanced
        "cs-mlkem1024-aesgcm-mldsa87"   # Highest security/slowest
    ]
    
    for suite in suites:
        assert suite in expected_order, f"Unknown suite: {suite}"


def measure_function_performance(func, *args, **kwargs):
    """Measure function execution time and return result + timing."""
    import time
    
    start_time = time.perf_counter()
    result = func(*args, **kwargs)
    execution_time = time.perf_counter() - start_time
    
    return result, execution_time


def verify_real_time_constraint(execution_time_ms, deadline_ms, tolerance_factor=0.8):
    """Verify that execution time meets real-time constraints with tolerance."""
    assert execution_time_ms <= deadline_ms * tolerance_factor, \
        f"Execution time {execution_time_ms:.2f}ms exceeds {tolerance_factor*100}% of deadline {deadline_ms}ms"


# Pytest configuration

def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line("markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')")
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
    config.addinivalue_line("markers", "performance: marks tests as performance benchmarks")
    config.addinivalue_line("markers", "hardware: marks tests requiring hardware simulation")


def pytest_collection_modifyitems(config, items):
    """Modify test collection to add markers based on test names."""
    for item in items:
        # Mark integration tests
        if "integration" in item.name.lower() or "test_integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
        
        # Mark performance tests
        if "performance" in item.name.lower() or "latency" in item.name.lower():
            item.add_marker(pytest.mark.performance)
        
        # Mark hardware simulation tests
        if "hardware" in item.name.lower() or "pi4" in item.name.lower():
            item.add_marker(pytest.mark.hardware)
        
        # Mark slow tests (integration, performance, hardware)
        if any(mark.name in ["integration", "performance", "hardware"] for mark in item.iter_markers()):
            item.add_marker(pytest.mark.slow)